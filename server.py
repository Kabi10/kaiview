import os
import json
import subprocess
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn

DEV_DIR = Path("C:/Dev")
META_FILE = DEV_DIR / "manager" / "projects_meta.json"
HTML_FILE = DEV_DIR / "manager" / "index.html"

SKIP = {"manager", ".claude", "__pycache__", "node_modules", ".git", "null", "shared", "tools"}

app = FastAPI(title="Dev Manager")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ProjectUpdate(BaseModel):
    description: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    ai_assigned: Optional[str] = None
    pinned: Optional[bool] = None
    notes: Optional[str] = None


def detect_stack(path: Path) -> list:
    stack = []
    try:
        files = {f.name for f in path.iterdir() if f.is_file()}
        subdirs = {d.name for d in path.iterdir() if d.is_dir()}
    except:
        return ["Unknown"]

    if "build.gradle.kts" in files or ("app" in subdirs and (path / "app" / "build.gradle.kts").exists()):
        stack.append("Android")

    if "package.json" in files:
        try:
            pkg = json.loads((path / "package.json").read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "react-native" in deps:
                stack.append("React Native")
            elif "react" in deps:
                stack.append("React")
            elif "next" in deps:
                stack.append("Next.js")
            else:
                stack.append("Node.js")
        except:
            stack.append("Node.js")

    py_files = any(f.endswith(".py") for f in files)
    if "requirements.txt" in files or "pyproject.toml" in files or py_files:
        stack.append("Python")

    if "bot.py" in files or "bot.js" in files:
        stack.append("Telegram Bot")

    has_ino = any(f.endswith(".ino") for f in files)
    if not has_ino:
        for d in subdirs:
            try:
                if any(f.name.endswith(".ino") for f in (path / d).iterdir() if f.is_file()):
                    has_ino = True
                    break
            except:
                pass
    if has_ino:
        stack.append("Arduino")

    if "vercel.json" in files:
        stack.append("Vercel")
    if "docker-compose.yml" in files or "Dockerfile" in files:
        stack.append("Docker")
    if "firebase.json" in files:
        stack.append("Firebase")
    if "supabase" in subdirs:
        stack.append("Supabase")

    return stack if stack else ["Unknown"]


def get_git_info(path: Path) -> dict:
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
        if not branch:
            return {"is_git": False}

        last_commit_raw = subprocess.check_output(
            ["git", "log", "-1", "--format=%s|||%cr|||%h"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()

        status_raw = subprocess.check_output(
            ["git", "status", "--short"],
            cwd=str(path), stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()

        parts = last_commit_raw.split("|||")
        return {
            "is_git": True,
            "branch": branch,
            "last_commit": parts[0] if parts else "",
            "commit_time": parts[1] if len(parts) > 1 else "",
            "commit_hash": parts[2] if len(parts) > 2 else "",
            "dirty": bool(status_raw),
            "changes": len(status_raw.splitlines()) if status_raw else 0,
        }
    except:
        return {"is_git": False}


def auto_category(name: str, stack: list) -> str:
    nl = name.lower()
    if "Android" in stack:
        return "Android"
    if "Arduino" in stack:
        return "Hardware"
    if "Telegram Bot" in stack or "bot" in nl:
        return "Bot"
    if "React" in stack or "Next.js" in stack or "React Native" in stack:
        return "Web"
    if "Python" in stack and "Node.js" not in stack:
        return "Python"
    if "Node.js" in stack:
        return "Backend"
    return "Other"


def load_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text())
        except:
            return {}
    return {}


def save_meta(meta: dict):
    META_FILE.write_text(json.dumps(meta, indent=2))


def get_description(path: Path) -> str:
    for readme_name in ["README.md", "readme.md", "README.txt"]:
        readme = path / readme_name
        if readme.exists():
            try:
                lines = readme.read_text(encoding="utf-8", errors="ignore").splitlines()
                for line in lines:
                    l = line.strip()
                    if l and not l.startswith("#") and not l.startswith("!") and not l.startswith("[") and len(l) > 10:
                        return l[:160]
            except:
                pass
    return ""


@app.get("/", response_class=HTMLResponse)
def root():
    return HTML_FILE.read_text(encoding="utf-8")


@app.get("/api/projects")
def list_projects():
    meta = load_meta()
    projects = []

    for item in sorted(DEV_DIR.iterdir()):
        if not item.is_dir() or item.name in SKIP or item.name.startswith("."):
            continue

        proj_meta = meta.get(item.name, {})
        stack = detect_stack(item)
        git_info = get_git_info(item)
        description = proj_meta.get("description", "") or get_description(item) or "No description yet."

        projects.append({
            "name": item.name,
            "description": description,
            "category": proj_meta.get("category", auto_category(item.name, stack)),
            "status": proj_meta.get("status", "Active"),
            "ai_assigned": proj_meta.get("ai_assigned", "None"),
            "stack": stack,
            "git": git_info,
            "pinned": proj_meta.get("pinned", False),
            "notes": proj_meta.get("notes", ""),
        })

    projects.sort(key=lambda x: (not x["pinned"], x["name"].lower()))
    return projects


@app.put("/api/projects/{name}")
def update_project(name: str, update: ProjectUpdate):
    meta = load_meta()
    if name not in meta:
        meta[name] = {}
    for field, value in update.dict(exclude_none=True).items():
        meta[name][field] = value
    save_meta(meta)
    return {"ok": True}


@app.post("/api/projects/{name}/open")
def open_vscode(name: str):
    path = DEV_DIR / name
    if not path.exists():
        raise HTTPException(404, "Project not found")
    try:
        subprocess.Popen(f'code "{path}"', shell=True)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


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
        return {"log": log, "status": status}
    except:
        return {"log": "", "status": "Not a git repository"}


@app.get("/api/stats")
def get_stats():
    meta = load_meta()
    projects = list_projects()
    ai_counts = {}
    status_counts = {}
    category_counts = {}

    for p in projects:
        ai = p["ai_assigned"]
        ai_counts[ai] = ai_counts.get(ai, 0) + 1
        s = p["status"]
        status_counts[s] = status_counts.get(s, 0) + 1
        c = p["category"]
        category_counts[c] = category_counts.get(c, 0) + 1

    return {
        "total": len(projects),
        "ai_counts": ai_counts,
        "status_counts": status_counts,
        "category_counts": category_counts,
    }


if __name__ == "__main__":
    print("Dev Manager running at http://localhost:3737")
    uvicorn.run(app, host="0.0.0.0", port=3737, reload=False)
