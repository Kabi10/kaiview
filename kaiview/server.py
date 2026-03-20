import asyncio
import importlib
import importlib.resources
import json
import re
import shlex
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Config ────────────────────────────────────────────────────────────────────

_KAIVIEW_DIR = Path.home() / ".kaiview"
_CFG_FILE    = _KAIVIEW_DIR / "config.toml"


def _build_default_config() -> dict:
    if sys.version_info >= (3, 11):
        import tomllib as _tl
    else:
        try:
            import tomllib as _tl
        except ImportError:
            import tomli as _tl  # type: ignore
    text = importlib.resources.files("kaiview").joinpath("config_template.toml").read_text()
    return _tl.loads(text)


def _load_config_from(path: Path) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib as _tl
    else:
        try:
            import tomllib as _tl
        except ImportError:
            import tomli as _tl  # type: ignore
    try:
        return _tl.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[kaiview] config.toml parse error: {e} — using defaults")
        return _build_default_config()


def _migrate_config_keys(cfg: dict) -> dict:
    """Migrate old key structure to new on first load of an existing config."""
    # [kaiview] → [projects]
    if "kaiview" in cfg and "projects" not in cfg:
        cfg["projects"] = cfg.pop("kaiview")
    # github.token → github.pat
    if "github" in cfg and "token" in cfg["github"] and "pat" not in cfg["github"]:
        cfg["github"]["pat"] = cfg["github"].pop("token")
    return cfg


def _ensure_config() -> dict:
    _KAIVIEW_DIR.mkdir(parents=True, exist_ok=True)
    if not _CFG_FILE.exists():
        template_bytes = importlib.resources.files("kaiview").joinpath("config_template.toml").read_bytes()
        _CFG_FILE.write_bytes(template_bytes)
        print(f"[kaiview] Created default config at {_CFG_FILE}")

    # DB migration: copy old kaiview.db from package dir if new location is empty
    new_db = _KAIVIEW_DIR / "kaiview.db"
    if not new_db.exists():
        import importlib.util as _imp_util
        pkg_spec = _imp_util.find_spec("kaiview")
        if pkg_spec and pkg_spec.origin:
            old_db = Path(pkg_spec.origin).parent / "kaiview.db"
            if old_db.exists():
                import shutil
                shutil.copy2(old_db, new_db)
                print(f"[kaiview] Migrated database from {old_db} to {new_db}")

    return _migrate_config_keys(_load_config_from(_CFG_FILE))


CFG = _ensure_config()


def _dev_dirs() -> list[Path]:
    """Return all project root directories. Supports dev_dirs (list) and legacy dev_dir (string)."""
    proj = CFG.get("projects", {})
    raw_list = proj.get("dev_dirs")
    if raw_list and isinstance(raw_list, list):
        paths = [Path(r).expanduser().resolve() for r in raw_list]
    else:
        raw = proj.get("dev_dir", "~")
        paths = [Path(raw).expanduser().resolve()]
    valid = [p for p in paths if p.is_dir()]
    return valid if valid else [Path.home()]


def _skip_set() -> set:
    return set(CFG.get("projects", {}).get("skip", [
        ".git", "node_modules", "__pycache__", ".venv", "venv"
    ]))


def _find_project_path(name: str) -> Path | None:
    """Find a project directory by name across all configured roots."""
    for d in DEV_DIRS:
        p = d / name
        if p.exists():
            return p
    return None


DEV_DIRS     = _dev_dirs()
SKIP         = _skip_set()
DB_PATH      = _KAIVIEW_DIR / "kaiview.db"
SCHEMA_VER   = 3
GITHUB_TOKEN = CFG.get("github", {}).get("pat", "")
HEALTH_CFG   = CFG.get("health", {})
AUTH_TOKEN   = CFG.get("auth", {}).get("token", "")

# Read HTML into memory at startup. importlib.resources.files() returns a Traversable,
# NOT a filesystem Path in installed wheels. Always read the content directly.
_HTML_CONTENT: str = importlib.resources.files("kaiview").joinpath("index.html").read_text(encoding="utf-8")

