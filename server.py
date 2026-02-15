

import io
import os
import re
import time
import secrets
import hashlib
import mimetypes
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template_string, send_file, abort, request, make_response
from werkzeug.utils import secure_filename
import markdown

app = Flask(__name__)

VAULT = Path(__file__).resolve().parent

import json as _json

_CONFIG_PATH = VAULT / "tachylite.config.json"
_DEFAULTS = {
    "port": 8000,
    "host": "0.0.0.0",
    "allow_edit_all": False,
    "allow_file_creation": True,
    "excluded_dirs": [".obsidian", "Templates", ".git", ".claude", ".trash", "__pycache__", "_site", ".github"],
    "excluded_files": ["server.py", "build_static.py", "deploy.sh", "deploy-tachylite.sh", "setup-autostart.sh", "tachylite.config.json"],
    "allowed_upload_extensions": [".md", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"],
    "poll_interval": 15,
}

def _load_config() -> dict:
    cfg = dict(_DEFAULTS)
    if _CONFIG_PATH.is_file():
        try:
            with open(_CONFIG_PATH) as f:
                user = _json.load(f)
            cfg.update(user)
        except Exception as e:
            print(f"Warning: could not load {_CONFIG_PATH.name}: {e}")
    return cfg

_cfg = _load_config()

PORT = _cfg["port"]
HOST = _cfg["host"]
ALLOW_EDIT_ALL = _cfg["allow_edit_all"]
ALLOW_FILE_CREATION = _cfg["allow_file_creation"]
EXCLUDED_DIRS = set(_cfg["excluded_dirs"])
EXCLUDED_FILES = set(_cfg["excluded_files"])
ALLOWED_UPLOAD_EXTENSIONS = set(_cfg["allowed_upload_extensions"])
POLL_INTERVAL = max(1, int(_cfg["poll_interval"]))

_tree_cache: dict = {"hash": None, "ts": 0.0}
_TREE_CACHE_TTL = POLL_INTERVAL
_session_files: dict[str, set[str]] = {}
_SESSION_COOKIE = "vault_sid"

def _walk_vault(root: Path):

    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in EXCLUDED_DIRS:
                    stack.append(Path(entry.path))
            elif entry.name not in EXCLUDED_FILES:
                try:
                    rel = str(Path(entry.path).relative_to(root))
                    yield rel, entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    pass


def _get_tree_hash() -> str:

    now = time.monotonic()
    if _tree_cache["hash"] is not None and (now - _tree_cache["ts"]) < _TREE_CACHE_TTL:
        return _tree_cache["hash"]
    h = hashlib.md5()
    for rel, mtime in _walk_vault(VAULT):
        h.update(f"{rel}:{mtime}".encode())
    digest = h.hexdigest()
    _tree_cache["hash"] = digest
    _tree_cache["ts"] = now
    return digest


def _invalidate_tree_cache():
    _tree_cache["hash"] = None
    _tree_cache["ts"] = 0.0


def _get_or_create_sid() -> tuple[str, bool]:

    sid = request.cookies.get(_SESSION_COOKIE)
    if sid and sid in _session_files:
        return sid, False
    sid = secrets.token_urlsafe(32)
    _session_files[sid] = set()
    return sid, True


def _sid_owns(rel_str: str) -> bool:

    if ALLOW_EDIT_ALL:
        return True
    sid = request.cookies.get(_SESSION_COOKIE, "")
    return sid in _session_files and rel_str in _session_files[sid]


def _safe_vault_path(raw_path: str) -> Path | None:

    if not raw_path:
        return None
    try:
        candidate = (VAULT / raw_path).resolve()
        candidate.relative_to(VAULT.resolve())
    except (ValueError, OSError):
        return None
    rel = candidate.relative_to(VAULT.resolve())
    if any(part in EXCLUDED_DIRS for part in rel.parts):
        return None
    if candidate.name in EXCLUDED_FILES:
        return None
    return candidate


def build_tree(root: Path, rel: Path = None, owned: set = None) -> list:

    if rel is None:
        rel = Path(".")
    if owned is None:
        owned = set()
    current = root / rel
    items = []
    try:
        entries = sorted(current.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return items
    for entry in entries:
        if entry.name.startswith(".") and entry.name in EXCLUDED_DIRS:
            continue
        if entry.name in EXCLUDED_DIRS:
            continue
        if entry.is_file() and entry.name in EXCLUDED_FILES:
            continue
        relative = rel / entry.name
        if entry.is_dir():
            children = build_tree(root, relative, owned)
            items.append({"name": entry.name, "path": str(relative), "type": "folder", "children": children})
        else:
            items.append({"name": entry.name, "path": str(relative), "type": "file",
                          "editable": ALLOW_EDIT_ALL or str(relative) in owned})
    return items


def resolve_vault_path(note_path: str) -> Path | None:

    target = VAULT / note_path
    if target.exists():
        return target

    name = Path(note_path).name
    for p in VAULT.rglob(name):
        if not any(part in EXCLUDED_DIRS for part in p.relative_to(VAULT).parts):
            return p
    return None


def process_wikilinks(text: str) -> str:


    def replace_embed(m):
        target = m.group(1)
        ext = Path(target).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
            return f'![{target}](/raw/{target})'
        elif ext == ".pdf":
            return f'[{target}](#pdf:{target})'
        return f'[{target}](#file:{target})'

    def replace_link(m):
        target = m.group(1)
        display = m.group(2) if m.group(2) else target
        ext = Path(target).suffix.lower()
        if ext == ".pdf":
            return f'[{display}](#pdf:{target})'
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
            return f'[{display}](#img:{target})'

        if not ext:
            target = target + ".md"
        return f'[{display}](#note:{target})'


    text = re.sub(r'!\[\[([^\]|]+?)(?:\|[^\]]*?)?\]\]', replace_embed, text)
    text = re.sub(r'\[\[([^\]|]+?)(?:\|([^\]]*?))?\]\]', replace_link, text)
    return text


def auto_link_urls(text: str) -> str:
    return re.sub(
        r'(?<!\]\()(?<!\()(https?://[^\s<>\)\]]+)',
        lambda m: f'[{m.group(1)}]({m.group(1)})',
        text,
    )


def render_markdown(text: str) -> str:
    text = process_wikilinks(text)
    text = auto_link_urls(text)
    extensions = ["fenced_code", "tables", "toc", "sane_lists", "nl2br"]
    try:
        import pygments
        extensions.append("codehilite")
    except ImportError:
        pass
    html = markdown.markdown(text, extensions=extensions)
    html = re.sub(
        r'<a href="(https?://[^"]+)"',
        r'<a href="\1" target="_blank" rel="noopener noreferrer"',
        html,
    )
    html = re.sub(
        r'<a href="(/raw/[^"]+)"(?![^>]*target=)',
        r'<a href="\1" target="_blank" rel="noopener noreferrer"',
        html,
    )
    return html


@app.route("/")
def index():
    return render_template_string(MAIN_TEMPLATE)


@app.route("/api/config")
def api_config():
    return jsonify({
        "allow_edit_all": ALLOW_EDIT_ALL,
        "allow_file_creation": ALLOW_FILE_CREATION,
        "poll_interval": POLL_INTERVAL,
    })


@app.route("/api/tree")
def api_tree():
    sid = request.cookies.get(_SESSION_COOKIE, "")
    owned = _session_files.get(sid, set())
    tree = build_tree(VAULT, owned=owned)
    return jsonify(tree)


@app.route("/api/check")
def api_check():
    note_path = request.args.get("note")
    note_mtime = None
    if note_path:
        fpath = resolve_vault_path(note_path)
        if fpath and fpath.is_file():
            try:
                note_mtime = fpath.stat().st_mtime
            except OSError:
                pass
    return jsonify({"note_mtime": note_mtime, "tree_hash": _get_tree_hash()})


@app.route("/api/note/<path:note_path>", methods=["GET", "PUT", "DELETE"])
def api_note(note_path):
    if request.method == "PUT":
        return _api_note_put(note_path)
    if request.method == "DELETE":
        return _api_note_delete(note_path)
    fpath = resolve_vault_path(note_path)
    if fpath is None or not fpath.is_file():
        abort(404)
    try:
        fpath.resolve().relative_to(VAULT.resolve())
    except ValueError:
        abort(403)
    text = fpath.read_text(errors="replace")
    html = render_markdown(text)
    mtime = fpath.stat().st_mtime
    rel_str = str(fpath.relative_to(VAULT))
    return jsonify({"html": html, "path": rel_str, "name": fpath.stem,
                    "mtime": mtime, "editable": _sid_owns(rel_str)})


def _api_note_put(note_path: str):
    fpath = _safe_vault_path(note_path)
    if fpath is None or not fpath.is_file():
        abort(404)
    rel_str = str(fpath.relative_to(VAULT))
    if not _sid_owns(rel_str):
        return jsonify({"error": "You can only edit files you created"}), 403
    body = request.get_json(silent=True) or {}
    content = body.get("content", "")
    if not isinstance(content, str):
        abort(400)
    fpath.write_text(content, encoding="utf-8")
    _invalidate_tree_cache()
    return jsonify({"ok": True, "path": rel_str, "mtime": fpath.stat().st_mtime})


def _api_note_delete(note_path: str):
    fpath = _safe_vault_path(note_path)
    if fpath is None or not fpath.is_file():
        abort(404)
    rel_str = str(fpath.relative_to(VAULT))
    if not _sid_owns(rel_str):
        return jsonify({"error": "You can only delete files you created"}), 403
    fpath.unlink()
    sid = request.cookies.get(_SESSION_COOKIE, "")
    if sid in _session_files:
        _session_files[sid].discard(rel_str)
    _invalidate_tree_cache()
    return jsonify({"ok": True})


@app.route("/api/note-raw/<path:note_path>")
def api_note_raw(note_path):
    fpath = resolve_vault_path(note_path)
    if fpath is None or not fpath.is_file():
        abort(404)
    try:
        fpath.resolve().relative_to(VAULT.resolve())
    except ValueError:
        abort(403)
    rel_str = str(fpath.relative_to(VAULT))
    if not _sid_owns(rel_str):
        return jsonify({"error": "Not editable"}), 403
    return jsonify({"content": fpath.read_text(errors="replace"), "path": rel_str})


@app.route("/api/files/new", methods=["POST"])
def api_files_new():
    if not ALLOW_FILE_CREATION:
        return jsonify({"ok": False, "error": "File creation is disabled"}), 403
    body = request.get_json(silent=True) or {}
    raw_path = body.get("path", "").strip()
    if not raw_path:
        return jsonify({"ok": False, "error": "Path is required"}), 400
    if not raw_path.endswith(".md"):
        raw_path += ".md"
    parts = [p for p in raw_path.replace("\\", "/").split("/") if p]
    clean_parts = [secure_filename(p) for p in parts]
    if not all(clean_parts):
        return jsonify({"ok": False, "error": "Invalid path"}), 400
    clean_path = str(Path(*clean_parts))
    fpath = _safe_vault_path(clean_path)
    if fpath is None:
        return jsonify({"ok": False, "error": "Invalid or forbidden path"}), 400
    if fpath.exists():
        return jsonify({"ok": False, "error": "File already exists"}), 409
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text("", encoding="utf-8")
    rel_str = str(fpath.relative_to(VAULT))
    sid, needs_set = _get_or_create_sid()
    _session_files[sid].add(rel_str)
    _invalidate_tree_cache()
    resp = make_response(jsonify({"ok": True, "path": rel_str}))
    if needs_set:
        resp.set_cookie(_SESSION_COOKIE, sid, samesite="Lax", httponly=True)
    return resp


@app.route("/api/files/upload", methods=["POST"])
def api_files_upload():
    if not ALLOW_FILE_CREATION:
        return jsonify({"ok": False, "error": "File uploads are disabled"}), 403
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "No filename"}), 400
    filename = secure_filename(f.filename)
    if not filename:
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return jsonify({"ok": False, "error": f"File type {ext} not allowed"}), 400
    folder = request.form.get("folder", "").strip()
    if folder:
        folder_parts = [secure_filename(p) for p in folder.replace("\\", "/").split("/") if p]
        if not all(folder_parts):
            return jsonify({"ok": False, "error": "Invalid folder"}), 400
        dest_rel = str(Path(*folder_parts) / filename)
    else:
        dest_rel = filename
    fpath = _safe_vault_path(dest_rel)
    if fpath is None:
        return jsonify({"ok": False, "error": "Forbidden path"}), 400
    if fpath.exists():
        stem = Path(filename).stem
        counter = 1
        while fpath.exists():
            new_name = f"{stem}_{counter}{ext}"
            dest_rel = str(Path(fpath.parent.relative_to(VAULT)) / new_name) if folder else new_name
            fpath = _safe_vault_path(dest_rel)
            if fpath is None:
                return jsonify({"ok": False, "error": "Conflict"}), 409
            counter += 1
    fpath.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(fpath))
    rel_str = str(fpath.relative_to(VAULT))
    sid, needs_set = _get_or_create_sid()
    _session_files[sid].add(rel_str)
    _invalidate_tree_cache()
    resp = make_response(jsonify({"ok": True, "path": rel_str}))
    if needs_set:
        resp.set_cookie(_SESSION_COOKIE, sid, samesite="Lax", httponly=True)
    return resp


