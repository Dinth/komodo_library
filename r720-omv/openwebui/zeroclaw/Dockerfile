# syntax=docker/dockerfile:1.7
#
# ZeroClaw (upstream Dockerfile.debian variant) built from the upstream release
# tag plus the local patches in patches/, with the channel-signal feature.
#
# Why a wrapper: upstream Dockerfile.debian uses the upstream repo itself as the
# build context, so there is no hook to patch sources before cargo runs. This
# file receives the upstream tag as the named build context `upstream` (see
# additional_contexts in ../compose.yaml), applies patches/*.patch in the `src`
# stage, mirrors upstream's .dockerignore on that tree, and then follows
# upstream Dockerfile.debian stage for stage, copying from `src` instead of the
# build context. Cache mount ids match upstream, so rebuilds reuse the cargo
# registry and target caches.
#
# Patches:
#   0001  upstream PR #10845 (merged to master 2026-09-14, not in v0.8.5): the
#         deferred MCP index lists one <=200-char summary line per tool instead
#         of its full description. Without it the "## Deferred Tools" section
#         was ~229k chars (~58k tokens) for our 312 MCP tools, so local models
#         only ever saw a truncated fragment of the prompt. tool_search still
#         searches and returns the full descriptions. Drop this patch once a
#         release contains it (v0.8.6+).
#
# Bump procedure: change the tag in compose additional_contexts AND the image
# tag together, re-diff upstream Dockerfile.debian for stage changes, and check
# every patch still applies (git apply --check).

# >>> copied from upstream Dockerfile.debian (v0.8.5) <<<
ARG ZEROCLAW_BASE_NODE=node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e
ARG ZEROCLAW_BASE_RUST_BOOKWORM=rust:1.98-bookworm@sha256:82150a52ec202c1b14d7817e14516c392bb7f5cfebd88f1ed531cb37ebd39922

