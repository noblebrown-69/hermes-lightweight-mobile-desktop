# Installing the phone apps as Android home-screen PWAs over Tailscale HTTPS

Hermes Phone, Shoin Phone, and [Oda Fit](https://github.com/noblebrown-69/oda-fit) all ship a web app manifest and icons. Chrome on Android will only turn a site into a real installed app (a **WebAPK**: own icon, own task in the app switcher, no URL bar) when the page is served over **HTTPS** from a trusted certificate. Plain `http://100.x.y.z:9124/` gets you a bookmark, not an app.

Tailscale Serve gives every machine on your tailnet a real Let's Encrypt certificate for `your-machine.your-tailnet.ts.net`, reachable only from your tailnet. This doc is the recipe.

Everything below uses placeholders:

| Placeholder | Meaning |
|---|---|
| `your-machine.your-tailnet.ts.net` | MagicDNS name of the box running the apps |
| `your-owui.your-tailnet.ts.net` | a second node name, only for apps that cannot live under a path prefix |
| `100.x.y.z` | that machine's Tailscale IPv4 (`tailscale ip -4`) |

## 1. Enable HTTPS and Serve on the tailnet

1. Admin console → **DNS**: turn on **MagicDNS** and **HTTPS Certificates**.
2. The first `tailscale serve` on a node prints a link to enable Serve for the tailnet if it is not enabled yet. Follow it once.
3. Let your normal user run `tailscale serve` without `sudo`:

   ```bash
   sudo tailscale set --operator=$USER
   ```

The first HTTPS request to a new name can take a few seconds while the certificate is issued.

## 2. One hostname, one app per path prefix

**Why not one port per app?** Android WebAPK scopes effectively ignore the port. If you install an app whose scope is `https://your-machine.your-tailnet.ts.net/` (port 443), Chrome/Android treats every URL on that host as belonging to it — including `https://your-machine.your-tailnet.ts.net:8443/`. The root-scoped app swallows the others: tapping the second icon opens the first app, or the second install silently replaces the first.

So: one HTTPS host on 443, and each app gets its **own path prefix** and its own manifest `scope`:

| App | Local listener | Prefix env / flag | Installed scope |
|---|---|---|---|
| Hermes Phone | `127.0.0.1:9124` | `HERMES_PHONE_BASE_PATH=/hermes` | `/hermes/` |
| Shoin Phone | `127.0.0.1:9123` | `SHOIN_PHONE_BASE_PATH=/shoin` | `/shoin/` |
| Oda Fit | `127.0.0.1:9120` | `fitd --base /fit` or `FITD_BASE_PATH=/fit` | `/fit/` |

With a prefix set, each server:

- serves its manifest with `start_url`, `scope`, `id`, and icon `src` under the prefix;
- rewrites its own HTML links and `fetch()` URLs under the prefix;
- accepts both prefixed (`/hermes/api/bots`) and unprefixed (`/api/bots`) paths, so it keeps working if you hit the port directly;
- redirects `GET /` and `GET /hermes` (no trailing slash) to `/hermes/`.

Leave the prefix empty and everything behaves as before (served at `/`).

### Serve config

Bind the apps to `127.0.0.1` (Tailscale Serve proxies from the node itself), set the prefix env vars in each systemd unit (see the `deploy/*.example` files), then:

```bash
tailscale serve --bg --set-path=/hermes http://127.0.0.1:9124/hermes
tailscale serve --bg --set-path=/shoin  http://127.0.0.1:9123/shoin
tailscale serve --bg --set-path=/fit    http://127.0.0.1:9120/fit

# Root redirect: a bare https://your-machine.your-tailnet.ts.net/ lands somewhere useful.
# Hermes Phone answers GET / with a 302 to /hermes/ when HERMES_PHONE_BASE_PATH is set.
tailscale serve --bg http://127.0.0.1:9124
```

Serve strips the mount path and appends the rest to the target URL, which is why each target repeats its prefix (`…:9124/hermes`): the app sees the prefixed path it expects.

`tailscale serve status` should then look like:

```
https://your-machine.your-tailnet.ts.net (tailnet only)
|-- /       proxy http://127.0.0.1:9124
|-- /fit    proxy http://127.0.0.1:9120/fit
|-- /shoin  proxy http://127.0.0.1:9123/shoin
|-- /hermes proxy http://127.0.0.1:9124/hermes
```

Do **not** use `tailscale funnel` for these — that would publish them to the internet.

### Install on the phone

1. Phone on the tailnet (Tailscale app connected).
2. Chrome → `https://your-machine.your-tailnet.ts.net/hermes/` → menu → **Add to Home screen** → **Install**.
3. Repeat for `/shoin/` and `/fit/`. Each should show up as a separate app with its own icon.

If an old root-scoped install exists from before you added prefixes, uninstall it first (long-press icon → App info → Uninstall) or it will keep capturing the host.

Auth notes: the manifest and icon routes are public on purpose (Chrome fetches them without cookies). Hermes Phone keeps its cookie after the first `?token=…` visit. Shoin Phone keeps token auth on by default; open `/shoin/?token=…` once before installing so the token is stored. `HERMES_PHONE_AUTH=off` / `SHOIN_PHONE_AUTH=off` are opt-in for people who treat Tailscale ACLs as the auth layer.

## 3. Apps that cannot take a path prefix (Open WebUI)

Some apps assume they own `/` (Open WebUI is one: absolute asset paths, its own manifest at `/manifest.json`). Putting them under `/owui` on the shared host breaks them, and putting them at `/` breaks the rule above.

Give them their **own Tailscale node** — a second hostname, so a second origin and a second WebAPK scope. You do not need a second machine: run a second `tailscaled` in userspace-networking mode with its own state dir and socket. See [`../open-webui-pwa/`](../open-webui-pwa/README.md) for the example units and the full recipe, including how to rename/re-icon Open WebUI as its own PWA via Serve path overlays without editing Open WebUI.

## 4. Icons: the circle-mask safe zone

Android launchers mask icons (circle, squircle, teardrop — the launcher decides).

- `"purpose": "any"` icons are shrunk and placed on a white/colored plate. They never get cropped, but can look small.
- `"purpose": "maskable"` icons are cropped to the launcher's shape. Only the **center circle with a radius of 40 % of the icon width** (an 80 % diameter circle) is guaranteed visible. Keep the glyph inside it and let the background bleed to all four edges.

Practical checks:

- Use a full-bleed square 512×512 PNG plus a 192×192.
- Test with [maskable.app](https://maskable.app/) before installing.
- The bundled icons are `purpose: any`. If you add a `maskable` entry, pad the artwork so nothing important is outside the safe circle.
- Changing an installed icon: Chrome refreshes WebAPK icons lazily (it can take a day). Uninstall + reinstall to force it.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Only "Add shortcut", no "Install" | Not HTTPS, manifest not reachable, or no 192/512 icon. Check `healthcheck.sh` with the full prefixed URL, e.g. `HERMES_PHONE_BASE_URL=https://your-machine.your-tailnet.ts.net/hermes ./healthcheck.sh`. |
| Second app opens the first | A root-scoped app on the same host (any port). Use prefixes; uninstall the root one. |
| App opens but API calls 404 | Prefix env not set on the server, or Serve target missing the prefix. |
| Blank page after install, fine in the browser | Token/cookie not stored yet. Open the prefixed URL with `?token=…` once in Chrome, then install. |
| Certificate error | HTTPS Certificates not enabled in the admin console, or MagicDNS off. |