@app.route("/api/download-all")
def api_download_all():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(VAULT.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(VAULT)
            if any(part in EXCLUDED_DIRS for part in rel.parts):
                continue
            if p.name in EXCLUDED_FILES:
                continue
            zf.write(p, str(rel))
    buf.seek(0)
    from datetime import datetime
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=f"notes_{stamp}.zip")


@app.route("/raw/<path:file_path>")
def raw_file(file_path):
    fpath = resolve_vault_path(file_path)
    if fpath is None or not fpath.is_file():
        abort(404)
    try:
        fpath.resolve().relative_to(VAULT.resolve())
    except ValueError:
        abort(403)
    mime, _ = mimetypes.guess_type(str(fpath))
    return send_file(fpath, mimetype=mime)


@app.route("/api/graph")
def api_graph():
    nodes = {}

    edges = []
    folders_seen = set()
    wikilink_re = re.compile(r'\[\[([^\]|]+?)(?:\|[^\]]*?)?\]\]')

    for p in sorted(VAULT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(VAULT)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if p.name in EXCLUDED_FILES:
            continue
        rel_str = str(rel)
        ext = p.suffix.lower()
        if ext == ".md":
            group = "note"
        elif ext == ".pdf":
            group = "pdf"
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
            group = "image"
        else:
            group = "other"
        display = p.stem if ext == ".md" else p.name
        nodes[rel_str] = {"id": rel_str, "name": display, "path": rel_str, "group": group}

        parent = rel.parent
        child_path = rel_str
        while str(parent) != ".":
            folder_str = str(parent)
            if folder_str not in folders_seen:
                folders_seen.add(folder_str)
                nodes[folder_str] = {
                    "id": folder_str, "name": parent.name,
                    "path": folder_str, "group": "folder"
                }
            edges.append({"source": folder_str, "target": child_path, "kind": "folder"})
            child_path = folder_str
            parent = parent.parent

    for rel_str, node in list(nodes.items()):
        if node["group"] != "note":
            continue
        fpath = VAULT / rel_str
        try:
            text = fpath.read_text(errors="replace")
        except OSError:
            continue
        for m in wikilink_re.finditer(text):
            target = m.group(1)
            target_path = None
            if (VAULT / target).exists():
                target_path = target
            elif (VAULT / (target + ".md")).exists():
                target_path = target + ".md"
            else:
                tname = Path(target).name
                if not Path(tname).suffix:
                    tname += ".md"
                for candidate in VAULT.rglob(tname):
                    crel = candidate.relative_to(VAULT)
                    if not any(part in EXCLUDED_DIRS for part in crel.parts):
                        target_path = str(crel)
                        break
            if target_path and target_path in nodes and target_path != rel_str:
                edges.append({"source": rel_str, "target": target_path, "kind": "link"})

    edge_set = set()
    unique_edges = []
    for e in edges:
        key = (e["source"], e["target"])
        if key not in edge_set:
            edge_set.add(key)
            unique_edges.append(e)
    edges = unique_edges

    link_counts = {}
    for e in edges:
        link_counts[e["source"]] = link_counts.get(e["source"], 0) + 1
        link_counts[e["target"]] = link_counts.get(e["target"], 0) + 1
    for n in nodes.values():
        n["links"] = link_counts.get(n["id"], 0)

    return jsonify({"nodes": list(nodes.values()), "edges": edges})


MAIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tachylite Live</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs" type="module"></script>
<script type="module">
import * as pdfjsLib from 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs';
pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.worker.min.mjs';
window.pdfjsLib = pdfjsLib;
</script>
<style>

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg-primary: #1a1a2e;
  --bg-secondary: #16162a;
  --bg-tertiary: #1f1f3a;
  --bg-hover: rgba(134,112,255,.08);
  --bg-active: rgba(134,112,255,.15);
  --text: #e0def4;
  --text-muted: #908caa;
  --text-faint: #6e6a86;
  --accent: #8673ff;
  --accent-hover: #a48fff;
  --accent-dim: rgba(134,112,255,.35);
  --border: rgba(255,255,255,.06);
  --border-strong: rgba(255,255,255,.1);
  --tag-bg: rgba(134,112,255,.12);
  --sidebar-width: 280px;
  --topbar-height: 38px;
  --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', Roboto, Oxygen, Ubuntu, sans-serif;
  --font-mono: 'Fira Code', 'JetBrains Mono', 'Source Code Pro', 'Consolas', monospace;
  --radius: 4px;
}

html, body { height: 100%; background: var(--bg-primary); color: var(--text); font-family: var(--font); font-size: 16px; line-height: 1.6; }
::selection { background: var(--accent-dim); }


.app { display: flex; height: 100vh; overflow: hidden; }

.sidebar {
  width: var(--sidebar-width);
  min-width: 180px;
  background: var(--bg-secondary);
  border-right: none;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: margin-left .2s;
  position: relative;
}
.sidebar.collapsed { margin-left: calc(-1 * var(--sidebar-width)); }

.sidebar-resize-handle {
  position: absolute;
  top: 0;
  right: 0;
  width: 3px;
  height: 100%;
  cursor: col-resize;
  background: transparent;
  z-index: 10;
  transition: background .15s;
}
.sidebar-resize-handle:hover,
.sidebar-resize-handle.active {
  background: var(--accent);
}

.sidebar-header {
  padding: 10px 14px;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: .08em;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
}
.sidebar-header svg { opacity: .5; }

.sidebar-header-actions {
  margin-left: auto;
  display: flex;
  gap: 2px;
}
.sidebar-btn {
  background: none;
  border: none;
  color: var(--text-faint);
  cursor: pointer;
  padding: 4px;
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  transition: background .15s, color .15s;
}
.sidebar-btn:hover { background: var(--bg-hover); color: var(--text); }

.file-tree {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.file-tree::-webkit-scrollbar { width: 5px; }
.file-tree::-webkit-scrollbar-track { background: transparent; }
.file-tree::-webkit-scrollbar-thumb { background: rgba(255,255,255,.08); border-radius: 10px; }
.file-tree::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.14); }

.tree-item {
  display: flex;
  align-items: center;
  padding: 2px 10px 2px calc(var(--depth, 0) * 16px + 10px);
  cursor: pointer;
  font-size: 13px;
  color: var(--text-muted);
  border-radius: var(--radius);
  margin: 1px 6px;
  white-space: nowrap;
  text-overflow: ellipsis;
  overflow: hidden;
  user-select: none;
  transition: background .12s, color .12s;
  position: relative;
}
.tree-item:hover { background: var(--bg-hover); color: var(--text); }
.tree-item.active { background: var(--bg-active); color: var(--accent-hover); }

.tree-icon {
  width: 15px;
  height: 15px;
  margin-right: 6px;
  flex-shrink: 0;
  opacity: .55;
}
.tree-item:hover .tree-icon { opacity: .8; }
.tree-item.active .tree-icon { opacity: 1; }

.tree-chevron {
  width: 14px;
  height: 14px;
  margin-right: 2px;
  flex-shrink: 0;
  opacity: .35;
  transition: transform .15s, opacity .15s;
}
.tree-item:hover .tree-chevron { opacity: .7; }
.tree-chevron.open { transform: rotate(90deg); opacity: .6; }

.tree-children { display: none; }
.tree-children.open { display: block; }

.tree-editable-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  flex-shrink: 0;
  margin-left: auto;
  opacity: .6;
}


.main { flex: 1; display: flex; flex-direction: column; min-width: 0; background: var(--bg-primary); }

.topbar {
  height: var(--topbar-height);
  min-height: var(--topbar-height);
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 12px;
  gap: 10px;
  -webkit-app-region: drag;
}
.topbar-toggle {
  background: none;
  border: none;
  color: var(--text-faint);
  cursor: pointer;
  padding: 4px;
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  -webkit-app-region: no-drag;
  transition: background .12s, color .12s;
}
.topbar-toggle:hover { background: var(--bg-hover); color: var(--text); }

.breadcrumb {
  font-size: 12px;
  color: var(--text-faint);
  display: flex;
  align-items: center;
  gap: 4px;
  overflow: hidden;
}
.breadcrumb span { color: var(--text-faint); font-weight: 400; }
.breadcrumb .crumb-active { color: var(--text-muted); font-weight: 500; }

.topbar-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
  align-items: center;
  -webkit-app-region: no-drag;
}
.topbar-btn {
  background: none;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  color: var(--text-muted);
  cursor: pointer;
  font-size: 12px;
  font-family: var(--font);
  padding: 3px 10px;
  transition: background .12s, color .12s, border-color .12s;
}
.topbar-btn:hover { background: var(--bg-hover); color: var(--text); border-color: var(--accent-dim); }
.topbar-btn.danger { border-color: rgba(235,111,146,.4); color: #eb6f92; }
.topbar-btn.danger:hover { background: rgba(235,111,146,.1); }
.topbar-btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.topbar-btn.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }

.content-area {
  flex: 1;
  overflow-y: auto;
  padding: 48px 56px;
  max-width: 100%;
  scroll-behavior: smooth;
}


.topbar-btn.save-success {
  background: #2e7d32 !important;
  border-color: #2e7d32 !important;
  color: #fff !important;
  transition: none;
}
.content-area::-webkit-scrollbar { width: 6px; }
.content-area::-webkit-scrollbar-track { background: transparent; }
.content-area::-webkit-scrollbar-thumb { background: rgba(255,255,255,.07); border-radius: 10px; }
.content-area::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.13); }


.welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-faint);
  gap: 16px;
}
.welcome svg { opacity: .2; }
.welcome p { font-size: 14px; color: var(--text-faint); }


.markdown-body { max-width: 750px; margin: 0 auto; color: var(--text); }

.markdown-body h1 { font-size: 1.9em; font-weight: 700; margin: 0 0 20px; padding-bottom: 10px; border-bottom: 1px solid var(--border); color: var(--text); letter-spacing: -.01em; }
.markdown-body h2 { font-size: 1.45em; font-weight: 600; margin: 32px 0 12px; color: var(--text); }
.markdown-body h3 { font-size: 1.2em; font-weight: 600; margin: 24px 0 8px; color: var(--text); }
.markdown-body h4, .markdown-body h5, .markdown-body h6 { font-size: 1.05em; font-weight: 600; margin: 20px 0 8px; color: var(--text); }

.markdown-body p { margin: 0 0 16px; line-height: 1.7; }
.markdown-body a { color: var(--accent); text-decoration: none; border-bottom: 1px solid transparent; transition: border-color .15s; }
.markdown-body a:hover { color: var(--accent-hover); border-bottom-color: var(--accent-hover); }

.markdown-body ul, .markdown-body ol { margin: 0 0 16px; padding-left: 2em; }
.markdown-body li { margin-bottom: 4px; line-height: 1.65; }
.markdown-body li input[type="checkbox"] { margin-right: 6px; accent-color: var(--accent); }

.markdown-body blockquote {
  border-left: 2px solid var(--accent-dim);
  padding: 4px 16px;
  margin: 0 0 16px;
  color: var(--text-muted);
  background: rgba(134,112,255,.04);
  border-radius: 0 var(--radius) var(--radius) 0;
}

