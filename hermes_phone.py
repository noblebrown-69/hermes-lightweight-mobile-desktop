#!/usr/bin/env python3
"""Hermes Phone — thin Tailscale-friendly web UI for Hermes gateway profiles.

stdlib only. Proxies OpenAI-compat chat to a local Hermes gateway.
Phone UI auth is a shared token; the gateway API key never leaves the server.
"""
from __future__ import annotations

import http.client
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Config (generic defaults — no personal IPs/paths/keys in committed code)
# ---------------------------------------------------------------------------

def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


BIND = os.environ.get("HERMES_PHONE_BIND", "127.0.0.1")
PORT = int(os.environ.get("HERMES_PHONE_PORT", "9124"))


def _normalize_base_path(raw: str) -> str:
    """Leading slash, no trailing slash; empty means root (legacy behavior)."""
    s = (raw or "").strip()
    if not s or s == "/":
        return ""
    if not s.startswith("/"):
        s = "/" + s
    return s.rstrip("/")


BASE_PATH = _normalize_base_path(os.environ.get("HERMES_PHONE_BASE_PATH", ""))
TOKEN_FILE = _expand(
    os.environ.get("HERMES_PHONE_TOKEN_FILE", "~/.config/hermes-phone/token")
)
API_KEY_FILE = _expand(
    os.environ.get("HERMES_PHONE_API_KEY_FILE", "~/.config/hermes-phone/api_key")
)
GATEWAY = os.environ.get("HERMES_PHONE_GATEWAY", "http://127.0.0.1:8642").rstrip("/")
HERMES_ENV = _expand(
    os.environ.get("HERMES_PHONE_HERMES_ENV", "~/.hermes/.env")
)
PROFILES_DIR = _expand(
    os.environ.get("HERMES_PHONE_PROFILES_DIR", "~/.hermes/profiles")
)
PROFILES_FILE = _expand(
    os.environ.get(
        "HERMES_PHONE_PROFILES_FILE",
        str(Path(__file__).resolve().parent / "profiles.json"),
    )
)
COOKIE_NAME = "hermes_phone_token"
SESSION_KEY_PREFIX = "hermes-phone"

APP_DIR = Path(__file__).resolve().parent