app = FastAPI(title="KaiView")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """If an auth token is configured, require it on all /api/* requests."""
    if AUTH_TOKEN and request.url.path.startswith("/api/"):
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {AUTH_TOKEN}":
            return JSONResponse(status_code=401, content={"error": "Unauthorized — provide Authorization: Bearer <token>"})
    return await call_next(request)

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
    github_url:         Optional[str]  = None
    live_url:           Optional[str]  = None
    docs_url:           Optional[str]  = None
    deploy_url:         Optional[str]  = None
    tags:               Optional[list] = None

class ParkRequest(BaseModel):
    note:         str = ""
    current_task: str = ""
    next_action:  str = ""
    blockers:     str = ""

class JournalEntry(BaseModel):
    body: str
    mood: str = "note"  # note | win | blocker | idea

class AiLogEntry(BaseModel):
    model:   str = ""
    topic:   str = ""
    outcome: str = ""
    notes:   str = ""


# ── Settings models ───────────────────────────────────────────────────────────

class HealthWeights(BaseModel):
    commit_weight:      int
    dirty_weight:       int
    readme_weight:      int
    description_weight: int

class SettingsResponse(BaseModel):
    port:        int
    dev_dirs:    list[str]
    github_pat:  str
    auth_token:  str
    skip:        list[str]
    health:      HealthWeights

