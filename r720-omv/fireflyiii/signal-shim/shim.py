#!/usr/bin/env python3
"""Firefly III Slack-notification → signal-cli-rest-api bridge.

Purpose:
    Firefly III 6.6.x can only notify over mail, Slack or Pushover (its ntfy
    channel is commented out upstream, see issue 12083). This service accepts
    Firefly's Slack webhook POSTs, flattens them to plain text and re-posts
    them to an existing signal-cli-rest-api instance, so Firefly's bill
    reminders and security notifications land in Signal alongside Grafana's.

    Firefly validates the configured webhook URL with
    FireflyIII\\Support\\Notifications\\UrlValidator::isValidWebhookURL(), which
    accepts *any* URL ending in "/slack" as well as the real Slack/Discord
    prefixes. That is the hook this service hangs off, so no Slack account or
    outbound internet access is involved.

    Firefly emits the LEGACY Slack format, not Block Kit, because its
    notifications build a SlackMessage with ->content() and ->attachment():
        {"text": "...", "attachments": [{"title": "...", "title_link": "..."}]}
    Block Kit "blocks" are still parsed defensively in case a future release
    switches over.

Dependencies:
    Python 3.11+ standard library only (http.server, urllib.request). No pip
    packages, so the image needs no build-time network access.

Author:
    AI (Claude), 2026-08-19.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("signal-shim")

# signal-cli-rest-api endpoint. Defaults to the published port on the Docker
# host rather than a container name: signal-rest-api lives in a different stack
# and this mirrors how Grafana's contact point already reaches it.
SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://10.10.1.13:3162/v2/send")

# Registered sender. MUST be sent as a JSON string - Grafana's contact point hit
# exactly this: a bare +447... expands to a JSON number and signal-cli-rest-api
# fails to unmarshal the body.
SIGNAL_PHONE_NUMBER = os.environ.get("SIGNAL_PHONE_NUMBER", "").strip()

# Per-user routing. Firefly stores slack_webhook_url as a PER-USER preference,
# so each user gets their own URL and the token in the path selects who the
# message is delivered to. This keeps one user's financial data out of another
# user's Signal thread - which matters here because the pre-existing
# SIGNAL_TECH_ID is a Signal *group* id, not a personal number.
#
# Format: "<token>:<recipient>[|<recipient>...],<token2>:<recipient>"
# Recipients are phone numbers or Signal group ids. Group ids are base64, whose
# alphabet contains no ":" or ",", so both separators are unambiguous.
SHIM_ROUTES_RAW = os.environ.get("SHIM_ROUTES", "").strip()

# Single-route fallback, kept so a one-user deployment needs no route map.
SHIM_TOKEN = os.environ.get("SHIM_TOKEN", "").strip()
SIGNAL_RECIPIENTS = [
    r.strip() for r in os.environ.get("SIGNAL_RECIPIENTS", "").split(",") if r.strip()
]

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "15"))
MAX_BODY_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 4000


# E.164: leading +, no leading zero, 7-15 digits. Group ids are long base64
# strings and are accepted as-is.
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def mask(value: str) -> str:
    """Redact a phone number for logging: keep enough to identify a typo."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:3]}…{value[-2:]} (len {len(value)})"


def is_valid_target(value: str) -> bool:
    """True for an E.164 number or something long enough to be a group id."""
    return bool(E164_RE.match(value)) or (value.startswith("g") and len(value) > 20)


def parse_routes() -> dict[str, list[str]]:
    """Build {token: [recipient, ...]} from SHIM_ROUTES, or the single-route env."""
    routes: dict[str, list[str]] = {}
    for entry in SHIM_ROUTES_RAW.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            LOG.warning("ignoring malformed SHIM_ROUTES entry (no colon)")
            continue
        token, _, recipients = entry.partition(":")
        token = token.strip()
        targets = [r.strip() for r in recipients.split("|") if r.strip()]
        if not token or not targets:
            LOG.warning("ignoring SHIM_ROUTES entry with empty token or recipients")
            continue
        valid = [t for t in targets if is_valid_target(t)]
        for bad in [t for t in targets if not is_valid_target(t)]:
            LOG.error(
                "route %s…: recipient %s is not an E.164 number or group id - dropped",
                token[:6],
                mask(bad),
            )
        if not valid:
            LOG.error("route %s…: no valid recipients left, route dropped", token[:6])
            continue
        routes[token] = valid

    if not routes and SHIM_TOKEN and SIGNAL_RECIPIENTS:
        routes[SHIM_TOKEN] = SIGNAL_RECIPIENTS
    return routes


ROUTES: dict[str, list[str]] = {}


def recipients_for_path(path: str) -> list[str] | None:
    """Return the recipients for a request path, or None if it matches no route.

    The path must be exactly /<token>/slack. The "/slack" suffix is what makes
    Firefly's UrlValidator::isValidWebhookURL() accept the URL at all.
    """
    suffix = "/slack"
    if not path.startswith("/") or not path.endswith(suffix):
        return None
    token = path[1: -len(suffix)]
    if not token or "/" in token:
        return None
    return ROUTES.get(token)


