# Hermes Lightweight Mobile Desktop — changes

## 2026-09 — PWA, streaming, path prefix, companion apps

### PWA install
- Serves `/manifest.webmanifest` (Hermes Phone/Hermes, `start_url` `/`, standalone, theme/background `#0e1116`).
- Serves `/icon-192.png`, `/icon-512.png`, `/apple-touch-icon.png` from files beside `hermes_phone.py`; stdlib solid-color fallback if missing.
- Manifest and icon routes do **not** require auth (health already public).
- `INDEX_HTML` links manifest, icons, apple-touch-icon, and `theme-color`.

### Streaming replies
- New `POST /api/chat/stream`: proxies gateway with `stream=true`, relays OpenAI SSE (`text/event-stream`, flush per chunk) using `http.client` incremental read; handles `data: [DONE]`.
- Same gateway headers as `/api/chat`: `Authorization`, `X-Hermes-Session-Id`, `X-Hermes-Session-Key`.
- Browser uses `fetch` + `ReadableStream` to append into the assistant bubble live; on non-200 / network / no chunks falls back to the non-streaming `/api/chat`.
- Keeps 429 busy messaging. `/api/chat` unchanged.

### Optional base path (PWA path prefix)
- Env `HERMES_PHONE_BASE_PATH` (e.g. `/hermes`): normalize leading slash, no trailing slash; empty = legacy root behavior.
- Server accepts both prefixed (`/hermes/...`) and unprefixed paths (strip prefix when present). With BASE set, GET `/` and GET `/hermes` (no slash) 302 to `/hermes/` (query preserved).
- HTML/JS: `window.__BASE__` plus server-side `@@BASE@@` substitution for head links and all `/api/...` fetch URLs. Manifest `start_url`/`scope`/`id`/icon `src` prefixed (empty BASE → explicit `/`).

### Catalog
- Display names from `SOUL.md` drop a trailing ` — System Prompt` so tiles stay short.
- Defaults unchanged from the OSS release: `HERMES_PHONE_DEFAULT_VISIBLE` (empty) and a single generic `assistant` fallback profile.

### Healthcheck
- `healthcheck.sh`: env `HERMES_PHONE_BASE_URL` (default port 9124, may include the path prefix), optional `HERMES_PHONE_TOKEN`, optional `--chat PROFILE` for one non-stream + one stream chat check.

### Companion apps and docs
- `shoin-phone/`: phone editor for a Markdown/HTML docs folder (token auth on by default).
- `open-webui-pwa/`: rename/re-icon Open WebUI as its own PWA via Tailscale Serve overlays.
- `docs/tailscale-https-pwa.md`: installing all of these as Android home-screen apps over Tailscale HTTPS.

### Unchanged
- Bind/port/auth defaults, catalog discovery, env var names.
- Runtime stdlib-only.