class SettingsUpdate(BaseModel):
    port:        int
    dev_dirs:    list[str]
    github_pat:  str
    auth_token:  str
    skip:        list[str]
    health:      HealthWeights


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
                outcome       TEXT DEFAULT '',
                notes         TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS journal (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name  TEXT NOT NULL,
                logged_at     TEXT NOT NULL,
                body          TEXT NOT NULL,
                mood          TEXT DEFAULT 'note'
            );
        """)

        await db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VER),))

        # ── Add new columns to existing tables if missing (safe migration) ──
        existing_proj_cols = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(projects)")
            ).fetchall()
        }
        for col, defn in [
            ("github_url", "TEXT DEFAULT ''"),
            ("live_url",   "TEXT DEFAULT ''"),
            ("docs_url",   "TEXT DEFAULT ''"),
            ("deploy_url", "TEXT DEFAULT ''"),
        ]:
            if col not in existing_proj_cols:
                await db.execute(f"ALTER TABLE projects ADD COLUMN {col} {defn}")

        existing_log_cols = {
            row[1] for row in await (
                await db.execute("PRAGMA table_info(ai_logs)")
            ).fetchall()
        }
        if "notes" not in existing_log_cols:
            await db.execute("ALTER TABLE ai_logs ADD COLUMN notes TEXT DEFAULT ''")

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
        # Only treat as a git repo if this directory IS the repo root.
        # This prevents picking up a parent repo (e.g. C:/Dev/.git) and
        # showing its hundreds of cross-project changes.
        git_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
        try:
            if Path(git_root).resolve() != path.resolve():
                return {"is_git": False}
        except Exception:
            return {"is_git": False}

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

        # Tracked changes only (staged + modified tracked files) — avoids inflated
        # counts from untracked build artifacts / node_modules.
        tracked_raw = subprocess.check_output(
            ["git", "status", "--short", "--untracked-files=no"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()

        # Untracked dirs/files grouped at directory level (--untracked-files=normal).
        # This counts node_modules/ as 1, not 40 000.
        untracked_raw = subprocess.check_output(
            ["git", "status", "--short", "--untracked-files=normal"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()

        tracked_lines   = tracked_raw.splitlines() if tracked_raw else []
        untracked_lines = [l for l in (untracked_raw.splitlines() if untracked_raw else [])
                           if l.startswith("??")]
        changes = len(tracked_lines) + len(untracked_lines)

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
            "dirty":             bool(tracked_raw or untracked_raw),
            "changes":           changes,
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


# ── Sparkline ────────────────────────────────────────────────────────────────

def get_sparkline(path: Path, days: int = 7) -> list[int]:
    """Return commit counts per day for the last N days (index 0 = oldest)."""
    try:
        since = f"{days} days ago"
        raw = subprocess.check_output(
            ["git", "log", f"--since={since}", "--format=%ct"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=8
        ).strip()
        if not raw:
            return [0] * days

        now_ts = int(datetime.now(timezone.utc).timestamp())
        buckets = [0] * days
        for line in raw.splitlines():
            try:
                ts  = int(line.strip())
                day = int((now_ts - ts) / 86400)
                if 0 <= day < days:
                    buckets[days - 1 - day] += 1
            except: pass
        return buckets
    except:
        return [0] * days


# ── Health score ──────────────────────────────────────────────────────────────

def compute_health(path: Path, git: dict, stack: list, meta: dict) -> int:
    """Compute 0-100 health score using config.toml weights."""
    h   = HEALTH_CFG
    score  = 100
    days   = git.get("days_since_commit", 999)
    status = meta.get("status", "Active")

    if status in ("Paused", "Archived"):
        return 50  # neutral — intentionally inactive

    stale_h = h.get("stale_high",   30);  pen_h = h.get("penalty_stale_high",   35)
    stale_m = h.get("stale_medium", 14);  pen_m = h.get("penalty_stale_medium", 20)
    stale_l = h.get("stale_low",     7);  pen_l = h.get("penalty_stale_low",    10)
    pen_w   = h.get("penalty_stale_week",   5)

    if days > stale_h * 2: score -= pen_h
    elif days > stale_h:   score -= pen_m
    elif days > stale_m:   score -= pen_l
    elif days > stale_l:   score -= pen_w

    if git.get("dirty"):                                    score -= h.get("penalty_dirty",    10)
    if not any((path / n).exists() for n in ["README.md", "readme.md"]):
                                                            score -= h.get("penalty_no_readme", 10)
    if not git.get("is_git"):                               score -= h.get("penalty_no_git",   20)
    if not meta.get("description"):                         score -= h.get("penalty_no_desc",   5)
    if meta.get("current_task"):                            score += h.get("bonus_has_task",    5)

    return max(0, min(100, score))


# ── Detect start command ──────────────────────────────────────────────────────

def detect_start_command(path: Path, stack: list) -> str:
    """Auto-detect the best command to start this project's dev environment."""
    try:
        files = {f.name for f in path.iterdir() if f.is_file()} if path.exists() else set()
    except PermissionError:
        files = set()

    if "Makefile" in files:
        return "make dev"
    if "docker-compose.yml" in files:
        return "docker-compose up"
    if "package.json" in files:
        try:
            pkg      = json.loads((path / "package.json").read_text())
            scripts  = pkg.get("scripts", {})
            for s in ["dev", "start", "serve"]:
                if s in scripts:
                    return f"npm run {s}"
        except: pass
        return "npm start"
    if "bot.py" in files:
        return "python bot.py"
    if "main.py" in files:
        return "python main.py"
    if "server.py" in files:
        return "python server.py"
    if "manage.py" in files:
        return "python manage.py runserver"
    if "requirements.txt" in files:
        return "python main.py"
    if "build.gradle.kts" in files:
        return "./gradlew run"
    return ""


# ── GitHub integration ────────────────────────────────────────────────────────

_gh_cache: dict[str, tuple[float, dict]] = {}   # name -> (ts, data)
_GH_TTL   = 300                                  # 5-minute cache

def _parse_github_remote(path: Path) -> tuple[str, str] | None:
    """Return (owner, repo) if origin remote is a GitHub URL, else None."""
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
    except Exception:
        return None

    # https://github.com/owner/repo.git  or  git@github.com:owner/repo.git
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


