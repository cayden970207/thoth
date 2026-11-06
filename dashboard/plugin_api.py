"""Thoth backend for the Hermes Desktop page (mounted at /api/plugins/thoth/).

Read routes return the shared brain of a project the user has opened with Thoth.
Action routes run what the user clicked on the Thoth page (merge, start an agent,
review, cancel, change coordinator); the click is the user's decision, so they
do not show a second Hermes approval prompt. The README discloses this.

Only projects Thoth itself registered (in plugin-data/thoth/projects.json) are
served, so the page cannot be pointed at an arbitrary folder.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

_PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _core():
    """Reuse the plugin's already-loaded core when Hermes imported it, else load our own copy."""
    for name in list(sys.modules):
        if name.endswith(".thoth_core.service") and "thoth" in name:
            mod = sys.modules[name]
            if Path(getattr(mod, "__file__", "")).resolve().parent.parent == _PLUGIN_DIR:
                return sys.modules[name[: -len(".service")]]
    pkg_name = "thoth_dashboard_core"
    if pkg_name in sys.modules:
        return sys.modules[pkg_name]
    spec = importlib.util.spec_from_file_location(
        pkg_name, _PLUGIN_DIR / "thoth_core" / "__init__.py", submodule_search_locations=[str(_PLUGIN_DIR / "thoth_core")]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return module


def _svc():
    core = _core()
    service = importlib.import_module(core.__name__ + ".service")
    settings = {}
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        settings = (((cfg.get("plugins") or {}).get("entries") or {}).get("thoth") or {}).get("settings") or {}
    except Exception:
        pass
    return service.Thoth(settings=settings, actor="hermes")


def _projects() -> list[str]:
    try:
        from plugins.plugin_storage import plugin_data_dir

        path = plugin_data_dir("thoth") / "projects.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception:
        data = []
    return [p for p in data if isinstance(p, str) and (Path(p) / ".thoth" / "brain.db").is_file()]


def _project(project: str) -> str:
    known = _projects()
    if not project:
        if not known:
            raise HTTPException(404, "No Thoth project yet. Ask Hermes to start one with thoth_init.")
        return known[0]
    resolved = str(Path(project).expanduser().resolve())
    if resolved not in known:
        raise HTTPException(403, "That folder is not a Thoth project Hermes opened")
    return resolved


def _brain_error(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


@router.get("/projects")
async def projects() -> dict[str, Any]:
    out = []
    for p in _projects():
        try:
            meta = _core_brain(p).meta()
        except Exception:
            meta = {}
        out.append({"path": p, "name": Path(p).name, "goal": meta.get("goal", ""), "coordinator": meta.get("coordinator", "hermes")})
    return {"projects": out}


def _core_brain(project: str):
    core = _core()
    brain = importlib.import_module(core.__name__ + ".brain")
    return brain.Brain.open(project)


@router.get("/snapshot")
async def snapshot(project: str = "") -> dict[str, Any]:
    path = _project(project)
    try:
        return _svc().snapshot(path)
    except Exception as exc:
        raise _brain_error(exc)


@router.post("/action")
async def action(body: dict) -> dict[str, Any]:
    path = _project(str(body.get("project") or ""))
    kind = str(body.get("action") or "")
    svc = _svc()
    task = body.get("task")
    try:
        if kind == "merge":
            result = svc.merge(path, task, override=bool(body.get("override")))
        elif kind == "assign":
            result = svc.assign(path, task, agent=str(body.get("agent") or ""), instructions=str(body.get("instructions") or ""),
                                model=str(body.get("model") or ""), effort=str(body.get("effort") or ""))
        elif kind == "review":
            result = svc.review(path, task, reviewer=str(body.get("reviewer") or ""), model=str(body.get("model") or ""), effort=str(body.get("effort") or ""))
        elif kind == "cancel":
            result = svc.cancel(path, body.get("run"))
        elif kind == "coordinator":
            result = svc.set_coordinator(path, str(body.get("agent") or ""))
        elif kind == "revise":
            result = svc.revise(path, task, str(body.get("feedback") or ""), agent=str(body.get("agent") or ""), files=body.get("files") or [],
                                model=str(body.get("model") or ""), effort=str(body.get("effort") or ""))
        elif kind == "restore":
            result = svc.restore(path, task, str(body.get("commit") or ""))
        elif kind == "sync":
            result = svc.sync(path, task)
        elif kind == "update":
            result = svc.update_task(path, task, title=str(body.get("title") or ""), detail=str(body.get("detail") or ""), agent=str(body.get("agent") or ""),
                                     kind=str(body.get("kind") or ""), role=str(body.get("role") or ""),
                                     model=body.get("model") if "model" in body else None, effort=body.get("effort") if "effort" in body else None)
        elif kind == "plan":
            result = svc.plan(path, body.get("tasks") or [])
        elif kind == "note":
            result = svc.note(path, str(body.get("kind") or "context"), str(body.get("body") or ""), task=task)
        else:
            raise HTTPException(400, f"Unknown action {kind!r}")
    except HTTPException:
        raise
    except Exception as exc:
        raise _brain_error(exc)
    return {"ok": True, **result}


_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
_MAX_THUMB_BYTES = 2_000_000


@router.get("/thumb")
async def thumb(project: str = "", task: str = "", path: str = "", commit: str = "") -> dict[str, Any]:
    """An image an agent produced, as a data URL for the page (images inside the project only).
    With ``commit``, the image as it was in that version (read from git, nothing is checked out)."""
    import base64
    import re
    import subprocess

    root = Path(_project(project))
    rel = Path(path)
    if not path or rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(400, "Give a path relative to the project")
    mime = _IMAGE_TYPES.get(rel.suffix.lower())
    if mime is None:
        raise HTTPException(415, "Only images have thumbnails")
    if commit:
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
            raise HTTPException(400, "Bad commit id")
        proc = subprocess.run(["git", "show", f"{commit}:{rel.as_posix()}"], cwd=str(root), capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
        if proc.returncode != 0 or not proc.stdout:
            raise HTTPException(404, "Not in that version")
        if len(proc.stdout) > _MAX_THUMB_BYTES:
            raise HTTPException(413, "Image too large for a thumbnail")
        return {"path": path, "mime": mime, "data": f"data:{mime};base64,{base64.b64encode(proc.stdout).decode('ascii')}"}
    candidates = []
    if task:
        candidates.append(root / ".thoth" / "worktrees" / task.upper() / rel)
    candidates.append(root / rel)
    for file in candidates:
        try:
            resolved = file.resolve()
        except OSError:
            continue
        if not str(resolved).startswith(str(root.resolve()) + "/") or not resolved.is_file():
            continue
        if resolved.stat().st_size > _MAX_THUMB_BYTES:
            raise HTTPException(413, "Image too large for a thumbnail")
        data = base64.b64encode(resolved.read_bytes()).decode("ascii")
        return {"path": path, "mime": mime, "data": f"data:{mime};base64,{data}"}
    raise HTTPException(404, "File not found")


def _connect_cmd(agent: str) -> list[str]:
    core = _core()
    dispatch = importlib.import_module(core.__name__ + ".dispatch")
    return [agent, "mcp", "add", "thoth", "--", sys.executable, "-I", str(dispatch.mcp_server_path()), "--as", agent]


def _cli_env() -> dict:
    core = _core()
    agents = importlib.import_module(core.__name__ + ".agents")
    return agents.clean_env()


def _connected(agent: str, project: str) -> bool:
    import shutil
    import subprocess

    exe = shutil.which(agent)
    if not exe:
        return False
    try:
        proc = subprocess.run([exe, "mcp", "get", "thoth"], cwd=project, capture_output=True, text=True, timeout=12,
                              stdin=subprocess.DEVNULL, env=_cli_env())
    except Exception:
        return False
    return proc.returncode == 0 and "thoth" in (proc.stdout + proc.stderr).lower()


@router.get("/connect")
async def connect(project: str = "") -> dict[str, Any]:
    """Setup commands that let Claude Code and Codex read and drive this brain, and whether they already can."""
    import asyncio
    import shlex
    import shutil

    path = _project(project)
    out: dict[str, Any] = {"project": path, "brain_file": str(Path(path) / ".thoth" / "BRAIN.md")}
    statuses = await asyncio.gather(*(asyncio.to_thread(_connected, a, path) for a in ("claude", "codex")))
    for agent, ok in zip(("claude", "codex"), statuses):
        cmd = _connect_cmd(agent)
        out[agent] = {
            "command": " ".join(shlex.quote(c) for c in cmd),
            "short": f"{agent} mcp add thoth -- python -I …/thoth/mcp_server.py --as {agent}",
            "installed": bool(shutil.which(agent)),
            "connected": ok,
            "scope": "this project" if agent == "claude" else "every project on this Mac",
        }
    return out


@router.post("/connect/{agent}")
async def connect_add(agent: str, body: dict) -> dict[str, Any]:
    """Run the setup command for the user (their click is the consent). Claude Code: local scope in this project;
    Codex: its global config.toml."""
    import asyncio
    import shutil
    import subprocess

    if agent not in ("claude", "codex"):
        raise HTTPException(404, "Unknown agent")
    path = _project(str(body.get("project") or ""))
    exe = shutil.which(agent)
    if not exe:
        raise HTTPException(400, f"{agent} is not installed")
    if await asyncio.to_thread(_connected, agent, path):
        return {"ok": True, "agent": agent, "already": True}
    cmd = [exe] + _connect_cmd(agent)[1:]

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=path, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, env=_cli_env())

    proc = await asyncio.to_thread(run)
    if proc.returncode != 0:
        raise HTTPException(400, (proc.stderr or proc.stdout or "the command failed").strip()[:400])
    return {"ok": True, "agent": agent, "already": False}


@router.get("/models")
async def models() -> dict[str, Any]:
    import asyncio

    return await asyncio.to_thread(lambda: _svc().models())
