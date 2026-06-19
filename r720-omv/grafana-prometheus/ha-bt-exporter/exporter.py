#!/usr/bin/env python3
"""Home Assistant Bluetooth → Prometheus exporter.

Purpose:
    Connect to the Home Assistant WebSocket API and subscribe to the two
    Bluetooth streams that are NOT available through HA's /api/prometheus
    export:
      * bluetooth/subscribe_advertisements        → every relayed BLE advert
        (address, name, rssi, source/proxy, connectable, tx_power, time)
      * bluetooth/subscribe_connection_allocations → per-proxy free/used
        connection slots
    The stream is translated into Prometheus metrics served on /metrics, giving
    the per-(device × proxy) RSSI map + advertisement rate + per-proxy slot
    usage. Optionally each advert is also emitted as one JSON log line on stdout
    (picked up by the existing Alloy → Loki docker-log pipeline) for ad-hoc
    "who heard device X" queries.

Dependencies:
    Python 3.13+, websockets>=13, prometheus_client>=0.21
    (installed in the accompanying Dockerfile).

Author:
    AI (Claude) — generated for the komodo_library monitoring stack.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from typing import Any

import websockets
from prometheus_client import Counter, Gauge, start_http_server

# --------------------------------------------------------------------------- #
# Configuration (all via environment — see compose.yaml / Komodo vars)
# --------------------------------------------------------------------------- #
HA_WS_URL = os.environ.get("HA_WS_URL", "ws://10.10.1.11:8123/api/websocket")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9110"))
# Drop a (device × proxy) series if no advert was seen within this many seconds.
# Bounds cardinality against BLE MAC-rotation / transient neighbours.
DEVICE_TTL_SECONDS = int(os.environ.get("DEVICE_TTL_SECONDS", "300"))
# Only export devices whose address matches this regex (empty = all).
ADDRESS_INCLUDE_REGEX = os.environ.get("ADDRESS_INCLUDE_REGEX", "")
# If true, ignore non-connectable adverts (beacons, randomised broadcasters).
CONNECTABLE_ONLY = os.environ.get("CONNECTABLE_ONLY", "false").lower() == "true"
# If true, emit one JSON log line per advert to stdout → Loki (can be noisy).
LOG_ADVERTS = os.environ.get("LOG_ADVERTS", "false").lower() == "true"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
RECONNECT_BACKOFF_MAX = 60  # seconds

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ha-bluetooth-exporter")

_addr_filter = re.compile(ADDRESS_INCLUDE_REGEX) if ADDRESS_INCLUDE_REGEX else None

# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
UP = Gauge("ha_bluetooth_up", "1 if the HA WebSocket subscription is active")
ADVERTS_TOTAL = Counter(
    "ha_bluetooth_advertisements_total",
    "Total BLE advertisements relayed, per proxy source",
    ["source"],
)
RSSI = Gauge(
    "ha_bluetooth_advertisement_rssi_dbm",
    "Last advertised RSSI (dBm) per device, per proxy that heard it",
    ["address", "source"],
)
TX_POWER = Gauge(
    "ha_bluetooth_advertisement_tx_power_dbm",
    "Last advertised TX power (dBm) per device, per proxy",
    ["address", "source"],
)
LAST_SEEN = Gauge(
    "ha_bluetooth_last_seen_timestamp_seconds",
    "Unix timestamp of the last advert for a device, per proxy",
    ["address", "source"],
)
DEVICE_INFO = Gauge(
    "ha_bluetooth_device_info",
    "Device metadata (value always 1); keyed by address only",
    ["address", "name", "connectable"],
)
SLOTS_FREE = Gauge(
    "ha_bluetooth_connection_slots_free",
    "Free BLE connection slots per proxy source",
    ["source"],
)
SLOTS_USED = Gauge(
    "ha_bluetooth_connection_slots_used",
    "Used BLE connection slots per proxy source",
    ["source"],
)
SLOTS_TOTAL = Gauge(
    "ha_bluetooth_connection_slots_total",
    "Total BLE connection slots per proxy source",
    ["source"],
)

# Bookkeeping for TTL eviction.
# (address, source) -> last_seen_epoch  and  address -> (name, connectable) label tuple
_seen: dict[tuple[str, str], float] = {}
_info_labels: dict[str, tuple[str, str]] = {}


def _included(address: str) -> bool:
    """Return True if the address passes the optional include filter."""
    return _addr_filter.search(address) is not None if _addr_filter else True


def _handle_advertisement(entry: dict[str, Any]) -> None:
    """Translate a single serialized advert into metric updates."""
    address = entry.get("address")
    source = entry.get("source")
    if not address or not source:
        return
    connectable = bool(entry.get("connectable", False))
    if CONNECTABLE_ONLY and not connectable:
        return
    if not _included(address):
        return

    ADVERTS_TOTAL.labels(source=source).inc()

    rssi = entry.get("rssi")
    if rssi is not None:
        RSSI.labels(address=address, source=source).set(rssi)
    tx_power = entry.get("tx_power")
    # HA reports a sentinel (-127) when the device omits TX power; skip it.
    if tx_power is not None and tx_power != -127:
        TX_POWER.labels(address=address, source=source).set(tx_power)

    now = time.time()
    LAST_SEEN.labels(address=address, source=source).set(now)
    _seen[(address, source)] = now

    name = (entry.get("name") or "").strip() or address
    conn = "true" if connectable else "false"
    prev = _info_labels.get(address)
    if prev != (name, conn):
        if prev is not None:
            # Name / connectability changed — drop the stale info series.
            try:
                DEVICE_INFO.remove(address, prev[0], prev[1])
            except KeyError:
                pass
        DEVICE_INFO.labels(address=address, name=name, connectable=conn).set(1)
        _info_labels[address] = (name, conn)

    if LOG_ADVERTS:
        print(
            json.dumps(
                {
                    "event": "bluetooth_advertisement",
                    "address": address,
                    "name": name,
                    "rssi": rssi,
                    "source": source,
                    "connectable": connectable,
                    "tx_power": tx_power,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )


def _handle_advert_event(event: Any) -> None:
    """Parse a subscribe_advertisements event payload (defensive)."""
    # Documented shape: {"add": [<service_info>, ...], "remove": [...]}.
    # Fall back to a bare list / single dict if the schema differs.
    if isinstance(event, dict):
        items = event.get("add", event.get("advertisements"))
        if items is None:
            items = [event] if "address" in event else []
    elif isinstance(event, list):
        items = event
    else:
        return
    for entry in items:
        if isinstance(entry, dict):
            _handle_advertisement(entry)


def _handle_allocations_event(event: Any) -> None:
    """Parse a subscribe_connection_allocations event payload (defensive)."""
    allocations = event.get("allocations", event) if isinstance(event, dict) else event
    if not isinstance(allocations, list):
        return
    for alloc in allocations:
        if not isinstance(alloc, dict):
            continue
        source = alloc.get("source")
        if not source:
            continue
        slots = alloc.get("slots")
        free = alloc.get("free")
        allocated = alloc.get("allocated")
        used = None
        if slots is not None and free is not None:
            used = slots - free
        elif isinstance(allocated, list):
            used = len(allocated)
        if slots is not None:
            SLOTS_TOTAL.labels(source=source).set(slots)
        if free is not None:
            SLOTS_FREE.labels(source=source).set(free)
        if used is not None:
            SLOTS_USED.labels(source=source).set(used)


def _evict_stale() -> None:
    """Remove (device × proxy) series not seen within DEVICE_TTL_SECONDS."""
    cutoff = time.time() - DEVICE_TTL_SECONDS
    stale = [key for key, ts in _seen.items() if ts < cutoff]
    live_addresses = {addr for (addr, _src), ts in _seen.items() if ts >= cutoff}
    for address, source in stale:
        for metric in (RSSI, TX_POWER, LAST_SEEN):
            try:
                metric.remove(address, source)
            except KeyError:
                pass
        _seen.pop((address, source), None)
    # Drop info series for addresses with no remaining live (addr, *) entry.
    for address in list(_info_labels):
        if address not in live_addresses:
            name, conn = _info_labels.pop(address)
            try:
                DEVICE_INFO.remove(address, name, conn)
            except KeyError:
                pass
    if stale:
        log.debug("evicted %d stale device×proxy series", len(stale))


async def _janitor() -> None:
    """Periodic TTL eviction loop."""
    interval = max(30, DEVICE_TTL_SECONDS // 2)
    while True:
        await asyncio.sleep(interval)
        _evict_stale()


async def _run_session() -> None:
    """One connect → auth → subscribe → consume cycle."""
    async with websockets.connect(HA_WS_URL, max_size=None, ping_interval=20) as ws:
        # 1. auth handshake
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"unexpected greeting: {msg.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"authentication failed: {msg}")
        log.info("authenticated to %s", HA_WS_URL)

        # 2. subscribe to both streams
        sub = {
            1: "bluetooth/subscribe_advertisements",
            2: "bluetooth/subscribe_connection_allocations",
        }
        for sub_id, sub_type in sub.items():
            await ws.send(json.dumps({"id": sub_id, "type": sub_type}))

        UP.set(1)
        log.info("subscribed to advertisements + connection allocations")

        # 3. consume events
        async for raw in ws:
            data = json.loads(raw)
            mtype = data.get("type")
            if mtype == "event":
                event = data.get("event")
                if data.get("id") == 1:
                    _handle_advert_event(event)
                elif data.get("id") == 2:
                    _handle_allocations_event(event)
            elif mtype == "result" and not data.get("success", True):
                log.error("subscription %s failed: %s", data.get("id"), data.get("error"))


async def _main() -> None:
    start_http_server(EXPORTER_PORT)
    log.info("metrics server listening on :%d/metrics", EXPORTER_PORT)
    asyncio.create_task(_janitor())

    backoff = 1
    while True:
        try:
            await _run_session()
            backoff = 1
        except Exception as exc:  # noqa: BLE001 — log & retry any failure
            UP.set(0)
            log.warning("session ended (%s); reconnecting in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)


if __name__ == "__main__":
    if not HA_TOKEN:
        raise SystemExit("HA_TOKEN is required")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)
    try:
        loop.run_until_complete(_main())
    except RuntimeError:
        pass  # loop.stop() during shutdown