async def fetch_github_data(path: Path, name: str) -> dict:
    """Fetch GitHub repo stats (cached, TTL 5 min)."""
    ts, cached = _gh_cache.get(name, (0.0, {}))
    if time.monotonic() - ts < _GH_TTL and cached:
        return cached

    info = await asyncio.to_thread(_parse_github_remote, path)
    if not info:
        return {}

    owner, repo = info
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    result: dict = {"owner": owner, "repo": repo, "url": f"https://github.com/{owner}/{repo}"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            # Repo metadata
            r = await client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
            if r.status_code == 200:
                d = r.json()
                result.update({
                    "stars":       d.get("stargazers_count", 0),
                    "forks":       d.get("forks_count", 0),
                    "open_issues": d.get("open_issues_count", 0),
                    "language":    d.get("language", ""),
                    "visibility":  d.get("visibility", "public"),
                    "default_branch": d.get("default_branch", "main"),
                })

            # Open PRs
            pr = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                params={"state": "open", "per_page": 1},
                headers={**headers, "Accept": "application/vnd.github+json"}
            )
            if pr.status_code == 200:
                # GitHub returns Link header for total count; use list length as minimum
                result["open_prs"] = len(pr.json())
                link = pr.headers.get("Link", "")
                m2 = re.search(r'page=(\d+)>; rel="last"', link)
                if m2:
                    result["open_prs"] = int(m2.group(1))

            # Latest CI run
            ci = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/actions/runs",
                params={"per_page": 1},
                headers=headers
            )
            if ci.status_code == 200:
                runs = ci.json().get("workflow_runs", [])
                if runs:
                    run = runs[0]
                    result["ci"] = {
                        "status":     run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "name":       run.get("name"),
                        "url":        run.get("html_url"),
                        "updated_at": run.get("updated_at"),
                    }
    except Exception as e:
        result["error"] = str(e)

    _gh_cache[name] = (time.monotonic(), result)
    return result


# ── Running processes registry ────────────────────────────────────────────────

running_processes: dict[str, subprocess.Popen] = {}


# ── Build project payload ─────────────────────────────────────────────────────

async def build_project(item: Path) -> dict:
    # DB read (async) + all blocking I/O in thread pool — run in parallel
    meta, stack, git, gh_remote = await asyncio.gather(
        db_get_project(item.name),
        asyncio.to_thread(detect_stack, item),
        asyncio.to_thread(get_git_info, item),
        asyncio.to_thread(_parse_github_remote, item),
    )
    days         = git.get("days_since_commit", 999)
    status       = meta.get("status", "Active")
    dirty        = git.get("dirty", False)
    current_task = meta.get("current_task", "")

    # Auto-populate github_url from git remote if not already stored
    auto_github_url = ""
    if gh_remote:
        owner, repo = gh_remote
        auto_github_url = f"https://github.com/{owner}/{repo}"

    description = meta.get("description", "") or await asyncio.to_thread(get_readme_desc, item) or "No description yet."
    category    = meta.get("category") or auto_category(item.name, stack)
    staleness   = compute_staleness(days, status, dirty)
    lens        = compute_lens(days, status, dirty, current_task)

    sparkline     = await asyncio.to_thread(get_sparkline, item) if git.get("is_git") else [0] * 7
    health        = compute_health(item, git, stack, {**meta, "status": status, "description": description})
    start_command = await asyncio.to_thread(detect_start_command, item, stack)
    is_running    = item.name in running_processes and running_processes[item.name].poll() is None

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
        "github_url":         meta.get("github_url", "") or auto_github_url,
        "live_url":           meta.get("live_url", ""),
        "docs_url":           meta.get("docs_url", ""),
        "deploy_url":         meta.get("deploy_url", ""),
        "stack":              stack,
        "git":                git,
        "staleness":          staleness,
        "lens":               lens,
        "sparkline":          sparkline,
        "health":             health,
        "start_command":      start_command,
        "is_running":         is_running,
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


# ── Watchdog git watcher ──────────────────────────────────────────────────────

_LOOP: asyncio.AbstractEventLoop | None = None


class _GitEventHandler(FileSystemEventHandler):
    """Fires when a .git/logs/HEAD or COMMIT_EDITMSG changes."""

    def __init__(self, project_name: str):
        super().__init__()
        self.project_name = project_name
        self._last = 0.0

    def on_modified(self, event):
        src = str(event.src_path).replace("\\", "/")
        if not any(k in src for k in ("logs/HEAD", "COMMIT_EDITMSG", "index")):
            return
        now = time.monotonic()
        if now - self._last < 2:        # debounce 2s
            return
        self._last = now
        if _LOOP:
            asyncio.run_coroutine_threadsafe(
                _broadcast_git_change(self.project_name), _LOOP
            )


