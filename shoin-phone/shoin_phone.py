#!/usr/bin/env python3
"""Shoin Phone — thin phone editor for a folder of Markdown/HTML docs (stdlib only).

Meant for a private bind (Tailscale IP or 127.0.0.1 behind `tailscale serve`).
Token auth is ON by default; SHOIN_PHONE_AUTH=off is an explicit opt-in.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import secrets
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Generic defaults — set SHOIN_PHONE_ROOT / SHOIN_PHONE_BIND per host (e.g. in the systemd unit).
ROOT = Path(os.path.expanduser(os.environ.get("SHOIN_PHONE_ROOT", "~/Docs"))).resolve()
BIND = os.environ.get("SHOIN_PHONE_BIND", "127.0.0.1")
PORT = int(os.environ.get("SHOIN_PHONE_PORT", "9123"))


def _normalize_base_path(raw: str) -> str:
    """Leading slash, no trailing slash; empty means root (legacy behavior)."""
    s = (raw or "").strip()
    if not s or s == "/":
        return ""
    if not s.startswith("/"):
        s = "/" + s
    return s.rstrip("/")


BASE_PATH = _normalize_base_path(os.environ.get("SHOIN_PHONE_BASE_PATH", ""))
TOKEN_PATH = Path(os.path.expanduser(os.environ.get("SHOIN_PHONE_TOKEN_FILE", "~/.config/shoin-phone/token")))
EXTS = {".html", ".htm", ".md", ".markdown", ".txt"}
APP_DIR = Path(__file__).resolve().parent


def _file_sha256(fp: Path) -> str:
    h = hashlib.sha256()
    with fp.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _minimal_png(size: int, rgb: tuple[int, int, int] = (212, 175, 55)) -> bytes:
    """Tiny solid-color PNG (stdlib) used when icon-*.png is missing."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b""
    r, g, b = rgb
    row = b"\x00" + bytes([r, g, b]) * size
    raw = row * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def load_icon(size: int) -> bytes:
    p = APP_DIR / f"icon-{size}.png"
    if p.is_file():
        return p.read_bytes()
    # gold accent on dark — matches Shoin theme
    return _minimal_png(size, (212, 175, 55))


_MANIFEST_BASE = {
    "name": "Shoin Phone",
    "short_name": "Shoin",
    "display": "standalone",
    "background_color": "#1a1714",
    "theme_color": "#1a1714",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    ],
}


def build_manifest():
    """Manifest with start_url/scope/id/icons prefixed by BASE_PATH (or /)."""
    base = BASE_PATH  # "" or "/shoin"
    icons = [{**ic, "src": base + ic["src"]} for ic in _MANIFEST_BASE["icons"]]
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


def load_token() -> str:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.is_file() and TOKEN_PATH.stat().st_size > 0:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    tok = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(tok + "\n", encoding="utf-8")
    TOKEN_PATH.chmod(0o600)
    return tok


TOKEN = load_token()
AUTH_OFF = os.environ.get("SHOIN_PHONE_AUTH", "on").strip().lower() in ("off", "0", "false", "no")


def safe_resolve(rel: str) -> Path:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or rel.startswith(".") and rel not in (".",):
        # allow normal paths; block absolute and empty for write
        pass
    candidate = (ROOT / rel).resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError as e:
        raise PermissionError("path escapes root") from e
    return candidate


