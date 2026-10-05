#!/usr/bin/env bash
# Purpose: run one ZeroClaw single-shot prompt against the isolated test config
#          (/tmp/zct inside the instance), time it, and summarise what the
#          logging proxy saw (per-request prompt tokens, latency, tool calls).
# Usage:   zcrun.sh <label> "<prompt>" [instance]
set -euo pipefail

LABEL="$1"
PROMPT="$2"
INSTANCE="${3:-zeroclaw-michal}"
OUT=/root/zctest/runs/$LABEL
LOGDIR=/root/zctest/log

# summarise: print one line per proxied request made during this run
summarise() {
  python3 - "$OUT" <<'EOF'
import json, sys, glob, os, re
out = sys.argv[1]
for f in sorted(glob.glob(out + "/req-*.json")):
    r = json.load(open(f))
    req = r.get("request") or {}
    msgs = req.get("messages") or []
    tools = req.get("tools") or []
    sys_len = sum(len(m.get("content") or "") for m in msgs if m.get("role") == "system")
    raw = r.get("response_raw", "")
    usage = re.findall(r'"prompt_tokens":(\d+),"completion_tokens":(\d+)', raw)
    calls = re.findall(r'"name":"([A-Za-z0-9_\-]+)"', raw)
    print(f"  req {r['n']:3d} {r['path']:<22} {r['status']} {r['seconds']:7.1f}s msgs={len(msgs):2d} "
          f"sys_chars={sys_len:6d} tools={len(tools):3d} usage={usage[-1] if usage else '-'} "
          f"calls={sorted(set(calls))[:6]}")
EOF
}

mkdir -p "$OUT"
before=$(ls "$LOGDIR" | wc -l)
start=$(date +%s.%N)
set +e
docker exec "$INSTANCE" timeout 900 zeroclaw --config-dir /tmp/zct agent -a assistant -m "$PROMPT" \
  >"$OUT/stdout.txt" 2>"$OUT/stderr.txt"
rc=$?
set -e
end=$(date +%s.%N)
# move this run's proxy records into the run folder
ls "$LOGDIR" | sort | tail -n +$((before + 1)) | while read -r f; do mv "$LOGDIR/$f" "$OUT/"; done
printf '== %s rc=%s wall=%.1fs\n' "$LABEL" "$rc" "$(python3 -c "print($end - $start)")"
summarise
echo "  --- reply:"
tail -c 1500 "$OUT/stdout.txt" | sed 's/^/  | /'
