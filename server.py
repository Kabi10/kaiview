import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

DEV_DIR    = Path("C:/Dev")
DB_PATH    = Path(__file__).parent / "kaiview.db"
HTML_FILE  = Path(__file__).parent / "index.html"
SCHEMA_VER = 2

SKIP = {
    "kaiview", "manager", ".claude", "__pycache__", "node_modules",
    ".git", "null", "shared", "tools", "chorus", "AI Convo"
}

app = FastAPI(title="KaiView")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ws_clients: list[WebSocket] = []


# ── Models ────────────────────────────────────────────────────────────────────

class ProjectUpdate(BaseModel):
    description:        Optional[str]  = None
    category:           Optional[str]  = None
    status:             Optional[str]  = None
    ai_assigned:        Optional[str]  = None
    pinned:             Optional[bool] = None
    notes:              Optional[str]  = None
    current_task:       Optional[str]  = None
    next_action:        Optional[str]  = None
    blockers:           Optional[str]  = None
    ai_context_snippet: Optional[str]  = None
    ai_stack_summary:   Optional[str]  = None
    ai_conventions:     Optional[str]  = None
    link_repo:          Optional[str]  = None
    link_docs:          Optional[str]  = None
    link_deploy:        Optional[str]  = None
    tags:               Optional[list] = None

class ParkRequest(BaseModel):
    note:         str = ""
    current_task: str = ""
    next_action:  str = ""
    blockers:     str = ""


