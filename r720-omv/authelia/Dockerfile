# Patched Authelia image: the official image with only /app/authelia swapped
# for a binary rebuilt from the same tag plus one line.
#
# Authelia 4.39.21+ ships go-webauthn v0.18, which rejects any WebAuthn client
# extension output the server didn't request. Every iOS browser (WebKit) always
# returns `appid: false`, so YubiKey logins from iPhones fail with:
#   Client returned the "appid" extension output which was not requested
# The patch sets ExtensionsUnsolicitedOutputPolicy to Ignore, which restores
# the pre-v0.18 behaviour. Upstream: https://github.com/authelia/authelia/issues/13146
#
# Drop this image and go back to the plain pinned image once the fix ships
# (milestone v4.40.0).
#
# BASE_IMAGE is pinned in compose.yaml (x-versions) and passed as a build arg,
# which is the source of truth; the default below only covers a bare `docker
# build` and should be kept in sync. The source tag is derived from it, so a
# bump + redeploy (rebuild) re-applies the patch to the new version.
ARG BASE_IMAGE="authelia/authelia:4.39.28"

FROM golang:1.27.1-trixie AS build
ARG BASE_IMAGE
WORKDIR /src
# The build fails loudly if the anchor line is missing (e.g. a bumped version
# moved it), which is the signal to re-check the patch. The web frontend is
# taken prebuilt from the same release rather than rebuilt with pnpm.
RUN set -eu; \
    v="v${BASE_IMAGE##*:}"; \
    curl -fsSL "https://github.com/authelia/authelia/archive/refs/tags/${v}.tar.gz" \
      | tar xz --strip-components=1; \
    curl -fsSL "https://github.com/authelia/authelia/releases/download/${v}/authelia-${v}-public_html.tar.gz" \
      | tar xz -C internal/server; \
    f=internal/middlewares/authelia_context.go; \
    n=$(grep -cxF "$(printf '\t\tMDS: ctx.Providers.MetaDataService,')" "$f" || true); \
    [ "$n" = 1 ] || { echo "patch anchor count $n in $f" >&2; exit 1; }; \
    sed -i 's/^\t\tMDS: ctx.Providers.MetaDataService,$/&\n\t\tExtensionsUnsolicitedOutputPolicy: protocol.UnsolicitedOutputPolicyIgnore,/' "$f"; \
    grep -q 'UnsolicitedOutputPolicyIgnore' "$f"; \
    x='github.com/authelia/authelia/v4/internal/utils'; \
    CGO_ENABLED=1 go build -buildmode=pie -trimpath -o /out/authelia -ldflags "-linkmode=external -s -w \
      -X '${x}.BuildTag=${v}' -X '${x}.BuildBranch=${v}' -X '${x}.BuildState=tagged clean' \
      -X '${x}.BuildExtra=patched: webauthn unsolicited extension outputs ignored (authelia#13146)'" \
      ./cmd/authelia; \
    /out/authelia --version

FROM ${BASE_IMAGE}
COPY --from=build /out/authelia /app/authelia
