

import json
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path


from server import (
    VAULT,
    EXCLUDED_DIRS,
    EXCLUDED_FILES,
    build_tree,
    render_markdown,
    MAIN_TEMPLATE,
)

README = """\
# Capstone Notes

My capstone research vault, viewable in two ways depending on the situation.

## What this is

This is a static snapshot of **[Tachylite](https://github.com/Jasminestrone/Tachylite)**, a custom web-based [Obsidian](https://obsidian.md) vault viewer I built for my capstone project.


## Two versions

### Tachylite Live - for collaboration

The full version is a Python/Flask app that runs on my laptop and serves the vault over the local network. Anyone on the same Wi-Fi can open it in their
browser. This makes it ideal for **in-class collaboration and live feedback**: a teacher or peer can browse my
research, read my notes, and even create or edit files in real time while I'm working on them. Changes show up instantly thanks to live reload polling.

What it offers:

- **Live markdown rendering** with Obsidian-style wikilink support (`[[links]]`)
- **In-browser editing** with a full markdown toolbar (bold, lists, code blocks, etc.)
- **File management** — create, upload, and delete notes with session-based permissions
- **Live reload** that auto-refreshes content when files change on disk
- **Built-in PDF viewer** with dark mode, zoom, and page navigation
- **Image viewer** with zoom and transparency grid

The issue is that it only works while my laptop is on and connected to the network. The moment I close it or leave, the site goes down and is entirely made useless.

### Tachylite Frozen - anytime access

This GitHub Pages site solves that problem. It's a frozen, read-only snapshot of the vault that's always online. Teachers can review my research at home, on the weekend, or whenever they need to without coordinating with me.

It keeps the full look and feel (dark theme, sidebar, PDF viewer, graph view, download button) but strips out the features that need a live server (editing, uploading, live reload).
"""

OUTPUT = VAULT / "_site"
DATA = OUTPUT / "data"
NOTES = DATA / "notes"
RAW = OUTPUT / "raw"