.markdown-body code {
  font-family: var(--font-mono);
  background: var(--bg-tertiary);
  padding: 2px 6px;
  border-radius: var(--radius);
  font-size: .85em;
  color: #c4a7e7;
}
.markdown-body pre {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 16px;
  overflow-x: auto;
  margin: 0 0 16px;
}
.markdown-body pre code {
  background: none;
  padding: 0;
  font-size: .85em;
  line-height: 1.55;
  color: var(--text);
}

.markdown-body table { border-collapse: collapse; width: 100%; margin: 0 0 16px; }
.markdown-body th, .markdown-body td {
  border: 1px solid var(--border-strong);
  padding: 8px 12px;
  text-align: left;
  font-size: .95em;
}
.markdown-body th { background: var(--bg-tertiary); font-weight: 600; color: var(--text-muted); }
.markdown-body tr:nth-child(even) { background: rgba(255,255,255,.015); }

.markdown-body img { max-width: 100%; border-radius: 6px; margin: 8px 0; }
.markdown-body hr { border: none; border-top: 1px solid var(--border); margin: 28px 0; }

.markdown-body .task-list-item { list-style: none; margin-left: -1.5em; }


.markdown-body pre code .kw, .markdown-body pre .kw { color: #c4a7e7; }
.markdown-body pre code .st, .markdown-body pre .st { color: #9ccfd8; }
.markdown-body pre code .co, .markdown-body pre .co { color: #6e6a86; font-style: italic; }


.editor-wrap {
  max-width: 750px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--topbar-height) - 80px);
  width: 100%;
}
.editor-toolbar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 6px 8px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-bottom: none;
  border-radius: 6px 6px 0 0;
  flex-wrap: wrap;
}
.editor-toolbar-sep {
  width: 1px;
  height: 20px;
  background: var(--border-strong);
  margin: 0 4px;
  flex-shrink: 0;
}
.editor-tool-btn {
  background: none;
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--text-faint);
  cursor: pointer;
  width: 30px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background .12s, color .12s, border-color .12s;
  font-family: var(--font);
  font-size: 13px;
  font-weight: 600;
  position: relative;
}
.editor-tool-btn:hover { background: var(--bg-hover); color: var(--text); border-color: var(--border-strong); }
.editor-tool-btn:active { background: var(--bg-active); }
.editor-tool-btn svg { width: 15px; height: 15px; }
.editor-tool-btn[title]:hover::after {
  content: attr(title);
  position: absolute;
  bottom: -28px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--bg-primary);
  border: 1px solid var(--border-strong);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 400;
  padding: 2px 8px;
  border-radius: var(--radius);
  white-space: nowrap;
  z-index: 10;
  pointer-events: none;
}
.editor-shortcut {
  font-size: 10px;
  color: var(--text-faint);
  margin-left: auto;
  padding-left: 12px;
  white-space: nowrap;
  opacity: .6;
}
.editor-textarea {
  flex: 1;
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 0 0 6px 6px;
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 14px;
  line-height: 1.7;
  padding: 16px;
  resize: none;
  outline: none;
  width: 100%;
  caret-color: var(--accent);
  tab-size: 4;
}
.editor-textarea:focus { border-color: var(--accent-dim); box-shadow: 0 0 0 2px rgba(134,112,255,.1); }


.modal-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,.7);
  backdrop-filter: blur(4px);
  z-index: 200;
  align-items: center;
  justify-content: center;
}
.modal-overlay.visible { display: flex; }
.modal {
  background: var(--bg-secondary);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  padding: 24px;
  width: 420px;
  max-width: 90vw;
  display: flex;
  flex-direction: column;
  gap: 16px;
  box-shadow: 0 20px 60px rgba(0,0,0,.5);
}
.modal h3 { font-size: 14px; font-weight: 600; color: var(--text); }
.modal input[type="text"] {
  background: var(--bg-tertiary);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  color: var(--text);
  font-size: 14px;
  font-family: var(--font);
  padding: 8px 12px;
  width: 100%;
  outline: none;
  caret-color: var(--accent);
}
.modal input[type="text"]:focus { border-color: var(--accent-dim); box-shadow: 0 0 0 2px rgba(134,112,255,.1); }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; }
.modal-btn {
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  font-family: var(--font);
  padding: 6px 16px;
  transition: background .12s;
}
.modal-btn.primary { background: var(--accent); color: #fff; }
.modal-btn.primary:hover { background: var(--accent-hover); }
.modal-btn.ghost { background: var(--bg-tertiary); color: var(--text-muted); }
.modal-btn.ghost:hover { background: var(--bg-hover); color: var(--text); }

#uploadInput { display: none; }


.pdf-viewer {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--topbar-height));
  width: 100%;
}
.pdf-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.pdf-toolbar .pdf-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.pdf-nav {
  display: flex;
  align-items: center;
  gap: 4px;
}
.pdf-nav-btn {
  background: none;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  color: var(--text-faint);
  cursor: pointer;
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background .12s, color .12s, border-color .12s;
}
.pdf-nav-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); border-color: var(--accent-dim); }
.pdf-nav-btn:disabled { opacity: .25; cursor: default; }
.pdf-nav-btn svg { width: 13px; height: 13px; }
.pdf-page-info {
  font-size: 12px;
  color: var(--text-faint);
  white-space: nowrap;
  min-width: 30px;
  text-align: center;
}
.pdf-page-input {
  background: var(--bg-tertiary);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  color: var(--text);
  font-size: 12px;
  font-family: var(--font);
  width: 40px;
  padding: 2px 4px;
  text-align: center;
  outline: none;
  caret-color: var(--accent);
}
.pdf-page-input:focus { border-color: var(--accent-dim); }
.pdf-zoom {
  display: flex;
  align-items: center;
  gap: 3px;
}
.pdf-zoom-label {
  font-size: 11px;
  color: var(--text-faint);
  min-width: 36px;
  text-align: center;
}
.pdf-toolbar-actions {
  margin-left: auto;
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
.pdf-toolbar-btn {
  background: none;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  color: var(--text-faint);
  cursor: pointer;
  font-size: 12px;
  font-family: var(--font);
  padding: 3px 10px;
  display: flex;
  align-items: center;
  gap: 5px;
  text-decoration: none;
  transition: background .12s, color .12s, border-color .12s;
}
.pdf-toolbar-btn:hover { background: var(--bg-hover); color: var(--text); border-color: var(--accent-dim); }
.pdf-toolbar-btn svg { width: 13px; height: 13px; }
.pdf-canvas-container {
  flex: 1;
  overflow: auto;
  background: #111122;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 24px 0;
  gap: 20px;
}
.pdf-canvas-container::-webkit-scrollbar { width: 6px; }
.pdf-canvas-container::-webkit-scrollbar-track { background: transparent; }
.pdf-canvas-container::-webkit-scrollbar-thumb { background: rgba(255,255,255,.07); border-radius: 10px; }
.pdf-canvas-container::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.13); }
.pdf-page-wrapper {
  box-shadow: 0 4px 20px rgba(0,0,0,.6);
  line-height: 0;
  flex-shrink: 0;
  border-radius: 2px;
  overflow: hidden;
}
.pdf-page-wrapper canvas {
  display: block;
  transition: filter .25s;
}
.pdf-canvas-container.pdf-darkmode .pdf-page-wrapper canvas {
  filter: invert(.88) hue-rotate(180deg) contrast(.9) brightness(.95);
}
.pdf-darkmode-btn.active {
  background: var(--bg-active) !important;
  color: var(--accent-hover) !important;
  border-color: var(--accent-dim) !important;
}
.pdf-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-faint);
  font-size: 13px;
  gap: 10px;
}
.pdf-loading-spinner {
  width: 18px;
  height: 18px;
  border: 2px solid var(--border-strong);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: pdf-spin .8s linear infinite;
}
@keyframes pdf-spin { to { transform: rotate(360deg); } }

.sidebar {
  background: linear-gradient(180deg, var(--bg-secondary) 0%, #131326 100%);
}


.graph-viewer {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--topbar-height));
  width: 100%;
  position: relative;
}
.graph-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  z-index: 2;
}
.graph-toolbar .graph-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-muted);
}
.graph-toolbar .graph-stats {
  font-size: 11px;
  color: var(--text-faint);
}
.graph-toolbar-actions {
  margin-left: auto;
  display: flex;
  gap: 4px;
}
.graph-toolbar-btn {
  background: none;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  color: var(--text-faint);
  cursor: pointer;
  font-size: 12px;
  font-family: var(--font);
  padding: 3px 10px;
  display: flex;
  align-items: center;
  gap: 5px;
  transition: background .12s, color .12s, border-color .12s;
}
.graph-toolbar-btn:hover { background: var(--bg-hover); color: var(--text); border-color: var(--accent-dim); }
.graph-toolbar-btn.active { background: var(--bg-active); color: var(--accent-hover); border-color: var(--accent-dim); }
.graph-toolbar-btn svg { width: 13px; height: 13px; }
.graph-canvas-wrap {
  flex: 1;
  position: relative;
  overflow: hidden;
  background: #0e0e1e;
  background-image:
    radial-gradient(ellipse at 30% 40%, rgba(134,112,255,.06) 0%, transparent 50%),
    radial-gradient(ellipse at 70% 70%, rgba(156,207,216,.04) 0%, transparent 50%);
}
.graph-canvas-wrap canvas {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
}
.graph-tooltip {
  position: absolute;
  background: var(--bg-secondary);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--text);
  pointer-events: none;
  opacity: 0;
  transition: opacity .15s;
  z-index: 5;
  white-space: nowrap;
  box-shadow: 0 4px 16px rgba(0,0,0,.5);
}
.graph-tooltip.visible { opacity: 1; }
.graph-tooltip .gt-path {
  font-size: 10px;
  color: var(--text-faint);
  margin-top: 2px;
}