async def _broadcast_git_change(name: str):
    path = _find_project_path(name)
    if not path:
        return
    git  = await asyncio.to_thread(get_git_info, path)
    if git.get("is_git") and ws_clients:
        await broadcast({
            "type":         "git_update",
            "projects":     [{"name": name, "git": git}],
            "refreshed_at": datetime.now().isoformat(),
        })


def _start_watchdog():
    """Start a watchdog Observer for every git project across all dev roots."""
    observer = Observer()
    seen: set[str] = set()
    for dev_dir in DEV_DIRS:
        try:
            for item in dev_dir.iterdir():
                if not item.is_dir() or item.name in SKIP or item.name.startswith("."):
                    continue
                if item.name in seen:
                    continue
                seen.add(item.name)
                git_dir = item / ".git"
                if git_dir.is_dir():
                    handler = _GitEventHandler(item.name)
                    observer.schedule(handler, str(git_dir), recursive=True)
        except Exception:
            pass
    observer.start()
    return observer


# Fallback: keep a slow background poll so newly-created repos are picked up
async def git_watcher():
    while True:
        await asyncio.sleep(300)        # 5-min fallback (watchdog handles real-time)
        items = await asyncio.to_thread(_scan_dir)
        updates = []
        for item in items:
            git = await asyncio.to_thread(get_git_info, item)
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
    global _LOOP
    _LOOP = asyncio.get_event_loop()
    await init_db()
    # Real-time watchdog (filesystem events) + slow fallback poll
    asyncio.to_thread(_start_watchdog)
    asyncio.create_task(git_watcher())
    port = CFG.get("server", {}).get("port", 3737)
    print(f"KaiView running at http://localhost:{port}")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root():
    return _HTML_CONTENT


def _scan_dir() -> list[Path]:
    """Synchronous filesystem scan across all dev roots — called via asyncio.to_thread."""
    seen: set[str] = set()
    result: list[Path] = []
    for dev_dir in DEV_DIRS:
        try:
            for p in dev_dir.iterdir():
                if p.is_dir() and p.name not in SKIP and not p.name.startswith(".") and p.name not in seen:
                    seen.add(p.name)
                    result.append(p)
        except Exception:
            pass
    return sorted(result, key=lambda p: p.name.lower())


@app.get("/api/projects")
async def list_projects():
    items    = await asyncio.to_thread(_scan_dir)
    # Build all projects in parallel — each awaits its own thread-pool calls
    projects = list(await asyncio.gather(*[build_project(item) for item in items]))
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
    path = _find_project_path(name)
    if not path:
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
        subprocess.Popen(["code", str(path)], shell=False)
    except Exception:
        pass

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
    path = _find_project_path(name)
    if not path:
        raise HTTPException(404)
    subprocess.Popen(["code", str(path)], shell=False)
    return {"ok": True}


@app.post("/api/projects/{name}/launch")
async def launch_project(name: str):
    """Start the project's dev environment in a terminal."""
    path = _find_project_path(name)
    if not path:
        raise HTTPException(404)

    # Kill if already running
    if name in running_processes:
        proc = running_processes[name]
        if proc.poll() is None:
            proc.terminate()
            del running_processes[name]
            return {"ok": True, "action": "stopped", "is_running": False}

    stack   = detect_stack(path)
    cmd     = detect_start_command(path, stack)
    if not cmd:
        raise HTTPException(400, "No start command detected for this project")

    try:
        args = shlex.split(cmd)   # safe tokenization — no shell=True
        proc = subprocess.Popen(
            args, cwd=str(path), shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True
        )
        running_processes[name] = proc
        return {"ok": True, "action": "started", "is_running": True, "command": cmd}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/projects/{name}/deps")
