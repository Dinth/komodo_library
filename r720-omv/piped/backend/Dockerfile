# syntax=docker/dockerfile:1
#
# Piped-Backend rebuilt against the NewPipeExtractor that NewPipe's stable app
# ships. Built by the "piped-backend" Komodo Build in ../../../builds.toml and
# consumed as piped-backend:latest by ../compose.yaml.
#
# Why: upstream's published image pins a FireMasterK extractor fork from
# 2026-05-29 that is 86 commits behind TeamNewPipe. It predates the SABR
# workaround (NewPipeExtractor#1508) and the client rework (#1529), so YouTube
# gives it only the muxed 360p itag 18 -- and nothing at all for videos that
# lack one (long VODs, ex-livestreams: "Could not get any stream"). Measured on
# r720 2026-09-26: this build returns adaptive formats up to 2160p60 plus five
# audio tracks for the same videos.
#
# Source is Piped-Backend PR #895 (open, reviewed; the only requested change
# was pinning the extractor to v0.26.5), fetched at a fixed SHA via the base
# repo's pull ref so a force-push or deleted fork branch cannot change it.
# Drop this whole Build once upstream merges an extractor update and the
# published 1337kavin/piped image catches up.
ARG PIPED_BACKEND_SHA=0661a240d8c83b5d7c8eb791e89e97295672d2c0
# Keep in step with NewPipe master's gradle/libs.versions.toml.
ARG NPE_VERSION=v0.26.5

FROM eclipse-temurin:21-jdk AS build
ARG PIPED_BACKEND_SHA
ARG NPE_VERSION
ADD --keep-git-dir=false https://github.com/TeamPiped/Piped-Backend.git#${PIPED_BACKEND_SHA} /app
WORKDIR /app
# 1. The PR tracks a dev extractor commit; pin the stable release instead.
# 2. The PR also renames Description.getContent() -> content() for that newer
#    dev API. v0.26.5 still has getContent(), so undo it. Both method calls and
#    Description::content method references need it.
# 3. Upstream CI writes VERSION before `docker build`; the Dockerfile COPYs it.
RUN sed -i -E "s#(com.github.TeamNewPipe:NewPipeExtractor:)[0-9a-f]{40}#\1${NPE_VERSION}#" build.gradle \
 && grep -q "NewPipeExtractor:${NPE_VERSION}" build.gradle \
 && grep -rlE 'content\(\)|Description::content' src/main/java \
    | xargs sed -i -E 's/Description::content\b/Description::getContent/g; s/\.content\(\)/.getContent()/g' \
 && echo "$(date -u +%Y%m%d)-${PIPED_BACKEND_SHA%"${PIPED_BACKEND_SHA#???????}"}-npe${NPE_VERSION#v}" > VERSION
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
