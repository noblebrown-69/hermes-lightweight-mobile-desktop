# Open WebUI as its own PWA (rename + re-icon, no Open WebUI edits)

A tiny overlay that installs [Open WebUI](https://github.com/open-webui/open-webui) on Android as its own home-screen app with **your** name and icon (the example uses **Oda**), without touching the Open WebUI container or files.

How it works:

1. Open WebUI assumes it owns `/`, so it cannot share the path-prefixed host used by the other apps (see [`../docs/tailscale-https-pwa.md`](../docs/tailscale-https-pwa.md)). It gets its **own Tailscale node** — a second, userspace `tailscaled` on the same machine with its own hostname.
2. A throwaway `python3 -m http.server` on `127.0.0.1:9131` serves a replacement `manifest.json` and icons.
3. `tailscale serve` on the second node sends `/` to Open WebUI and overlays a handful of exact paths (`/manifest.json`, `/static/favicon.png`, …) to the static server. More specific Serve paths win, so the browser sees your manifest and icons; everything else is stock Open WebUI.

Upgrading Open WebUI does not undo this, because nothing inside it changed.

## Files

| File | Purpose |
|---|---|
| `manifest.json.example` | Replacement manifest (name, colors, icon paths). Rename to `manifest.json`. |
| `icon-192.png`, `icon-512.png` | Example Oda icons. Replace with your own full-bleed PNGs. |
| `deploy/owui-pwa-static.service.example` | systemd user unit for the static server on `127.0.0.1:9131` |
| `deploy/tailscaled-owui.service.example` | systemd user unit for the second userspace `tailscaled` node |

## 1. Static overlay directory

```bash
D=~/.local/share/owui-pwa
mkdir -p "$D/static"
cp manifest.json.example "$D/manifest.json"     # edit name/short_name/colors
cp icon-192.png icon-512.png "$D/"
for f in favicon logo apple-touch-icon splash; do cp icon-512.png "$D/static/$f.png"; done

mkdir -p ~/.config/systemd/user
cp deploy/owui-pwa-static.service.example ~/.config/systemd/user/owui-pwa-static.service
systemctl --user daemon-reload
systemctl --user enable --now owui-pwa-static.service
curl -s http://127.0.0.1:9131/manifest.json
```

The manifest's icon `src` values point at `/oda-pwa/icon-*.png`; the Serve route below maps `/oda-pwa/` onto the static server root. Change both together if you rename it.

## 2. Second Tailscale node (userspace)

```bash
cp deploy/tailscaled-owui.service.example ~/.config/systemd/user/tailscaled-owui.service
systemctl --user daemon-reload
systemctl --user enable --now tailscaled-owui.service

TS="tailscale --socket=$HOME/.local/share/ts-owui/tailscaled.sock"
$TS up --hostname=your-owui          # prints a login URL the first time
```

This `tailscaled` runs as your user, so no `--operator` step is needed. It has its own state dir and socket, so the host's main `tailscaled` and its Serve config are untouched. Userspace networking needs no root and no TUN device; Serve can still proxy to `127.0.0.1` on the host.

## 3. Serve config on the second node

Assuming Open WebUI listens on `127.0.0.1:8080` (use whatever port you published):

```bash
TS="tailscale --socket=$HOME/.local/share/ts-owui/tailscaled.sock"
$TS serve --bg http://127.0.0.1:8080
$TS serve --bg --set-path=/oda-pwa                     http://127.0.0.1:9131
$TS serve --bg --set-path=/manifest.json               http://127.0.0.1:9131/manifest.json
$TS serve --bg --set-path=/static/favicon.png          http://127.0.0.1:9131/static/favicon.png
$TS serve --bg --set-path=/static/logo.png             http://127.0.0.1:9131/static/logo.png
$TS serve --bg --set-path=/static/splash.png           http://127.0.0.1:9131/static/splash.png
$TS serve --bg --set-path=/static/apple-touch-icon.png http://127.0.0.1:9131/static/apple-touch-icon.png
$TS serve status
```

Expected:

```
https://your-owui.your-tailnet.ts.net (tailnet only)
|-- /                            proxy http://127.0.0.1:8080
|-- /oda-pwa                     proxy http://127.0.0.1:9131
|-- /manifest.json               proxy http://127.0.0.1:9131/manifest.json
|-- /static/logo.png             proxy http://127.0.0.1:9131/static/logo.png
|-- /static/splash.png           proxy http://127.0.0.1:9131/static/splash.png
|-- /static/favicon.png          proxy http://127.0.0.1:9131/static/favicon.png
|-- /static/apple-touch-icon.png proxy http://127.0.0.1:9131/static/apple-touch-icon.png
```

Then on the phone: Chrome → `https://your-owui.your-tailnet.ts.net/` → **Install**. The home-screen app is called whatever `short_name` says.

If Open WebUI starts serving its manifest or icons from different paths in a future release, check the page source for `<link rel="manifest">` / `rel="icon"` and add matching overlay routes.

## Icons

Android masks home-screen icons. The example manifest lists the 512 icon as both `any` and `maskable`; for `maskable`, keep the artwork inside the center circle (radius 40 % of the width). Details in [`../docs/tailscale-https-pwa.md`](../docs/tailscale-https-pwa.md#4-icons-the-circle-mask-safe-zone).
