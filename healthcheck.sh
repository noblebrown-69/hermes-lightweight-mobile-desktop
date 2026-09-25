#!/usr/bin/env bash
# Hermes Phone healthcheck — no hardcoded IPs/tokens/paths.
set -euo pipefail

# BASE_URL may include a path prefix (e.g. https://host/hermes). Trailing slash stripped once.
BASE_URL="${HERMES_PHONE_BASE_URL:-http://127.0.0.1:${HERMES_PHONE_PORT:-9124}}"
BASE_URL="${BASE_URL%/}"
TOKEN="${HERMES_PHONE_TOKEN:-}"
fail=0
CHAT_PROFILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --chat) CHAT_PROFILE="${2:-}"; shift 2 ;;
    *) echo "usage: $0 [--chat PROFILE]"; exit 2 ;;
  esac
done

ok() { echo "OK  $*"; }
bad() { echo "FAIL $*"; fail=1; }

auth_hdr=()
if [[ -n "$TOKEN" ]]; then
  auth_hdr=(-H "Authorization: Bearer $TOKEN")
fi

code=$(curl -s -o /tmp/hermes_hc_health.body -w "%{http_code}" "$BASE_URL/api/health" || echo "000")
if [[ "$code" == "200" ]]; then ok "GET /api/health -> 200"; else bad "GET /api/health -> $code (want 200)"; fi

code=$(curl -s -o /tmp/hermes_hc_manifest.body -w "%{http_code}" "$BASE_URL/manifest.webmanifest" || echo "000")
if [[ "$code" == "200" ]] && python3 -c "import json; json.load(open('/tmp/hermes_hc_manifest.body'))" 2>/dev/null; then
  ok "GET /manifest.webmanifest -> 200 JSON"
else
  bad "GET /manifest.webmanifest -> $code / not JSON"
fi

code=$(curl -s -o /tmp/hermes_hc_icon.body -w "%{http_code}" "$BASE_URL/icon-192.png" || echo "000")
if [[ "$code" == "200" ]]; then ok "GET /icon-192.png -> 200"; else bad "GET /icon-192.png -> $code"; fi

if [[ -n "$CHAT_PROFILE" ]]; then
  payload=$(python3 -c "import json; print(json.dumps({
    'profile': '''$CHAT_PROFILE''',
    'session_id': 'smoke_20260101_000000_abc123',
    'messages': [{'role':'user','content':'ping'}],
  }))")

  code=$(curl -s -o /tmp/hermes_hc_chat.body -w "%{http_code}" \
    "${auth_hdr[@]}" -H "Content-Type: application/json" \
    -d "$payload" "$BASE_URL/api/chat" || echo "000")
  if [[ "$code" == "200" ]] && python3 -c "
import json
d=json.load(open('/tmp/hermes_hc_chat.body'))
t=(d.get('text') or d.get('content') or '')
assert t.strip(), d
print(t[:80])
" 2>/tmp/hermes_hc_chat.err; then
    ok "POST /api/chat -> 200 text"
  else
    bad "POST /api/chat -> $code / no text ($(cat /tmp/hermes_hc_chat.err 2>/dev/null | tr '\n' ' '))"
  fi

  code=$(curl -s -o /tmp/hermes_hc_stream.body -w "%{http_code}" \
    "${auth_hdr[@]}" -H "Content-Type: application/json" \
    -d "$payload" "$BASE_URL/api/chat/stream" || echo "000")
  if [[ "$code" == "200" ]] && python3 -c "
import json,sys
raw=open('/tmp/hermes_hc_stream.body').read()
parts=[]
for line in raw.splitlines():
    if not line.startswith('data:'): continue
    data=line[5:].strip()
    if not data or data=='[DONE]': continue
    try: obj=json.loads(data)
    except Exception: continue
    ch=obj.get('choices') or []
    if ch:
      d=(ch[0].get('delta') or {})
      if d.get('content'): parts.append(str(d['content']))
text=''.join(parts)
assert text.strip(), repr(raw[:200])
print(text[:80])
" 2>/tmp/hermes_hc_stream.err; then
    ok "POST /api/chat/stream -> 200 SSE text"
  else
    bad "POST /api/chat/stream -> $code / no text ($(cat /tmp/hermes_hc_stream.err 2>/dev/null | tr '\n' ' '))"
  fi
fi

exit "$fail"