def scan_deps(name: str):
    """Scan dependency files and return a summary."""
    path = _find_project_path(name)
    if not path:
        raise HTTPException(404)

    result: dict = {"name": name, "deps": [], "warnings": []}

    # Python
    req = path / "requirements.txt"
    if req.exists():
        lines = [l.strip() for l in req.read_text(errors="ignore").splitlines()
                 if l.strip() and not l.startswith("#")]
        result["deps"].extend({"file": "requirements.txt", "pkg": l} for l in lines[:30])

    pyproject = path / "pyproject.toml"
    if pyproject.exists():
        result["deps"].append({"file": "pyproject.toml", "pkg": "(see file)"})

    # Node
    pkg_json = path / "package.json"
    if pkg_json.exists():
        try:
            pkg  = json.loads(pkg_json.read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            for k, v in list(deps.items())[:30]:
                result["deps"].append({"file": "package.json", "pkg": f"{k}@{v}"})
        except:
            result["warnings"].append("Could not parse package.json")

    # Gradle
    gradle = path / "app" / "build.gradle.kts"
    if not gradle.exists():
        gradle = path / "build.gradle.kts"
    if gradle.exists():
        text = gradle.read_text(errors="ignore")
        import re
        for m in re.findall(r'implementation\s*["\']([^"\']+)["\']', text)[:20]:
            result["deps"].append({"file": "build.gradle.kts", "pkg": m})

    result["total"] = len(result["deps"])
    return result


@app.get("/api/projects/{name}/github")
async def github_data(name: str):
    """Return cached GitHub stats (stars, forks, open PRs, CI status)."""
    path = _find_project_path(name)
    if not path:
        raise HTTPException(404)
    data = await fetch_github_data(path, name)
    return data


@app.get("/api/projects/{name}/git")
def git_details(name: str):
    path = _find_project_path(name)
    if not path:
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


@app.get("/api/projects/{name}/journal")
async def get_journal(name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM journal WHERE project_name=? ORDER BY logged_at DESC LIMIT 50",
            (name,)
        )).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/projects/{name}/journal")
async def add_journal(name: str, entry: JournalEntry):
    if not entry.body.strip():
        raise HTTPException(400, "Journal entry cannot be empty")
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO journal (project_name, logged_at, body, mood) VALUES (?,?,?,?)",
            (name, now, entry.body.strip(), entry.mood)
        )
        await db.commit()
    return {"ok": True}


@app.delete("/api/journal/{entry_id}")
async def delete_journal(entry_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM journal WHERE id=?", (entry_id,))
        await db.commit()
    return {"ok": True}


@app.get("/api/projects/{name}/ai-logs")
async def get_ai_logs(name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM ai_logs WHERE project_name=? ORDER BY logged_at DESC",
            (name,)
        )).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/projects/{name}/ai-logs")
async def add_ai_log(name: str, entry: AiLogEntry):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO ai_logs (project_name, logged_at, model, topic, outcome, notes) VALUES (?,?,?,?,?,?)",
            (name, now, entry.model, entry.topic, entry.outcome, entry.notes)
        )
        await db.commit()
    return {"ok": True}


