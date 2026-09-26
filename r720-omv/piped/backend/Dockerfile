# syntax=docker/dockerfile:1
#
# Piped-Backend rebuilt against the NewPipeExtractor that NewPipe's stable app
# ships. Built by the "piped-backend" Komodo Build in ../../../builds.toml
# (weekly, via the rebuild-local-images Procedure) and consumed as
# piped-backend:latest by ../compose.yaml.
#
# Why: upstream's published image pins a FireMasterK extractor fork from
# 2026-05-29, 86 commits behind TeamNewPipe. It predates the SABR workaround
# (NewPipeExtractor#1508) and the client rework (#1529), so YouTube gives it
# only the muxed 360p itag 18 -- and nothing for videos that lack one (long
# VODs, ex-livestreams: "Could not get any stream"). Measured on r720
# 2026-09-26: the same code on NewPipe stable's extractor returns adaptive
# formats up to 2160p60 and five audio tracks.
#
# Nothing here is pinned, on purpose -- every build takes:
#   * Piped-Backend master (so upstream fixes arrive), and
#   * the extractor + nanojson versions from NewPipe master's
#     gradle/libs.versions.toml (NewPipe's stable release line).
# Once upstream Piped catches up, the substitution below is a no-op. If NewPipe
# ever ships an extractor API that Piped's code doesn't compile against, this
# build fails, the running image stays, and the Procedure's failure alert says
# so -- the moment to look is then, not before. Retire this Build entirely when
# 1337kavin/piped tracks TeamNewPipe again.

FROM eclipse-temurin:21-jdk AS build
ADD --keep-git-dir=false https://github.com/TeamPiped/Piped-Backend.git#master /app
ADD https://raw.githubusercontent.com/TeamNewPipe/NewPipe/master/gradle/libs.versions.toml /tmp/newpipe-libs.toml
WORKDIR /app
# Swap whichever fork/commit upstream pins for NewPipe stable's versions,
# excluding the extractor's transitive nanojson so exactly one copy ships.
# The greps make the build fail loudly if either line wasn't rewritten, rather
# than silently keeping the stale fork.
RUN set -eu; \
    npe=$(sed -nE 's/^teamnewpipe-newpipe-extractor *= *"([^"]+)".*/\1/p' /tmp/newpipe-libs.toml); \
    nj=$(sed -nE 's/^teamnewpipe-nanojson *= *"([^"]+)".*/\1/p' /tmp/newpipe-libs.toml); \
    test -n "$npe" && test -n "$nj"; \
    sed -i -E \
      -e "s#implementation[ (]*'com\.github\.[A-Za-z0-9]+:NewPipeExtractor:[^']+'\)?( *\{)?#implementation('com.github.TeamNewPipe:NewPipeExtractor:${npe}') { exclude group: 'com.github.TeamNewPipe', module: 'nanojson' }#" \
      -e "s#'com\.github\.[A-Za-z0-9]+:nanojson:[^']+'#'com.github.TeamNewPipe:nanojson:${nj}'#" \
      build.gradle; \
    grep -q "com.github.TeamNewPipe:NewPipeExtractor:${npe}'" build.gradle; \
    grep -q "com.github.TeamNewPipe:nanojson:${nj}'" build.gradle; \
    echo "$(date -u +%Y%m%d)-master-npe${npe#v}" > VERSION; \
    echo "Building Piped-Backend master with NewPipeExtractor ${npe}, nanojson ${nj}"
RUN --mount=type=cache,target=/root/.gradle/caches/ \
    ./gradlew --no-daemon shadowJar

# Runtime stage mirrors upstream's Dockerfile.
FROM eclipse-temurin:21-jre
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app/
COPY --from=build /app/hotspot-entrypoint.sh /app/docker-healthcheck.sh /
COPY --from=build /app/build/libs/piped-1.0-all.jar /app/piped.jar
COPY --from=build /app/VERSION .
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD /docker-healthcheck.sh
ENTRYPOINT ["/hotspot-entrypoint.sh"]
