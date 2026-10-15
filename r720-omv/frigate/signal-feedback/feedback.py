#!/usr/bin/env python3
"""Frigate person alerts on Signal, with emoji reactions fed back to Frigate+.

Purpose:
    Home Assistant decides WHEN to alert (zone, alarm and family guards stay in
    the HA automation) and POSTs {camera, zone, message} to /feedback/notify.
    This service then:
      1. finds the Frigate event behind the alert (latest person event in that
         camera + zone, last few minutes),
      2. sends that event's annotated snapshot to the Signal group as the bot,
      3. records Signal's send timestamp -> Frigate event id in SQLite.

    A second thread holds the signal-cli-rest-api receive websocket open (the
    bot number runs in json-rpc mode, where /v1/receive is a websocket, not a
    GET). When anyone reacts to one of those alerts:
      ✅ / 👍  -> POST /api/events/<id>/plus            (correct detection)
      ❌ / 👎  -> PUT  /api/events/<id>/false_positive  (wrong detection)
    and the bot reacts to the alert with 📤 on success or ⚠️ on failure.

    The timestamp map is the only way to tie a reaction back to an event: a
    Signal reaction carries just the target message's sent-timestamp, and
    signal-cli keeps no message history to look it up in. That is why this
    service, not HA's notify.signal_messenger, has to do the sending.

    Frigate only accepts a Frigate+ submission once the event has ENDED (the
    clean snapshot is written at the end), so a reaction on a still-running
    event is retried every RETRY_INTERVAL seconds for up to RETRY_FOR seconds.

    Removing a reaction does nothing: Frigate has no API to withdraw a Frigate+
    submission; fix those on plus.frigate.video.

Dependencies:
    Python 3.11+ standard library only (http.server, sqlite3, socket, ssl,
    urllib). The websocket client is a minimal RFC 6455 receiver below, so the
    image needs no pip install.

Author:
    AI (Claude), 2026-10-15.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("frigate-signal-feedback")

# signal-cli-rest-api, reached over the shared `signal-rest-api` docker network.
SIGNAL_API = os.environ.get("SIGNAL_API", "http://signal-rest-api:8080").rstrip("/")
# Bot account that both sends the alerts and receives the reactions.
SIGNAL_NUMBER = os.environ.get("SIGNAL_NUMBER", "").strip()
# Group the alerts go to, in signal-cli-rest-api's "group.<base64>" form.
SIGNAL_RECIPIENT = os.environ.get("SIGNAL_RECIPIENT", "").strip()
# Frigate's unauthenticated API (Traefik frigateapi entrypoint on the host).
FRIGATE_API = os.environ.get("FRIGATE_API", "http://10.10.1.13:5000/api").rstrip("/")
# Shared secret HA sends as "Authorization: Bearer <token>".
NOTIFY_TOKEN = os.environ.get("NOTIFY_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "/data/feedback.sqlite3")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
# How far back an event may have started and still match an alert. HA can wait
# up to 45 s in its family guard before notifying, so this must exceed that.
EVENT_LOOKBACK = int(os.environ.get("EVENT_LOOKBACK", "300"))
RETRY_INTERVAL = int(os.environ.get("RETRY_INTERVAL", "60"))
RETRY_FOR = int(os.environ.get("RETRY_FOR", "3600"))
# Mappings older than this are pruned; reacting to an older alert is a no-op.
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "30"))

POSITIVE = {"✅", "✔️", "✔", "👍", "☑️"}
NEGATIVE = {"❌", "✖️", "👎", "🚫", "❎"}
ACK_OK = "📤"
ACK_FAIL = "⚠️"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
class Store:
    """SQLite map of Signal message timestamp -> Frigate event, plus a retry queue."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                signal_ts  INTEGER PRIMARY KEY,
                event_id   TEXT NOT NULL,
                camera     TEXT NOT NULL,
                recipient  TEXT NOT NULL,
                created    REAL NOT NULL,
                verdict    TEXT
            );
            CREATE TABLE IF NOT EXISTS pending (
                signal_ts  INTEGER PRIMARY KEY,
                verdict    TEXT NOT NULL,
                first_try  REAL NOT NULL,
                next_try   REAL NOT NULL
            );
            """
        )
        self._db.commit()

    def add_alert(self, signal_ts: int, event_id: str, camera: str, recipient: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO alerts VALUES (?, ?, ?, ?, ?, NULL)",
                (signal_ts, event_id, camera, recipient, time.time()),
            )
            self._db.execute(
                "DELETE FROM alerts WHERE created < ?", (time.time() - KEEP_DAYS * 86400,)
            )
            self._db.commit()

    def get_alert(self, signal_ts: int) -> tuple[str, str, str, str | None] | None:
        with self._lock:
            return self._db.execute(
                "SELECT event_id, camera, recipient, verdict FROM alerts WHERE signal_ts = ?",
                (signal_ts,),
            ).fetchone()

    def set_verdict(self, signal_ts: int, verdict: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE alerts SET verdict = ? WHERE signal_ts = ?", (verdict, signal_ts)
            )
            self._db.execute("DELETE FROM pending WHERE signal_ts = ?", (signal_ts,))
            self._db.commit()

    def queue_retry(self, signal_ts: int, verdict: str) -> None:
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT first_try FROM pending WHERE signal_ts = ?", (signal_ts,)
            ).fetchone()
            first = row[0] if row else now
            self._db.execute(
                "INSERT OR REPLACE INTO pending VALUES (?, ?, ?, ?)",
                (signal_ts, verdict, first, now + RETRY_INTERVAL),
            )
            self._db.commit()

    def due_retries(self) -> list[tuple[int, str, float]]:
        with self._lock:
            return self._db.execute(
                "SELECT signal_ts, verdict, first_try FROM pending WHERE next_try <= ?",
                (time.time(),),
            ).fetchall()

    def drop_retry(self, signal_ts: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pending WHERE signal_ts = ?", (signal_ts,))
            self._db.commit()


STORE: Store
# Serialises verdict handling so a reaction and its retry never race.
VERDICT_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def http(method: str, url: str, body: dict | None = None, timeout: int = 30) -> tuple[int, bytes]:
    """Perform an HTTP request; return (status, body) without raising on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def find_event(camera: str, zone: str | None) -> dict | None:
    """Return the most recent person event on camera (and zone) started within EVENT_LOOKBACK."""
    params = {
        "camera": camera,
        "label": "person",
        "after": int(time.time()) - EVENT_LOOKBACK,
        "limit": 5,
        "has_snapshot": 1,
    }
    if zone:
        params["zones"] = zone
    status, body = http("GET", f"{FRIGATE_API}/events?{urllib.parse.urlencode(params)}")
    if status != 200:
        LOG.warning("Frigate event lookup failed: %s %s", status, body[:200])
        return None
    events = json.loads(body)
    return events[0] if events else None