def list_tree():
    items = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in EXTS:
            continue
        if any(part.startswith(".") for part in p.relative_to(ROOT).parts):
            continue
        st = p.stat()
        items.append({
            "path": str(p.relative_to(ROOT)).replace("\\", "/"),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    return items


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#1a1714">
<link rel="manifest" href="@@BASE@@/manifest.webmanifest">
<link rel="icon" type="image/png" sizes="192x192" href="@@BASE@@/icon-192.png">
<link rel="apple-touch-icon" href="@@BASE@@/icon-192.png">
<title>Shoin Phone</title>
<style>
:root { --bg:#1a1714; --card:#241f1a; --ink:#f5efe6; --muted:#9a8f82; --accent:#d4af37; --line:#3c3228; --folder:#c4b59a; }
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, -apple-system, sans-serif; background:var(--bg); color:var(--ink); }
header { padding:.85rem 1rem; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--bg); z-index:2; }
header h1 { margin:0; font-size:1.15rem; color:var(--accent); font-weight:600; }
header p { margin:.25rem 0 0; font-size:.75rem; color:var(--muted); }
.layout { display:flex; flex-direction:column; min-height:100dvh; }
@media (min-width:800px) { .layout { flex-direction:row; height:100dvh; } .list { width:340px; border-right:1px solid var(--line); } }
.list { max-height:42dvh; overflow:auto; -webkit-overflow-scrolling:touch; }
@media (min-width:800px) { .list { max-height:none; } }
.list-tools { display:flex; gap:.4rem; padding:.5rem .75rem; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--bg); z-index:1; }
.list-tools button { flex:1; font-size:.75rem; padding:.4rem; background:var(--card); color:var(--ink); border:1px solid var(--line); border-radius:6px; }
.tree { padding:.25rem 0 .75rem; }
.tree details { margin:0; }
.tree summary {
  list-style:none; cursor:pointer; user-select:none;
  padding:.45rem .75rem .45rem calc(.5rem + var(--d,0) * 0.85rem);
  color:var(--folder); font-size:.9rem; font-weight:600;
  display:flex; align-items:center; gap:.4rem;
}
.tree summary::-webkit-details-marker { display:none; }
.tree summary::before { content:"▸"; width:0.9em; color:var(--muted); font-size:.85rem; }
.tree details[open] > summary::before { content:"▾"; }
.tree summary:active { background:var(--card); }
.tree .file {
  display:block; width:100%; text-align:left;
  background:transparent; border:0;
  color:var(--ink); padding:.5rem .75rem .5rem calc(1.4rem + var(--d,0) * 0.85rem);
  font:inherit; font-size:.9rem;
}
.tree .file:active, .tree .file.active { background:var(--card); }
.tree .file .meta { display:block; font-size:.68rem; color:var(--muted); margin-top:.12rem; font-weight:400; }
.editor { flex:1; display:flex; flex-direction:column; min-height:0; }
.toolbar { display:flex; gap:.5rem; padding:.6rem 1rem; border-bottom:1px solid var(--line); align-items:center; flex-wrap:wrap; }
.toolbar .path { flex:1; font-size:.8rem; color:var(--muted); word-break:break-all; }
button.primary { background:var(--accent); color:#1a1714; border:0; border-radius:8px; padding:.55rem 1rem; font-weight:600; }
button.ghost { background:var(--card); color:var(--ink); border:1px solid var(--line); border-radius:8px; padding:.55rem .8rem; }
textarea, #htmlEdit { flex:1; width:100%; border:0; padding:1rem; background:var(--card); color:var(--ink); min-height:50dvh; }
textarea { resize:none; font: 15px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
#htmlEdit { overflow:auto; -webkit-overflow-scrolling:touch; font: 16px/1.5 Georgia, "Times New Roman", serif; outline:none; }
#htmlEdit ul.checklist, #htmlEdit ul { padding-left:1.25rem; }
#htmlEdit li.task { margin:.35rem 0; }
#htmlEdit p { margin:.5rem 0; }
.mode { font-size:.7rem; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.toolbar .path { flex:1 1 100%; order:-1; margin-bottom:.15rem; }
@media (min-width:600px) { .toolbar .path { flex:1; order:0; margin-bottom:0; } }
#status { font-size:.75rem; color:var(--muted); padding:.4rem 1rem .8rem; }
#status.err { color:#e88; }
#tokenGate { padding:1.5rem 1rem; }
#tokenGate input { width:100%; padding:.7rem; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--ink); margin:.5rem 0 1rem; }
#conflictModal {
  display:none; position:fixed; inset:0; z-index:40;
  background:rgba(0,0,0,.55); align-items:center; justify-content:center; padding:1rem;
}
#conflictModal.open { display:flex; }
#conflictSheet {
  background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:1.1rem 1rem; max-width:360px; width:100%;
}
#conflictSheet h2 { margin:0 0 .5rem; font-size:1.05rem; color:var(--accent); }
#conflictSheet p { margin:0 0 1rem; font-size:.85rem; color:var(--muted); line-height:1.4; }
#conflictSheet .actions { display:flex; gap:.5rem; flex-wrap:wrap; }
#conflictSheet .actions button { flex:1; min-width:120px; }
</style>
</head>
<body>
<div id="tokenGate" hidden>
  <h1>Shoin Phone</h1>
  <p>Paste the bearer token (on the server: <code>~/.config/shoin-phone/token</code>). Saved in this browser.</p>
  <input id="tokenInput" type="password" autocomplete="off" placeholder="token">
  <button class="primary" id="tokenSave">Continue</button>
</div>
<div class="layout" id="app" hidden>
  <aside class="list">
    <div class="list-tools">
      <button type="button" id="expandAll">Expand</button>
      <button type="button" id="collapseAll">Collapse</button>
      <button type="button" id="reloadBtn">Reload</button>
    </div>
    <div class="tree" id="list"></div>
  </aside>
  <main class="editor">
    <header>
      <h1>Shoin Phone</h1>
      <p>Tailscale · same Docs as desktop Shoin</p>
    </header>
    <div class="toolbar">
      <span class="path" id="curPath">Select a file</span>
      <button class="primary" id="saveBtn" type="button" disabled>Save</button>
    </div>
    <div class="toolbar" style="border-top:0;padding-top:0;padding-bottom:.35rem">
      <span class="mode" id="editMode"></span>
    </div>
    <textarea id="body" spellcheck="true" placeholder="Open a file…" disabled hidden></textarea>
    <div id="htmlEdit" contenteditable="true" spellcheck="true" hidden></div>
    <div id="status">Ready</div>
  </main>
</div>
<div id="conflictModal" role="dialog" aria-modal="true" hidden>
  <div id="conflictSheet">
    <h2>File changed on disk</h2>
    <p id="conflictMsg">This file was modified elsewhere since you opened it. Reload discards your edits; Overwrite keeps yours.</p>
    <div class="actions">
      <button type="button" class="ghost" id="conflictReload">Reload</button>
      <button type="button" class="primary" id="conflictOverwrite">Overwrite anyway</button>
    </div>
  </div>
</div>
<script>
window.__BASE__ = "@@BASE@@";
const qs = new URLSearchParams(location.search);
let token = qs.get('token') || localStorage.getItem('shoinPhoneToken') || '';
if ("@@NOAUTH@@" === "1" && !token) token = 'noauth';
let current = null; // {path, mtime, sha256, kind: 'html'|'text'}
let openFolders = new Set(JSON.parse(localStorage.getItem('shoinPhoneOpenFolders') || '[]'));
const $ = (id) => document.getElementById(id);

function isHtmlPath(path) {
  return /\.html?$/i.test(path || '');
}

function setStatus(msg, err) {
  const s = $('status');
  s.textContent = msg;
  s.className = err ? 'err' : '';
}

async function api(path, opts={}) {
  const headers = Object.assign({'Authorization': 'Bearer ' + token}, opts.headers || {});
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  if (res.status === 401) {
    localStorage.removeItem('shoinPhoneToken');
    token = '';
    showGate();
    throw new Error('unauthorized');
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = {raw:text}; }
  if (!res.ok) throw new Error((data && data.error) || res.statusText);
  return data;
}

function showGate() { $('tokenGate').hidden = false; $('app').hidden = true; }
function showApp() { $('tokenGate').hidden = true; $('app').hidden = false; }

function buildTree(files) {
  const root = { name: '', dirs: {}, files: [] };
  for (const it of files) {
    const parts = it.path.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const name = parts[i];
      if (!node.dirs[name]) node.dirs[name] = { name, dirs: {}, files: [] };
      node = node.dirs[name];
    }
    node.files.push({ name: parts[parts.length - 1], ...it });
  }
  return root;
}