.img-viewer {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--topbar-height));
  width: 100%;
}
.img-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.img-toolbar .img-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.img-toolbar .img-meta {
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
  margin-left: 4px;
}
.img-toolbar-actions {
  margin-left: auto;
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
.img-toolbar-btn {
  background: none;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  color: var(--text-faint);
  cursor: pointer;
  font-size: 12px;
  font-family: var(--font);
  padding: 3px 10px;
  display: flex;
  align-items: center;
  gap: 5px;
  text-decoration: none;
  transition: background .12s, color .12s, border-color .12s;
}
.img-toolbar-btn:hover { background: var(--bg-hover); color: var(--text); border-color: var(--accent-dim); }
.img-toolbar-btn svg { width: 13px; height: 13px; }
.img-toolbar-btn.active {
  background: var(--bg-active);
  color: var(--accent-hover);
  border-color: var(--accent-dim);
}
.img-container {
  flex: 1;
  overflow: auto;
  background: #111122;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  cursor: grab;
}
.img-container:active { cursor: grabbing; }
.img-container::-webkit-scrollbar { width: 6px; height: 6px; }
.img-container::-webkit-scrollbar-track { background: transparent; }
.img-container::-webkit-scrollbar-thumb { background: rgba(255,255,255,.07); border-radius: 10px; }
.img-container img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  transition: transform .2s ease;
  user-select: none;
  -webkit-user-drag: none;
}
.img-container.zoomed {
  align-items: flex-start;
  justify-content: flex-start;
  cursor: grab;
}
.img-container.zoomed img {
  max-width: none;
  max-height: none;
}
.img-checker-bg {
  background-image:
    linear-gradient(45deg, #1a1a2e 25%, transparent 25%),
    linear-gradient(-45deg, #1a1a2e 25%, transparent 25%),
    linear-gradient(45deg, transparent 75%, #1a1a2e 75%),
    linear-gradient(-45deg, transparent 75%, #1a1a2e 75%);
  background-size: 20px 20px;
  background-position: 0 0, 0 10px, 10px -10px, -10px 0px;
  background-color: #151530;
}
.img-zoom-controls {
  display: flex;
  align-items: center;
  gap: 3px;
}
.img-zoom-btn {
  background: none;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  color: var(--text-faint);
  cursor: pointer;
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background .12s, color .12s, border-color .12s;
}
.img-zoom-btn:hover { background: var(--bg-hover); color: var(--text); border-color: var(--accent-dim); }
.img-zoom-btn svg { width: 13px; height: 13px; }
.img-zoom-label {
  font-size: 11px;
  color: var(--text-faint);
  min-width: 36px;
  text-align: center;
}


@media (max-width: 768px) {
  :root { --sidebar-width: 260px; }
  .content-area { padding: 20px 16px; }
  .sidebar { position: fixed; z-index: 100; height: 100vh; top: 0; left: 0; box-shadow: 8px 0 30px rgba(0,0,0,.6); }
  .sidebar.collapsed { margin-left: calc(-1 * var(--sidebar-width)); box-shadow: none; }
  .overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 99; }
  .overlay.visible { display: block; }
  .editor-wrap { height: calc(100vh - var(--topbar-height) - 40px); }
}
</style>
</head>
<body>
<div class="app">
  <!-- Sidebar -->
  <nav class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
      Tachylite <span style="font-weight:400;opacity:.6;font-size:10px">LIVE</span>
      <span id="fileCount" style="font-size:10px;font-weight:400;opacity:.5;margin-left:2px"></span>
      <div class="sidebar-header-actions">
        <button class="sidebar-btn" id="newNoteBtn" title="New note">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        </button>
        <button class="sidebar-btn" id="uploadBtn" title="Upload file">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0018 9h-1.26A8 8 0 103 16.3"/></svg>
        </button>
        <button class="sidebar-btn" id="graphViewBtn" title="Graph view">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="8" r="2"/><circle cx="12" cy="18" r="2"/><line x1="7.5" y1="7.5" x2="10.5" y2="16.5"/><line x1="16.5" y1="9.5" x2="13.5" y2="16.5"/><line x1="8" y1="6.5" x2="16" y2="7.5"/></svg>
        </button>
        <button class="sidebar-btn" id="downloadAllBtn" title="Download all">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="8 17 12 21 16 17"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.88 18.09A5 5 0 0018 9h-1.26A8 8 0 103 16.29"/></svg>
        </button>
      </div>
    </div>
    <input type="file" id="uploadInput">
    <div class="file-tree" id="fileTree"></div>
    <div class="sidebar-resize-handle" id="sidebarResize"></div>
  </nav>
  <div class="overlay" id="overlay"></div>

  <!-- New Note Modal -->
  <div class="modal-overlay" id="newNoteModal">
    <div class="modal">
      <h3>New Note</h3>
      <input type="text" id="newNotePath" placeholder="e.g. Projects/my-idea.md" autocomplete="off">
      <div class="modal-actions">
        <button class="modal-btn ghost" id="newNoteCancel">Cancel</button>
        <button class="modal-btn primary" id="newNoteConfirm">Create</button>
      </div>
    </div>
  </div>

  <!-- Main -->
  <div class="main">
    <div class="topbar">
      <button class="topbar-toggle" id="toggleSidebar" title="Toggle sidebar">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
      </button>
      <div class="breadcrumb" id="breadcrumb">
        <span>Select a note from the sidebar</span>
      </div>
      <div class="topbar-actions" id="topbarActions"></div>
    </div>
    <div class="content-area" id="contentArea">
      <div class="welcome">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
        <p>Select a note from the sidebar</p>
        <div style="font-size:12px;color:var(--text-faint);margin-top:8px;line-height:1.8;text-align:center">
          <span style="background:var(--bg-tertiary);padding:2px 8px;border-radius:3px;margin:0 2px">Ctrl+N</span> new note &nbsp;&middot;&nbsp;
          <span style="background:var(--bg-tertiary);padding:2px 8px;border-radius:3px;margin:0 2px">Ctrl+S</span> save
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const fileTree = $('#fileTree');
const contentArea = $('#contentArea');
const breadcrumb = $('#breadcrumb');
const sidebar = $('#sidebar');
const overlay = $('#overlay');
const topbarActions = $('#topbarActions');
const newNoteModal = $('#newNoteModal');
const newNotePath = $('#newNotePath');
const uploadInput = $('#uploadInput');
let serverConfig = { allow_edit_all: false, allow_file_creation: true, poll_interval: 15 };
fetch('/api/config').then(r => r.json()).then(cfg => {
  serverConfig = cfg;
  if (!cfg.allow_file_creation) {
    $('#newNoteBtn').style.display = 'none';
    $('#uploadBtn').style.display = 'none';
  }
  startPolling(cfg.poll_interval || 15);
}).catch(() => { startPolling(15); });
$('#toggleSidebar').addEventListener('click', () => {
  sidebar.classList.toggle('collapsed');
  overlay.classList.toggle('visible', !sidebar.classList.contains('collapsed') && window.innerWidth <= 768);
});
overlay.addEventListener('click', () => {
  sidebar.classList.add('collapsed');
  overlay.classList.remove('visible');
});
(() => {
  const handle = $('#sidebarResize');
  let dragging = false;
  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    dragging = true;
    handle.classList.add('active');
    sidebar.style.transition = 'none';
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const newWidth = Math.min(Math.max(e.clientX, 180), 600);
    sidebar.style.width = newWidth + 'px';
    sidebar.style.minWidth = newWidth + 'px';
    document.documentElement.style.setProperty('--sidebar-width', newWidth + 'px');
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('active');
    sidebar.style.transition = '';
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
})();
function iconSvg(type, name) {
  if (type === 'folder') {
    return '<svg class="tree-icon" viewBox="0 0 24 24" fill="none" stroke="#908caa" stroke-width="2"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>';
  }
  const ext = name.split('.').pop().toLowerCase();
  if (ext === 'pdf') {
    return '<svg class="tree-icon" viewBox="0 0 24 24" fill="none" stroke="#eb6f92" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
  }
  if (['png','jpg','jpeg','gif','svg','webp'].includes(ext)) {
    return '<svg class="tree-icon" viewBox="0 0 24 24" fill="none" stroke="#9ccfd8" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>';
  }
  return '<svg class="tree-icon" viewBox="0 0 24 24" fill="none" stroke="#6e6a86" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
}

function chevronSvg() {
  return '<svg class="tree-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>';
}

function renderTree(items, container, depth = 0) {
  items.forEach(item => {
    if (item.type === 'folder') {
      const row = document.createElement('div');
      row.className = 'tree-item';
      row.style.setProperty('--depth', depth);
      row.innerHTML = chevronSvg() + iconSvg('folder', item.name) + `<span>${esc(item.name)}</span>`;

      const children = document.createElement('div');
      children.className = 'tree-children';

      row.addEventListener('click', () => {
        const chev = row.querySelector('.tree-chevron');
        chev.classList.toggle('open');
        children.classList.toggle('open');
      });

      container.appendChild(row);
      container.appendChild(children);
      if (item.children) renderTree(item.children, children, depth + 1);
    } else {
      const row = document.createElement('div');
      row.className = 'tree-item';
      row.dataset.path = item.path;
      if (item.editable) row.dataset.editable = '1';
      row.style.setProperty('--depth', depth);
      const ext = item.name.split('.').pop().toLowerCase();
      const dot = item.editable ? '<span class="tree-editable-dot" title="You created this"></span>' : '';
      const displayName = item.name.endsWith('.md') ? item.name.slice(0, -3) : item.name;
      row.innerHTML = `<span style="width:16px;flex-shrink:0"></span>` + iconSvg('file', item.name) + `<span>${esc(displayName)}</span>` + dot;

      row.addEventListener('click', () => {
        if (ext === 'pdf') {
          loadPdf(item.path, item.name);
        } else if (['png','jpg','jpeg','gif','svg','webp'].includes(ext)) {
          loadImage(item.path, item.name);
        } else if (item.name.endsWith('.md')) {
          loadNote(item.path);
        } else {
          window.open('/raw/' + encodeURIPath(item.path), '_blank');
        }
      });

      container.appendChild(row);
    }
  });
}

function encodeURIPath(p) {
  return p.split('/').map(encodeURIComponent).join('/');
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
let activeItem = null;
let currentNotePath = null;
let currentNoteMtime = null;
let currentNoteEditable = false;
let editMode = false;
let lastTreeHash = null;
function updateTopbar() {
  if (editMode) {
    topbarActions.innerHTML =
      '<button class="topbar-btn primary" id="saveBtn">Save</button>' +
      '<button class="topbar-btn" id="cancelBtn">Cancel</button>';
    $('#saveBtn').addEventListener('click', saveEdit);
    $('#cancelBtn').addEventListener('click', cancelEdit);
  } else if (currentNoteEditable) {
    topbarActions.innerHTML =
      '<button class="topbar-btn" id="editBtn">Edit</button>' +
      '<button class="topbar-btn danger" id="deleteBtn">Delete</button>';
    $('#editBtn').addEventListener('click', enterEditMode);
    $('#deleteBtn').addEventListener('click', deleteNote);
  } else {
    topbarActions.innerHTML = '';
  }
}
async function loadNote(path, {preserveScroll = false} = {}) {
  contentArea.style.padding = '';
  if (activeItem) activeItem.classList.remove('active');
  const items = fileTree.querySelectorAll('.tree-item[data-path]');
  items.forEach(el => {
    if (el.dataset.path === path) { el.classList.add('active'); activeItem = el; }
  });
  const parts = path.split('/');
  breadcrumb.innerHTML = parts.map((p, i) =>
    i === parts.length - 1
      ? `<span class="crumb-active">${esc(p)}</span>`
      : `<span>${esc(p)}</span><span>/</span>`
  ).join('');

  const scrollPos = preserveScroll ? contentArea.scrollTop : null;
  try {
    const res = await fetch('/api/note/' + encodeURIPath(path));
    if (!res.ok) throw new Error('Not found');
    const data = await res.json();
    const resolvedPath = data.path || path;
    const noteTitle = resolvedPath.split('/').pop().replace(/\.md$/i, '');
    const hasH1 = data.html.trimStart().startsWith('<h1');
    const titleHtml = hasH1 ? '' : '<h1>' + esc(noteTitle) + '</h1>';
    contentArea.innerHTML = '<div class="markdown-body">' + titleHtml + data.html + '</div>';
    currentNotePath = resolvedPath;
    currentNoteMtime = data.mtime;
    currentNoteEditable = data.editable || false;
    editMode = false;
    updateTopbar();
    revealInTree(resolvedPath);
    contentArea.querySelectorAll('a[href^="#note:"]').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        const target = a.getAttribute('href').slice(6);
        loadNote(target);
        revealInTree(target);
      });
    });
    contentArea.querySelectorAll('a[href^="#pdf:"]').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        const target = a.getAttribute('href').slice(5);
        loadPdf(target, target.split('/').pop());
      });
    });
    contentArea.querySelectorAll('a[href^="#img:"]').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        const target = a.getAttribute('href').slice(5);
        loadImage(target, target.split('/').pop());
      });
    });
    contentArea.querySelectorAll('img').forEach(img => {
      const src = img.getAttribute('src');
      if (src && !src.startsWith('/') && !src.startsWith('http')) {
        img.src = '/raw/' + encodeURIPath(src);
      }
      img.style.cursor = 'pointer';
      img.addEventListener('click', e => {
        e.preventDefault();
        const imgSrc = img.getAttribute('src') || '';
        const imgPath = imgSrc.startsWith('/raw/') ? decodeURIComponent(imgSrc.slice(5)) : imgSrc;
        loadImage(imgPath, imgPath.split('/').pop());
      });
    });
    contentArea.scrollTop = scrollPos !== null ? scrollPos : 0;
    if (!preserveScroll) {
      history.pushState({ path }, '', '?note=' + encodeURIComponent(path));
    }
  } catch (e) {
    contentArea.innerHTML = '<div class="welcome"><p>Could not load note: ' + esc(path) + '</p></div>';
  }
  if (window.innerWidth <= 768 && !preserveScroll) {
    sidebar.classList.add('collapsed');
    overlay.classList.remove('visible');
  }
}