def fetch_image(url: str) -> str | None:
    """Download an image and return it as a signal-cli-rest-api data-URI attachment."""
    status, body = http("GET", url)
    if status != 200 or not body:
        LOG.warning("Snapshot fetch failed: %s %s", status, url)
        return None
    return "data:image/jpeg;filename=snapshot.jpg;base64," + base64.b64encode(body).decode()


def signal_send(message: str, recipient: str, attachment: str | None = None,
                quote_ts: int | None = None) -> int | None:
    """Send a Signal message as the bot; return Signal's sent timestamp."""
    body: dict = {"message": message, "number": SIGNAL_NUMBER, "recipients": [recipient]}
    if attachment:
        body["base64_attachments"] = [attachment]
    if quote_ts:
        body["quote_timestamp"] = quote_ts
        body["quote_author"] = SIGNAL_NUMBER
    status, resp = http("POST", f"{SIGNAL_API}/v2/send", body, timeout=60)
    if status not in (200, 201):
        LOG.error("Signal send failed: %s %s", status, resp[:300])
        return None
    try:
        return int(json.loads(resp)["timestamp"])
    except (ValueError, KeyError, TypeError):
        LOG.error("Signal send returned no timestamp: %s", resp[:300])
        return None


def signal_react(emoji: str, recipient: str, target_ts: int) -> None:
    """React to one of the bot's own messages."""
    status, resp = http(
        "POST",
        f"{SIGNAL_API}/v1/reactions/{urllib.parse.quote(SIGNAL_NUMBER)}",
        {"reaction": emoji, "recipient": recipient,
         "target_author": SIGNAL_NUMBER, "timestamp": target_ts},
    )
    if status not in (200, 201, 204):
        LOG.warning("Ack reaction failed: %s %s", status, resp[:200])