@app.delete("/api/ai-logs/{log_id}")
async def delete_ai_log(log_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM ai_logs WHERE id=?", (log_id,))
        await db.commit()
    return {"ok": True}


# ── Cross-project file search ─────────────────────────────────────────────────

_SEARCH_SKIP = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build", ".next"}
_MAX_SEARCH_RESULTS = 200


def _rg_search(q: str, root: Path, project: str | None) -> list[dict]:
    """Search using ripgrep if available."""
    results = []
    search_dirs = []
    if project:
        p = root / project
        if p.is_dir():
            search_dirs = [p]
    else:
        try:
            search_dirs = [
                d for d in root.iterdir()
                if d.is_dir() and d.name not in SKIP and not d.name.startswith(".")
            ]
        except Exception:
            return []

    for sdir in search_dirs:
        proj_name = sdir.name
        try:
            proc = subprocess.run(
                [
                    "rg", "--line-number", "--no-heading", "--with-filename",
                    "--max-count=50", "--max-filesize=500K",
                    "--glob=!node_modules", "--glob=!.git", "--glob=!__pycache__",
                    "--glob=!venv", "--glob=!.venv", "--glob=!dist", "--glob=!build",
                    "-e", q,
                    str(sdir),
                ],
                capture_output=True, text=True, timeout=15
            )
            for line in proc.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                file_abs, lineno, content = parts[0], parts[1], parts[2]
                try:
                    rel = Path(file_abs).relative_to(sdir)
                except ValueError:
                    rel = Path(file_abs).name
                results.append({
                    "project": proj_name,
                    "file":    str(rel).replace("\\", "/"),
                    "line":    int(lineno) if lineno.isdigit() else 0,
                    "content": content.strip()[:200],
                })
                if len(results) >= _MAX_SEARCH_RESULTS:
                    return results
        except Exception:
            pass
    return results


def _py_search(q: str, root: Path, project: str | None) -> list[dict]:
    """Pure-Python fallback search using pathlib + re."""
    results = []
    pattern = re.compile(re.escape(q), re.IGNORECASE)

    if project:
        proj_dirs = [(project, root / project)]
    else:
        try:
            proj_dirs = [
                (d.name, d) for d in sorted(root.iterdir())
                if d.is_dir() and d.name not in SKIP and not d.name.startswith(".")
            ]
        except Exception:
            return []

    for proj_name, sdir in proj_dirs:
        try:
            for fpath in sdir.rglob("*"):
                # Skip unwanted dirs
                if any(part in _SEARCH_SKIP for part in fpath.parts):
                    continue
                if not fpath.is_file():
                    continue
                # Skip likely binary files by extension
                if fpath.suffix.lower() in {
                    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff",
                    ".woff2", ".ttf", ".eot", ".otf", ".pdf", ".zip", ".gz",
                    ".tar", ".bin", ".exe", ".dll", ".so", ".pyc", ".class",
                    ".db", ".sqlite", ".sqlite3",
                }:
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        try:
                            rel = fpath.relative_to(sdir)
                        except ValueError:
                            rel = fpath.name
                        results.append({
                            "project": proj_name,
                            "file":    str(rel).replace("\\", "/"),
                            "line":    lineno,
                            "content": line.strip()[:200],
                        })
                        if len(results) >= _MAX_SEARCH_RESULTS:
                            return results
        except Exception:
            pass
    return results


@app.get("/api/search/files")
async def search_files(q: str = "", project: str = ""):
    """Cross-project file content search. Uses rg if available, falls back to Python."""
    if not q or len(q.strip()) < 2:
        return []
    q = q.strip()
    proj = project.strip() or None

    # Try ripgrep first
    rg_available = await asyncio.to_thread(lambda: bool(subprocess.run(
        ["rg", "--version"], capture_output=True, timeout=3
    ).returncode == 0))

    all_results: list[dict] = []
    for dev_dir in DEV_DIRS:
        if rg_available:
            chunk = await asyncio.to_thread(_rg_search, q, dev_dir, proj)
        else:
            chunk = await asyncio.to_thread(_py_search, q, dev_dir, proj)
        all_results.extend(chunk)
        if len(all_results) >= _MAX_SEARCH_RESULTS:
            break

    return all_results[:_MAX_SEARCH_RESULTS]


@app.get("/api/search")
async def search_all(q: str = ""):
    """Full-text search across project name, description, notes, current_task, ai_context."""
    if not q or len(q.strip()) < 2:
        return []
    term = q.strip().lower()
    results = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("""
            SELECT name, description, current_task, notes, ai_context_snippet, status, category
            FROM projects
            WHERE lower(name) LIKE ? OR lower(description) LIKE ? OR lower(current_task) LIKE ?
               OR lower(notes) LIKE ? OR lower(ai_context_snippet) LIKE ?
            LIMIT 20
        """, (f"%{term}%",)*5)).fetchall()
        for r in rows:
            results.append(dict(r))
    return results


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