def collect_files():

    files = []
    for p in sorted(VAULT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(VAULT)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if p.name in EXCLUDED_FILES:
            continue

        if str(rel).startswith("_site"):
            continue
        if p.name == "build_static.py":
            continue
        files.append(rel)
    return files


def generate_tree():

    tree = build_tree(VAULT, owned=set())

    def clean(items):
        return [
            {**item, "children": clean(item.get("children", []))}
            if item.get("type") == "folder"
            else item
            for item in items
            if item.get("name") not in ("_site", "build_static.py", ".github")
        ]
    return clean(tree)


def generate_graph(files):

    wikilink_re = re.compile(r'\[\[([^\]|]+?)(?:\|[^\]]*?)?\]\]')
    nodes = {}
    edges = []
    folders_seen = set()

    for rel in files:
        p = VAULT / rel
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

    link_counts = {}
    for e in unique_edges:
        link_counts[e["source"]] = link_counts.get(e["source"], 0) + 1
        link_counts[e["target"]] = link_counts.get(e["target"], 0) + 1
    for n in nodes.values():
        n["links"] = link_counts.get(n["id"], 0)

    return {"nodes": list(nodes.values()), "edges": unique_edges}


def generate_note_json(rel):

    fpath = VAULT / rel
    text = fpath.read_text(errors="replace")
    html = render_markdown(text)

    html = html.replace('src="/raw/', 'src="raw/')
    html = html.replace('href="/raw/', 'href="raw/')
    rel_str = str(rel)
    return {
        "html": html,
        "path": rel_str,
        "name": fpath.stem,
        "mtime": fpath.stat().st_mtime,
        "editable": False,
    }


def patch_template(html, zip_name="notes.zip"):


    html = html.replace("fetch('/api/tree')", "fetch('data/tree.json')")


    html = html.replace(
        "fetch('/api/note/' + encodeURIPath(path))",
        "fetch('data/notes/' + encodeURIPath(path) + '.json')",
    )


    html = html.replace("fetch('/api/graph')", "fetch('data/graph.json')")


    html = html.replace("'/raw/' + encodeURIPath", "'raw/' + encodeURIPath")
    html = html.replace("window.open('/raw/'", "window.open('raw/'")


    html = html.replace(
        "async function pollCheck() {",
        "async function pollCheck() { return; // disabled in static mode\n// original body follows (unreachable):",
    )


    html = html.replace(
        "imgSrc.startsWith('/raw/')",
        "(imgSrc.startsWith('/raw/') || imgSrc.startsWith('raw/'))",
    )
    html = html.replace(
        "decodeURIComponent(imgSrc.slice(5))",
        "decodeURIComponent(imgSrc.startsWith('/raw/') ? imgSrc.slice(5) : imgSrc.slice(4))",
    )


    html = html.replace(
        "if (mod && e.key === 'n' && !editMode) {",
        "if (false && mod && e.key === 'n' && !editMode) {",
    )


    icicle_svg = (
        "%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='14'%3E"

        "%3Cpolygon points='2,0 3.5,0 2.8,4' fill='%239cc8e0' opacity='.3'/%3E"

        "%3Cpolygon points='6,0 9,0 7,10' fill='%238cb8d4' opacity='.45'/%3E"

        "%3Cpolygon points='12,0 13,0 12.5,3' fill='%23a0d0e8' opacity='.25'/%3E"

        "%3Cpolygon points='16,0 20.5,0 17.8,14' fill='%237aafc8' opacity='.4'/%3E"

        "%3Cpolygon points='23,0 24.5,0 23.8,7' fill='%239cc8e0' opacity='.35'/%3E"

        "%3Cpolygon points='27,0 30,0 28.8,9' fill='%238cb8d4' opacity='.38'/%3E"

        "%3Cpolygon points='33,0 34,0 33.5,3.5' fill='%23a0d0e8' opacity='.22'/%3E"

        "%3Cpolygon points='37,0 39,0 37.8,11' fill='%237aafc8' opacity='.42'/%3E"

        "%3Cpolygon points='42,0 45.5,0 43.5,6' fill='%239cc8e0' opacity='.3'/%3E"

        "%3Cpolygon points='48,0 49.5,0 48.6,12' fill='%238cb8d4' opacity='.36'/%3E"

        "%3Cpolygon points='52,0 53,0 52.5,2.5' fill='%23a0d0e8' opacity='.2'/%3E"

        "%3Cpolygon points='55,0 58,0 56.2,8' fill='%237aafc8' opacity='.38'/%3E"
        "%3C/svg%3E"
    )
    frozen_css = (
        "#newNoteBtn, #uploadBtn, #uploadInput, #newNoteModal { display: none !important; }\n"
        "#frozenLabel { position: relative; display: inline-block; overflow: visible !important; }\n"
        "#frozenLabel::after {\n"
        "  content: '';\n"
        "  position: absolute;\n"
        "  bottom: -14px;\n"
        "  left: -2px;\n"
        "  right: -2px;\n"
        "  height: 14px;\n"
        f"  background: url(\"data:image/svg+xml,{icicle_svg}\") repeat-x;\n"
        "  pointer-events: none;\n"
        "  filter: drop-shadow(0 1px 3px rgba(100,170,220,0.2));\n"
        "}\n"
    )
    html = html.replace("</style>", frozen_css + "</style>")


    html = html.replace(
        "window.location.href = '/api/download-all';",
        f"window.location.href = '{zip_name}';",
    )


    # --- last-edited tooltip on breadcrumb hover ---
    last_edited_css = (
        ".breadcrumb { position: relative; cursor: default; }\n"
        ".breadcrumb .last-edited-tip {\n"
        "  display: none; position: absolute; top: 100%; left: 0;\n"
        "  margin-top: 4px; padding: 4px 10px;\n"
        "  background: var(--bg-tertiary); color: var(--text-muted);\n"
        "  font-size: 11px; border-radius: 4px; white-space: nowrap;\n"
        "  border: 1px solid var(--border-strong);\n"
        "  pointer-events: none; z-index: 100;\n"
        "}\n"
        ".breadcrumb:hover .last-edited-tip { display: block; }\n"
    )
    html = html.replace("</style>", last_edited_css + "</style>")

    html = html.replace(
        "currentNoteMtime = data.mtime;",
        "currentNoteMtime = data.mtime;\n"
        "    {\n"
        "      let tip = breadcrumb.querySelector('.last-edited-tip');\n"
        "      if (!tip) { tip = document.createElement('span'); tip.className = 'last-edited-tip'; breadcrumb.appendChild(tip); }\n"
        "      if (data.mtime) {\n"
        "        const d = new Date(data.mtime * 1000);\n"
        "        tip.textContent = 'Last edited: ' + d.toLocaleDateString(undefined, {year:'numeric',month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString(undefined, {hour:'2-digit',minute:'2-digit'});\n"
        "      } else { tip.textContent = ''; }\n"
        "    }",
        1,
    )


    html = html.replace(
        '<span style="background:var(--bg-tertiary);padding:2px 8px;border-radius:3px;margin:0 2px">Ctrl+N</span> new note &nbsp;&middot;&nbsp;\n'
        '          <span style="background:var(--bg-tertiary);padding:2px 8px;border-radius:3px;margin:0 2px">Ctrl+S</span> save',
        'Read-only view &middot; Hosted on GitHub Pages',
    )


    html = html.replace("<title>Tachylite Live</title>", "<title>Tachylite Frozen</title>")
    html = html.replace(
        'Tachylite <span style="font-weight:400;opacity:.6;font-size:10px">LIVE</span>',
        'Tachylite <span id="frozenLabel" style="font-weight:400;opacity:.8;font-size:10px">FROZEN</span>',
    )


    html = html.replace("--bg-primary: #1a1a2e;", "--bg-primary: #141921;")
    html = html.replace("--bg-secondary: #16162a;", "--bg-secondary: #10151c;")
    html = html.replace("--bg-tertiary: #1f1f3a;", "--bg-tertiary: #1a2130;")

    html = html.replace("--accent: #8673ff;", "--accent: #6b9fce;")
    html = html.replace("--accent-hover: #a48fff;", "--accent-hover: #8bb8e0;")
    html = html.replace("--accent-dim: rgba(134,112,255,.35);", "--accent-dim: rgba(107,159,206,.30);")
    html = html.replace("--tag-bg: rgba(134,112,255,.12);", "--tag-bg: rgba(107,159,206,.10);")
    html = html.replace("--bg-hover: rgba(134,112,255,.08);", "--bg-hover: rgba(107,159,206,.07);")
    html = html.replace("--bg-active: rgba(134,112,255,.15);", "--bg-active: rgba(107,159,206,.13);")

    html = html.replace("--text: #e0def4;", "--text: #dae3ed;")
    html = html.replace("--text-muted: #908caa;", "--text-muted: #7e8fa0;")
    html = html.replace("--text-faint: #6e6a86;", "--text-faint: #5c6d7c;")

    html = html.replace("--border: rgba(255,255,255,.06);", "--border: rgba(160,190,220,.06);")
    html = html.replace("--border-strong: rgba(255,255,255,.1);", "--border-strong: rgba(160,190,220,.1);")

    return html


def build():

    print("Building static site...")


    if OUTPUT.exists():
        for item in OUTPUT.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    NOTES.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)

    files = collect_files()
    md_files = [f for f in files if f.suffix.lower() == ".md"]
    other_files = [f for f in files if f.suffix.lower() != ".md"]


    tree = generate_tree()
    (DATA / "tree.json").write_text(json.dumps(tree), encoding="utf-8")
    print(f"  tree.json ({len(files)} files)")


    graph = generate_graph(files)
    (DATA / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    print(f"  graph.json ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges)")


    for rel in md_files:
        note_data = generate_note_json(rel)
        out_path = NOTES / (str(rel) + ".json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(note_data), encoding="utf-8")
    print(f"  {len(md_files)} notes rendered")


    for rel in files:
        src = VAULT / rel
        dst = RAW / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print(f"  {len(files)} raw files copied")


    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    zip_name = f"notes_{stamp}.zip"
    zip_path = OUTPUT / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.write(VAULT / rel, str(rel))
    zip_size = zip_path.stat().st_size / 1024
    print(f"  {zip_name} ({zip_size:.0f} KB, {len(files)} files)")


    patched = patch_template(MAIN_TEMPLATE, zip_name)
    (OUTPUT / "index.html").write_text(patched, encoding="utf-8")
    print("  index.html generated")


    (OUTPUT / ".nojekyll").write_text("", encoding="utf-8")


    (OUTPUT / "README.md").write_text(README, encoding="utf-8")

    print(f"\nDone! Static site is in: {OUTPUT}")
    print("To test locally:  cd _site && python3 -m http.server 8080")


if __name__ == "__main__":
    build()
