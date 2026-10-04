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
      * when Google refuses a change coming from Nextcloud (HTTP 400 on the
        PUT, typically an attendee's copy of someone else's event), forgets
        that one event's sync history so the retry treats it as a conflict and
        `conflict_resolution = "b wins"` puts Google's copy back into
        Nextcloud. Only for pairs listed in VDIRSYNCER_GOOGLE_WINS_ON_REJECT,
        at most VDIRSYNCER_RESET_MAX events per pair per run, and never the
        same event twice in 24h - a repeat means something keeps re-creating
        the change, and that should fail visibly instead;
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
      VDIRSYNCER_GOOGLE_WINS_ON_REJECT
                                   pairs whose second storage is Google and
                                   whose conflict_resolution is "b wins"
      VDIRSYNCER_RESET_MAX         refused events reset per pair per run
                                   (default 5); more than that resets none
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
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path(os.environ.get("VDIRSYNCER_CONFIG", "/vdirsyncer/config"))
VDIRSYNCER = os.environ.get("VDIRSYNCER_EXECUTABLE_PATH", "/usr/local/bin/vdirsyncer")
STATUS_FILE = Path(os.environ.get("VDIRSYNCER_JOB_STATUS_FILE", "/job/vdirsyncer.json"))
RETRY_DELAY = int(os.environ.get("VDIRSYNCER_RETRY_DELAY", "45"))
GOOGLE_WINS_ON_REJECT = {
    pair.strip()
    for pair in os.environ.get("VDIRSYNCER_GOOGLE_WINS_ON_REJECT", "").split(",")
    if pair.strip()
}
RESET_MAX = int(os.environ.get("VDIRSYNCER_RESET_MAX", "5"))

# A pair due "every 60 minutes" on a 15-minute cron would otherwise slip to 75
# whenever the previous run finished a few seconds after the tick.
INTERVAL_SLACK = 120
COMMAND_TIMEOUT = 600
HISTORY_WINDOW = 24 * 3600
ERROR_LENGTH = 200

# vdirsyncer's line for a PUT that Google answered with 400, e.g.
#   error: Unknown error occurred for nextcloud_google/personal: 400,
#   message='Bad Request', url='https://apidata.googleusercontent.com/caldav/...'
REFUSAL = re.compile(
    r"Unknown error occurred for ([^/\s]+)/(\S+): 400, message='Bad Request', "
    r"url='(https://apidata\.googleusercontent\.com/[^']+)'"
)


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
    """Metasync then sync one pair. Returns (ok, first error line or "", output)."""
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
            return False, f"{action} timed out", ""
        # Pass vdirsyncer's own output through, so `docker logs` reads as before.
        print(result.stdout, end="", flush=True)
        if result.returncode != 0:
            errors = [
                line[len("error: "):]
                for line in result.stdout.splitlines()
                if line.startswith("error: ") and "-vdebug" not in line
            ]
            error = (errors[0] if errors else f"{action} exited {result.returncode}")
            return False, error[:ERROR_LENGTH], result.stdout
    return True, "", ""


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


def reset_refused(pair, output, status_path, recent, now):
    """Forget the sync history of events Google refused, so Google's copy wins.

    Returns one {t, pair, ident} record per event reset. Leaves the database
    untouched when there are more refusals than RESET_MAX or an event was
    already reset in the last 24h.
    """
    refused = sorted({(collection, url) for p, collection, url in REFUSAL.findall(output) if p == pair})
    if not refused:
        return []
    if len(refused) > RESET_MAX:
        print(f"error: Google refused {len(refused)} changes in {pair}, more than "
              f"VDIRSYNCER_RESET_MAX={RESET_MAX}; resetting none of them", flush=True)
        return []
    done = []
    backed_up = set()
    for collection, url in refused:
        database = status_path / pair / f"{collection}.items"
        path = urllib.parse.urlsplit(url).path
        connection = sqlite3.connect(database)
        try:
            row = connection.execute(
                "SELECT ident FROM status WHERE href_b IN (?, ?)",
                (path, urllib.parse.unquote(path)),
            ).fetchone()
            if row is None:
                print(f"warning: no sync history for refused {url}; nothing to reset", flush=True)
                continue
            ident = row[0]
            if (pair, ident) in recent:
                print(f"error: Google refused {ident} in {pair} again after it was reset "
                      "within the last 24h; leaving it failing", flush=True)
                continue
            if database not in backed_up:
                # One rolling copy of the database as it was before this run's resets.
                backup = sqlite3.connect(database.with_name(database.name + ".pre-reset"))
                connection.backup(backup)
                backup.close()
                backed_up.add(database)
            connection.execute("DELETE FROM status WHERE ident = ?", (ident,))
            connection.commit()
        finally:
            connection.close()
        print(f"Google refused Nextcloud's change to {ident} in {pair}; reset it so "
              "the retry puts Google's copy back into Nextcloud", flush=True)
        done.append({"t": int(now), "pair": pair, "ident": ident})
    return done


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
    outputs = {}
    for pair in due:
        ok, error, output = sync_pair(pair)
        if not ok:
            errors[pair] = error
            outputs[pair] = output

    recent = [entry for entry in state.get("reset", []) if started - entry["t"] < HISTORY_WINDOW]
    recent_keys = {(entry["pair"], entry["ident"]) for entry in recent}
    for pair in sorted(errors):
        if pair in GOOGLE_WINS_ON_REJECT:
            recent += reset_refused(pair, outputs[pair], status_path, recent_keys, started)

    retried = sorted(errors)
    if retried:
        print(f"Retrying in {RETRY_DELAY}s: {', '.join(retried)}", flush=True)
        time.sleep(RETRY_DELAY)
        for pair in retried:
            ok, error, _ = sync_pair(pair)
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
        # events whose Nextcloud change Google refused and that were put back
        # to Google's copy; listed so a reset is never silent
        "reset24h": len(recent),
        "reset": recent,
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