# --------------------------------------------------------------------------- #
# Frigate+ submission
# --------------------------------------------------------------------------- #
def submit(event_id: str, verdict: str) -> tuple[str, str]:
    """Submit one verdict to Frigate+. Returns ("ok"|"retry"|"fail", detail)."""
    status, body = http("GET", f"{FRIGATE_API}/events/{event_id}")
    if status != 200:
        return "fail", f"event {event_id} not found ({status})"
    event = json.loads(body)
    if event.get("end_time") is None:
        return "retry", "event still in progress"

    if verdict == "positive":
        if event.get("plus_id"):
            return "ok", "already on Frigate+"
        status, body = http("POST", f"{FRIGATE_API}/events/{event_id}/plus",
                            {"include_annotation": 1})
    else:
        if event.get("false_positive"):
            return "ok", "already marked false positive"
        status, body = http("PUT", f"{FRIGATE_API}/events/{event_id}/false_positive")

    try:
        detail = json.loads(body).get("message", "")
    except (ValueError, AttributeError):
        detail = body[:200].decode(errors="replace")
    if 200 <= status < 300:
        return "ok", detail
    # Frigate answers 400 while the snapshot is not yet on disk; anything 5xx
    # is likely Frigate+ itself being unreachable. Both are worth retrying.
    if status >= 500 or "snapshot" in detail.lower():
        return "retry", f"{status} {detail}"
    return "fail", f"{status} {detail}"


def handle_verdict(signal_ts: int, verdict: str, first_try: float | None = None) -> None:
    """Apply a verdict for the alert sent at signal_ts, acking or queueing a retry."""
    with VERDICT_LOCK:
        row = STORE.get_alert(signal_ts)
        if not row:
            return
        event_id, camera, recipient, previous = row
        if previous == verdict:
            STORE.drop_retry(signal_ts)
            return
        result, detail = submit(event_id, verdict)
        LOG.info("verdict=%s event=%s camera=%s -> %s (%s)",
                 verdict, event_id, camera, result, detail)
        if result == "ok":
            STORE.set_verdict(signal_ts, verdict)
            signal_react(ACK_OK, recipient, signal_ts)
        elif result == "retry" and (first_try is None or time.time() - first_try < RETRY_FOR):
            STORE.queue_retry(signal_ts, verdict)
        else:
            STORE.drop_retry(signal_ts)
            signal_react(ACK_FAIL, recipient, signal_ts)
            signal_send(f"Frigate+ submission failed: {detail}", recipient, quote_ts=signal_ts)


def retry_loop() -> None:
    """Re-attempt verdicts on events that had not ended when the reaction arrived."""
    while True:
        time.sleep(15)
        try:
            for signal_ts, verdict, first_try in STORE.due_retries():
                handle_verdict(signal_ts, verdict, first_try)
        except Exception:  # noqa: BLE001 - keep the loop alive
            LOG.exception("retry loop error")


