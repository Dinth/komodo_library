# Recon toolbox: the ProjectDiscovery CLIs in one image, for external
# attack-surface reconnaissance (discovery and fingerprinting, no exploitation).
# The container idles; tools are run with `docker exec recon <tool> ...`.
#
# Built from the GitHub release binaries rather than the projectdiscovery/*
# Docker Hub images because Hub lags for several tools (2026-11: dnsx 1.2.2 vs
# 1.3.1, tlsx 1.2.2 vs 1.4.0, alterx 0.0.6 vs 0.1.0).
#
# To bump a tool: change its version AND its sha256 below (the linux_amd64.zip
# line of the release's checksums file), then bump the -pN image tag in
# compose.yaml so Komodo rebuilds.
#
# BASE_IMAGE is pinned in compose.yaml (x-versions) and passed as a build arg,
# which is the source of truth; the default below only covers a bare `docker
# build` and should be kept in sync.
ARG BASE_IMAGE="debian:13.7-slim"

FROM ${BASE_IMAGE} AS fetch
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /dl
RUN set -eu; \
    fetch() { \
      f="${1}_${2}_linux_amd64.zip"; \
      curl -fsSL -o "$f" "https://github.com/projectdiscovery/${1}/releases/download/v${2}/${f}"; \
      echo "${3}  ${f}" | sha256sum -c -; \
      unzip -q -o "$f" "$1" -d /out; \
      chmod 0755 "/out/${1}"; \
    }; \
    fetch nuclei    3.11.1 ea63d4ae232808cd7c6bc00d0142428e231fab59dae01042246097d195835ab6; \
    fetch subfinder 2.17.0 a39c3e5c7a26ef3c430915c4d6cbf7716a00f26769a75e9405aba796142eba37; \
    fetch naabu     2.7.0  0cc77c0ca32d2f519ffbaa7665bb0e7c88c40753ab22f03551d453a1a823e9f0; \
    fetch httpx     1.12.0 9d8439e8b6c9aa7d1e2314817a392e00d5178da3af5652f7475f88868f418f76; \
    fetch dnsx      1.3.1  438b964653056dd51dcfe614b1a16f8bced3cc48a1d27bc07cc6fdf2ef2a9533; \
    fetch tlsx      1.4.0  05628cec246e292e2f9afc6e7bc5de95aaa0fbe78fd556d5ad220b9f4702d364; \
    fetch katana    1.8.0  cb027ae99d0757061b70c07a15c7a80f62c5a535ab52c4a329f80c4b0a7309d2; \
    fetch alterx    0.1.0  ffae5b25138cb27b58900b9b5849e02d0390385fce404252d32e469f2a0e97ac

FROM ${BASE_IMAGE}
# libpcap: naabu links against it. dnsutils/whois: dig (AXFR attempts) and
# registration lookups. jq: the tools all emit JSONL.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl dnsutils jq libpcap0.8t64 whois \
    && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /out/ /usr/local/bin/
COPY nuclei-recon.yaml /etc/recon/nuclei-recon.yaml
# `nuclei-recon` = nuclei with the non-intrusive profile forced on. Plain
# `nuclei` stays available and unrestricted.
RUN printf '#!/bin/sh\nexec nuclei -config /etc/recon/nuclei-recon.yaml "$@"\n' \
      > /usr/local/bin/nuclei-recon \
    && chmod 0755 /usr/local/bin/nuclei-recon

# Tool state (nuclei templates, subfinder API keys, results) lives under HOME,
# which compose bind-mounts.
ENV HOME=/recon
WORKDIR /recon
USER 65534:65534
CMD ["sleep", "infinity"]
