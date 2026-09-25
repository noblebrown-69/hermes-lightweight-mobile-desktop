# Shoin Phone — changes

## Public build defaults
- No machine-specific defaults in code: `SHOIN_PHONE_ROOT` defaults to `~/Docs`, `SHOIN_PHONE_BIND` to `127.0.0.1`. Set real values in the systemd unit (`deploy/shoin-phone.service.example`).
- Token auth stays **on** by default.

## PWA install
- Serves `/manifest.webmanifest` (name/short_name Shoin Phone/Shoin, `start_url` `/`, `display` standalone, theme/background `#1a1714`).
- Serves `/icon-192.png`, `/icon-512.png`, `/apple-touch-icon.png` from files beside `shoin_phone.py`; falls back to a tiny stdlib solid-color PNG if missing.
- Manifest and icon routes do **not** require auth.
- `INDEX_HTML` links manifest, icons, apple-touch-icon, and `theme-color`.

## Conflict guard
- `GET /api/file` returns `mtime` and `sha256`.
- `PUT /api/file` compares client `mtime` to disk; mismatch → `409` with current `mtime`/`sha256`/`content`.
- Client may send `force: true` to overwrite anyway.
- Browser shows Reload (discard edits) vs Overwrite anyway; updates stored `mtime`/`sha256` after a successful save.

## Healthcheck
- `healthcheck.sh`: env `SHOIN_PHONE_BASE_URL` (default `http://127.0.0.1:$SHOIN_PHONE_PORT` or 9123), optional `SHOIN_PHONE_TOKEN` for `/api/health`.
- Checks `/`, manifest JSON, icon-192; exits non-zero on failure.

## Optional base path (PWA path prefix)
- Env `SHOIN_PHONE_BASE_PATH` (e.g. `/shoin`): normalize leading slash, no trailing slash; empty = legacy root behavior.
- Server accepts both prefixed (`/shoin/...`) and unprefixed paths. With BASE set, GET `/` and GET `/shoin` (no slash) 302 to `/shoin/` (query preserved).
- HTML/JS: `window.__BASE__` plus server-side `@@BASE@@` substitution for head links and all `/api/...` fetch URLs. Manifest `start_url`/`scope`/`id`/icon `src` prefixed (empty BASE → explicit `/`).
- `healthcheck.sh`: `SHOIN_PHONE_BASE_URL` may include the path prefix (no double slashes).

## Optional no-auth
- `SHOIN_PHONE_AUTH=off` skips the bearer token (use only on a private tailnet with ACLs you trust). Default stays on.

## Unchanged
- Existing API routes and env var names.
- Runtime remains stdlib-only (Pillow used only offline to generate icons).
