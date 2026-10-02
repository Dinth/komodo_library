# Patched vdirsyncer image: three changes to vdirsyncer's DAVStorage._put,
# applied by patch_dav.py (the reasons are in its docstring):
#
# 1. Record the server's Location response header instead of the request URL,
#    so Google Contacts uploads are not deleted and re-uploaded until Google
#    tombstones them. Upstream: https://github.com/pimutils/vdirsyncer/issues/1223
# 2. Repair Google's malformed year-less BDAY on the way out; Nextcloud answers
#    it with a 500.
# 3. Strip ORGANIZER/ATTENDEE from uploads into the paths named in
#    VDIRSYNCER_STRIP_SCHEDULING_PATHS (compose.yaml), so a one-way mirror into
#    Nextcloud does not set off Nextcloud's invitation handling.
#
# Drop 1 and 2 and go back to the plain pinned base once (1) ships upstream
# and Google/Sabre stop mangling year-less BDAYs.
#
# BASE_IMAGE is pinned in compose.yaml (x-versions) and passed as a build arg,
# which is the source of truth; the default below only covers a bare `docker
# build` and should be kept in sync. Bump + redeploy (rebuild) to update.
ARG BASE_IMAGE="bleala/vdirsyncer:2.7.0"
FROM ${BASE_IMAGE}

# Apply the patch as root (base image ends with USER vdirsyncer); the build
# fails loudly if an expected source line is missing (e.g. base bumped
# vdirsyncer), which is the signal to re-check the patch.
USER root
COPY patch_dav.py /tmp/patch_dav.py
RUN python3 /tmp/patch_dav.py && rm /tmp/patch_dav.py

# Scheduled job wrapper: syncs pair by pair, retries failures once, lets
# contacts run less often than calendars, and writes the status document the
# Homepage tile reads. See the docstring in vdirsyncer_job.py.
COPY --chmod=0755 vdirsyncer_job.py /usr/local/bin/vdirsyncer-job
USER vdirsyncer

# The image's start.sh can only schedule its own fixed `metasync && sync` line,
# so write the crontab here instead. Same supercronic, same CRON_TIME/CRON_FILE
# variables, and the image's healthcheck (pgrep supercronic + CRON_FILE) still
# holds. AUTOSYNC/AUTODISCOVER/AUTOUPDATE no longer do anything.
ENTRYPOINT ["/bin/sh", "-c", "printf '%s %s\\n' \"$CRON_TIME\" /usr/local/bin/vdirsyncer-job > \"$CRON_FILE\" && exec \"$SUPERCRONIC_EXECUTABLE_PATH\" \"$CRON_FILE\""]
