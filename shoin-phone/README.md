# Shoin Phone

A thin phone editor for a folder of Markdown / HTML / text docs — the same tree a desktop Shoin (or any editor) opens. Built for a phone on Tailscale.

- Collapsible folder tree (open/closed state remembered per device)
- Edit `.md`, `.markdown`, `.txt` as text; `.html` / `.htm` in a rendered, editable view
- **Conflict guard**: saves send the file's `mtime`; if the file changed on disk since you opened it you get a `409` and a choice — Reload (discard your edits) or Overwrite anyway
- Atomic writes (temp file + rename); paths can't escape the docs root
- Installable PWA (manifest + icons), optional URL path prefix
- stdlib Python only (`ThreadingHTTPServer`)

## Quick start

```bash
cd shoin-phone
export SHOIN_PHONE_ROOT=~/Docs          # folder to edit
export SHOIN_PHONE_BIND=127.0.0.1       # or your Tailscale IPv4
export SHOIN_PHONE_PORT=9123
python3 shoin_phone.py
```

A token is auto-minted to `~/.config/shoin-phone/token` on first run. Open:

```
http://127.0.0.1:9123/?token=<token>
```

The token is saved in the phone browser (`localStorage`), so later visits and the installed PWA don't need it in the URL. Or paste it into the gate screen.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `SHOIN_PHONE_ROOT` | `~/Docs` | Docs folder (must exist) |
| `SHOIN_PHONE_BIND` | `127.0.0.1` | Listen address. Use `127.0.0.1` behind `tailscale serve`, or your Tailscale IPv4. Never `0.0.0.0` on an untrusted network. |
| `SHOIN_PHONE_PORT` | `9123` | Listen port |
| `SHOIN_PHONE_TOKEN_FILE` | `~/.config/shoin-phone/token` | Bearer token file (auto-created, `0600`) |
| `SHOIN_PHONE_AUTH` | `on` | **Token auth is on by default.** `off` skips it — opt in only if your Tailscale ACLs are the auth you want. |
| `SHOIN_PHONE_BASE_PATH` | _(empty)_ | URL prefix, e.g. `/shoin`, for PWA install under one HTTPS host |

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/` | no (page asks for token) | UI |
| GET | `/manifest.webmanifest`, `/icon-192.png`, `/icon-512.png`, `/apple-touch-icon.png` | no | PWA |
| GET | `/api/health` | token | `{ok, root, bind, port}` |
| GET | `/api/tree` | token | `{files: [{path, size, mtime}]}` |
| GET | `/api/file?path=` | token | `{path, mtime, sha256, content}` |
| PUT | `/api/file?path=` | token | `{content, mtime?, force?}` → `200` or `409 {mtime, sha256, content}` |

Token goes in `Authorization: Bearer <token>` (or `?token=`).

## Install as a phone app

Needs HTTPS. With Tailscale Serve and `SHOIN_PHONE_BASE_PATH=/shoin`:

```bash
tailscale serve --bg --set-path=/shoin http://127.0.0.1:9123/shoin
```

Then open `https://your-machine.your-tailnet.ts.net/shoin/?token=<token>` in Chrome on the phone and **Install**. Full recipe (why path prefixes, root redirect, icons): [`../docs/tailscale-https-pwa.md`](../docs/tailscale-https-pwa.md).

## systemd (user unit)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/shoin-phone.service.example ~/.config/systemd/user/shoin-phone.service
# edit ExecStart path, SHOIN_PHONE_ROOT, bind, prefix
systemctl --user daemon-reload
systemctl --user enable --now shoin-phone.service
```

## Healthcheck

```bash
./healthcheck.sh                                    # http://127.0.0.1:9123
SHOIN_PHONE_BASE_URL=https://your-machine.your-tailnet.ts.net/shoin \
SHOIN_PHONE_TOKEN="$(cat ~/.config/shoin-phone/token)" ./healthcheck.sh
```

Checks `/`, the manifest JSON, `icon-192.png`, and (with a token) `/api/health`. Exits non-zero on failure.

## Security

- Anyone with the token can read and overwrite every doc under the root. Treat it like a password.
- Keep the bind private (Tailscale or localhost). Don't `tailscale funnel` it.
