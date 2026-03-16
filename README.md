# KaiView

> Your AI-powered developer OS. One dashboard for every project on your machine.

![KaiView Logo](logo.png)

## What is KaiView?

KaiView is a local-first developer dashboard that scans your project folders and gives you a live, intelligent view of everything you're building — git status, tech stacks, AI assignments, session memory, and more. No cloud required. No subscriptions. Runs on your machine.

## Features

- **Auto project discovery** — scans your dev folder and detects stack (Android, React, Python, Node.js, Arduino, and more)
- **Live git status** — branch, dirty files, last commit per project
- **AI assignment** — track which AI model is working on each project
- **Project cards** — status, category, description, notes, pinned
- **Open in VS Code** — one click to launch any project
- **Filter & search** — by status, category, AI, or keyword
- **Local metadata** — everything stored locally in JSON (SQLite migration coming)

## Roadmap

- [ ] SQLite migration
- [ ] Resume / Park session system
- [ ] Active Now / Needs Attention / Neglected view
- [ ] Staleness score + sparklines
- [ ] Background git watcher (WebSocket)
- [ ] Dependency scanner
- [ ] Dev environment launcher
- [ ] AI context payload per project
- [ ] Cross-project code search

## Getting Started

### Requirements

- Python 3.10+
- pip

### Install

```bash
git clone https://github.com/Kabi10/kaiview.git
cd kaiview
pip install -r requirements.txt
```

### Run

```bash
python server.py
```

Open [http://localhost:3737](http://localhost:3737)

## Stack

- **Backend:** Python, FastAPI, uvicorn
- **Frontend:** Vanilla HTML/CSS/JS
- **Storage:** JSON (SQLite coming)
- **Git:** subprocess + git CLI

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT © [Kabi10](https://github.com/Kabi10)
