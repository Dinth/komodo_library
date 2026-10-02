#!/usr/bin/env python3
"""Build-time patch for vdirsyncer's DAV storage (vdirsyncer/storage/dav.py).

Purpose:
    Applied once by the Dockerfile. Three changes, each anchored on an exact
    source line so the build fails loudly when a base image bump moves the
    code:

    1. DAVStorage._put: record the server's Location response header instead
       of the request URL. Google CardDAV reassigns the href on PUT; without
       this vdirsyncer deletes and re-uploads its own contacts until Google
       tombstones them and every upload 400s. Upstream:
       pimutils/vdirsyncer#1223.

    2. DAVStorage._put: repair Google's malformed year-less birthday
       (`BDAY;VALUE=DATE,X-APPLE-OMIT-YEAR=1604:`, a comma where a semicolon
       belongs) on the way out; Nextcloud/SabreDAV answers it with a 500.
       Only what is sent changes, never what vdirsyncer hashes, so there is
       no re-sync churn.

    3. DAVSession.request: on PUT and DELETE to a URL containing one of
       VDIRSYNCER_NO_SCHEDULING_URLS (comma separated, read at runtime), send
       `Schedule-Reply: F` and `X-NC-Scheduling: false`. Both make Nextcloud
       skip CalDAV scheduling for that request (Sabre's Schedule plugin and
       Nextcloud's own override each check one of them). A sync client is
       copying events, not sending invitations: without the switch, every
       event with ATTENDEEs written into Nextcloud is rewritten by the server,
       delivered into other local users' calendars (changing their copies,
       which their own Google sync then tries to push and Google refuses),
       emailed to the attendees, and - for detached recurrence instances -
       crashes Sabre's iTip broker with a 500.

Dependencies:
    Python 3 standard library; a pipx-installed vdirsyncer under
    /opt/pipx/venvs/vdirsyncer.

Author: AI (Claude)
"""

import glob

HELPER = '''import os as _os

_NO_SCHEDULING_URLS = tuple(
    part.strip()
    for part in _os.environ.get("VDIRSYNCER_NO_SCHEDULING_URLS", "").split(",")
    if part.strip()
)
_NO_SCHEDULING_HEADERS = {"Schedule-Reply": "F", "X-NC-Scheduling": "false"}


'''

REPLACEMENTS = [
    # 1. Location header
    (
        "        href = self._normalize_href(str(response.url))",
        '        location = response.headers.get("Location")\n'
        "        href = self._normalize_href(location or str(response.url))",
    ),
    # 2. year-less BDAY separator
    (
        'data=item.raw.encode("utf-8")',
        'data=item.raw.replace(";VALUE=DATE,X-APPLE-OMIT-YEAR", '
        '";VALUE=DATE;X-APPLE-OMIT-YEAR").encode("utf-8")',
    ),
    # 3. no server-side scheduling for our own writes
    (
        "        more.update(kwargs)\n",
        "        more.update(kwargs)\n"
        '        if method in ("PUT", "DELETE") and any(\n'
        "            part in str(url) for part in _NO_SCHEDULING_URLS\n"
        "        ):\n"
        '            more["headers"] = {**(more.get("headers") or {}), **_NO_SCHEDULING_HEADERS}\n',
    ),
    # helper the block above uses
    (
        "dav_logger = logging.getLogger(__name__)",
        HELPER + "dav_logger = logging.getLogger(__name__)",
    ),
]


def main():
    """Apply every replacement, insisting each anchor occurs exactly once."""
    (path,) = glob.glob(
        "/opt/pipx/venvs/vdirsyncer/lib/python3.*/site-packages/vdirsyncer/storage/dav.py"
    )
    with open(path) as handle:
        source = handle.read()
    for anchor, replacement in REPLACEMENTS:
        count = source.count(anchor)
        assert count == 1, f"anchor occurs {count} times, expected 1: {anchor!r}"
        source = source.replace(anchor, replacement)
    compile(source, path, "exec")
    with open(path, "w") as handle:
        handle.write(source)
    print("patched dav.py: Location header, BDAY separator, no-scheduling headers")


if __name__ == "__main__":
    main()