def flatten_payload(payload: dict) -> str:
    """Reduce a Slack webhook payload to the plain text Signal will carry.

    Handles the legacy format Firefly actually sends (text + attachments) and,
    defensively, Block Kit "blocks" should upstream ever switch.
    """
    lines: list[str] = []

    text = (payload.get("text") or "").strip()
    if text:
        lines.append(text)

    for attachment in payload.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        for key in ("title", "text", "fallback"):
            value = (attachment.get(key) or "").strip()
            if value and value not in lines:
                lines.append(value)
        link = (attachment.get("title_link") or "").strip()
        if link:
            lines.append(link)
        for field in attachment.get("fields") or []:
            if isinstance(field, dict):
                title = (field.get("title") or "").strip()
                value = (field.get("value") or "").strip()
                if title or value:
                    lines.append(f"{title}: {value}".strip(": "))

    # Block Kit fallback - only consulted if nothing was found above.
    if not lines:
        for block in payload.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            block_text = block.get("text")
            if isinstance(block_text, dict):
                value = (block_text.get("text") or "").strip()
                if value:
                    lines.append(value)

    message = "\n".join(lines).strip()
    if not message:
        message = "Firefly III sent an empty notification."
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[: MAX_MESSAGE_CHARS - 1] + "…"
    return message


def send_to_signal(message: str, recipients: list[str]) -> None:
    """POST one plain-text message to signal-cli-rest-api. Raises on failure."""
    body = json.dumps(
        {
            "message": message,
            "number": str(SIGNAL_PHONE_NUMBER),
            "recipients": recipients,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        SIGNAL_API_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            if response.status >= 300:
                raise RuntimeError(f"signal-cli-rest-api returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        # The status alone is not actionable - signal-cli-rest-api explains the
        # real problem (bad sender, unregistered recipient) in the body.
        try:
            detail = exc.read(2048).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - never mask the original failure
            detail = "<body unreadable>"
        raise RuntimeError(f"HTTP {exc.code} from signal-cli-rest-api: {detail}") from exc


class Handler(BaseHTTPRequestHandler):
    """Answers GET /healthz and POST on the configured /…/slack path."""

    server_version = "firefly-signal-shim"

    def _reply(self, status: int, text: str) -> None:
        """Write a short plain-text response."""
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        """Health probe for the container healthcheck."""
        if self.path == "/healthz":
            self._reply(200, "ok")
            return
        self._reply(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        """Accept a Slack-format notification and relay it to Signal."""
        recipients = recipients_for_path(self.path)
        if recipients is None:
            # Deliberately does not echo the path: it carries the route token.
            LOG.warning("rejected POST to an unrecognised path")
            self._reply(404, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reply(400, "bad content-length")
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(400, "bad content-length")
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            LOG.warning("undecodable payload: %s", exc)
            self._reply(400, "invalid json")
            return
        if not isinstance(payload, dict):
            self._reply(400, "invalid json")
            return

        message = flatten_payload(payload)
        try:
            send_to_signal(message, recipients)
        except (urllib.error.URLError, RuntimeError, OSError) as exc:
            # Deliberately a 5xx: Laravel's Slack channel raises on non-2xx, so
            # a broken relay shows up in Firefly's log instead of vanishing.
            LOG.error("relay to signal failed: %s", exc)
            self._reply(502, "relay failed")
            return

        LOG.info(
            "relayed notification to signal (%d chars, %d recipient(s))",
            len(message),
            len(recipients),
        )
        self._reply(200, "ok")

    def log_message(self, fmt: str, *args: object) -> None:
        """Route the stdlib access log through logging (stdout → Alloy → Loki)."""
        LOG.debug("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    """Validate configuration and serve until terminated."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    global ROUTES
    ROUTES = parse_routes()

    if not SIGNAL_PHONE_NUMBER:
        LOG.error("SIGNAL_PHONE_NUMBER is required")
        raise SystemExit(1)
    if not E164_RE.match(SIGNAL_PHONE_NUMBER):
        # Caught a real "SIGNAL_PHONE_NUMBER==+44..." typo in Komodo, which
        # otherwise surfaced only as an opaque HTTP 400 at send time.
        LOG.error(
            "SIGNAL_PHONE_NUMBER is not a valid E.164 number: %s - "
            "check for stray characters in the Komodo variable",
            mask(SIGNAL_PHONE_NUMBER),
        )
        raise SystemExit(1)
    if not ROUTES:
        LOG.error("no routes configured: set SHIM_ROUTES (or SHIM_TOKEN + SIGNAL_RECIPIENTS)")
        raise SystemExit(1)

    LOG.info(
        "listening on :%d with %d route(s), relaying to %s",
        LISTEN_PORT,
        len(ROUTES),
        SIGNAL_API_URL,
    )
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