function revealInTree(path) {
  const parts = path.split('/');
  let container = fileTree;
  for (let i = 0; i < parts.length - 1; i++) {
    const folderItems = container.querySelectorAll(':scope > .tree-item');
    for (const item of folderItems) {
      if (item.textContent.trim() === parts[i]) {
        const chev = item.querySelector('.tree-chevron');
        if (chev) chev.classList.add('open');
        const next = item.nextElementSibling;
        if (next && next.classList.contains('tree-children')) {
          next.classList.add('open');
          container = next;
        }
        break;
      }
    }
  }
  const items = fileTree.querySelectorAll('.tree-item[data-path]');
  items.forEach(el => {
    if (el.dataset.path === path) {
      if (activeItem && activeItem !== el) activeItem.classList.remove('active');
      el.classList.add('active');
      activeItem = el;
      el.scrollIntoView({ block: 'nearest' });
    }
  });
}
let pdfState = { doc: null, scale: 1.5, rendering: false };

function loadPdf(path, name) {
  if (activeItem) activeItem.classList.remove('active');
  const items = fileTree.querySelectorAll('.tree-item[data-path]');
  items.forEach(el => {
    if (el.dataset.path === path) { el.classList.add('active'); activeItem = el; }
  });
  const parts = path.split('/');
  breadcrumb.innerHTML = parts.map((p, i) =>
    i === parts.length - 1
      ? `<span class="crumb-active">${esc(p)}</span>`
      : `<span>${esc(p)}</span><span>/</span>`
  ).join('');
  currentNotePath = null;
  currentNoteMtime = null;
  currentNoteEditable = false;
  editMode = false;
  topbarActions.innerHTML = '';

  const rawUrl = '/raw/' + encodeURIPath(path);
  const displayName = esc(name || path.split('/').pop());

  contentArea.innerHTML = '';
  contentArea.style.padding = '0';

  const viewer = document.createElement('div');
  viewer.className = 'pdf-viewer';

  viewer.innerHTML =
    '<div class="pdf-toolbar">' +
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#e06c75" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
      '<span class="pdf-title">' + displayName + '</span>' +
      '<div class="pdf-nav">' +
        '<button class="pdf-nav-btn" id="pdfPrev" title="Previous page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg></button>' +
        '<input class="pdf-page-input" id="pdfPageInput" type="text" value="1">' +
        '<span class="pdf-page-info" id="pdfPageInfo">/ 1</span>' +
        '<button class="pdf-nav-btn" id="pdfNext" title="Next page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 6 15 12 9 18"/></svg></button>' +
      '</div>' +
      '<div class="pdf-zoom">' +
        '<button class="pdf-nav-btn" id="pdfZoomOut" title="Zoom out"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"/></svg></button>' +
        '<span class="pdf-zoom-label" id="pdfZoomLabel">150%</span>' +
        '<button class="pdf-nav-btn" id="pdfZoomIn" title="Zoom in"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>' +
        '<button class="pdf-nav-btn" id="pdfFitWidth" title="Fit to width"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg></button>' +
      '</div>' +
      '<div class="pdf-toolbar-actions">' +
        '<button class="pdf-toolbar-btn pdf-darkmode-btn" id="pdfDarkMode" title="Toggle dark mode">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>' +
          'Dark' +
        '</button>' +
        '<a class="pdf-toolbar-btn" href="' + rawUrl + '" download title="Download">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="8 17 12 21 16 17"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.88 18.09A5 5 0 0018 9h-1.26A8 8 0 103 16.29"/></svg>' +
          'Download' +
        '</a>' +
      '</div>' +
    '</div>' +
    '<div class="pdf-canvas-container" id="pdfCanvasContainer">' +
      '<div class="pdf-loading"><div class="pdf-loading-spinner"></div>Loading PDF...</div>' +
    '</div>';

  contentArea.appendChild(viewer);

  const canvasContainer = viewer.querySelector('#pdfCanvasContainer');
  const pageInput = viewer.querySelector('#pdfPageInput');
  const pageInfo = viewer.querySelector('#pdfPageInfo');
  const zoomLabel = viewer.querySelector('#pdfZoomLabel');
  const prevBtn = viewer.querySelector('#pdfPrev');
  const nextBtn = viewer.querySelector('#pdfNext');
  const zoomInBtn = viewer.querySelector('#pdfZoomIn');
  const zoomOutBtn = viewer.querySelector('#pdfZoomOut');
  const fitWidthBtn = viewer.querySelector('#pdfFitWidth');
  const darkModeBtn = viewer.querySelector('#pdfDarkMode');
  if (localStorage.getItem('pdfDarkMode') !== '0') {
    canvasContainer.classList.add('pdf-darkmode');
    darkModeBtn.classList.add('active');
  }
  darkModeBtn.addEventListener('click', () => {
    canvasContainer.classList.toggle('pdf-darkmode');
    darkModeBtn.classList.toggle('active');
    localStorage.setItem('pdfDarkMode', canvasContainer.classList.contains('pdf-darkmode') ? '1' : '0');
  });

  let currentPage = 1;
  let totalPages = 1;
  let pdfDoc = null;
  let currentScale = 1.5;
  let fitToWidth = true;
  let renderQueued = false;

  function getFitScale() {
    if (!pdfDoc) return 1.5;
    const w = canvasContainer.clientWidth - 40;
    return w > 0 && pdfDoc._page1Width ? w / pdfDoc._page1Width : 1.5;
  }

  function effectiveScale() {
    return fitToWidth ? getFitScale() : currentScale;
  }

  function updateNav() {
    pageInput.value = currentPage;
    pageInfo.textContent = '/ ' + totalPages;
    prevBtn.disabled = currentPage <= 1;
    nextBtn.disabled = currentPage >= totalPages;
    zoomLabel.textContent = Math.round(effectiveScale() * 100) + '%';
  }

  async function renderAllPages() {
    if (!pdfDoc) return;
    canvasContainer.innerHTML = '';
    const scale = effectiveScale();
    const dpr = window.devicePixelRatio || 1;
    for (let i = 1; i <= totalPages; i++) {
      const page = await pdfDoc.getPage(i);
      const viewport = page.getViewport({ scale: scale });
      const wrapper = document.createElement('div');
      wrapper.className = 'pdf-page-wrapper';
      wrapper.dataset.page = i;
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');
      canvas.width = Math.floor(viewport.width * dpr);
      canvas.height = Math.floor(viewport.height * dpr);
      canvas.style.width = Math.floor(viewport.width) + 'px';
      canvas.style.height = Math.floor(viewport.height) + 'px';
      ctx.scale(dpr, dpr);
      wrapper.appendChild(canvas);
      canvasContainer.appendChild(wrapper);
      await page.render({ canvasContext: ctx, viewport: viewport }).promise;
    }
  }

  function scrollToPage(num) {
    const wrapper = canvasContainer.querySelector('[data-page="' + num + '"]');
    if (wrapper) wrapper.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  canvasContainer.addEventListener('scroll', () => {
    const wrappers = canvasContainer.querySelectorAll('.pdf-page-wrapper');
    const containerTop = canvasContainer.scrollTop;
    const containerMid = containerTop + canvasContainer.clientHeight / 3;
    let closest = 1;
    wrappers.forEach(w => {
      const top = w.offsetTop - canvasContainer.offsetTop;
      if (top <= containerMid) closest = parseInt(w.dataset.page);
    });
    if (closest !== currentPage) {
      currentPage = closest;
      updateNav();
    }
  });

  prevBtn.addEventListener('click', () => {
    if (currentPage > 1) { currentPage--; updateNav(); scrollToPage(currentPage); }
  });
  nextBtn.addEventListener('click', () => {
    if (currentPage < totalPages) { currentPage++; updateNav(); scrollToPage(currentPage); }
  });
  pageInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      let val = parseInt(pageInput.value);
      if (val >= 1 && val <= totalPages) { currentPage = val; updateNav(); scrollToPage(currentPage); }
      else { pageInput.value = currentPage; }
    }
  });
  zoomInBtn.addEventListener('click', async () => {
    fitToWidth = false;
    currentScale = Math.min(effectiveScale() + 0.25, 5);
    updateNav();
    await renderAllPages();
    scrollToPage(currentPage);
  });
  zoomOutBtn.addEventListener('click', async () => {
    fitToWidth = false;
    currentScale = Math.max(effectiveScale() - 0.25, 0.5);
    updateNav();
    await renderAllPages();
    scrollToPage(currentPage);
  });
  fitWidthBtn.addEventListener('click', async () => {
    fitToWidth = true;
    updateNav();
    await renderAllPages();
    scrollToPage(currentPage);
  });
  const resizeObserver = new ResizeObserver(() => {
    if (!pdfDoc || !fitToWidth) return;
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(async () => {
      updateNav();
      await renderAllPages();
      scrollToPage(currentPage);
      renderQueued = false;
    });
  });
  resizeObserver.observe(canvasContainer);
  (async () => {
    try {
      while (!window.pdfjsLib) await new Promise(r => setTimeout(r, 50));
      pdfDoc = await window.pdfjsLib.getDocument(rawUrl).promise;
      totalPages = pdfDoc.numPages;
      currentPage = 1;
      const firstPage = await pdfDoc.getPage(1);
      pdfDoc._page1Width = firstPage.getViewport({ scale: 1 }).width;

      updateNav();
      await renderAllPages();
    } catch (e) {
      canvasContainer.innerHTML = '<div class="pdf-loading">Failed to load PDF. <a href="' + rawUrl + '" target="_blank" style="color:var(--accent)">Open directly</a></div>';
    }
  })();

  history.pushState({ path, pdf: true }, '', '?pdf=' + encodeURIComponent(path));
  if (window.innerWidth <= 768) {
    sidebar.classList.add('collapsed');
    overlay.classList.remove('visible');
  }
}
function loadImage(path, name) {
  if (activeItem) activeItem.classList.remove('active');
  const items = fileTree.querySelectorAll('.tree-item[data-path]');
  items.forEach(el => {
    if (el.dataset.path === path) { el.classList.add('active'); activeItem = el; }
  });
  const parts = path.split('/');
  breadcrumb.innerHTML = parts.map((p, i) =>
    i === parts.length - 1
      ? `<span class="crumb-active">${esc(p)}</span>`
      : `<span>${esc(p)}</span><span>/</span>`
  ).join('');
  currentNotePath = null;
  currentNoteMtime = null;
  currentNoteEditable = false;
  editMode = false;
  topbarActions.innerHTML = '';

  const rawUrl = '/raw/' + encodeURIPath(path);
  const displayName = esc(name || path.split('/').pop());
  const ext = (name || path).split('.').pop().toLowerCase();

  contentArea.innerHTML = '';
  contentArea.style.padding = '0';

  const viewer = document.createElement('div');
  viewer.className = 'img-viewer';

  viewer.innerHTML =
    '<div class="img-toolbar">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#9ccfd8" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>' +
      '<span class="img-title">' + displayName + '</span>' +
      '<span class="img-meta" id="imgMeta"></span>' +
      '<div class="img-zoom-controls">' +
        '<button class="img-zoom-btn" id="imgZoomOut" title="Zoom out"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"/></svg></button>' +
        '<span class="img-zoom-label" id="imgZoomLabel">Fit</span>' +
        '<button class="img-zoom-btn" id="imgZoomIn" title="Zoom in"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>' +
        '<button class="img-zoom-btn" id="imgZoomReset" title="Fit to view"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg></button>' +
      '</div>' +
      '<div class="img-toolbar-actions">' +
        (ext === 'png' || ext === 'webp' || ext === 'gif' || ext === 'svg' ?
          '<button class="img-toolbar-btn" id="imgCheckerBg" title="Toggle transparency grid">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>' +
            'Grid' +
          '</button>' : '') +
        '<a class="img-toolbar-btn" href="' + rawUrl + '" download title="Download">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="8 17 12 21 16 17"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.88 18.09A5 5 0 0018 9h-1.26A8 8 0 103 16.29"/></svg>' +
          'Download' +
        '</a>' +
      '</div>' +
    '</div>' +
    '<div class="img-container" id="imgContainer">' +
      '<img id="imgElement" src="' + rawUrl + '" alt="' + displayName + '">' +
    '</div>';

  contentArea.appendChild(viewer);

  const imgContainer = viewer.querySelector('#imgContainer');
  const imgEl = viewer.querySelector('#imgElement');
  const imgMeta = viewer.querySelector('#imgMeta');
  const zoomLabel = viewer.querySelector('#imgZoomLabel');
  const checkerBtn = viewer.querySelector('#imgCheckerBg');

  let imgScale = 0;
  let naturalW = 0, naturalH = 0;

  imgEl.addEventListener('load', () => {
    naturalW = imgEl.naturalWidth;
    naturalH = imgEl.naturalHeight;
    imgMeta.textContent = naturalW + ' x ' + naturalH;
  });

  function applyZoom() {
    if (imgScale === 0) {
      imgContainer.classList.remove('zoomed');
      imgEl.style.transform = '';
      imgEl.style.width = '';
      imgEl.style.height = '';
      zoomLabel.textContent = 'Fit';
    } else {
      imgContainer.classList.add('zoomed');
      const w = Math.round(naturalW * imgScale);
      const h = Math.round(naturalH * imgScale);
      imgEl.style.width = w + 'px';
      imgEl.style.height = h + 'px';
      imgEl.style.transform = '';
      zoomLabel.textContent = Math.round(imgScale * 100) + '%';
    }
  }

  viewer.querySelector('#imgZoomIn').addEventListener('click', () => {
    if (imgScale === 0) {
      const cw = imgContainer.clientWidth, ch = imgContainer.clientHeight;
      imgScale = naturalW > 0 ? Math.min(cw / naturalW, ch / naturalH, 1) : 1;
    }
    imgScale = Math.min(imgScale + 0.25, 10);
    applyZoom();
  });

  viewer.querySelector('#imgZoomOut').addEventListener('click', () => {
    if (imgScale === 0) {
      const cw = imgContainer.clientWidth, ch = imgContainer.clientHeight;
      imgScale = naturalW > 0 ? Math.min(cw / naturalW, ch / naturalH, 1) : 1;
    }
    imgScale = Math.max(imgScale - 0.25, 0.1);
    applyZoom();
  });

  viewer.querySelector('#imgZoomReset').addEventListener('click', () => {
    imgScale = 0;
    applyZoom();
  });
  imgEl.addEventListener('dblclick', e => {
    e.preventDefault();
    if (imgScale === 0) {
      imgScale = 1;
    } else {
      imgScale = 0;
    }
    applyZoom();
  });
  imgContainer.addEventListener('wheel', e => {
    e.preventDefault();
    if (imgScale === 0) {
      const cw = imgContainer.clientWidth, ch = imgContainer.clientHeight;
      imgScale = naturalW > 0 ? Math.min(cw / naturalW, ch / naturalH, 1) : 1;
    }
    const delta = e.deltaY > 0 ? -0.1 : 0.1;
    imgScale = Math.min(Math.max(imgScale + delta, 0.1), 10);
    applyZoom();
  }, { passive: false });
  let panning = false, panStartX = 0, panStartY = 0, scrollStartX = 0, scrollStartY = 0;
  imgContainer.addEventListener('mousedown', e => {
    if (imgScale === 0) return;
    panning = true;
    panStartX = e.clientX;
    panStartY = e.clientY;
    scrollStartX = imgContainer.scrollLeft;
    scrollStartY = imgContainer.scrollTop;
    imgContainer.style.cursor = 'grabbing';
  });
  document.addEventListener('mousemove', e => {
    if (!panning) return;
    imgContainer.scrollLeft = scrollStartX - (e.clientX - panStartX);
    imgContainer.scrollTop = scrollStartY - (e.clientY - panStartY);
  });
  document.addEventListener('mouseup', () => {
    if (!panning) return;
    panning = false;
    imgContainer.style.cursor = '';
  });
  if (checkerBtn) {
    checkerBtn.addEventListener('click', () => {
      imgContainer.classList.toggle('img-checker-bg');
      checkerBtn.classList.toggle('active');
    });
  }

  history.pushState({ path, img: true }, '', '?img=' + encodeURIComponent(path));

  if (window.innerWidth <= 768) {
    sidebar.classList.add('collapsed');
    overlay.classList.remove('visible');
  }
}
const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
const modKey = isMac ? 'Cmd' : 'Ctrl';

