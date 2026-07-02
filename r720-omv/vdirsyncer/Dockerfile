# Patched vdirsyncer image. Two fixes to vdirsyncer's DAVStorage._put:
#
# 1. Record the server's Location response header (Google CardDAV reassigns the
#    resource href on PUT) instead of the request URL. Without it, google_contacts
#    uploads never match what list() returns next sync, so vdirsyncer deletes and
#    re-uploads its own contacts, Google tombstones the deleted UID hrefs, and
#    every upload 400s. Upstream: https://github.com/pimutils/vdirsyncer/issues
#
# 2. Work around Google emitting a malformed BDAY for year-less birthdays
#    (`BDAY;VALUE=DATE,X-APPLE-OMIT-YEAR=1604:` — comma instead of a semicolon
#    between two params), which crashes Nextcloud/SabreDAV with a 500 TypeError
#    (strtoupper on an array). Fix the separator on the way out.
#
# Drop this image and go back to the plain pinned base once (1) ships upstream
# and Google/Sabre stop mangling year-less BDAYs.
#
# BASE_IMAGE is pinned in compose.yaml (x-versions) and passed as a build arg,
# which is the source of truth; the default below only covers a bare `docker
# build` and should be kept in sync. Bump + redeploy (rebuild) to update.
ARG BASE_IMAGE="bleala/vdirsyncer:2.6.1"
FROM ${BASE_IMAGE}

# Apply the patch as root (base image ends with USER vdirsyncer); the build
# fails loudly if the expected source line is missing (e.g. base bumped
# vdirsyncer), which is the signal to re-check the patch.
USER root
RUN python3 -c "import glob; p=glob.glob('/opt/pipx/venvs/vdirsyncer/lib/python3.*/site-packages/vdirsyncer/storage/dav.py')[0]; s=open(p).read(); o1='        href = self._normalize_href(str(response.url))'; n1='        location = response.headers.get(\"Location\")\n        href = self._normalize_href(location or str(response.url))'; assert s.count(o1)==1, 'anchor1 count %d' % s.count(o1); s=s.replace(o1,n1); o2='data=item.raw.encode(\"utf-8\")'; n2='data=item.raw.replace(\";VALUE=DATE,X-APPLE-OMIT-YEAR\", \";VALUE=DATE;X-APPLE-OMIT-YEAR\").encode(\"utf-8\")'; assert s.count(o2)==1, 'anchor2 count %d' % s.count(o2); s=s.replace(o2,n2); open(p,'w').write(s); print('patched dav.py: Location header + BDAY separator workaround')"
USER vdirsyncer