def _minimal_png(size: int, rgb: tuple[int, int, int] = (59, 130, 246)) -> bytes:
    """Solid-color PNG fallback when icon-*.png is missing (stdlib only)."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    r, g, b = rgb
    row = b"\x00" + bytes([r, g, b]) * size
    raw = row * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def load_icon(size: int) -> bytes:
    path = APP_DIR / f"icon-{size}.png"
    if path.is_file():
        return path.read_bytes()
    return _minimal_png(size, (59, 130, 246))


_MANIFEST_BASE = {
    "name": "Hermes Phone",
    "short_name": "Hermes",
    "display": "standalone",
    "background_color": "#0e1116",
    "theme_color": "#0e1116",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    ],
}


def build_manifest() -> dict[str, Any]:
    """Manifest with start_url/scope/id/icons prefixed by BASE_PATH (or /)."""
    base = BASE_PATH  # "" or "/hermes"
    icons = []
    for ic in _MANIFEST_BASE["icons"]:
        icons.append({**ic, "src": base + ic["src"]})
    return {
        "name": _MANIFEST_BASE["name"],
        "short_name": _MANIFEST_BASE["short_name"],
        "start_url": base + "/",
        "scope": base + "/",
        "id": base + "/",
        "display": _MANIFEST_BASE["display"],
        "background_color": _MANIFEST_BASE["background_color"],
        "theme_color": _MANIFEST_BASE["theme_color"],
        "icons": icons,
    }



def catalog_mode() -> str:
    """discover (default) or file (old allowlist-only / HERMES_PHONE_CATALOG=file)."""
    return (os.environ.get("HERMES_PHONE_CATALOG", "discover") or "discover").strip().lower()


def auth_mode() -> str:
    """off | token (default). Use off when the bind is already private (e.g. Tailscale-only)."""
    return (os.environ.get("HERMES_PHONE_AUTH", "token") or "token").strip().lower()

# Ids that default to visible in the panel when localStorage has no preference.
_DEFAULT_VISIBLE_IDS = frozenset(
    x.strip() for x in os.environ.get("HERMES_PHONE_DEFAULT_VISIBLE", "").split(",") if x.strip()
)

# Built-in example catalog (file mode / empty discover fallback)
_DEFAULT_PROFILES = [
    # Used only when profiles dir is missing and no overlays are set.
    {"id": "assistant", "name": "Assistant", "subtitle": "assistant", "default_visible": True},
]


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k:
            out[k] = v
    return out


def load_api_key() -> str:
    """Prefer env, then api_key file, then API_SERVER_KEY from hermes .env."""
    env_key = os.environ.get("HERMES_PHONE_API_KEY", "").strip()
    if env_key:
        return env_key
    if API_KEY_FILE.is_file():
        try:
            k = API_KEY_FILE.read_text(encoding="utf-8").strip()
            if k:
                return k
        except OSError:
            pass
    dotenv = _parse_dotenv(HERMES_ENV)
    return (dotenv.get("API_SERVER_KEY") or "").strip()


def ensure_token() -> str:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.is_file():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(tok + "\n", encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return tok


def _clean_display_name(name: str) -> str:
    """Strip Desktop-ish suffixes so tiles stay short on phone."""
    n = name.strip()
    for suffix in (" — System Prompt", " - System Prompt", " — System prompt", " - System prompt"):
        if n.endswith(suffix):
            n = n[: -len(suffix)].rstrip(" —-").strip()
    return n or name.strip()


def _soul_display_name(profile_dir: Path) -> str | None:
    soul = profile_dir / "SOUL.md"
    if not soul.is_file():
        return None
    try:
        text = soul.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            name = s[2:].strip()
            if name:
                return name
    return None


def _overlay_map() -> dict[str, dict[str, Any]]:
    """Optional name/subtitle/default_visible overlays from env or profiles.json."""
    overlays: dict[str, dict[str, Any]] = {}

    def ingest(data: Any) -> None:
        if not isinstance(data, list):
            return
        for p in data:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or p.get("profile") or "").strip()
            if not pid:
                continue
            entry: dict[str, Any] = {}
            if "name" in p and p["name"] is not None:
                entry["name"] = str(p["name"])
            if "subtitle" in p and p["subtitle"] is not None:
                entry["subtitle"] = str(p["subtitle"])
            if "default_visible" in p:
                entry["default_visible"] = bool(p["default_visible"])
            overlays[pid] = entry

    raw = os.environ.get("HERMES_PHONE_PROFILES", "").strip()
    if raw:
        try:
            ingest(json.loads(raw))
        except json.JSONDecodeError:
            pass
    if PROFILES_FILE.is_file():
        try:
            ingest(json.loads(PROFILES_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return overlays


def _norm_profile(p: Any, *, default_visible: bool | None = None) -> dict[str, Any]:
    if not isinstance(p, dict):
        raise ValueError("profile must be object")
    pid = str(p.get("id") or p.get("profile") or "").strip()
    if not pid:
        raise ValueError("profile missing id")
    if "default_visible" in p:
        vis = bool(p["default_visible"])
    elif default_visible is not None:
        vis = bool(default_visible)
    else:
        vis = pid in _DEFAULT_VISIBLE_IDS
    return {
        "id": pid,
        "name": str(p.get("name") or pid),
        "subtitle": str(p.get("subtitle") or pid),
        "default_visible": vis,
    }


def _load_profiles_file_mode() -> list[dict[str, Any]]:
    """Old allowlist-only catalog (HERMES_PHONE_CATALOG=file)."""
    raw = os.environ.get("HERMES_PHONE_PROFILES", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                return [_norm_profile(p) for p in data]
        except json.JSONDecodeError:
            pass
    if PROFILES_FILE.is_file():
        try:
            data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return [_norm_profile(p) for p in data]
        except (OSError, json.JSONDecodeError):
            pass
    return [dict(p) for p in _DEFAULT_PROFILES]


def _discover_profile_dirs() -> list[Path]:
    if not PROFILES_DIR.is_dir():
        return []
    out: list[Path] = []
    try:
        entries = sorted(PROFILES_DIR.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for p in entries:
        if not p.is_dir():
            continue
        name = p.name
        if not name or name.startswith("."):
            continue
        out.append(p)
    return out


def load_profiles() -> list[dict[str, Any]]:
    """Discover ~/.hermes/profiles (default) or file allowlist; overlays optional."""
    if catalog_mode() == "file":
        return _load_profiles_file_mode()

    overlays = _overlay_map()
    dirs = _discover_profile_dirs()
    if not dirs:
        # Empty profiles dir → fall back to defaults so UI still works.
        base = [dict(p) for p in _DEFAULT_PROFILES]
        for p in base:
            ov = overlays.get(p["id"]) or {}
            p.update({k: v for k, v in ov.items() if k in ("name", "subtitle", "default_visible")})
            if "default_visible" not in ov:
                p["default_visible"] = p["id"] in _DEFAULT_VISIBLE_IDS
        return base

    bots: list[dict[str, Any]] = []
    for d in dirs:
        pid = d.name
        soul_name = _soul_display_name(d)
        ov = overlays.get(pid) or {}
        name = _clean_display_name(str(ov.get("name") or soul_name or pid))
        subtitle = str(ov.get("subtitle") or pid)
        if "default_visible" in ov:
            vis = bool(ov["default_visible"])
        else:
            vis = pid in _DEFAULT_VISIBLE_IDS
        bots.append(
            {
                "id": pid,
                "name": name,
                "subtitle": subtitle,
                "default_visible": vis,
            }
        )
    return bots


def mint_session_id() -> str:
    """Prefer hermes_state_ids.new_session_id; fallback YYYYMMDD_HHMMSS_hex6."""
    hermes_agent = Path.home() / ".hermes" / "hermes-agent"
    if hermes_agent.is_dir():
        sp = str(hermes_agent)
        if sp not in sys.path:
            sys.path.insert(0, sp)
        try:
            from hermes_state_ids import new_session_id  # type: ignore

            return str(new_session_id(datetime.now(timezone.utc)))
        except Exception:
            pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{secrets.token_hex(3)}"


# Loaded at startup into memory — never served to the client.
TOKEN = ensure_token()
API_KEY = load_api_key()
# Snapshot for boot log only; handlers reload via load_profiles() each request.
_BOOT_PROFILES = load_profiles()

# ---------------------------------------------------------------------------
# HTML (mobile-first dark UI)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="theme-color" content="#0e1116"/>
<link rel="manifest" href="@@BASE@@/manifest.webmanifest"/>
<link rel="icon" type="image/png" sizes="192x192" href="@@BASE@@/icon-192.png"/>
<link rel="apple-touch-icon" href="@@BASE@@/icon-192.png"/>
<title>Hermes Phone</title>
<style>
:root {
  --bg: #0e1116;
  --panel: #161b22;
  --border: #2a323d;
  --text: #e6edf3;
  --muted: #8b949e;
  --accent: #3b82f6;
  --user: #1f6feb;
  --bot: #21262d;
  --danger: #f85149;
  --ok: #3fb950;
  --warn: #d29922;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; height: 100%; background: var(--bg); color: var(--text);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
body { display: flex; flex-direction: column; }
header {
  display: flex; align-items: center; gap: 10px; padding: 10px 12px;
  border-bottom: 1px solid var(--border); background: var(--panel);
  position: sticky; top: 0; z-index: 5;
}
header h1 { font-size: 16px; margin: 0; font-weight: 600; flex: 1; }
header .sid { font-size: 11px; color: var(--muted); max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
button, .tile {
  appearance: none; border: 1px solid var(--border); background: var(--bg);
  color: var(--text); border-radius: 10px; padding: 8px 12px; cursor: pointer;
}
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
#bots {
  display: flex; gap: 8px; overflow-x: auto; padding: 10px 12px;
  border-bottom: 1px solid var(--border); background: var(--panel);
  -webkit-overflow-scrolling: touch;
}
.tile {
  min-width: 110px; text-align: left; flex: 0 0 auto;
  display: flex; flex-direction: column; gap: 2px;
}
.tile.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.tile .n { font-weight: 600; font-size: 14px; }
.tile .s { font-size: 11px; color: var(--muted); }
#chat {
  flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 10px;
}
.bubble {
  max-width: 92%; padding: 10px 12px; border-radius: 14px; white-space: pre-wrap;
  word-break: break-word;
}
.bubble.user { align-self: flex-end; background: var(--user); }
.bubble.assistant { align-self: flex-start; background: var(--bot); border: 1px solid var(--border); }
.bubble.system { align-self: center; background: transparent; color: var(--muted); font-size: 13px; }
.bubble.err { align-self: center; color: var(--danger); }
#composer {
  display: flex; gap: 8px; padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
  border-top: 1px solid var(--border); background: var(--panel);
  position: sticky; bottom: 0;
}
#composer textarea {
  flex: 1; resize: none; min-height: 44px; max-height: 120px;
  background: var(--bg); color: var(--text); border: 1px solid var(--border);
  border-radius: 12px; padding: 10px 12px; font: inherit;
}
#busy { display: none; padding: 6px 12px; font-size: 13px; color: var(--muted); }
#busy.on { display: block; }
/* Bots visibility sheet */
#botsModal {
  display: none; position: fixed; inset: 0; z-index: 50;
  background: rgba(0,0,0,0.55); align-items: flex-end; justify-content: center;
}
#botsModal.open { display: flex; }
#botsSheet {
  width: 100%; max-width: 520px; max-height: 88vh;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 16px 16px 0 0; padding: 14px 14px calc(14px + env(safe-area-inset-bottom));
  display: flex; flex-direction: column; gap: 10px;
  box-shadow: 0 -8px 32px rgba(0,0,0,0.45);
}
#botsSheet h2 { margin: 0; font-size: 17px; font-weight: 600; }
#botsTip { font-size: 12px; color: var(--muted); margin: 0; }
#botsTip.warn { color: var(--warn); }
#botsList {
  overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 4px;
  -webkit-overflow-scrolling: touch;
}
.bot-row {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 8px; border-radius: 10px; border: 1px solid transparent;
}
.bot-row:active, .bot-row:hover { background: var(--bg); }
.bot-row label { flex: 1; display: flex; flex-direction: column; gap: 2px; cursor: pointer; }
.bot-row .bn { font-weight: 600; font-size: 14px; }
.bot-row .bi { font-size: 11px; color: var(--muted); }
.bot-row input[type=checkbox] {
  width: 20px; height: 20px; accent-color: var(--accent); flex-shrink: 0;
}
#botsActions { display: flex; gap: 8px; justify-content: flex-end; }
@media (min-width: 640px) {
  #botsModal { align-items: center; }
  #botsSheet { border-radius: 16px; max-height: 80vh; }
}
</style>
</head>
<body>
<header>
  <h1 id="title">Hermes Phone</h1>
  <span class="sid" id="sidLabel"></span>
  <button type="button" id="botsMenuBtn" title="Choose visible bots">Bots</button>
  <button type="button" id="newChatBtn" title="New chat for active bot">New chat</button>
</header>
<nav id="bots" aria-label="Bots"></nav>
<div id="busy">Thinking…</div>
<main id="chat"></main>
<form id="composer">
  <textarea id="input" rows="1" placeholder="Message…" enterkeyhint="send"></textarea>
  <button class="primary" type="submit" id="sendBtn">Send</button>
</form>
<div id="botsModal" role="dialog" aria-modal="true" aria-labelledby="botsSheetTitle" hidden>
  <div id="botsSheet">
    <h2 id="botsSheetTitle">Visible bots</h2>
    <p id="botsTip">Tip: 3–4 visible works best on phone.</p>
    <div id="botsList"></div>
    <div id="botsActions">
      <button type="button" id="botsCancelBtn">Cancel</button>
      <button type="button" class="primary" id="botsDoneBtn">Done</button>
    </div>
  </div>
</div>
<script>
window.__BASE__ = "@@BASE@@";

(function () {
  const LS_KEY = "hermes-phone.sessions";
  const LS_TRANSCRIPTS = "hermes-phone.transcripts";
  const LS_VISIBLE = "hermes-phone.visible";
  const chat = document.getElementById("chat");
  const botsEl = document.getElementById("bots");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("sendBtn");
  const newChatBtn = document.getElementById("newChatBtn");
  const botsMenuBtn = document.getElementById("botsMenuBtn");
  const botsModal = document.getElementById("botsModal");
  const botsList = document.getElementById("botsList");
  const botsTip = document.getElementById("botsTip");
  const botsDoneBtn = document.getElementById("botsDoneBtn");
  const botsCancelBtn = document.getElementById("botsCancelBtn");
  const busy = document.getElementById("busy");
  const title = document.getElementById("title");
  const sidLabel = document.getElementById("sidLabel");

  let bots = [];
  let visibleIds = [];
  let activeId = null;
  let sessionId = null;
  let messages = []; // {role, content} for active profile
  let sending = false;
  let draftVisible = null;

  function authHeaders() {
    const h = {"Content-Type": "application/json"};
    const q = new URLSearchParams(location.search).get("token");
    if (q) h["Authorization"] = "Bearer " + q;
    return h;
  }

  function loadMap(key) {
    try { return JSON.parse(localStorage.getItem(key) || "{}") || {}; }
    catch { return {}; }
  }
  function saveMap(key, map) {
    localStorage.setItem(key, JSON.stringify(map));
  }

  function defaultVisibleIds() {
    const fromFlag = bots.filter(b => b.default_visible).map(b => b.id);
    if (fromFlag.length) return fromFlag.slice(0, 4);
    // No defaults configured: show the first two so the bar isn't empty or crowded.
    return bots.slice(0, 2).map(b => b.id);
  }

  function loadVisibleIds() {
    try {
      const raw = localStorage.getItem(LS_VISIBLE);
      if (raw == null) return defaultVisibleIds();
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return defaultVisibleIds();
      const known = new Set(bots.map(b => b.id));
      const filtered = arr.map(String).filter(id => known.has(id));
      return filtered;
    } catch {
      return defaultVisibleIds();
    }
  }

  function saveVisibleIds(ids) {
    localStorage.setItem(LS_VISIBLE, JSON.stringify(ids));
  }

  function visibleBots() {
    const set = new Set(visibleIds);
    return bots.filter(b => set.has(b.id));
  }

  function setBusy(on, text) {
    busy.classList.toggle("on", !!on);
    busy.textContent = text || "Thinking…";
    sendBtn.disabled = !!on;
    sending = !!on;
  }

  function render() {
    chat.innerHTML = "";
    if (!messages.length) {
      const d = document.createElement("div");
      d.className = "bubble system";
      d.textContent = activeId ? "New conversation — say hello." : "Pick a bot.";
      chat.appendChild(d);
    } else {
      for (const m of messages) {
        const d = document.createElement("div");
        d.className = "bubble " + (m.role === "user" ? "user" : "assistant");
        d.textContent = m.content;
        chat.appendChild(d);
      }
    }
    chat.scrollTop = chat.scrollHeight;
    sidLabel.textContent = sessionId || "";
    const bot = bots.find(b => b.id === activeId);
    title.textContent = bot ? bot.name : "Hermes Phone";
  }

  function persistTranscript() {
    if (!activeId) return;
    const t = loadMap(LS_TRANSCRIPTS);
    t[activeId] = messages;
    saveMap(LS_TRANSCRIPTS, t);
  }

  async function ensureSession(profileId) {
    const map = loadMap(LS_KEY);
    if (map[profileId]) return map[profileId];
    const r = await fetch("@@BASE@@/api/new-session", {method: "POST", headers: authHeaders(), body: "{}"});
    if (!r.ok) throw new Error("mint session failed: " + r.status);
    const data = await r.json();
    map[profileId] = data.session_id;
    saveMap(LS_KEY, map);
    return data.session_id;
  }

  function renderTiles() {
    botsEl.innerHTML = "";
    const vis = visibleBots();
    for (const b of vis) {
      const t = document.createElement("button");
      t.type = "button";
      t.className = "tile" + (b.id === activeId ? " active" : "");
      t.dataset.id = b.id;
      t.innerHTML = '<span class="n"></span><span class="s"></span>';
      t.querySelector(".n").textContent = b.name;
      t.querySelector(".s").textContent = b.subtitle || b.id;
      t.addEventListener("click", () => selectBot(b.id));
      botsEl.appendChild(t);
    }
  }

  async function selectBot(id) {
    activeId = id;
    renderTiles();
    sessionId = await ensureSession(id);
    const t = loadMap(LS_TRANSCRIPTS);
    messages = Array.isArray(t[id]) ? t[id] : [];
    render();
    input.focus();
  }

  async function clearActiveBot() {
    activeId = null;
    sessionId = null;
    messages = [];
    renderTiles();
    render();
  }

  async function applyVisibility(ids) {
    visibleIds = ids.slice();
    saveVisibleIds(visibleIds);
    renderTiles();
    const vis = visibleBots();
    if (activeId && !visibleIds.includes(activeId)) {
      if (vis.length) await selectBot(vis[0].id);
      else await clearActiveBot();
    } else if (!activeId && vis.length) {
      await selectBot(vis[0].id);
    } else {
      render();
    }
  }

  function updateTipCount() {
    const n = (draftVisible || []).length;
    botsTip.textContent = "Tip: 3–4 visible works best on phone.";
    botsTip.classList.toggle("warn", n > 4);
    if (n > 4) {
      botsTip.textContent = "Tip: 3–4 visible works best on phone. (" + n + " selected — still OK)";
    }
  }

  function openBotsMenu() {
    draftVisible = visibleIds.slice();
    botsList.innerHTML = "";
    for (const b of bots) {
      const row = document.createElement("div");
      row.className = "bot-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.id = "vis-" + b.id;
      cb.checked = draftVisible.includes(b.id);
      cb.addEventListener("change", () => {
        if (cb.checked) {
          if (!draftVisible.includes(b.id)) draftVisible.push(b.id);
        } else {
          draftVisible = draftVisible.filter(id => id !== b.id);
        }
        updateTipCount();
      });
      const lab = document.createElement("label");
      lab.htmlFor = cb.id;
      lab.innerHTML = '<span class="bn"></span><span class="bi"></span>';
      lab.querySelector(".bn").textContent = b.name;
      lab.querySelector(".bi").textContent = b.id;
      row.appendChild(cb);
      row.appendChild(lab);
      botsList.appendChild(row);
    }
    updateTipCount();
    botsModal.hidden = false;
    botsModal.classList.add("open");
  }

  function closeBotsMenu() {
    botsModal.classList.remove("open");
    botsModal.hidden = true;
    draftVisible = null;
  }

  async function newChat() {
    if (!activeId) return;
    const r = await fetch("@@BASE@@/api/new-session", {method: "POST", headers: authHeaders(), body: "{}"});
    if (!r.ok) { alert("Could not mint session"); return; }
    const data = await r.json();
    const map = loadMap(LS_KEY);
    map[activeId] = data.session_id;
    saveMap(LS_KEY, map);
    sessionId = data.session_id;
    messages = [];
    persistTranscript();
    render();
    input.focus();
  }

  function showBusyErr() {
    const name = (bots.find(b => b.id === activeId) || {}).name || "Bot";
    const err = document.createElement("div");
    err.className = "bubble err";
    err.textContent = name + " is busy — try again";
    chat.appendChild(err);
  }

  function showErr(msg) {
    const err = document.createElement("div");
    err.className = "bubble err";
    err.textContent = msg;
    chat.appendChild(err);
  }

  async function sendNonStream(payload) {
    const r = await fetch("@@BASE@@/api/chat", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 429) {
      showBusyErr();
      return;
    }
    if (!r.ok) {
      showErr(data.error || ("Error " + r.status));
      return;
    }
    const reply = data.text || data.content || "";
    messages.push({role: "assistant", content: reply});
    if (data.session_id) sessionId = data.session_id;
    persistTranscript();
    render();
  }

  async function tryStream(payload) {
    let r;
    try {
      r = await fetch("@@BASE@@/api/chat/stream", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(payload),
      });
    } catch (e) {
      return false;
    }
    if (r.status === 429) {
      showBusyErr();
      return "handled";
    }
    if (!r.ok || !r.body) return false;
    const ct = (r.headers.get("content-type") || "").toLowerCase();
    if (ct.indexOf("text/event-stream") < 0) return false;

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let reply = "";
    let gotChunk = false;
    messages.push({role: "assistant", content: ""});
    render();
    const bubbles = chat.querySelectorAll(".bubble.assistant");
    const bubble = bubbles[bubbles.length - 1];

    try {
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        const lines = buf.split("\n");
        buf = lines.pop();
        for (const line of lines) {
          const trimmed = line.trimEnd();
          if (!trimmed.startsWith("data:")) continue;
          let data = trimmed.slice(5);
          if (data.startsWith(" ")) data = data.slice(1);
          data = data.trim();
          if (!data || data === "[DONE]") continue;
          let obj;
          try { obj = JSON.parse(data); } catch { continue; }
          const choices = obj.choices || [];
          let delta = "";
          if (choices.length) {
            const d = choices[0].delta || {};
            if (d && d.content) delta = String(d.content);
            else {
              const msg = choices[0].message || {};
              if (msg && msg.content) delta = String(msg.content);
            }
          } else if (obj.content) {
            delta = String(obj.content);
          }
          if (delta) {
            gotChunk = true;
            reply += delta;
            messages[messages.length - 1].content = reply;
            if (bubble) bubble.textContent = reply;
            chat.scrollTop = chat.scrollHeight;
          }
        }
      }
    } catch (e) {
      if (!gotChunk) {
        messages.pop();
        return false;
      }
    }
    if (!gotChunk) {
      messages.pop();
      return false;
    }
    persistTranscript();
    return true;
  }

  async function send(ev) {
    ev && ev.preventDefault();
    if (sending || !activeId || !sessionId) return;
    const text = (input.value || "").trim();
    if (!text) return;
    input.value = "";
    messages.push({role: "user", content: text});
    persistTranscript();
    render();
    setBusy(true);

    const payload = {
      profile: activeId,
      session_id: sessionId,
      messages: messages.map(m => ({role: m.role, content: m.content})),
    };

    try {
      const streamed = await tryStream(payload);
      if (streamed === "handled") {
        /* 429 already shown */
      } else if (!streamed) {
        await sendNonStream(payload);
      }
    } catch (e) {
      showErr(String(e.message || e));
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  async function boot() {
    const r = await fetch("@@BASE@@/api/bots", {headers: authHeaders()});
    if (r.status === 401) {
      chat.innerHTML = '<div class="bubble err">Unauthorized — open with ?token=…</div>';
      return;
    }
    bots = await r.json();
    if (!Array.isArray(bots)) bots = [];
    visibleIds = loadVisibleIds();
    renderTiles();
    const vis = visibleBots();
    if (vis.length) await selectBot(vis[0].id);
    else {
      activeId = null;
      render();
    }
  }

  document.getElementById("composer").addEventListener("submit", send);
  newChatBtn.addEventListener("click", newChat);
  botsMenuBtn.addEventListener("click", openBotsMenu);
  botsCancelBtn.addEventListener("click", closeBotsMenu);
  botsDoneBtn.addEventListener("click", async () => {
    const ids = (draftVisible || []).slice();
    closeBotsMenu();
    await applyVisibility(ids);
  });
  botsModal.addEventListener("click", (e) => {
    if (e.target === botsModal) closeBotsMenu();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  boot();
})();
</script>
</body>
</html>
"""



# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _strip_base_path(path: str) -> str:
    """If path is under BASE_PATH, return path with BASE_PATH removed; else unchanged."""
    if not BASE_PATH:
        return path
    if path == BASE_PATH or path == BASE_PATH + "/":
        return "/"
    if path.startswith(BASE_PATH + "/"):
        return path[len(BASE_PATH) :] or "/"
    return path


def _render_index_html() -> bytes:
    return INDEX_HTML.replace("@@BASE@@", BASE_PATH).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesPhone/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _token_ok(self) -> bool:
        # Query ?token=
        qs = parse_qs(urlparse(self.path).query)
        if TOKEN in (qs.get("token") or []):
            return True
        # Authorization: Bearer
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            if auth[7:].strip() == TOKEN:
                return True
        # Cookie
        cookie_header = self.headers.get("Cookie") or ""
        if cookie_header:
            jar = SimpleCookie()
            try:
                jar.load(cookie_header)
            except Exception:
                jar = SimpleCookie()
            morsel = jar.get(COOKIE_NAME)
            if morsel and morsel.value == TOKEN:
                return True
        return False

    def _set_auth_cookie(self) -> None:
        # Persist token after first successful auth (Shoin-style).
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000",
        )

    def _send(self, code: int, body: bytes, content_type: str, set_cookie: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self._set_auth_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any, set_cookie: bool = False) -> None:
        body = json.dumps(obj).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", set_cookie=set_cookie)

    def _auth_disabled(self) -> bool:
        return auth_mode() in ("off", "0", "false", "no", "disabled")

    def _require_auth(self) -> bool:
        if self._auth_disabled() or self._token_ok():
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def _redirect_base(self, parsed) -> bool:
        """When BASE_PATH set: GET / or GET BASE (no slash) -> 302 BASE/. Query preserved."""
        if not BASE_PATH:
            return False
        p = parsed.path
        if p == "/" or p == BASE_PATH:
            loc = BASE_PATH + "/"
            if parsed.query:
                loc = loc + "?" + parsed.query
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if self._redirect_base(parsed):
            return
        path = _strip_base_path(parsed.path)
        path = path.rstrip("/") or "/"

        # Public (no auth): health, PWA manifest, icons — browsers fetch without cookies.
        if path == "/api/health":
            profiles = load_profiles()
            self._json(
                200,
                {
                    "ok": True,
                    "gateway": GATEWAY,
                    "profiles": len(profiles),
                    "catalog_mode": catalog_mode(),
                    "profiles_dir": str(PROFILES_DIR),
                    "api_key_loaded": bool(API_KEY),
                    "auth": auth_mode(),
                },
            )
            return

        if path == "/manifest.webmanifest":
            body = json.dumps(build_manifest()).encode("utf-8")
            self._send(200, body, "application/manifest+json; charset=utf-8")
            return

        if path == "/icon-192.png":
            self._send(200, load_icon(192), "image/png")
            return

        if path == "/icon-512.png":
            self._send(200, load_icon(512), "image/png")
            return

        if path == "/apple-touch-icon.png" or path == "/apple-touch-icon":
            self._send(200, load_icon(192), "image/png")
            return

        if not self._require_auth():
            return

        if path == "/":
            body = _render_index_html()
            self._send(200, body, "text/html; charset=utf-8", set_cookie=not self._auth_disabled())
            return

        if path == "/api/bots":
            # Reload so Desktop-added profiles appear without service restart.
            self._json(200, load_profiles(), set_cookie=not self._auth_disabled())
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = _strip_base_path(parsed.path)
        path = path.rstrip("/") or "/"

        if not self._require_auth():
            return

        if path == "/api/new-session":
            self._json(200, {"session_id": mint_session_id()}, set_cookie=True)
            return

        if path == "/api/chat":
            self._handle_chat()
            return

        if path == "/api/chat/stream":
            self._handle_chat_stream()
            return

        self._json(404, {"error": "not found"})

    def _handle_chat(self) -> None:
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        profile = str(body.get("profile") or "").strip()
        session_id = str(body.get("session_id") or "").strip()
        messages = body.get("messages")

        catalog = load_profiles()
        profile_ids = {p["id"] for p in catalog}
        if profile not in profile_ids:
            self._json(400, {"error": "profile not in catalog", "profile": profile})
            return
        if not session_id:
            self._json(400, {"error": "session_id required"})
            return
        if not isinstance(messages, list) or not messages:
            self._json(400, {"error": "messages required"})
            return
        if not API_KEY:
            self._json(500, {"error": "gateway api key not configured on server"})
            return

        # Normalize messages to OpenAI-compat shape
        clean_msgs = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "")
            content = m.get("content")
            if role not in ("user", "assistant", "system"):
                continue
            if not isinstance(content, str):
                content = json.dumps(content)
            clean_msgs.append({"role": role, "content": content})
        if not clean_msgs:
            self._json(400, {"error": "no valid messages"})
            return

        url = f"{GATEWAY}/p/{profile}/v1/chat/completions"
        payload = {
            "model": profile,
            "messages": clean_msgs,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Id": session_id,
            "X-Hermes-Session-Key": f"{SESSION_KEY_PREFIX}:{profile}",
        }

        text, err_status, err_body = _proxy_chat(url, payload, headers)
        if err_status is not None:
            if err_status == 429:
                self._json(429, {"error": "busy", "detail": err_body})
            else:
                self._json(
                    502 if err_status >= 500 else err_status,
                    {"error": "gateway error", "status": err_status, "detail": err_body},
                )
            return

        self._json(200, {"text": text, "session_id": session_id, "profile": profile})


    def _handle_chat_stream(self) -> None:
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        profile = str(body.get("profile") or "").strip()
        session_id = str(body.get("session_id") or "").strip()
        messages = body.get("messages")

        catalog = load_profiles()
        profile_ids = {p["id"] for p in catalog}
        if profile not in profile_ids:
            self._json(400, {"error": "profile not in catalog", "profile": profile})
            return
        if not session_id:
            self._json(400, {"error": "session_id required"})
            return
        if not isinstance(messages, list) or not messages:
            self._json(400, {"error": "messages required"})
            return
        if not API_KEY:
            self._json(500, {"error": "gateway api key not configured on server"})
            return

        clean_msgs = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "")
            content = m.get("content")
            if role not in ("user", "assistant", "system"):
                continue
            if not isinstance(content, str):
                content = json.dumps(content)
            clean_msgs.append({"role": role, "content": content})
        if not clean_msgs:
            self._json(400, {"error": "no valid messages"})
            return

        url = f"{GATEWAY}/p/{profile}/v1/chat/completions"
        payload = {
            "model": profile,
            "messages": clean_msgs,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Id": session_id,
            "X-Hermes-Session-Key": f"{SESSION_KEY_PREFIX}:{profile}",
        }

        err_status, err_body, conn, resp = _open_gateway_stream(url, payload, headers)
        if err_status is not None:
            if err_status == 429:
                self._json(429, {"error": "busy", "detail": err_body})
            else:
                self._json(
                    502 if err_status >= 500 else err_status,
                    {"error": "gateway error", "status": err_status, "detail": err_body},
                )
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return

        assert resp is not None and conn is not None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _open_gateway_stream(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[int | None, str, http.client.HTTPConnection | None, Any]:
    """Open streaming POST to gateway. Returns (err_status, err_body, conn, resp)."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = path + "?" + parsed.query
    body = json.dumps(payload).encode("utf-8")
    try:
        if parsed.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=300)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=300)
        req_headers = dict(headers)
        req_headers["Content-Length"] = str(len(body))
        conn.request("POST", path, body=body, headers=req_headers)
        resp = conn.getresponse()
    except Exception as e:
        return 502, str(e), None, None

    if resp.status != 200:
        detail = ""
        try:
            detail = resp.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            detail = str(resp.status)
        return resp.status, detail, conn, None

    return None, "", conn, resp


def _proxy_chat(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[str, int | None, str]:
    """POST to gateway; prefer SSE stream parse, fall back to non-stream."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            detail = str(e)
        return "", e.code, detail
    except Exception as e:
        return "", 502, str(e)

    if "text/event-stream" in ctype or raw.startswith(b"data:"):
        text = _parse_sse(raw.decode("utf-8", errors="replace"))
        if text:
            return text, None, ""
        # empty SSE — try non-stream retry
        return _proxy_chat_nonstream(url, payload, headers)

    # Non-stream JSON response
    try:
        obj = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace"), None, ""
    text = _extract_assistant(obj)
    return text, None, ""


def _proxy_chat_nonstream(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[str, int | None, str]:
    payload = dict(payload)
    payload["stream"] = False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
        obj = json.loads(raw.decode("utf-8"))
        return _extract_assistant(obj), None, ""
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            detail = str(e)
        return "", e.code, detail
    except Exception as e:
        return "", 502, str(e)


def _parse_sse(body: str) -> str:
    parts: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            parts.append(data)
            continue
        # OpenAI chunk: choices[0].delta.content
        choices = obj.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            if isinstance(delta, dict) and delta.get("content"):
                parts.append(str(delta["content"]))
            msg = choices[0].get("message") or {}
            if isinstance(msg, dict) and msg.get("content") and not delta.get("content"):
                parts.append(str(msg["content"]))
        elif obj.get("content"):
            parts.append(str(obj["content"]))
    return "".join(parts)


def _extract_assistant(obj: Any) -> str:
    if not isinstance(obj, dict):
        return str(obj)
    choices = obj.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        if isinstance(msg, dict) and msg.get("content") is not None:
            return str(msg["content"])
        if choices[0].get("text") is not None:
            return str(choices[0]["text"])
    if obj.get("content") is not None:
        return str(obj["content"])
    return json.dumps(obj)


def main() -> None:
    if not API_KEY:
        sys.stderr.write(
            "WARNING: no gateway API key (set HERMES_PHONE_API_KEY, "
            f"{API_KEY_FILE}, or API_SERVER_KEY in {HERMES_ENV})\n"
        )
    ids = sorted(p["id"] for p in _BOOT_PROFILES)
    sys.stderr.write(
        "Hermes Phone listening on http://%s:%s%s/  catalog=%s profiles_dir=%s  "
        "profiles(%s)=%s  auth=%s token_file=%s\n"
        % (
            BIND,
            PORT,
            BASE_PATH or "",
            catalog_mode(),
            PROFILES_DIR,
            len(ids),
            ",".join(ids),
            auth_mode(),
            TOKEN_FILE,
        )
    )
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
