#!/usr/bin/env bash
# ZeroClaw regression suite — run on omv (10.10.1.13) as root after an image
# bump, a patch change or a config change.
#
#   zcsuite.sh <label> [instance] [test-id ...]
#
# What it does:
#   1. starts zc-proxy (zcproxy.py, a throwaway logging proxy in front of
#      Ollama) on the openwebui network;
#   2. copies the instance's live config + workspace to /tmp/zct inside the
#      container, pointed at the proxy (memory/sessions stay isolated there);
#   3. runs each read-only prompt below as a single-shot `zeroclaw agent`
#      (single-shot runs auto-deny anything needing approval, so nothing is
#      ever written), and grades it on the tools called and the reply;
#   4. aborts if Home Assistant's pinned gemma4:e4b leaves the GPU.
# Results: /root/zctest/runs/<label>-<id>/ (proxied requests, stdout, stderr)
# plus a summary table at the end.
#
# Requirements: zcproxy.py + zcrun.sh next to this script; HA's e4b and
# gemma4:12b-32k must fit together on the RTX 5000 (whisper on the T4).
set -euo pipefail

LABEL="$1"
INSTANCE="${2:-zeroclaw-michal}"
shift $(( $# >= 2 ? 2 : 1 ))
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK=/root/zctest
mkdir -p "$WORK/log" "$WORK/runs"
chmod 777 "$WORK/log"

# id % prompt % tool regex the turn must call % reply regex (case-insensitive)
TESTS=$(cat <<'EOF'
a-joke-1%Tell me a joke%^$%.
b-joke-2%Tell me a different joke%^$%.
c-ha-temp%What is the current temperature in the living room?%ha_search%[0-9]+([.,][0-9]+)? ?°?C
d-nc-cal%What is on my calendar in the next 7 days?%nc_calendar%.
e-grocy%What is on my Grocy shopping list, and is anything in stock expiring in the next 5 days?%grocy__get_(shopping_list|stock|stock_volatile)%.
f-firefly%How much did I spend on groceries in September 2026, and what were the three biggest grocery transactions?%firefly__%£|GBP|[0-9]
g-grafana%Using Grafana/Loki, were there any error-level log lines from the zeroclaw-michal container in the last 24 hours? Summarise briefly.%grafana__query_loki%.
h-web%Search the web: what is the latest released version of Ollama?%web_search%[0-9]+\.[0-9]+
i-mem-store%Please remember this for the future: my preferred coffee is a flat white.%memory_store%.
j-mem-recall%What kind of coffee do I prefer?%^$%flat white
k-multi%Is anyone home right now according to Home Assistant, and what is the outside temperature?%homeassistant__%.
EOF
)

# guard: abort the suite (after re-pinning) if HA's model is no longer loaded.
# Retries because `docker exec ollama ollama ps` occasionally fails on its own.
guard() {
  local i
  for i in 1 2 3; do
    docker exec ollama ollama ps 2>/dev/null | grep -q '^gemma4:e4b ' && return 0
    sleep 3
  done
  echo "!! gemma4:e4b not loaded after $1 — re-pinning and aborting"
  curl -s -m 300 http://127.0.0.1:11434/api/generate \
    -d '{"model":"gemma4:e4b","keep_alive":-1,"options":{"num_ctx":16384}}' >/dev/null
  exit 2
}

# start_proxy: (re)start the logging proxy container
start_proxy() {
  docker rm -f zc-proxy >/dev/null 2>&1 || true
  docker run -d --rm --name zc-proxy --network openwebui --memory 256m \
    -v "$HERE/zcproxy.py:/p.py:ro" -v "$WORK/log:/log" \
    python:3.12-alpine python -u /p.py >/dev/null
}

# make_test_config: copy the live config/workspace into /tmp/zct, via the proxy
make_test_config() {
  docker exec "$INSTANCE" sh -c '
    rm -rf /tmp/zct && mkdir -p /tmp/zct &&
    cp -a /zeroclaw-data/.zeroclaw/config.toml /zeroclaw-data/.zeroclaw/agents /tmp/zct/ &&
    { [ ! -e /zeroclaw-data/.zeroclaw/.secret_key ] || cp -a /zeroclaw-data/.zeroclaw/.secret_key /tmp/zct/; } &&
    sed -i "s#^uri = \"http://ollama:11434\"#uri = \"http://zc-proxy:8080\"#" /tmp/zct/config.toml &&
    grep -q "zc-proxy" /tmp/zct/config.toml'
}

# grade: PASS/FAIL one run from its proxied requests and its reply
grade() {
  local out="$1" tool_re="$2" reply_re="$3" calls reply
  calls=$(cat "$out"/req-*.json 2>/dev/null | grep -oE '"name": ?"[A-Za-z0-9_-]+"' | sed -E 's/.*"([^"]+)"$/\1/' | sort -u | tr '\n' ' ' || true)
  reply=$(cat "$out/stdout.txt")
  if [ "$tool_re" != '^$' ] && ! grep -qE "$tool_re" <<<"$calls"; then
    echo "FAIL(tool)"; return
  fi
  if ! grep -qiE "$reply_re" <<<"$reply"; then
    echo "FAIL(reply)"; return
  fi
  # A reply that gives up still "matches" a loose pattern, so catch those too.
  if grep -qiE 'maximum tool iterations|having trouble|unable to (find|retrieve|access|connect|get)|I (was|am) unable|could ?n.t (find|retrieve|access|get)|isError' <<<"$reply"; then
    echo "FAIL(gave-up)"; return
  fi
  echo "PASS"
}

start_proxy
make_test_config
guard start
summary=""
while IFS='%' read -r id prompt tool_re reply_re; do
  [ -z "$id" ] && continue
  if [ $# -gt 0 ] && ! printf '%s\n' "$@" | grep -qx "$id"; then continue; fi
  "$HERE/zcrun.sh" "$LABEL-$id" "$prompt" "$INSTANCE" | tee "$WORK/runs/$LABEL-$id.txt"
  verdict=$(grade "$WORK/runs/$LABEL-$id" "$tool_re" "$reply_re")
  wall=$(sed -nE 's/.*wall=([0-9.]+)s.*/\1/p' "$WORK/runs/$LABEL-$id.txt" | head -1)
  summary+="$(printf '%-14s %-12s %7ss' "$id" "$verdict" "$wall")"$'\n'
  guard "$id"
done <<<"$TESTS"
docker rm -f zc-proxy >/dev/null 2>&1 || true
echo "=== $LABEL summary ($INSTANCE)"
printf '%s' "$summary"
