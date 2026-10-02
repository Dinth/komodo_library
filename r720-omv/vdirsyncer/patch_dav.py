#!/usr/bin/env python3
"""Build-time patch for vdirsyncer's DAV storage (vdirsyncer/storage/dav.py).

Purpose:
    Applied once by the Dockerfile. Three changes to DAVStorage._put, each
    anchored on an exact source line so the build fails loudly when a base
    image bump moves the code:

    1. Record the server's Location response header instead of the request
       URL. Google CardDAV reassigns the href on PUT; without this vdirsyncer
       deletes and re-uploads its own contacts until Google tombstones them
       and every upload 400s. Upstream: pimutils/vdirsyncer#1223.

    2. Repair Google's malformed year-less birthday
       (`BDAY;VALUE=DATE,X-APPLE-OMIT-YEAR=1604:`, a comma where a semicolon
       belongs) on the way out; Nextcloud/SabreDAV answers it with a 500.

    3. Drop ORGANIZER and ATTENDEE lines from uploads whose target href
       contains one of VDIRSYNCER_STRIP_SCHEDULING_PATHS (comma separated,
       read at runtime). Nextcloud runs CalDAV scheduling on every event that
       carries them: it rewrites other local users' copies of the same event,
       tries to email the attendees, and crashes on detached recurrence
       instances. Only safe for a ONE-WAY mirror into that path - on a two-way
       pair the stripped copy would travel back and remove the attendees at
       the source.

    2 and 3 only change what is sent, never what vdirsyncer hashes, so they
    cause no re-sync churn.

Dependencies:
    Python 3 standard library; a pipx-installed vdirsyncer under
    /opt/pipx/venvs/vdirsyncer.

Author: AI (Claude)
"""

import glob

HELPER = '''import os as _os
import re as _re

_STRIP_SCHEDULING_PATHS = tuple(
    path.strip()
    for path in _os.environ.get("VDIRSYNCER_STRIP_SCHEDULING_PATHS", "").split(",")
    if path.strip()
)
_SCHEDULING_LINES = _re.compile(
    r"^(?:ORGANIZER|ATTENDEE)[;:].*(?:\\r?\\n[ \\t].*)*\\r?\\n",
    _re.IGNORECASE | _re.MULTILINE,
)


def _outgoing(href, raw):
    raw = raw.replace(";VALUE=DATE,X-APPLE-OMIT-YEAR", ";VALUE=DATE;X-APPLE-OMIT-YEAR")
    if any(path in href for path in _STRIP_SCHEDULING_PATHS):
        raw = _SCHEDULING_LINES.sub("", raw)
    return raw


'''

REPLACEMENTS = [
    # 1. Location header
    (
        "        href = self._normalize_href(str(response.url))",
        '        location = response.headers.get("Location")\n'
        "        href = self._normalize_href(location or str(response.url))",
    ),
    # 2 + 3. outgoing body
    (
        'data=item.raw.encode("utf-8")',
        'data=_outgoing(href, item.raw).encode("utf-8")',
    ),
    # helper the line above calls
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
    print("patched dav.py: Location header, BDAY separator, scheduling strip")


if __name__ == "__main__":
    main()