function editorWrap(ta, prefix, suffix) {
  const start = ta.selectionStart, end = ta.selectionEnd;
  const sel = ta.value.substring(start, end);
  const before = ta.value.substring(0, start);
  const after = ta.value.substring(end);
  if (before.endsWith(prefix) && after.startsWith(suffix)) {
    ta.value = before.slice(0, -prefix.length) + sel + after.slice(suffix.length);
    ta.selectionStart = start - prefix.length;
    ta.selectionEnd = end - prefix.length;
  } else if (sel.startsWith(prefix) && sel.endsWith(suffix)) {
    ta.value = before + sel.slice(prefix.length, -suffix.length) + after;
    ta.selectionStart = start;
    ta.selectionEnd = end - prefix.length - suffix.length;
  } else {
    ta.value = before + prefix + sel + suffix + after;
    ta.selectionStart = start + prefix.length;
    ta.selectionEnd = end + prefix.length;
  }
  ta.focus();
}

function editorLinePrefix(ta, prefix) {
  const start = ta.selectionStart, end = ta.selectionEnd;
  const val = ta.value;
  let lineStart = val.lastIndexOf('\n', start - 1) + 1;
  let lineEnd = val.indexOf('\n', end);
  if (lineEnd === -1) lineEnd = val.length;
  const lines = val.substring(lineStart, lineEnd).split('\n');
  const allHave = lines.every(l => l.startsWith(prefix));
  const newLines = allHave
    ? lines.map(l => l.slice(prefix.length))
    : lines.map(l => prefix + l);
  const joined = newLines.join('\n');
  ta.value = val.substring(0, lineStart) + joined + val.substring(lineEnd);
  ta.selectionStart = lineStart;
  ta.selectionEnd = lineStart + joined.length;
  ta.focus();
}

function editorNumberedList(ta) {
  const start = ta.selectionStart, end = ta.selectionEnd;
  const val = ta.value;
  let lineStart = val.lastIndexOf('\n', start - 1) + 1;
  let lineEnd = val.indexOf('\n', end);
  if (lineEnd === -1) lineEnd = val.length;
  const lines = val.substring(lineStart, lineEnd).split('\n');
  const allNumbered = lines.every(l => /^\d+\.\s/.test(l));
  const newLines = allNumbered
    ? lines.map(l => l.replace(/^\d+\.\s/, ''))
    : lines.map((l, i) => (i + 1) + '. ' + l);
  const joined = newLines.join('\n');
  ta.value = val.substring(0, lineStart) + joined + val.substring(lineEnd);
  ta.selectionStart = lineStart;
  ta.selectionEnd = lineStart + joined.length;
  ta.focus();
}

function editorInsert(ta, text) {
  const start = ta.selectionStart;
  ta.value = ta.value.substring(0, start) + text + ta.value.substring(ta.selectionEnd);
  ta.selectionStart = ta.selectionEnd = start + text.length;
  ta.focus();
}

async function enterEditMode() {
  if (!currentNotePath || !currentNoteEditable) return;
  try {
    const res = await fetch('/api/note-raw/' + encodeURIPath(currentNotePath));
    if (!res.ok) throw new Error('Cannot load raw');
    const data = await res.json();
    editMode = true;
    updateTopbar();
    const wrap = document.createElement('div');
    wrap.className = 'editor-wrap';
    const toolbar = document.createElement('div');
    toolbar.className = 'editor-toolbar';
    toolbar.innerHTML =
      '<button class="editor-tool-btn" data-action="bold" title="Bold (' + modKey + '+B)"><b>B</b></button>' +
      '<button class="editor-tool-btn" data-action="italic" title="Italic (' + modKey + '+I)"><i>I</i></button>' +
      '<button class="editor-tool-btn" data-action="strikethrough" title="Strikethrough"><s>S</s></button>' +
      '<button class="editor-tool-btn" data-action="code" title="Inline code (' + modKey + '+E)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg></button>' +
      '<span class="editor-toolbar-sep"></span>' +
      '<button class="editor-tool-btn" data-action="h1" title="Heading 1">H1</button>' +
      '<button class="editor-tool-btn" data-action="h2" title="Heading 2">H2</button>' +
      '<button class="editor-tool-btn" data-action="h3" title="Heading 3">H3</button>' +
      '<span class="editor-toolbar-sep"></span>' +
      '<button class="editor-tool-btn" data-action="ul" title="Bullet list"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1" fill="currentColor"/><circle cx="4" cy="12" r="1" fill="currentColor"/><circle cx="4" cy="18" r="1" fill="currentColor"/></svg></button>' +
      '<button class="editor-tool-btn" data-action="ol" title="Numbered list"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="10" y1="6" x2="21" y2="6"/><line x1="10" y1="12" x2="21" y2="12"/><line x1="10" y1="18" x2="21" y2="18"/><text x="2" y="8" fill="currentColor" font-size="7" font-family="sans-serif" stroke="none">1</text><text x="2" y="14" fill="currentColor" font-size="7" font-family="sans-serif" stroke="none">2</text><text x="2" y="20" fill="currentColor" font-size="7" font-family="sans-serif" stroke="none">3</text></svg></button>' +
      '<button class="editor-tool-btn" data-action="checklist" title="Checklist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="4" height="4" rx="1"/><line x1="10" y1="7" x2="21" y2="7"/><rect x="3" y="15" width="4" height="4" rx="1"/><line x1="10" y1="17" x2="21" y2="17"/><polyline points="4 16 5.5 17.5 7 15" stroke-width="1.5"/></svg></button>' +
      '<span class="editor-toolbar-sep"></span>' +
      '<button class="editor-tool-btn" data-action="quote" title="Quote"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="3" y2="18"/><line x1="8" y1="8" x2="21" y2="8"/><line x1="8" y1="12" x2="18" y2="12"/><line x1="8" y1="16" x2="15" y2="16"/></svg></button>' +
      '<button class="editor-tool-btn" data-action="link" title="Link (' + modKey + '+K)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/></svg></button>' +
      '<button class="editor-tool-btn" data-action="hr" title="Horizontal rule">--</button>' +
      '<button class="editor-tool-btn" data-action="codeblock" title="Code block"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><polyline points="8 8 4 12 8 16"/><polyline points="16 8 20 12 16 16"/></svg></button>' +
      '<span class="editor-shortcut">' + modKey + '+S to save</span>';
    wrap.appendChild(toolbar);

    const ta = document.createElement('textarea');
    ta.className = 'editor-textarea';
    ta.id = 'editorTextarea';
    ta.value = data.content;
    ta.placeholder = 'Start writing...';
    wrap.appendChild(ta);
    contentArea.innerHTML = '';
    contentArea.appendChild(wrap);
    ta.focus();
    toolbar.addEventListener('click', e => {
      const btn = e.target.closest('[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      switch (action) {
        case 'bold': editorWrap(ta, '**', '**'); break;
        case 'italic': editorWrap(ta, '*', '*'); break;
        case 'strikethrough': editorWrap(ta, '~~', '~~'); break;
        case 'code': editorWrap(ta, '`', '`'); break;
        case 'h1': editorLinePrefix(ta, '# '); break;
        case 'h2': editorLinePrefix(ta, '## '); break;
        case 'h3': editorLinePrefix(ta, '### '); break;
        case 'ul': editorLinePrefix(ta, '- '); break;
        case 'ol': editorNumberedList(ta); break;
        case 'checklist': editorLinePrefix(ta, '- [ ] '); break;
        case 'quote': editorLinePrefix(ta, '> '); break;
        case 'link': {
          const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
          if (sel) {
            editorWrap(ta, '[', '](url)');
          } else {
            editorInsert(ta, '[link text](url)');
          }
          break;
        }
        case 'hr': editorInsert(ta, '\n---\n'); break;
        case 'codeblock': {
          const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
          if (sel) {
            editorWrap(ta, '```\n', '\n```');
          } else {
            editorInsert(ta, '```\n\n```');
            ta.selectionStart = ta.selectionEnd - 4;
            ta.selectionEnd = ta.selectionStart;
          }
          break;
        }
      }
    });
    ta.addEventListener('keydown', e => {
      const mod = isMac ? e.metaKey : e.ctrlKey;
      if (mod && e.key === 'b') { e.preventDefault(); editorWrap(ta, '**', '**'); }
      else if (mod && e.key === 'i') { e.preventDefault(); editorWrap(ta, '*', '*'); }
      else if (mod && e.key === 'e') { e.preventDefault(); editorWrap(ta, '`', '`'); }
      else if (mod && e.key === 'k') { e.preventDefault(); const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd); if (sel) editorWrap(ta, '[', '](url)'); else editorInsert(ta, '[link text](url)'); }
      else if (mod && e.key === 's') { e.preventDefault(); saveEdit(); }
      else if (e.key === 'Tab') {
        e.preventDefault();
        const start = ta.selectionStart, end = ta.selectionEnd;
        if (start === end) {
          ta.value = ta.value.substring(0, start) + '\t' + ta.value.substring(end);
          ta.selectionStart = ta.selectionEnd = start + 1;
        } else {
          const val = ta.value;
          let lineStart = val.lastIndexOf('\n', start - 1) + 1;
          let lineEnd = val.indexOf('\n', end);
          if (lineEnd === -1) lineEnd = val.length;
          const lines = val.substring(lineStart, lineEnd).split('\n');
          const newLines = e.shiftKey
            ? lines.map(l => l.startsWith('\t') ? l.slice(1) : l.startsWith('  ') ? l.slice(2) : l)
            : lines.map(l => '\t' + l);
          const joined = newLines.join('\n');
          ta.value = val.substring(0, lineStart) + joined + val.substring(lineEnd);
          ta.selectionStart = lineStart;
          ta.selectionEnd = lineStart + joined.length;
        }
      }
      else if (e.key === 'Enter') {
        const val = ta.value;
        const pos = ta.selectionStart;
        const lineStart = val.lastIndexOf('\n', pos - 1) + 1;
        const line = val.substring(lineStart, pos);
        let prefix = '';
        const bulletMatch = line.match(/^(\s*[-*+]\s)/);
        const numMatch = line.match(/^(\s*)(\d+)\.\s/);
        const checkMatch = line.match(/^(\s*-\s\[[ x]\]\s)/);
        if (checkMatch) {
          if (line.trim() === '- [ ]' || line.trim() === '- [x]') {
            e.preventDefault();
            ta.value = val.substring(0, lineStart) + '\n' + val.substring(pos);
            ta.selectionStart = ta.selectionEnd = lineStart + 1;
          } else {
            prefix = checkMatch[1].replace(/\[x\]/, '[ ]');
          }
        } else if (numMatch) {
          if (line.trim() === numMatch[2] + '.') {
            e.preventDefault();
            ta.value = val.substring(0, lineStart) + '\n' + val.substring(pos);
            ta.selectionStart = ta.selectionEnd = lineStart + 1;
          } else {
            prefix = numMatch[1] + (parseInt(numMatch[2]) + 1) + '. ';
          }
        } else if (bulletMatch) {
          if (line.trim() === '-' || line.trim() === '*' || line.trim() === '+') {
            e.preventDefault();
            ta.value = val.substring(0, lineStart) + '\n' + val.substring(pos);
            ta.selectionStart = ta.selectionEnd = lineStart + 1;
          } else {
            prefix = bulletMatch[1];
          }
        }
        if (prefix) {
          e.preventDefault();
          const insert = '\n' + prefix;
          ta.value = val.substring(0, pos) + insert + val.substring(ta.selectionEnd);
          ta.selectionStart = ta.selectionEnd = pos + insert.length;
        }
      }
    });

  } catch(e) {
    alert('Could not enter edit mode');
  }
}