function saveOpenState() {
  localStorage.setItem('shoinPhoneOpenFolders', JSON.stringify([...openFolders]));
}

function renderTree(node, depth, prefix) {
  const frag = document.createDocumentFragment();
  const dirNames = Object.keys(node.dirs).sort((a,b) => a.localeCompare(b, undefined, {sensitivity:'base'}));
  for (const name of dirNames) {
    const child = node.dirs[name];
    const key = prefix ? prefix + '/' + name : name;
    const det = document.createElement('details');
    det.dataset.folder = key;
    det.style.setProperty('--d', depth);
    if (openFolders.has(key) || depth === 0) det.open = true;
    det.addEventListener('toggle', () => {
      if (det.open) openFolders.add(key); else openFolders.delete(key);
      saveOpenState();
    });
    const sum = document.createElement('summary');
    sum.textContent = name;
    det.appendChild(sum);
    det.appendChild(renderTree(child, depth + 1, key));
    frag.appendChild(det);
  }
  const files = node.files.slice().sort((a,b) => a.name.localeCompare(b.name, undefined, {sensitivity:'base'}));
  for (const it of files) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'file';
    b.style.setProperty('--d', depth);
    b.dataset.path = it.path;
    b.innerHTML = '<span>' + it.name.replace(/</g,'&lt;') + '</span><span class="meta">' + it.size + ' B</span>';
    b.onclick = () => openFile(it.path, b);
    frag.appendChild(b);
  }
  return frag;
}