# ── Stage S: patched upstream source ────────────────────────────
FROM ${ZEROCLAW_BASE_RUST_BOOKWORM} AS src
COPY --from=upstream . /src
COPY patches/ /patches/
WORKDIR /src
RUN set -eux; \
    for p in /patches/*.patch; do git apply --verbose "$p"; done
# Mirror upstream .dockerignore. Its patterns are root-relative, so only
# top-level files and directories are removed, exactly as a context copy would.
RUN set -eux; \
    rm -rf .git .gitignore .githooks target web/node_modules web/dist docs \
           examples .github deny.toml LICENSE coverage lcov.info .idea .vscode \
           .windsurf scripts; \
    rm -f ./*.md ./*.png ./*.svg ./*.jpg ./*.jpeg ./*.gif ./*.db ./*.db-journal \
          ./*.tmp ./*.bak ./*.swp ./.env ./*.profraw ./*.profdata; \
    find apps -mindepth 1 -maxdepth 1 ! -name tauri ! -name zerocode ! -name zerorelay -exec rm -rf {} +; \
    find apps/tauri -mindepth 1 -maxdepth 1 ! -name Cargo.toml -exec rm -rf {} +
# Manifest-only tree for the dependency pre-fetch layer. Equivalent to
# upstream's `COPY --parents crates/*/Cargo.toml` globs without the labs syntax.
RUN set -eux; \
    mkdir -p /manifests; \
    cp --parents Cargo.toml Cargo.lock apps/tauri/Cargo.toml apps/zerocode/Cargo.toml \
       apps/zerorelay/Cargo.toml tools/fill-translations/Cargo.toml xtask/Cargo.toml /manifests/; \
    cp --parents crates/*/Cargo.toml /manifests/; \
    for f in crates/*/tests/fixtures/*/Cargo.toml; do \
      if [ -f "$f" ]; then cp --parents "$f" /manifests/; fi; \
    done; \
    cp -r wit /manifests/wit

# ── Stage 0: Frontend build ─────────────────────────────────────
FROM ${ZEROCLAW_BASE_NODE} AS web-node

FROM ${ZEROCLAW_BASE_RUST_BOOKWORM} AS web-builder
WORKDIR /app
COPY --from=web-node /usr/local/bin/node /usr/local/bin/node
COPY --from=web-node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y \
        g++ \
        pkg-config \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && rm -rf /var/lib/apt/lists/*
COPY --from=src /src/web/package.json /src/web/package-lock.json web/
RUN cd web && npm ci --ignore-scripts
COPY --from=src /src/ .
RUN mkdir -p apps/tauri/src \
    && echo "fn main() {}" > apps/tauri/src/main.rs \
    && echo "fn main() {}" > apps/tauri/build.rs
RUN --mount=type=cache,id=zeroclaw-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=zeroclaw-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=zeroclaw-web-target,target=/app/target,sharing=locked \
    cargo web build

# ── Stage 1: Build (match runtime glibc baseline) ───────────────
FROM ${ZEROCLAW_BASE_RUST_BOOKWORM} AS builder

WORKDIR /app
# Upstream dist feature set plus channel-signal (compose passes the same string).
ARG ZEROCLAW_CARGO_FLAGS="--no-default-features --features acp-bridge,agent-runtime,channel-acp-server,channel-discord,channel-email,channel-filesystem,channel-git,channel-lark,channel-matrix,channel-telegram,channel-webhook,gateway,observability-prometheus,schema-export,whatsapp-web,channel-signal"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y \
        pkg-config \
        g++ \
    && rm -rf /var/lib/apt/lists/*

# 1. Copy manifests (+ wit/) to cache dependencies
COPY --from=src /manifests/ ./
RUN mkdir -p src src/bin benches apps/tauri/src apps/zerocode/src apps/zerorelay/src tools/fill-translations/src xtask/src/bin \
    && echo "fn main() {}" > src/main.rs \
    && echo "" > src/lib.rs \
    && echo "fn main() {}" > src/bin/zeroclaw-acp-bridge.rs \
    && echo "fn main() {}" > benches/agent_benchmarks.rs \
    && echo "fn main() {}" > apps/tauri/src/main.rs \
    && echo "fn main() {}" > apps/tauri/build.rs \
    && echo "fn main() {}" > apps/zerocode/src/main.rs \
    && echo "fn main() {}" > apps/zerocode/build.rs \
    && echo "fn main() {}" > apps/zerorelay/src/main.rs \
    && echo "" > apps/zerorelay/src/lib.rs \
    && echo "fn main() {}" > tools/fill-translations/src/main.rs \
    && echo "" > xtask/src/lib.rs \
    && echo "fn main() {}" > xtask/src/bin/mdbook.rs \
    && echo "fn main() {}" > xtask/src/bin/fluent.rs \
    && echo "fn main() {}" > xtask/src/bin/web.rs \
    && mkdir -p crates/zeroclaw-hardware/examples \
    && echo "fn main() {}" > crates/zeroclaw-hardware/examples/esp32_sim.rs \
    && for d in crates/*/; do mkdir -p "${d}src" && printf '' > "${d}src/lib.rs"; done \
    && for d in crates/*/tests/fixtures/*/; do [ -f "${d}Cargo.toml" ] || continue; mkdir -p "${d}src" && printf '' > "${d}src/lib.rs"; done \
    && printf 'fn main() {}' > crates/zeroclaw-log/tests/fixtures/attribution-macro-consumer/src/main.rs
RUN --mount=type=cache,id=zeroclaw-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=zeroclaw-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=zeroclaw-target,target=/app/target,sharing=locked \
    if [ -n "$ZEROCLAW_CARGO_FLAGS" ]; then \
      cargo build --release --locked -p zeroclaw -p zerocode $ZEROCLAW_CARGO_FLAGS; \
    else \
      cargo build --release --locked -p zeroclaw -p zerocode; \
    fi
RUN rm -rf src benches crates xtask tools/fill-translations

# 2. Copy only build-relevant source paths from the patched tree
COPY --from=src /src/src/ src/
COPY --from=src /src/benches/ benches/
COPY --from=src /src/crates/ crates/
COPY --from=src /src/xtask/ xtask/
COPY --from=src /src/tools/fill-translations/ tools/fill-translations/
COPY --from=src /src/apps/zerocode/ apps/zerocode/
COPY --from=src /src/web/src/ web/src/
COPY --from=src /src/locales.toml .
COPY --from=web-builder /app/web/dist web/dist
RUN touch src/main.rs src/lib.rs apps/zerocode/src/main.rs
RUN --mount=type=cache,id=zeroclaw-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=zeroclaw-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=zeroclaw-target,target=/app/target,sharing=locked \
    rm -rf target/release/.fingerprint/zeroclaw-* \
           target/release/deps/zeroclaw-* \
           target/release/incremental/zeroclaw-* \
           target/release/deps/zeroclaw_* \
           target/release/incremental/zeroclaw_* \
           target/release/.fingerprint/xtask-* \
           target/release/deps/xtask-* \
           target/release/.fingerprint/fill-translations-* \
           target/release/deps/fill_translations-* \
           target/release/.fingerprint/zerocode-* \
           target/release/deps/zerocode-* \
           target/release/incremental/zerocode-* && \
    if [ -n "$ZEROCLAW_CARGO_FLAGS" ]; then \
      cargo build --release --locked -p zeroclaw -p zerocode $ZEROCLAW_CARGO_FLAGS; \
    else \
      cargo build --release --locked -p zeroclaw -p zerocode; \
    fi && \
    cp target/release/zeroclaw /app/zeroclaw && \
    cp target/release/zerocode /app/zerocode && \
    strip /app/zeroclaw /app/zerocode
RUN for b in zeroclaw zerocode; do \
      size=$(stat -c%s "/app/$b") && \
      if [ "$size" -lt 1000000 ]; then echo "ERROR: $b too small (${size} bytes), likely dummy build artifact" && exit 1; fi; \
    done

# Runtime directory structure and default config (upstream, unchanged).
RUN mkdir -p /zeroclaw-data/.zeroclaw /zeroclaw-data/data && \
    printf '%s\n' \
        'api_key = ""' \
        'default_provider = "openrouter"' \
        'default_model = "anthropic/claude-sonnet-4-20250514"' \
        'default_temperature = 0.7' \
        '' \
        '[gateway]' \
        'port = 42617' \
        'host = "[::]"' \
        'allow_public_bind = true' \
        'web_dist_dir = "/usr/share/zeroclawlabs/web/dist"' \
        '' \
        '[risk_profiles.default]' \
        'level = "supervised"' \
        'auto_approve = ["file_read", "file_write", "file_edit", "memory_recall", "memory_store", "web_search_tool", "web_fetch", "calculator", "glob_search", "content_search", "image_info", "weather", "git_operations"]' \
        > /zeroclaw-data/.zeroclaw/config.toml && \
    chown -R 65534:65534 /zeroclaw-data

# ── Stage 2: Runtime (Debian with shell) ────────────────────────
FROM debian:bookworm-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        vim-tiny \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/zeroclaw /usr/local/bin/zeroclaw
COPY --from=builder /app/zerocode /usr/local/bin/zerocode
COPY --from=builder /zeroclaw-data /zeroclaw-data
COPY --from=web-builder /app/web/dist /usr/share/zeroclawlabs/web/dist

LABEL org.opencontainers.image.description="ZeroClaw v0.8.5 debian + channel-signal + PR #10845 (local patch)"

ENV LANG=C.UTF-8
ENV ZEROCLAW_DATA_DIR=/zeroclaw-data/data
ENV HOME=/zeroclaw-data
ENV ZEROCLAW_GATEWAY_PORT=42617

WORKDIR /zeroclaw-data
USER 65534:65534
EXPOSE 42617
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=10s \
    CMD ["zeroclaw", "status", "--format=exit-code"]
ENTRYPOINT ["zeroclaw"]
CMD ["daemon"]