@app.get("/api/settings", response_model=SettingsResponse)
def get_settings():
    raw_pat   = CFG.get("github", {}).get("pat", "")
    raw_token = CFG.get("auth", {}).get("token", "")
    proj      = CFG.get("projects", {})
    # Return dev_dirs; fall back to legacy dev_dir if present
    dev_dirs  = proj.get("dev_dirs") or [proj.get("dev_dir", "~")]
    return {
        "port":       CFG.get("server", {}).get("port", 3737),
        "dev_dirs":   dev_dirs,
        "github_pat": "__MASKED__" if raw_pat else "",
        "auth_token": "__MASKED__" if raw_token else "",
        "skip":       proj.get("skip", []),
        "health":     CFG.get("health", {
            "commit_weight": 40, "dirty_weight": 20,
            "readme_weight": 20, "description_weight": 20,
        }),
    }


@app.post("/api/settings")
def update_settings(body: SettingsUpdate):
    global CFG, DEV_DIRS, SKIP, GITHUB_TOKEN, HEALTH_CFG, AUTH_TOKEN

    # Validate all dev_dirs exist
    if not body.dev_dirs:
        return JSONResponse(status_code=422, content={"error": "dev_dirs must not be empty", "code": "dev_dirs_empty"})
    for raw_dir in body.dev_dirs:
        p = Path(raw_dir).expanduser().resolve()
        if not p.is_dir():
            return JSONResponse(status_code=422, content={"error": f"Directory not found: {raw_dir}", "code": "dev_dir_not_found"})

    if not (1024 <= body.port <= 65535):
        return JSONResponse(status_code=422, content={"error": "Port must be 1024–65535", "code": "invalid_port"})

    w = body.health
    total = w.commit_weight + w.dirty_weight + w.readme_weight + w.description_weight
    if total != 100:
        return JSONResponse(status_code=422, content={"error": f"Weights must sum to 100 (got {total})", "code": "weights_dont_sum_to_100"})

    existing_pat   = CFG.get("github", {}).get("pat", "")
    existing_token = CFG.get("auth", {}).get("token", "")
    new_pat   = existing_pat   if body.github_pat  == "__MASKED__" else body.github_pat
    new_token = existing_token if body.auth_token  == "__MASKED__" else body.auth_token
    current_port = CFG.get("server", {}).get("port", 3737)

    # Build TOML — escape backslashes for Windows paths
    def _safe(s: str) -> str:
        return s.replace("\\", "\\\\")

    dev_dirs_toml = ", ".join(f'"{_safe(d)}"' for d in body.dev_dirs)
    skip_toml     = ", ".join(f'"{s}"' for s in body.skip)
    new_toml = (
        f'[server]\nport = {body.port}\n\n'
        f'[projects]\ndev_dirs = [{dev_dirs_toml}]\nskip = [{skip_toml}]\n\n'
        f'[github]\npat = "{_safe(new_pat)}"\n\n'
        f'[auth]\ntoken = "{_safe(new_token)}"\n\n'
        f'[health]\ncommit_weight = {w.commit_weight}\n'
        f'dirty_weight = {w.dirty_weight}\n'
        f'readme_weight = {w.readme_weight}\n'
        f'description_weight = {w.description_weight}\n'
    )
    _CFG_FILE.write_text(new_toml, encoding="utf-8")

    # Hot-reload in-memory globals (port change requires manual restart)
    CFG          = _load_config_from(_CFG_FILE)
    DEV_DIRS     = _dev_dirs()
    SKIP         = _skip_set()
    GITHUB_TOKEN = CFG.get("github", {}).get("pat", "")
    HEALTH_CFG   = CFG.get("health", {})
    AUTH_TOKEN   = CFG.get("auth", {}).get("token", "")

    result: dict = {"ok": True}
    if body.port != current_port:
        result["restart_required"] = True
        result["new_port"]         = body.port
    return result


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


def main():
    """Entry point for `kaiview` CLI command."""
    port = CFG.get("server", {}).get("port", 3737)

    def _open_browser():
        import time
        time.sleep(1.2)
        try:
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass  # silently skip on headless/CI

    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run("kaiview.server:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