async function saveEdit() {
  const ta = $('#editorTextarea');
  if (!ta) return;
  const saveBtn = $('#saveBtn');
  try {
    const res = await fetch('/api/note/' + encodeURIPath(currentNotePath), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: ta.value})
    });
    if (!res.ok) throw new Error('Save failed');
    if (saveBtn) {
      saveBtn.classList.add('save-success');
      saveBtn.textContent = 'Saved';
      setTimeout(() => {
        editMode = false;
        loadNote(currentNotePath);
      }, 600);
    } else {
      editMode = false;
      await loadNote(currentNotePath);
    }
  } catch(e) {
    alert('Save failed');
  }
}

function cancelEdit() {
  editMode = false;
  loadNote(currentNotePath);
}

async function deleteNote() {
  if (!currentNotePath || !currentNoteEditable) return;
  const name = currentNotePath.split('/').pop();
  if (!confirm('Delete "' + name + '"? This cannot be undone.')) return;
  try {
    const res = await fetch('/api/note/' + encodeURIPath(currentNotePath), { method: 'DELETE' });
    if (!res.ok) throw new Error('Delete failed');
    currentNotePath = null;
    currentNoteMtime = null;
    currentNoteEditable = false;
    editMode = false;
    topbarActions.innerHTML = '';
    breadcrumb.innerHTML = '<span>Select a note from the sidebar</span>';
    contentArea.innerHTML = '<div class="welcome"><p>Note deleted.</p></div>';
    await reloadTree();
  } catch(e) {
    alert('Delete failed');
  }
}
$('#newNoteBtn').addEventListener('click', () => {
  newNotePath.value = '';
  newNoteModal.classList.add('visible');
  setTimeout(() => newNotePath.focus(), 50);
});
$('#newNoteCancel').addEventListener('click', () => newNoteModal.classList.remove('visible'));
newNoteModal.addEventListener('click', e => {
  if (e.target === newNoteModal) newNoteModal.classList.remove('visible');
});
newNotePath.addEventListener('keydown', e => {
  if (e.key === 'Enter') $('#newNoteConfirm').click();
  if (e.key === 'Escape') newNoteModal.classList.remove('visible');
});
$('#newNoteConfirm').addEventListener('click', async () => {
  const p = newNotePath.value.trim();
  if (!p) return;
  try {
    const res = await fetch('/api/files/new', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: p})
    });
    const data = await res.json();
    if (!data.ok) { alert(data.error || 'Failed'); return; }
    newNoteModal.classList.remove('visible');
    await reloadTree();
    await loadNote(data.path);
    if (currentNoteEditable) enterEditMode();
  } catch(e) {
    alert('Failed to create note');
  }
});
$('#graphViewBtn').addEventListener('click', () => openGraphView());

