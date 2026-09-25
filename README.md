# Hermes Lightweight Mobile Desktop

A thin, mobile-first web UI for [Hermes](https://github.com/NousResearch/hermes-agent) — the side panel of bots you know from Desktop, ready for a phone on Tailscale (or localhost).

Tap a bot. Chat. Switch bots without losing the gym-thread. No Desktop session reuse, no context bloat.


## Why this exists

Hermes Desktop is excellent on a workstation. On a phone it is not. The classic web dashboard works, but it is cluttered for the common case: *talk to one specialist while you are away from the desk*.

This project is that common case only:

- Horizontal bot tiles (keep **3–4** visible for space — soft recommendation, not a hard lock)
- **Bots** menu to toggle which profiles appear — **every** discovered profile is toggleable
- Sticky per-bot phone sessions (switch away and back; the transcript continues)
- **New chat** rotates the session id for the *active* bot only
- Same `~/.hermes/profiles` tools, memory, and skills via the gateway
- Separate phone session lane from Desktop (fresh `X-Hermes-Session-Id`, phone-scoped `X-Hermes-Session-Key`)
- Streaming replies (SSE) with automatic fallback to a plain request
- Installable as a phone app (PWA manifest + icons), optionally under a path prefix like `/hermes`
- stdlib Python only (`ThreadingHTTPServer`) — no Node, no framework

## What's in this repo

| Path | What |
|------|------|
| `hermes_phone.py` | Hermes Phone — the bot side panel + chat (this README) |
| [`shoin-phone/`](shoin-phone/README.md) | Shoin Phone — edit a folder of Markdown/HTML docs from your phone, with a conflict guard |
| [`open-webui-pwa/`](open-webui-pwa/README.md) | Install Open WebUI as its own renamed/re-iconed phone app via Tailscale Serve overlays (no Open WebUI edits) |
| [`docs/tailscale-https-pwa.md`](docs/tailscale-https-pwa.md) | How to install all of these as Android home-screen apps over Tailscale HTTPS |

Also in the family, in its own repo: **[Oda Fit](https://github.com/noblebrown-69/oda-fit)** — a tiny C workout logger for a Hermes fitness ledger, same PWA/path-prefix setup (`/fit`).

## Requirements

- Python 3.10+
- A running Hermes gateway with OpenAI-compatible multiplex routes
- Gateway API key (`API_SERVER_KEY` in Hermes `.env`, or `HERMES_PHONE_API_KEY`)

## Quick start

```bash
git clone https://github.com/noblebrown-69/hermes-lightweight-mobile-desktop.git
cd hermes-lightweight-mobile-desktop

# optional display-name / default_visible overlays
cp profiles.example.json profiles.json   # edit; gitignored

export HERMES_PHONE_BIND=127.0.0.1
export HERMES_PHONE_PORT=9124
export HERMES_PHONE_GATEWAY=http://127.0.0.1:8642
python3 hermes_phone.py
```

Open `http://127.0.0.1:9124/`.

With default auth (`HERMES_PHONE_AUTH=token`), the first visit needs `?token=…` (token is auto-minted under `~/.config/hermes-phone/token`). After that a cookie keeps you signed in. On a Tailscale-only bind you can set `HERMES_PHONE_AUTH=off` for a clean bookmark URL.

### Tailscale (typical phone setup)

1. Bind to your machine's Tailscale IPv4 (not `0.0.0.0` on the public internet).
2. Optional: `HERMES_PHONE_AUTH=off` if the Tailscale ACL is enough.
3. Bookmark `http://<tailscale-ip>:9124/` on the phone.

See `deploy/hermes-phone.service.example` for a systemd user unit template (and `deploy/hermes-phone.launcher.example` for the `~/.local/bin/hermes-phone` wrapper it runs).

### Install as a phone app (HTTPS PWA)

Android only installs a real app over HTTPS. Tailscale Serve gives you a tailnet-only certificate:

```bash
export HERMES_PHONE_BIND=127.0.0.1
export HERMES_PHONE_BASE_PATH=/hermes
python3 hermes_phone.py &
tailscale serve --bg --set-path=/hermes http://127.0.0.1:9124/hermes
tailscale serve --bg http://127.0.0.1:9124     # optional: bare host redirects to /hermes/
```

Open `https://your-machine.your-tailnet.ts.net/hermes/` in Chrome on the phone → **Install**. Use one hostname with a path prefix per app (`/hermes`, `/shoin`, `/fit`), not one port per app — Android WebAPK scopes ignore ports, so a root-scoped app on :443 swallows apps on other ports of the same host. The full walkthrough (enabling HTTPS/Serve, operator, root redirect, a second node for Open WebUI, icon safe zone) is in **[docs/tailscale-https-pwa.md](docs/tailscale-https-pwa.md)**.

### Healthcheck

```bash
./healthcheck.sh                                   # http://127.0.0.1:9124
HERMES_PHONE_BASE_URL=https://your-machine.your-tailnet.ts.net/hermes ./healthcheck.sh
HERMES_PHONE_TOKEN=... ./healthcheck.sh --chat assistant   # plus one normal + one streaming chat
```

Checks `/api/health`, the manifest JSON, and `icon-192.png`; exits non-zero on failure.

## How chat works

| Concern | Behavior |
|--------|----------|
| Catalog | Discovers folders under `~/.hermes/profiles` (names from `SOUL.md`) |
| Visibility | Client `localStorage` key `hermes-phone.visible` — Bots menu |
| Soft tip | “3–4 visible works best on phone” (you can still enable more) |
| Session | Sticky `X-Hermes-Session-Id` per profile in the browser |
| Memory channel | `X-Hermes-Session-Key: hermes-phone:<profile>` |
| Gateway | `POST /p/<profile>/v1/chat/completions` with Bearer API key **on the server only** |
| Desktop | Never reuse Desktop session ids |

Optional overlays (`profiles.json` or `HERMES_PHONE_PROFILES` JSON) can rename bots or set `default_visible`. They do **not** hide bots from the Bots menu.

Set `HERMES_PHONE_CATALOG=file` to use a file/env list as the *only* catalog (old allowlist mode) if you prefer not to auto-discover.

Set `HERMES_PHONE_DEFAULT_VISIBLE=id1,id2` to mark those ids `default_visible` when discovering (first visit before the user customizes the Bots menu).

## Surviving Hermes Desktop updates

This UI speaks the **gateway OpenAI-compat contract**, not Desktop IPC:

- Health: `GET` your gateway `/health` (or equivalent)
- Chat: `POST /p/<profile>/v1/chat/completions`
- Headers: `Authorization: Bearer <API_SERVER_KEY>`, `X-Hermes-Session-Id`, optional `X-Hermes-Session-Key`

After a Hermes Desktop / agent upgrade:

1. Confirm the gateway still listens and `/health` is ok.
2. Send one short phone message to a profile.
3. If multiplex paths or session header names change upstream, update this proxy — do not fork Desktop's live session key into the phone UI.

Keep phone sessions in their own lane so Desktop context length stays healthy.

## Configuration (env)

| Variable | Default | Notes |
|----------|---------|--------|
| `HERMES_PHONE_BIND` | `127.0.0.1` | Use your Tailscale IP on a workstation |
| `HERMES_PHONE_PORT` | `9124` | |
| `HERMES_PHONE_GATEWAY` | `http://127.0.0.1:8642` | |
| `HERMES_PHONE_AUTH` | `token` | `off` / `none` / `open` disables the shared token |
| `HERMES_PHONE_TOKEN_FILE` | `~/.config/hermes-phone/token` | |
| `HERMES_PHONE_HERMES_ENV` | `~/.hermes/.env` | Reads `API_SERVER_KEY` |
| `HERMES_PHONE_API_KEY` / `_FILE` | — | Override gateway key |
| `HERMES_PHONE_PROFILES_DIR` | `~/.hermes/profiles` | Discovery root |
| `HERMES_PHONE_PROFILES_FILE` | `./profiles.json` | Optional overlays |
| `HERMES_PHONE_PROFILES` | — | JSON overlays/list via env |
| `HERMES_PHONE_DEFAULT_VISIBLE` | _(empty)_ | Comma-separated ids |
| `HERMES_PHONE_CATALOG` | `discover` | `file` = allowlist-only |
| `HERMES_PHONE_BASE_PATH` | _(empty)_ | URL prefix, e.g. `/hermes`, for PWA install under one HTTPS host |

## API (phone server)

- `GET /` — UI
- `GET /api/health`
- `GET /api/bots` — full catalog (reload on each request)
- `POST /api/new-session` — mint session id
- `POST /api/chat` — `{profile, session_id, messages}` → proxies to gateway
- `POST /api/chat/stream` — same body; relays the gateway's OpenAI SSE stream
- `GET /manifest.webmanifest`, `/icon-192.png`, `/icon-512.png`, `/apple-touch-icon.png` — PWA (no auth)

With `HERMES_PHONE_BASE_PATH=/hermes`, every route is also served under `/hermes/…`, and `GET /` redirects to `/hermes/`.

## Security

- Prefer Tailscale (or localhost) binds; do not expose this on the public internet without a reverse proxy and strong auth.
- The gateway API key never ships in HTML/JS.
- Phone token auth is an extra lock for other devices on the same private network.

## License

MIT — see [LICENSE](LICENSE).

## Credits

Built for the Hermes open-source agent ecosystem. Not affiliated with Nous Research beyond using the public Hermes gateway APIs.
