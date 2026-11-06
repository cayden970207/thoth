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
            result = svc.assign(path, task, agent=str(body.get("agent") or ""), instructions=str(body.get("instructions") or ""))
        elif kind == "review":
            result = svc.review(path, task, reviewer=str(body.get("reviewer") or ""))
        elif kind == "cancel":
            result = svc.cancel(path, body.get("run"))
        elif kind == "coordinator":
            result = svc.set_coordinator(path, str(body.get("agent") or ""))
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
async def thumb(project: str = "", task: str = "", path: str = "") -> dict[str, Any]:
    """An image an agent produced, as a data URL for the page (images inside the project only)."""
    import base64

    root = Path(_project(project))
    rel = Path(path)
    if not path or rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(400, "Give a path relative to the project")
    mime = _IMAGE_TYPES.get(rel.suffix.lower())
    if mime is None:
        raise HTTPException(415, "Only images have thumbnails")
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
