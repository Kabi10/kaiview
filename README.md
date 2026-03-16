# KaiView

> Your AI-powered developer OS. One dashboard for every project on your machine.

![KaiView Logo](logo.png)

**KaiView** is a local-first developer dashboard that scans your project folders and gives you a live, intelligent view of everything you're building — git activity, tech stacks, AI context, session memory, and project health. No cloud. No subscriptions. One command to run.

---

## Features

| | |
|---|---|
| 🔍 **Auto project discovery** | Scans your dev folder, detects stack (Android, React, Python, Node.js, Docker, Arduino…) |
| ⎇ **Live git watcher** | Branch, dirty files, last commit, 7-day commit sparkline — updated via WebSocket |
| ♥ **Health score** | 0–100 score per project based on git freshness, README, dirty tree, active task |
| 🎯 **3-lens view** | Active Now / Needs Attention / Neglected — see what needs your focus |
| ▶ **Resume / ⏸ Park** | Save session state with current task, next action, blockers — pick up exactly where you left off |
| 🤖 **AI context payload** | Store stack summary, conventions, and context snippet — auto-copied to clipboard on resume |
| 🚀 **Dev launcher** | Auto-detect and start/stop dev server (`npm run dev`, `python bot.py`, `docker-compose up`…) |
| 📦 **Dependency scanner** | Scan `requirements.txt`, `package.json`, `build.gradle.kts` per project |
| ⌘K **Command palette** | Fuzzy search across all projects with inline actions |
| ⬛ **Kanban view** | Board-style view grouped by status (Active / In Progress / Paused / Complete) |
| 📓 **Project journal** | Per-project log with mood tags (Note / Win / Blocker / Idea) |
| 🔎 **Full-text search** | Search across name, description, tasks, notes, AI context |

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/Kabi10/kaiview.git
cd kaiview

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your dev folder path (edit server.py line 15 if not C:/Dev)
# DEV_DIR = Path("C:/Dev")

# 4. Run
python server.py
```

Open **http://localhost:3737**

> **First run:** KaiView auto-scans `DEV_DIR`. No config needed — git history, stack detection, and metadata are read automatically.

---

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+K` | Open command palette |
| `Ctrl+F` | Focus search bar |
| `R` | Refresh projects |
| `Esc` | Close modal / palette |

---

## Configuration

Edit the top of `server.py`:

```python
DEV_DIR    = Path("C:/Dev")   # your projects root
DB_PATH    = Path(__file__).parent / "kaiview.db"
```

Projects in `SKIP` set are excluded from the dashboard.

---

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/projects` | All projects with full payload |
| `PUT` | `/api/projects/{name}` | Update metadata |
| `POST` | `/api/projects/{name}/resume` | Start session, open VS Code |
| `POST` | `/api/projects/{name}/park` | Save session state |
| `POST` | `/api/projects/{name}/launch` | Start/stop dev server |
| `GET` | `/api/projects/{name}/git` | Git log + status |
| `GET` | `/api/projects/{name}/deps` | Dependency list |
| `GET` | `/api/projects/{name}/journal` | Journal entries |
| `POST` | `/api/projects/{name}/journal` | Add journal entry |
| `GET` | `/api/search?q=` | Full-text search |
| `GET` | `/api/stats` | Counts by lens/status/category/AI |
| `WS` | `/ws` | Live git updates |

---

## Stack

- **Backend:** Python 3.10+, FastAPI, uvicorn, aiosqlite
- **Frontend:** Vanilla HTML/CSS/JS (zero npm, zero build step)
- **Storage:** SQLite via aiosqlite (auto-migrates from legacy JSON)
- **Git:** subprocess + git CLI

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). PRs welcome — especially new stack detectors, platform connectors, and UI improvements.

## License

MIT © [Kabi10](https://github.com/Kabi10)