# ── Database ──────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS projects (
                name                TEXT PRIMARY KEY,
                description         TEXT DEFAULT '',
                category            TEXT DEFAULT 'Other',
                status              TEXT DEFAULT 'Active',
                ai_assigned         TEXT DEFAULT 'None',
                pinned              INTEGER DEFAULT 0,
                notes               TEXT DEFAULT '',
                current_task        TEXT DEFAULT '',
                next_action         TEXT DEFAULT '',
                blockers            TEXT DEFAULT '',
                focus_updated_at    TEXT DEFAULT '',
                ai_context_snippet  TEXT DEFAULT '',
                ai_stack_summary    TEXT DEFAULT '',
                ai_conventions      TEXT DEFAULT '',
                last_opened_at      TEXT DEFAULT '',
                tags                TEXT DEFAULT '[]',
                link_repo           TEXT DEFAULT '',
                link_docs           TEXT DEFAULT '',
                link_deploy         TEXT DEFAULT '',
                created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name  TEXT NOT NULL,
                started_at    TEXT NOT NULL,
                ended_at      TEXT,
                note          TEXT DEFAULT '',
                current_task  TEXT DEFAULT '',
                next_action   TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ai_logs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name  TEXT NOT NULL,
                logged_at     TEXT NOT NULL,
                model         TEXT DEFAULT '',
                topic         TEXT DEFAULT '',
                outcome       TEXT DEFAULT ''
            );
        """)

        await db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VER),))

        # ── Migrate from JSON if exists and not yet migrated
        json_path = Path(__file__).parent / "projects_meta.json"
        row = await (await db.execute("SELECT value FROM meta WHERE key='json_migrated'")).fetchone()
        if json_path.exists() and not row:
            try:
                old = json.loads(json_path.read_text())
                for name, m in old.items():
                    await db.execute("""
                        INSERT OR IGNORE INTO projects
                            (name, description, category, status, ai_assigned, pinned, notes)
                        VALUES (?,?,?,?,?,?,?)
                    """, (
                        name,
                        m.get("description", ""),
                        m.get("category", "Other"),
                        m.get("status", "Active"),
                        m.get("ai_assigned", "None"),
                        1 if m.get("pinned") else 0,
                        m.get("notes", ""),
                    ))
                await db.execute("INSERT INTO meta VALUES ('json_migrated','1')")
                print(f"Migrated {len(old)} projects from JSON to SQLite")
            except Exception as e:
                print(f"Migration warning: {e}")

        await db.commit()


async def db_get_project(name: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM projects WHERE name=?", (name,))).fetchone()
        return dict(row) if row else {}


async def db_upsert_project(name: str, fields: dict):
    if not fields:
        return
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    cols   = ", ".join(fields.keys())
    places = ", ".join(["?"] * len(fields))
    vals   = list(fields.values())
    update = ", ".join(f"{k}=excluded.{k}" for k in fields)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"""
            INSERT INTO projects (name, {cols}) VALUES (?, {places})
            ON CONFLICT(name) DO UPDATE SET {update}
        """, [name] + vals)
        await db.commit()


# ── Stack / Git helpers ───────────────────────────────────────────────────────

def detect_stack(path: Path) -> list:
    stack = []
    try:
        files   = {f.name for f in path.iterdir() if f.is_file()}
        subdirs = {d.name for d in path.iterdir() if d.is_dir()}
    except:
        return ["Unknown"]

    if "build.gradle.kts" in files or ("app" in subdirs and (path / "app" / "build.gradle.kts").exists()):
        stack.append("Android")
    if "package.json" in files:
        try:
            pkg  = json.loads((path / "package.json").read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "react-native" in deps:      stack.append("React Native")
            elif "react" in deps:           stack.append("React")
            elif "next" in deps:            stack.append("Next.js")
            else:                           stack.append("Node.js")
        except:
            stack.append("Node.js")
    if any(f.endswith(".py") for f in files) or "requirements.txt" in files:
        stack.append("Python")
    if "bot.py" in files:
        stack.append("Telegram Bot")
    has_ino = any(f.endswith(".ino") for f in files)
    if not has_ino:
        for d in subdirs:
            try:
                if any(f.name.endswith(".ino") for f in (path / d).iterdir() if f.is_file()):
                    has_ino = True; break
            except: pass
    if has_ino:                              stack.append("Arduino")
    if "vercel.json" in files:              stack.append("Vercel")
    if "firebase.json" in files:            stack.append("Firebase")
    if "supabase" in subdirs:               stack.append("Supabase")
    if "docker-compose.yml" in files or "Dockerfile" in files: stack.append("Docker")
    return stack or ["Unknown"]


def get_git_info(path: Path) -> dict:
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
        if not branch:
            return {"is_git": False}

        raw = subprocess.check_output(
            ["git", "log", "-1", "--format=%s|||%ct|||%h"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()

        status_raw = subprocess.check_output(
            ["git", "status", "--short"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()

        parts       = raw.split("|||")
        commit_msg  = parts[0] if parts else ""
        commit_ts   = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 0
        commit_hash = parts[2].strip() if len(parts) > 2 else ""

        now_ts = int(datetime.now(timezone.utc).timestamp())
        days   = (now_ts - commit_ts) / 86400 if commit_ts else 999

        if days < 0.04:   t = "just now"
        elif days < 1:    t = f"{int(days*24)}h ago"
        elif days < 2:    t = "yesterday"
        elif days < 7:    t = f"{int(days)}d ago"
        elif days < 30:   t = f"{int(days/7)}w ago"
        else:             t = f"{int(days/30)}mo ago"

        return {
            "is_git":            True,
            "branch":            branch,
            "last_commit":       commit_msg,
            "commit_time":       t,
            "commit_hash":       commit_hash,
            "commit_ts":         commit_ts,
            "days_since_commit": round(days, 1),
            "dirty":             bool(status_raw),
            "changes":           len(status_raw.splitlines()) if status_raw else 0,
        }
    except:
        return {"is_git": False}


def compute_staleness(days: float, status: str, dirty: bool) -> int:
    if status in ("Paused", "Archived", "Complete"):
        return 0
    if days > 30:    score = min(85 + int((days - 30) / 10), 100)
    elif days > 14:  score = 65
    elif days > 7:   score = 40
    elif days > 2:   score = 15
    else:            score = 0
    if dirty and days > 1:
        score = min(score + 10, 100)
    return score


def compute_lens(days: float, status: str, dirty: bool, current_task: str) -> str:
    if status in ("Paused", "Archived", "Complete"):
        return "other"
    if days <= 2:
        return "active_now"
    if days > 30:
        return "neglected"
    if current_task and days > 5:
        return "needs_attention"
    if dirty and days > 3:
        return "needs_attention"
    return "active_now"


def auto_category(name: str, stack: list) -> str:
    if "Android" in stack:                          return "Android"
    if "Arduino" in stack:                          return "Hardware"
    if "Telegram Bot" in stack or "bot" in name.lower(): return "Bot"
    if "React" in stack or "Next.js" in stack:      return "Web"
    if "Python" in stack and "Node.js" not in stack:return "Python"
    if "Node.js" in stack:                          return "Backend"
    return "Other"


def get_readme_desc(path: Path) -> str:
    for n in ["README.md", "readme.md", "README.txt"]:
        f = path / n
        if f.exists():
            try:
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    l = line.strip()
                    if l and not l.startswith("#") and not l.startswith("!") \
                            and not l.startswith("[") and len(l) > 10:
                        return l[:160]
            except: pass
    return ""


# ── Build project payload ─────────────────────────────────────────────────────

async def build_project(item: Path) -> dict:
    meta         = await db_get_project(item.name)
    stack        = detect_stack(item)
    git          = get_git_info(item)
    days         = git.get("days_since_commit", 999)
    status       = meta.get("status", "Active")
    dirty        = git.get("dirty", False)
    current_task = meta.get("current_task", "")

    description = meta.get("description", "") or get_readme_desc(item) or "No description yet."
    category    = meta.get("category") or auto_category(item.name, stack)
    staleness   = compute_staleness(days, status, dirty)
    lens        = compute_lens(days, status, dirty, current_task)

    return {
        "name":               item.name,
        "description":        description,
        "category":           category,
        "status":             status,
        "ai_assigned":        meta.get("ai_assigned", "None"),
        "pinned":             bool(meta.get("pinned", 0)),
        "notes":              meta.get("notes", ""),
        "current_task":       current_task,
        "next_action":        meta.get("next_action", ""),
        "blockers":           meta.get("blockers", ""),
        "focus_updated_at":   meta.get("focus_updated_at", ""),
        "ai_context_snippet": meta.get("ai_context_snippet", ""),
        "ai_stack_summary":   meta.get("ai_stack_summary", ""),
        "ai_conventions":     meta.get("ai_conventions", ""),
        "last_opened_at":     meta.get("last_opened_at", ""),
        "tags":               json.loads(meta.get("tags", "[]") or "[]"),
        "link_repo":          meta.get("link_repo", ""),
        "link_docs":          meta.get("link_docs", ""),
        "link_deploy":        meta.get("link_deploy", ""),
        "stack":              stack,
        "git":                git,
        "staleness":          staleness,
        "lens":               lens,
    }


# ── Background git watcher ────────────────────────────────────────────────────

async def broadcast(data: dict):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(data)
        except:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


async def git_watcher():
    while True:
        await asyncio.sleep(60)
        updates = []
        for item in DEV_DIR.iterdir():
            if not item.is_dir() or item.name in SKIP or item.name.startswith("."):
                continue
            git = get_git_info(item)
            if git.get("is_git"):
                updates.append({"name": item.name, "git": git})
        if updates and ws_clients:
            await broadcast({
                "type":         "git_update",
                "projects":     updates,
                "refreshed_at": datetime.now().isoformat(),
            })


# ── App lifecycle ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await init_db()
    asyncio.create_task(git_watcher())
    print("KaiView running at http://localhost:3737")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root():
    return HTML_FILE.read_text(encoding="utf-8")


@app.get("/api/projects")
async def list_projects():
    projects = []
    for item in sorted(DEV_DIR.iterdir()):
        if not item.is_dir() or item.name in SKIP or item.name.startswith("."):
            continue
        projects.append(await build_project(item))
    projects.sort(key=lambda x: (not x["pinned"], x["name"].lower()))
    return projects


@app.put("/api/projects/{name}")
async def update_project(name: str, update: ProjectUpdate):
    fields = {k: v for k, v in update.dict().items() if v is not None}
    if "tags" in fields:
        fields["tags"] = json.dumps(fields["tags"])
    if "pinned" in fields:
        fields["pinned"] = 1 if fields["pinned"] else 0
    await db_upsert_project(name, fields)
    return {"ok": True}


@app.post("/api/projects/{name}/resume")
async def resume_project(name: str):
    path = DEV_DIR / name
    if not path.exists():
        raise HTTPException(404)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (project_name, started_at) VALUES (?,?)", (name, now)
        )
        await db.execute(
            "UPDATE projects SET last_opened_at=?, updated_at=? WHERE name=?", (now, now, name)
        )
        await db.commit()
    try:
        subprocess.Popen(f'code "{path}"', shell=True)
    except: pass

    meta   = await db_get_project(name)
    ai_ctx = ""
    if meta.get("ai_context_snippet") or meta.get("ai_stack_summary"):
        ai_ctx = f"Project: {name}\n"
        if meta.get("ai_stack_summary"):
            ai_ctx += f"Stack: {meta['ai_stack_summary']}\n"
        if meta.get("current_task"):
            ai_ctx += f"Current focus: {meta['current_task']}\n"
        if meta.get("ai_conventions"):
            ai_ctx += f"Conventions: {meta['ai_conventions']}\n"
        if meta.get("ai_context_snippet"):
            ai_ctx += f"\nContext: {meta['ai_context_snippet']}"

    return {
        "ok":           True,
        "current_task": meta.get("current_task", ""),
        "next_action":  meta.get("next_action", ""),
        "blockers":     meta.get("blockers", ""),
        "ai_context":   ai_ctx,
    }


@app.post("/api/projects/{name}/park")
async def park_project(name: str, body: ParkRequest):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE sessions SET ended_at=?, note=?, current_task=?, next_action=?
            WHERE project_name=? AND ended_at IS NULL
        """, (now, body.note, body.current_task, body.next_action, name))
        await db.execute("""
            UPDATE projects SET
                current_task=?, next_action=?, blockers=?,
                focus_updated_at=?, updated_at=?
            WHERE name=?
        """, (body.current_task, body.next_action, body.blockers, now, now, name))
        await db.commit()
    return {"ok": True}


