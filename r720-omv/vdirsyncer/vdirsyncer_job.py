#!/usr/bin/env python3
"""Scheduled vdirsyncer run: per-pair sync, one retry, status file for Homepage.

Purpose:
    Replaces the image's fixed `vdirsyncer metasync && vdirsyncer sync` cron
    line. Every tick it:
      * syncs each pair in its own vdirsyncer process, one after another, so
        one pair's failure no longer takes the others down with it and the
        Google storages stop refreshing the shared token concurrently;
      * skips pairs that are not due yet (VDIRSYNCER_PAIR_INTERVALS), which is
        how contacts run hourly while calendars keep the cron cadence;
      * retries the pairs that failed, once, after VDIRSYNCER_RETRY_DELAY;
      * writes VDIRSYNCER_JOB_STATUS_FILE, the document the Homepage tile
        reads. The same file is the job's own state (last success per pair,
        24h run history), so there is nothing else to keep in sync.
    Exits 1 when a pair is still failing after the retry, so supercronic logs
    `error running command` exactly as it did before.

    Environment:
      VDIRSYNCER_CONFIG            config path (image default)
      VDIRSYNCER_EXECUTABLE_PATH   vdirsyncer binary (image default)
      VDIRSYNCER_PAIR_INTERVALS    "pair=minutes,pair=minutes"; unlisted pairs
                                   run every tick
      VDIRSYNCER_RETRY_DELAY       seconds before the retry (default 45)
      VDIRSYNCER_JOB_STATUS_FILE   status/state document (default
                                   /job/vdirsyncer.json)

Dependencies:
    Python 3 standard library only; the vdirsyncer CLI.

Author: AI (Claude)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path(os.environ.get("VDIRSYNCER_CONFIG", "/vdirsyncer/config"))
VDIRSYNCER = os.environ.get("VDIRSYNCER_EXECUTABLE_PATH", "/usr/local/bin/vdirsyncer")
STATUS_FILE = Path(os.environ.get("VDIRSYNCER_JOB_STATUS_FILE", "/job/vdirsyncer.json"))
RETRY_DELAY = int(os.environ.get("VDIRSYNCER_RETRY_DELAY", "45"))

# A pair due "every 60 minutes" on a 15-minute cron would otherwise slip to 75
# whenever the previous run finished a few seconds after the tick.
INTERVAL_SLACK = 120
COMMAND_TIMEOUT = 600
HISTORY_WINDOW = 24 * 3600
ERROR_LENGTH = 200


def iso(epoch):
    """Format an epoch as the UTC ISO timestamp Homepage's relativeDate parses."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_config():
    """Return (pair names in file order, status_path) from the vdirsyncer config."""
    text = CONFIG.read_text()
    pairs = re.findall(r"^\[pair\s+([^\]\s]+)\]", text, re.MULTILINE)
    status = re.search(r'^status_path\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return pairs, Path(status.group(1) if status else "/status")


def parse_intervals():
    """Parse VDIRSYNCER_PAIR_INTERVALS into {pair: seconds}."""
    intervals = {}
    for entry in os.environ.get("VDIRSYNCER_PAIR_INTERVALS", "").split(","):
        if not entry.strip():
            continue
        pair, _, minutes = entry.partition("=")
        intervals[pair.strip()] = int(minutes) * 60
    return intervals


def load_state():
    """Load the previous status document; a missing or corrupt one starts fresh."""
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    """Write the status document via tmp + rename so nginx never serves a partial."""
    tmp = STATUS_FILE.with_name(STATUS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n")
    os.replace(tmp, STATUS_FILE)


def sync_pair(pair):
    """Metasync then sync one pair. Returns (ok, first error line or "")."""
    for action in ("metasync", "sync"):
        try:
            result = subprocess.run(
                [VDIRSYNCER, "-c", str(CONFIG), action, pair],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=COMMAND_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(f"error: {action} {pair} timed out after {COMMAND_TIMEOUT}s", flush=True)
            return False, f"{action} timed out"
        # Pass vdirsyncer's own output through, so `docker logs` reads as before.
        print(result.stdout, end="", flush=True)
        if result.returncode != 0:
            errors = [
                line[len("error: "):]
                for line in result.stdout.splitlines()
                if line.startswith("error: ") and "-vdebug" not in line
            ]
            return False, (errors[0] if errors else f"{action} exited {result.returncode}")[:ERROR_LENGTH]
    return True, ""


def count_items(status_path, pair):
    """Count the items vdirsyncer tracks for a pair, summed over its collections."""
    total = 0
    for database in sorted((status_path / pair).glob("*.items")):
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                total += connection.execute("SELECT COUNT(*) FROM status").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.Error:
            return None
    return total


def main():
    """Run every due pair, retry the failures once, publish the status document."""
    started = time.time()
    pairs, status_path = read_config()
    intervals = parse_intervals()
    state = load_state()
    pair_state = {pair: state.get("pairs", {}).get(pair, {}) for pair in pairs}

    due = [
        pair
        for pair in pairs
        if started - pair_state[pair].get("lastSuccessEpoch", 0)
        >= intervals.get(pair, 0) - INTERVAL_SLACK
    ]
    skipped = [pair for pair in pairs if pair not in due]
    if skipped:
        print(f"Not due yet, skipping: {', '.join(skipped)}", flush=True)

    errors = {}
    for pair in due:
        ok, error = sync_pair(pair)
        if not ok:
            errors[pair] = error

    retried = sorted(errors)
    if retried:
        print(f"Retrying in {RETRY_DELAY}s: {', '.join(retried)}", flush=True)
        time.sleep(RETRY_DELAY)
        for pair in retried:
            ok, error = sync_pair(pair)
            if ok:
                del errors[pair]
            else:
                errors[pair] = error

    finished = time.time()
    for pair in pairs:
        entry = pair_state[pair]
        if pair in due:
            entry["state"] = -1 if pair in errors else 1
            entry["error"] = errors.get(pair, "")
            if pair not in errors:
                entry["lastSuccessEpoch"] = int(finished)
                entry["lastSuccess"] = iso(finished)
        entry.setdefault("state", 0)
        entry["intervalMinutes"] = intervals.get(pair, 0) // 60
        entry["items"] = count_items(status_path, pair)

    run_ok = not errors
    runs = [run for run in state.get("runs", []) if started - run["t"] < HISTORY_WINDOW]
    runs.append({"t": int(started), "ok": run_ok, "retried": bool(retried)})
    failed = sum(1 for run in runs if not run["ok"])

    state = {
        "updated": iso(finished),
        "lastRun": iso(started),
        "lastSuccess": iso(finished) if run_ok else state.get("lastSuccess"),
        # Homepage colours on `parseFloat(raw) > 0`: 1 = clean, 2 = needed the
        # retry, -1 = still failing.
        "state": (2 if retried else 1) if run_ok else -1,
        "runs24h": len(runs),
        "failed24h": failed,
        "retried24h": sum(1 for run in runs if run["retried"]),
        "failedState": 1 if failed == 0 else -1,
        "pairs": pair_state,
        "runs": runs,
    }
    try:
        save_state(state)
    except OSError as exc:
        # The sync itself is what matters; a tile problem must not fail the job.
        print(f"warning: could not write {STATUS_FILE}: {exc}", flush=True)

    if errors:
        for pair, error in errors.items():
            print(f"error: {pair} still failing after retry: {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