async function loadTree() {
  const data = await api('@@BASE@@/api/tree');
  const list = $('list');
  list.innerHTML = '';
  list.appendChild(renderTree(buildTree(data.files), 0, ''));
  if (current) {
    const active = list.querySelector('.file[data-path="' + CSS.escape(current.path) + '"]');
    if (active) active.classList.add('active');
  }
  setStatus(data.files.length + ' files');
}

function setAllDetails(open) {
  document.querySelectorAll('#list details').forEach(d => {
    d.open = open;
    const key = d.dataset.folder;
    if (!key) return;
    if (open) openFolders.add(key); else openFolders.delete(key);
  });
  saveOpenState();
}

function showEditor(kind, content) {
  const ta = $('body');
  const he = $('htmlEdit');
  if (kind === 'html') {
    ta.hidden = true;
    ta.disabled = true;
    he.hidden = false;
    he.setAttribute('contenteditable', 'true');
    he.innerHTML = content && content.trim() ? content : '<p><br></p>';
    $('editMode').textContent = 'Rich text · tap to edit';
  } else {
    he.hidden = true;
    he.removeAttribute('contenteditable');
    he.innerHTML = '';
    ta.hidden = false;
    ta.disabled = false;
    ta.value = content;
    $('editMode').textContent = 'Plain text';
  }
}

function readEditor() {
  if (!current) return '';
  if (current.kind === 'html') return $('htmlEdit').innerHTML;
  return $('body').value;
}

async function openFile(path, btn) {
  document.querySelectorAll('.list .file').forEach(x => x.classList.remove('active'));
  if (btn) btn.classList.add('active');
  const data = await api('@@BASE@@/api/file?path=' + encodeURIComponent(path));
  const kind = isHtmlPath(data.path) ? 'html' : 'text';
  current = {path: data.path, mtime: data.mtime, sha256: data.sha256 || null, kind};
  $('curPath').textContent = data.path;
  $('saveBtn').disabled = false;
  showEditor(kind, data.content);
  setStatus('Opened · tap in the page to edit');
}

function showConflictModal(onReload, onOverwrite) {
  const modal = $('conflictModal');
  modal.hidden = false;
  modal.classList.add('open');
  const cleanup = () => {
    modal.classList.remove('open');
    modal.hidden = true;
    $('conflictReload').onclick = null;
    $('conflictOverwrite').onclick = null;
  };
  $('conflictReload').onclick = async () => { cleanup(); await onReload(); };
  $('conflictOverwrite').onclick = async () => { cleanup(); await onOverwrite(); };
}

async function saveFile(force) {
  if (!current) return;
  setStatus(force ? 'Overwriting…' : 'Saving…');
  try {
    const headers = Object.assign({'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'});
    const body = {content: readEditor(), mtime: current.mtime};
    if (current.sha256) body.sha256 = current.sha256;
    if (force) body.force = true;
    const res = await fetch('@@BASE@@/api/file?path=' + encodeURIComponent(current.path), {
      method: 'PUT', headers, body: JSON.stringify(body),
    });
    if (res.status === 401) {
      localStorage.removeItem('shoinPhoneToken');
      token = '';
      showGate();
      throw new Error('unauthorized');
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = {raw:text}; }
    if (res.status === 409) {
      setStatus('Conflict — choose Reload or Overwrite', true);
      showConflictModal(
        async () => {
          const fresh = await api('@@BASE@@/api/file?path=' + encodeURIComponent(current.path));
          current.mtime = fresh.mtime;
          current.sha256 = fresh.sha256 || null;
          showEditor(current.kind, fresh.content);
          setStatus('Reloaded from disk · your edits were discarded');
        },
        async () => { await saveFile(true); }
      );
      return;
    }
    if (!res.ok) throw new Error((data && data.error) || res.statusText);
    current.mtime = data.mtime;
    if (data.sha256) current.sha256 = data.sha256;
    setStatus('Saved');
  } catch (e) {
    setStatus(String(e.message || e), true);
  }
}