async function openGraphView() {
  contentArea.style.padding = '0';
  currentNotePath = null;
  currentNoteMtime = null;
  currentNoteEditable = false;
  editMode = false;
  topbarActions.innerHTML = '';
  if (activeItem) { activeItem.classList.remove('active'); activeItem = null; }
  breadcrumb.innerHTML = '<span class="crumb-active">Graph View</span>';

  contentArea.innerHTML = '';
  const viewer = document.createElement('div');
  viewer.className = 'graph-viewer';
  viewer.innerHTML =
    '<div class="graph-toolbar">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="8" r="2"/><circle cx="12" cy="18" r="2"/><line x1="7.5" y1="7.5" x2="10.5" y2="16.5"/><line x1="16.5" y1="9.5" x2="13.5" y2="16.5"/><line x1="8" y1="6.5" x2="16" y2="7.5"/></svg>' +
      '<span class="graph-title">Knowledge Graph</span>' +
      '<span class="graph-stats" id="graphStats"></span>' +
      '<div class="graph-toolbar-actions">' +
        '<button class="graph-toolbar-btn" id="graphFilter" title="Toggle non-notes">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>' +
          'Notes only' +
        '</button>' +
        '<button class="graph-toolbar-btn" id="graphReset" title="Reset view">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 102.13-9.36L1 10"/></svg>' +
          'Reset' +
        '</button>' +
      '</div>' +
    '</div>' +
    '<div class="graph-canvas-wrap" id="graphWrap">' +
      '<canvas id="graphCanvas"></canvas>' +
      '<div class="graph-tooltip" id="graphTooltip"></div>' +
    '</div>';
  contentArea.appendChild(viewer);

  const wrap = viewer.querySelector('#graphWrap');
  const canvas = viewer.querySelector('#graphCanvas');
  const ctx = canvas.getContext('2d');
  const tooltip = viewer.querySelector('#graphTooltip');
  const statsEl = viewer.querySelector('#graphStats');
  let notesOnly = false;
  let graphData;
  try {
    const res = await fetch('/api/graph');
    graphData = await res.json();
  } catch (e) {
    wrap.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-faint)">Failed to load graph</div>';
    return;
  }

  let allNodes = graphData.nodes;
  let allEdges = graphData.edges;
  let nodes, edges;

  function filterGraph() {
    if (notesOnly) {
      nodes = allNodes.filter(n => n.group === 'note');
      const noteIds = new Set(nodes.map(n => n.id));
      edges = allEdges.filter(e => noteIds.has(e.source) && noteIds.has(e.target));
    } else {
      nodes = [...allNodes];
      edges = [...allEdges];
    }
    statsEl.textContent = nodes.length + ' files, ' + edges.length + ' links';
    initSimulation();
  }
  const groupColors = { note: '#8673ff', pdf: '#eb6f92', image: '#9ccfd8', folder: '#e0def4', other: '#6e6a86' };
  let simNodes = [];
  let animId = null;
  let camX = 0, camY = 0, camZoom = 1;
  let dragging = null, dragOffX = 0, dragOffY = 0;
  let panning = false, panStartX = 0, panStartY = 0, panCamX = 0, panCamY = 0;
  let hoverNode = null;

  function initSimulation() {
    if (animId) cancelAnimationFrame(animId);
    const W = wrap.clientWidth, H = wrap.clientHeight;
    simNodes = nodes.map((n, i) => ({
      ...n,
      x: (Math.random() - 0.5) * W * 0.6,
      y: (Math.random() - 0.5) * H * 0.6,
      vx: 0, vy: 0,
      r: Math.max(4, Math.min(12, 4 + (n.links || 0) * 1.5))
    }));
    const idxMap = {};
    simNodes.forEach((n, i) => idxMap[n.id] = i);
    edges.forEach(e => { e.si = idxMap[e.source]; e.ti = idxMap[e.target]; });
    camX = 0; camY = 0; camZoom = 1;
    simulate();
  }

  function simulate() {
    const alpha = 0.3;
    const repulsion = 800;
    const attraction = 0.008;
    const linkDist = 100;
    const damping = 0.85;
    const center = 0.01;
    let iterations = 0;

    function tick() {
      simNodes.forEach(n => {
        n.vx -= n.x * center;
        n.vy -= n.y * center;
      });
      for (let i = 0; i < simNodes.length; i++) {
        for (let j = i + 1; j < simNodes.length; j++) {
          let dx = simNodes[j].x - simNodes[i].x;
          let dy = simNodes[j].y - simNodes[i].y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 1) d2 = 1;
          const f = repulsion / d2;
          const fx = dx * f, fy = dy * f;
          simNodes[i].vx -= fx; simNodes[i].vy -= fy;
          simNodes[j].vx += fx; simNodes[j].vy += fy;
        }
      }
      edges.forEach(e => {
        if (e.si === undefined || e.ti === undefined) return;
        const a = simNodes[e.si], b = simNodes[e.ti];
        if (!a || !b) return;
        let dx = b.x - a.x, dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const f = (d - linkDist) * attraction;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      });
      simNodes.forEach(n => {
        if (n === dragging) return;
        n.vx *= damping; n.vy *= damping;
        n.x += n.vx; n.y += n.vy;
      });
      iterations++;
      draw();
      if (iterations < 300) animId = requestAnimationFrame(tick);
    }
    animId = requestAnimationFrame(tick);
  }

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = wrap.clientWidth * dpr;
    canvas.height = wrap.clientHeight * dpr;
    canvas.style.width = wrap.clientWidth + 'px';
    canvas.style.height = wrap.clientHeight + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  resize();
  new ResizeObserver(resize).observe(wrap);

  function screenToWorld(sx, sy) {
    const W = wrap.clientWidth, H = wrap.clientHeight;
    return {
      x: (sx - W / 2) / camZoom + camX,
      y: (sy - H / 2) / camZoom + camY
    };
  }

  function draw() {
    const W = wrap.clientWidth, H = wrap.clientHeight;
    ctx.clearRect(0, 0, W, H);
    ctx.save();
    ctx.translate(W / 2, H / 2);
    ctx.scale(camZoom, camZoom);
    ctx.translate(-camX, -camY);
    edges.forEach(e => {
      if (e.si === undefined || e.ti === undefined) return;
      const a = simNodes[e.si], b = simNodes[e.ti];
      if (!a || !b) return;
      const isHover = hoverNode && (a === hoverNode || b === hoverNode);
      const isFolder = e.kind === 'folder';
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      if (isHover) {
        ctx.strokeStyle = isFolder ? 'rgba(224,222,244,.35)' : 'rgba(134,112,255,.5)';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([]);
      } else if (isFolder) {
        ctx.strokeStyle = 'rgba(224,222,244,.08)';
        ctx.lineWidth = 0.7;
        ctx.setLineDash([3, 4]);
      } else {
        ctx.strokeStyle = 'rgba(134,112,255,.15)';
        ctx.lineWidth = 1;
        ctx.setLineDash([]);
      }
      ctx.stroke();
      ctx.setLineDash([]);
    });
    simNodes.forEach(n => {
      const isHover = n === hoverNode;
      const col = groupColors[n.group] || groupColors.other;
      const isFolder = n.group === 'folder';
      if (isHover) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r + 10, 0, Math.PI * 2);
        const grad = ctx.createRadialGradient(n.x, n.y, n.r, n.x, n.y, n.r + 10);
        grad.addColorStop(0, col + '40');
        grad.addColorStop(1, 'transparent');
        ctx.fillStyle = grad;
        ctx.fill();
      }

      if (isFolder) {
        const s = n.r * 1.6;
        const rr = 3;
        ctx.beginPath();
        ctx.moveTo(n.x - s/2 + rr, n.y - s/2);
        ctx.lineTo(n.x + s/2 - rr, n.y - s/2);
        ctx.quadraticCurveTo(n.x + s/2, n.y - s/2, n.x + s/2, n.y - s/2 + rr);
        ctx.lineTo(n.x + s/2, n.y + s/2 - rr);
        ctx.quadraticCurveTo(n.x + s/2, n.y + s/2, n.x + s/2 - rr, n.y + s/2);
        ctx.lineTo(n.x - s/2 + rr, n.y + s/2);
        ctx.quadraticCurveTo(n.x - s/2, n.y + s/2, n.x - s/2, n.y + s/2 - rr);
        ctx.lineTo(n.x - s/2, n.y - s/2 + rr);
        ctx.quadraticCurveTo(n.x - s/2, n.y - s/2, n.x - s/2 + rr, n.y - s/2);
        ctx.closePath();
        ctx.fillStyle = isHover ? col : col + '30';
        ctx.fill();
        ctx.strokeStyle = isHover ? col : col + '60';
        ctx.lineWidth = 1;
        ctx.stroke();
      } else {
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
        ctx.fillStyle = isHover ? col : col + 'aa';
        ctx.fill();
      }
      if (isHover || isFolder || n.r >= 8) {
        ctx.font = ((isHover || isFolder) ? '600 ' : '') + (isFolder ? '12' : '11') + 'px ' + getComputedStyle(document.body).fontFamily;
        ctx.fillStyle = isHover ? '#fff' : isFolder ? 'rgba(224,222,244,.75)' : 'rgba(224,222,244,.6)';
        ctx.textAlign = 'center';
        const offset = isFolder ? n.r * 1.6 / 2 + 8 : n.r + 6;
        ctx.fillText(n.name, n.x, n.y - offset);
      }
    });

    ctx.restore();
  }
  function nodeAt(sx, sy) {
    const w = screenToWorld(sx, sy);
    for (let i = simNodes.length - 1; i >= 0; i--) {
      const n = simNodes[i];
      const dx = w.x - n.x, dy = w.y - n.y;
      if (dx * dx + dy * dy < (n.r + 4) * (n.r + 4) / (camZoom * camZoom) + n.r * n.r) return n;
    }
    return null;
  }

  canvas.addEventListener('mousedown', e => {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    const n = nodeAt(sx, sy);
    if (n) {
      dragging = n;
      const w = screenToWorld(sx, sy);
      dragOffX = n.x - w.x;
      dragOffY = n.y - w.y;
      canvas.style.cursor = 'grabbing';
    } else {
      panning = true;
      panStartX = e.clientX; panStartY = e.clientY;
      panCamX = camX; panCamY = camY;
      canvas.style.cursor = 'grabbing';
    }
  });

  canvas.addEventListener('mousemove', e => {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    if (dragging) {
      const w = screenToWorld(sx, sy);
      dragging.x = w.x + dragOffX;
      dragging.y = w.y + dragOffY;
      dragging.vx = 0; dragging.vy = 0;
      draw();
    } else if (panning) {
      camX = panCamX - (e.clientX - panStartX) / camZoom;
      camY = panCamY - (e.clientY - panStartY) / camZoom;
      draw();
    } else {
      const n = nodeAt(sx, sy);
      if (n !== hoverNode) {
        hoverNode = n;
        draw();
        if (n) {
          tooltip.innerHTML = '<div>' + esc(n.name) + '</div><div class="gt-path">' + esc(n.path) + '</div>';
          tooltip.classList.add('visible');
          canvas.style.cursor = 'pointer';
        } else {
          tooltip.classList.remove('visible');
          canvas.style.cursor = 'default';
        }
      }
      if (n) {
        tooltip.style.left = (e.clientX - wrap.getBoundingClientRect().left + 12) + 'px';
        tooltip.style.top = (e.clientY - wrap.getBoundingClientRect().top - 10) + 'px';
      }
    }
  });

  document.addEventListener('mouseup', () => {
    if (dragging) { dragging = null; canvas.style.cursor = 'default'; }
    if (panning) { panning = false; canvas.style.cursor = 'default'; }
  });
  canvas.addEventListener('dblclick', e => {
    const rect = canvas.getBoundingClientRect();
    const n = nodeAt(e.clientX - rect.left, e.clientY - rect.top);
    if (!n) return;
    if (n.group === 'note') loadNote(n.path);
    else if (n.group === 'pdf') loadPdf(n.path, n.name);
    else if (n.group === 'image') loadImage(n.path, n.name);
  });
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    const factor = e.deltaY > 0 ? 0.9 : 1.1;
    camZoom = Math.min(Math.max(camZoom * factor, 0.2), 5);
    draw();
  }, { passive: false });
  viewer.querySelector('#graphFilter').addEventListener('click', function() {
    notesOnly = !notesOnly;
    this.classList.toggle('active', notesOnly);
    filterGraph();
  });
  viewer.querySelector('#graphReset').addEventListener('click', () => {
    camX = 0; camY = 0; camZoom = 1;
    filterGraph();
  });

  filterGraph();

  history.pushState({ graph: true }, '', '?graph=1');
  if (window.innerWidth <= 768) {
    sidebar.classList.add('collapsed');
    overlay.classList.remove('visible');
  }
}
$('#downloadAllBtn').addEventListener('click', () => {
  window.location.href = '/api/download-all';
});
$('#uploadBtn').addEventListener('click', () => uploadInput.click());
uploadInput.addEventListener('change', async () => {
  const file = uploadInput.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/files/upload', {method: 'POST', body: fd});
    const data = await res.json();
    if (!data.ok) { alert(data.error || 'Upload failed'); return; }
    await reloadTree();
    if (data.path.endsWith('.md')) loadNote(data.path);
  } catch(e) {
    alert('Upload failed');
  }
  uploadInput.value = '';
});
window.addEventListener('popstate', e => {
  if (e.state && e.state.graph) {
    openGraphView();
  } else if (e.state && e.state.pdf) {
    loadPdf(e.state.path);
  } else if (e.state && e.state.img) {
    loadImage(e.state.path);
  } else if (e.state && e.state.path) {
    loadNote(e.state.path);
  }
});
document.addEventListener('keydown', e => {
  const mod = navigator.platform.toUpperCase().indexOf('MAC') >= 0 ? e.metaKey : e.ctrlKey;
  if (mod && e.key === 'n' && !editMode && serverConfig.allow_file_creation) {
    e.preventDefault();
    newNotePath.value = '';
    newNoteModal.classList.add('visible');
    setTimeout(() => newNotePath.focus(), 50);
  }
  if (e.key === 'Escape') {
    if (newNoteModal.classList.contains('visible')) {
      newNoteModal.classList.remove('visible');
    } else if (editMode) {
      cancelEdit();
    }
  }
});
(async () => {
  const res = await fetch('/api/tree');
  const tree = await res.json();
  renderTree(tree, fileTree);
  const fc = $('#fileCount');
  if (fc) fc.textContent = countFiles(tree);
  const params = new URLSearchParams(location.search);
  const note = params.get('note');
  const pdfParam = params.get('pdf');
  const imgParam = params.get('img');
  const graphParam = params.get('graph');
  const targetPath = note || pdfParam || imgParam;
  if (graphParam) {
    openGraphView();
  } else if (pdfParam) {
    loadPdf(pdfParam);
  } else if (imgParam) {
    loadImage(imgParam);
  } else if (note) {
    loadNote(note);
  }
  if (targetPath) {
    const parts = targetPath.split('/');
    let container = fileTree;
    for (let i = 0; i < parts.length - 1; i++) {
      const folderItems = container.querySelectorAll(':scope > .tree-item');
      for (const item of folderItems) {
        if (item.textContent.trim() === parts[i]) {
          const chev = item.querySelector('.tree-chevron');
          if (chev) chev.classList.add('open');
          const next = item.nextElementSibling;
          if (next && next.classList.contains('tree-children')) {
            next.classList.add('open');
            container = next;
          }
          break;
        }
      }
    }
  }
})();
let pollPaused = false;

document.addEventListener('visibilitychange', () => {
  pollPaused = document.hidden;
  if (!document.hidden) pollCheck();
});

function countFiles(items) {
  let n = 0;
  items.forEach(item => {
    if (item.type === 'file') n++;
    else if (item.children) n += countFiles(item.children);
  });
  return n;
}

async function reloadTree() {
  const openPaths = new Set();
  fileTree.querySelectorAll('.tree-chevron.open').forEach(chev => {
    const row = chev.closest('.tree-item');
    if (row) openPaths.add(row.textContent.trim());
  });

  const res = await fetch('/api/tree');
  const tree = await res.json();
  fileTree.innerHTML = '';
  renderTree(tree, fileTree);
  const fc = $('#fileCount');
  if (fc) fc.textContent = countFiles(tree);
  fileTree.querySelectorAll('.tree-item').forEach(row => {
    if (!row.dataset.path && openPaths.has(row.textContent.trim())) {
      const chev = row.querySelector('.tree-chevron');
      const children = row.nextElementSibling;
      if (chev) chev.classList.add('open');
      if (children && children.classList.contains('tree-children')) children.classList.add('open');
    }
  });
  if (currentNotePath) {
    fileTree.querySelectorAll('.tree-item[data-path]').forEach(el => {
      if (el.dataset.path === currentNotePath) {
        el.classList.add('active');
        activeItem = el;
      }
    });
  }
}

async function pollCheck() {
  try {
    const url = '/api/check' + (currentNotePath ? '?note=' + encodeURIComponent(currentNotePath) : '');
    const res = await fetch(url);
    const data = await res.json();
    if (lastTreeHash !== null && data.tree_hash !== lastTreeHash) {
      await reloadTree();
    }
    lastTreeHash = data.tree_hash;
    if (currentNotePath && !editMode && currentNoteMtime !== null && data.note_mtime !== null && data.note_mtime !== currentNoteMtime) {
      loadNote(currentNotePath, {preserveScroll: true});
    }
  } catch (e) {  }
}

function startPolling(seconds) {
  setInterval(() => {
    if (!pollPaused) pollCheck();
  }, seconds * 1000);
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import socket
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    print(f"Serving vault: {VAULT}")
    print(f"Open http://localhost:{PORT}    (this machine)")
    print(f"     http://{local_ip}:{PORT}  (other devices on network)")
    app.run(host=HOST, port=PORT)
