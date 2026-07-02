# Patched vdirsyncer image.
#
# Adds a one-line fix to vdirsyncer's DAVStorage._put so it records the server's
# Location response header (Google CardDAV reassigns the resource href on PUT)
# instead of the request URL. Without it, google_contacts uploads never match
# what list() returns next sync, so vdirsyncer deletes and re-uploads its own
# contacts, Google tombstones the deleted UID hrefs, and every upload 400s.
#
# Upstream issue: https://github.com/pimutils/vdirsyncer/issues (Location header
# ignored in DAVStorage._put). Drop this image and go back to the plain pinned
# base image once the fix ships upstream.
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
RUN python3 -c "import glob; p=glob.glob('/opt/pipx/venvs/vdirsyncer/lib/python3.*/site-packages/vdirsyncer/storage/dav.py')[0]; s=open(p).read(); old='        href = self._normalize_href(str(response.url))'; new='        location = response.headers.get(\"Location\")\n        href = self._normalize_href(location or str(response.url))'; assert s.count(old)==1, 'expected exactly 1 patch anchor, found %d' % s.count(old); open(p,'w').write(s.replace(old,new)); print('patched vdirsyncer DAVStorage._put in', p)"
USER vdirsyncer
