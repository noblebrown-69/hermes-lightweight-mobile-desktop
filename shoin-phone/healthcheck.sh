#!/usr/bin/env bash
# Shoin Phone healthcheck — no hardcoded IPs/tokens/paths.
set -euo pipefail

# BASE_URL may include a path prefix (e.g. https://host/shoin). Trailing slash stripped once.
BASE_URL="${SHOIN_PHONE_BASE_URL:-http://127.0.0.1:${SHOIN_PHONE_PORT:-9123}}"
BASE_URL="${BASE_URL%/}"
TOKEN="${SHOIN_PHONE_TOKEN:-}"
fail=0

ok() { echo "OK  $*"; }
bad() { echo "FAIL $*"; fail=1; }

code=$(curl -s -o /tmp/shoin_hc_root.body -w "%{http_code}" "$BASE_URL/" || echo "000")
if [[ "$code" == "200" ]]; then ok "GET / -> 200"; else bad "GET / -> $code (want 200)"; fi

code=$(curl -s -o /tmp/shoin_hc_manifest.body -w "%{http_code}" "$BASE_URL/manifest.webmanifest" || echo "000")
if [[ "$code" == "200" ]] && python3 -c "import json,sys; json.load(open('/tmp/shoin_hc_manifest.body'))" 2>/dev/null; then
  ok "GET /manifest.webmanifest -> 200 JSON"
else
  bad "GET /manifest.webmanifest -> $code / not JSON"
fi

code=$(curl -s -o /tmp/shoin_hc_icon.body -w "%{http_code}" "$BASE_URL/icon-192.png" || echo "000")
if [[ "$code" == "200" ]]; then ok "GET /icon-192.png -> 200"; else bad "GET /icon-192.png -> $code"; fi

if [[ -n "$TOKEN" ]]; then
  code=$(curl -s -o /tmp/shoin_hc_health.body -w "%{http_code}" \
    -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/health" || echo "000")
  if [[ "$code" == "200" ]]; then ok "GET /api/health -> 200"; else bad "GET /api/health -> $code"; fi
else
  ok "skip /api/health (set SHOIN_PHONE_TOKEN to check)"
fi

exit "$fail"