# --------------------------------------------------------------------------- #
# Signal receive websocket (minimal RFC 6455 client, receive-only)
# --------------------------------------------------------------------------- #
class WebSocket:
    """Just enough of a websocket client to read text frames and answer pings."""

    def __init__(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        host, port = parsed.hostname, parsed.port or 80
        self.sock = socket.create_connection((host, port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.sock.sendall(
            (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
             "Sec-WebSocket-Version: 13\r\n\r\n").encode()
        )
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("closed during handshake")
            header += chunk
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"handshake refused: {header[:200]!r}")
        # No read timeout once open: signal-cli can be silent for hours. Dead
        # peers are caught by TCP keepalive instead.
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, val in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, opt):
                self.sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("websocket closed")
            buf += chunk
        return buf

    def _send(self, opcode: int, payload: bytes) -> None:
        # Client frames must be masked.
        mask = os.urandom(4)
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def recv_text(self) -> str:
        """Block until a complete text message arrives."""
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping
                self._send(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise ConnectionError("server closed websocket")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def on_envelope(msg: dict) -> None:
    """Act on reactions that target one of our alerts."""
    env = msg.get("envelope") or msg.get("params", {}).get("envelope") or {}
    reaction = (env.get("dataMessage") or {}).get("reaction")
    if not reaction or reaction.get("isRemove"):
        return
    target_author = reaction.get("targetAuthorNumber") or reaction.get("targetAuthor")
    if target_author != SIGNAL_NUMBER:
        return
    emoji = reaction.get("emoji", "")
    if emoji in POSITIVE:
        verdict = "positive"
    elif emoji in NEGATIVE:
        verdict = "negative"
    else:
        return
    target_ts = int(reaction.get("targetSentTimestamp", 0))
    who = env.get("sourceName") or env.get("sourceNumber") or env.get("sourceUuid")
    LOG.info("reaction %s from %s on alert %s", emoji, who, target_ts)
    handle_verdict(target_ts, verdict)


def receive_loop() -> None:
    """Hold the receive websocket open forever, reconnecting with backoff."""
    url = SIGNAL_API.replace("http://", "ws://", 1) + \
        f"/v1/receive/{urllib.parse.quote(SIGNAL_NUMBER)}"
    backoff = 5
    while True:
        ws = None
        try:
            ws = WebSocket(url)
            LOG.info("receive websocket connected")
            backoff = 5
            while True:
                raw = ws.recv_text()
                try:
                    on_envelope(json.loads(raw))
                except (ValueError, TypeError):
                    LOG.debug("non-JSON frame: %s", raw[:200])
                except Exception:  # noqa: BLE001 - one bad message must not drop the socket
                    LOG.exception("error handling message")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("receive websocket down (%s); reconnecting in %ss", exc, backoff)
        finally:
            if ws:
                ws.close()
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


# --------------------------------------------------------------------------- #
# HTTP API for Home Assistant
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    """POST /feedback/notify from HA; GET /feedback/health for probes."""

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/feedback/health", "/health"):
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/feedback/notify":
            self._reply(404, {"error": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        if not NOTIFY_TOKEN or not hmac.compare_digest(auth, f"Bearer {NOTIFY_TOKEN}"):
            self._reply(401, {"error": "unauthorised"})
            return
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            camera = req["camera"]
        except (ValueError, KeyError, TypeError):
            self._reply(400, {"error": "expected JSON with camera, zone, message"})
            return
        zone = req.get("zone")
        message = req.get("message") or f"Person detected on {camera}"
        recipient = req.get("recipient") or SIGNAL_RECIPIENT

        event = find_event(camera, zone)
        if event:
            image = fetch_image(
                f"{FRIGATE_API}/events/{event['id']}/snapshot.jpg?bbox=1&timestamp=1&h=720")
            text = f"{message}\nReact ✅ correct · ❌ wrong → Frigate+"
        else:
            # No matching event: still alert, from the live frame, just without
            # the Frigate+ hook.
            image = fetch_image(f"{FRIGATE_API}/{camera}/latest.jpg?bbox=1&timestamp=1&h=720")
            text = message
        ts = signal_send(text, recipient, image)
        if ts is None:
            self._reply(502, {"error": "signal send failed"})
            return
        if event:
            STORE.add_alert(ts, event["id"], camera, recipient)
        LOG.info("alert sent ts=%s camera=%s zone=%s event=%s",
                 ts, camera, zone, event["id"] if event else None)
        self._reply(200, {"timestamp": ts, "event_id": event["id"] if event else None})

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    """Start the receive, retry and HTTP threads."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    missing = [n for n, v in (("SIGNAL_NUMBER", SIGNAL_NUMBER),
                              ("SIGNAL_RECIPIENT", SIGNAL_RECIPIENT),
                              ("NOTIFY_TOKEN", NOTIFY_TOKEN)) if not v]
    if missing:
        LOG.error("missing required env: %s", ", ".join(missing))
        sys.exit(2)
    # Komodo passes an undefined [[VAR]] through literally - catch it here
    # rather than sending to a recipient called "[[SIGNAL_HA_ID]]".
    for name, value in (("SIGNAL_NUMBER", SIGNAL_NUMBER), ("SIGNAL_RECIPIENT", SIGNAL_RECIPIENT),
                        ("NOTIFY_TOKEN", NOTIFY_TOKEN)):
        if value.startswith("[["):
            LOG.error("%s is an undefined Komodo variable: %s", name, value)
            sys.exit(2)

    global STORE
    STORE = Store(DB_PATH)
    threading.Thread(target=receive_loop, name="receive", daemon=True).start()
    threading.Thread(target=retry_loop, name="retry", daemon=True).start()
    LOG.info("listening on :%s, bot %s", LISTEN_PORT, SIGNAL_NUMBER)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