@app.post("/api/projects/{name}/open")
def open_vscode(name: str):
    path = DEV_DIR / name
    if not path.exists():
        raise HTTPException(404)
    subprocess.Popen(f'code "{path}"', shell=True)
    return {"ok": True}


@app.get("/api/projects/{name}/git")
def git_details(name: str):
    path = DEV_DIR / name
    if not path.exists():
        raise HTTPException(404)
    try:
        log = subprocess.check_output(
            ["git", "log", "--oneline", "-15"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--short"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
        return {"log": log, "status": status or "Working tree clean"}
    except:
        return {"log": "", "status": "Not a git repository"}


@app.get("/api/projects/{name}/sessions")
async def get_sessions(name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM sessions WHERE project_name=? ORDER BY started_at DESC LIMIT 10",
            (name,)
        )).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/stats")
async def get_stats():
    projects = await list_projects()
    ai_c, st_c, cat_c, lens_c = {}, {}, {}, {}
    for p in projects:
        for d, k in [(ai_c, p["ai_assigned"]), (st_c, p["status"]),
                     (cat_c, p["category"]),   (lens_c, p["lens"])]:
            d[k] = d.get(k, 0) + 1
    return {"total": len(projects), "ai_counts": ai_c,
            "status_counts": st_c, "category_counts": cat_c, "lens_counts": lens_c}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in ws_clients:
            ws_clients.remove(ws)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3737, reload=False)
