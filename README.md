# Tachylite
This project is sort of EOL, this is an internal tool I've been using that I wanted to publish because why not. It willl likely recieve more updates as I change it for my own personal use, however I cant guarantee a lot of support.

A live [Obsidian](https://obsidian.md) vault viewer built with Flask. Drop it into any vault and browse, edit, and share your notes from a browser — on the same machine or across your local network.


## Quick start

```bash
pip install -r requirements.txt
python server.py
```

Open [http://localhost:8000](http://localhost:8000). It serves whatever directory it lives in, so just drop `server.py` into any Obsidian vault and run it. Other devices on the same network can connect too.

## Features

- Live markdown rendering with `[[wikilink]]` and `![[embed]]` support
- In-browser editing with a markdown toolbar
- Interactive knowledge graph
- PDF viewer with dark mode, image galleries, file uploads
- Auto-refreshes when files change on disk
- Static site export for GitHub Pages

## Configuration

All settings live in `tachylite.config.json` next to `server.py`. If the file is missing, sensible defaults are used. You only need to include keys you want to change.

A minimal example that makes everything editable:

```json
{
  "allow_edit_all": true
}
```

<details>
<summary><strong>All settings</strong></summary>

| Key | Default | Description |
|---|---|---|
| `port` | `8000` | Port to serve on |
| `host` | `"0.0.0.0"` | Bind address. Use `"127.0.0.1"` for local-only access |
| `allow_edit_all` | `false` | Let anyone edit any note (not just ones they created this session) |
| `allow_file_creation` | `true` | Show "New note" and "Upload" buttons. `false` = read-only |
| `poll_interval` | `15` | Seconds between auto-refresh polls for file changes |
| `excluded_dirs` | `[".obsidian", ...]` | Folders hidden from the sidebar |
| `excluded_files` | `["server.py", ...]` | Files hidden from the sidebar |
| `allowed_upload_extensions` | `[".md", ".txt", ...]` | File types allowed for upload |

</details>

<details>
<summary><strong>Example setups</strong></summary>

| Use case | `allow_edit_all` | `allow_file_creation` |
|---|---|---|
| Default (visitors can only touch what they make) | `false` | `true` |
| Shared wiki (everyone edits everything) | `true` | `true` |
| Read-only viewer | `false` | `false` |

</details>

<details>
<summary><strong>Auto-start on boot</strong></summary>

```bash
bash setup-autostart.sh            # install
bash setup-autostart.sh --remove   # uninstall
```

Detects your OS and picks the right method: systemd (Linux), launchd (macOS), Startup folder (Windows), Task Scheduler (WSL), or cron (fallback).

</details>

<details>
<summary><strong>Static export</strong></summary>

```bash
python build_static.py
```

Generates a read-only snapshot in `_site/` suitable for GitHub Pages.

</details>

<details>
<summary><strong>Tachylite vs Quartz</strong></summary>

[Quartz](https://quartz.jzhao.xyz/) is a static-site generator for Obsidian vaults — great for polished public websites. Tachylite is a live server, not a build tool.

| | Tachylite | Quartz |
|---|---|---|
| How it works | Live Flask server | Static site generator |
| Editing | In the browser | Edit in Obsidian, rebuild |
| Setup | `pip install flask markdown` | Node.js v22+, npm, build step |
| Live updates | Auto-polls for changes | Rebuild required |
| Dependencies | Python 3, two pip packages | Node.js ecosystem |

Use Quartz for a public-facing site with full-text search. Use Tachylite to pull up your notes on your phone over the local network and edit them without opening Obsidian.

</details>