$('tokenSave').onclick = () => {
  token = $('tokenInput').value.trim();
  if (!token) return;
  localStorage.setItem('shoinPhoneToken', token);
  boot();
};
$('reloadBtn').onclick = () => loadTree().catch(e => setStatus(String(e), true));
$('expandAll').onclick = () => setAllDetails(true);
$('collapseAll').onclick = () => setAllDetails(false);
$('saveBtn').onclick = () => saveFile();

async function boot() {
  if (!token) { showGate(); return; }
  showApp();
  try {
    await api('@@BASE@@/api/health');
    await loadTree();
  } catch (e) {
    setStatus(String(e), true);
    if (!token) showGate();
  }
}
if (qs.get('token')) localStorage.setItem('shoinPhoneToken', token);
boot();
</script>
</body>
</html>
"""


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
    return INDEX_HTML.replace("@@BASE@@", BASE_PATH).replace("@@NOAUTH@@", "1" if AUTH_OFF else "0").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "ShoinPhone/0.1"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

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

    def _auth(self) -> bool:
        if AUTH_OFF:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), TOKEN):
            return True
        q = parse_qs(urlparse(self.path).query)
        tok = (q.get("token") or [""])[0]
        return bool(tok) and secrets.compare_digest(tok, TOKEN)

    def _send(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        if self._redirect_base(u):
            return
        path = _strip_base_path(u.path)
        if path == "/":
            return self._send(200, _render_index_html(), "text/html; charset=utf-8")
        if path == "/manifest.webmanifest":
            body = json.dumps(build_manifest()).encode("utf-8")
            return self._send(200, body, "application/manifest+json; charset=utf-8")
        if path == "/icon-192.png":
            return self._send(200, load_icon(192), "image/png")
        if path == "/icon-512.png":
            return self._send(200, load_icon(512), "image/png")
        if path == "/apple-touch-icon.png":
            return self._send(200, load_icon(192), "image/png")
        if not self._auth():
            return self._json(401, {"error": "unauthorized"})
        if path == "/api/health":
            return self._json(200, {"ok": True, "root": str(ROOT), "bind": BIND, "port": PORT})
        if path == "/api/tree":
            return self._json(200, {"files": list_tree()})
        if path == "/api/file":
            q = parse_qs(u.query)
            rel = (q.get("path") or [""])[0]
            try:
                fp = safe_resolve(rel)
            except PermissionError:
                return self._json(400, {"error": "bad path"})
            if not fp.is_file():
                return self._json(404, {"error": "not found"})
            if fp.suffix.lower() not in EXTS:
                return self._json(400, {"error": "unsupported type"})
            data = fp.read_text(encoding="utf-8", errors="replace")
            st = fp.stat()
            return self._json(200, {
                "path": str(fp.relative_to(ROOT)).replace("\\", "/"),
                "mtime": int(st.st_mtime),
                "sha256": _file_sha256(fp),
                "content": data,
            })
        return self._json(404, {"error": "not found"})

    def do_PUT(self):
        if not self._auth():
            return self._json(401, {"error": "unauthorized"})
        u = urlparse(self.path)
        if _strip_base_path(u.path) != "/api/file":
            return self._json(404, {"error": "not found"})
        q = parse_qs(u.query)
        rel = (q.get("path") or [""])[0]
        try:
            fp = safe_resolve(rel)
        except PermissionError:
            return self._json(400, {"error": "bad path"})
        if fp.suffix.lower() not in EXTS:
            return self._json(400, {"error": "unsupported type"})
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        content = payload.get("content")
        if not isinstance(content, str):
            return self._json(400, {"error": "content string required"})
        force = bool(payload.get("force"))
        client_mtime = payload.get("mtime")
        if fp.exists() and client_mtime is not None and not force:
            cur = int(fp.stat().st_mtime)
            if cur != int(client_mtime):
                return self._json(409, {
                    "error": "conflict",
                    "mtime": cur,
                    "sha256": _file_sha256(fp),
                    "content": fp.read_text(encoding="utf-8", errors="replace"),
                })
        fp.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                         dir=str(fp.parent), prefix=".shoin-phone-") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        os.replace(tmp_path, fp)
        return self._json(200, {
            "ok": True,
            "path": str(fp.relative_to(ROOT)).replace("\\", "/"),
            "mtime": int(fp.stat().st_mtime),
            "sha256": _file_sha256(fp),
        })


def main():
    if not ROOT.is_dir():
        raise SystemExit(f"Docs root missing: {ROOT}")
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"Shoin Phone on http://{BIND}:{PORT}{BASE_PATH or ''}/ root={ROOT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
