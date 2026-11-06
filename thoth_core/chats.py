"""Which Hermes chat belongs to which Thoth project, kept on disk so it survives restarts.

Each Thoth project has one conversation with Hermes. The plugin records the chat a
project's requests went to (by session id), and the Desktop side window reads it to
show the right project next to the chat the user is looking at. Session ids are
opaque strings; a chat may be known by more than one (Hermes has durable and
runtime ids), so any of them can be bound to the same project.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

MAX_SESSIONS = 300


def _file(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / "chats.json"


def load(data_dir: str | os.PathLike) -> dict:
    try:
        data = json.loads(_file(data_dir).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict) and isinstance(data.get("projects"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"sessions": {}, "projects": {}}


def bind(data_dir: str | os.PathLike, session: str, project: str, primary: bool = False) -> None:
    """Remember that ``session`` talks about ``project``. ``primary`` makes it the project's own chat
    (the one Thoth sends that project's requests to); otherwise the project keeps its existing chat."""
    session, project = str(session or "").strip(), str(project or "").strip()
    if not session or not project:
        return
    data = load(data_dir)
    data["sessions"][session] = {"project": project, "at": time.time()}
    if primary or project not in data["projects"]:
        data["projects"][project] = session
    if len(data["sessions"]) > MAX_SESSIONS:
        keep = sorted(data["sessions"].items(), key=lambda kv: kv[1].get("at", 0))[-MAX_SESSIONS:]
        data["sessions"] = dict(keep)
    path = _file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def project_of(data_dir: str | os.PathLike, *sessions: str) -> str:
    """The project the first known of these session ids talks about, else ""."""
    known = load(data_dir)["sessions"]
    for s in sessions:
        hit = known.get(str(s or ""))
        if hit and hit.get("project"):
            return str(hit["project"])
    return ""


def chat_of(data_dir: str | os.PathLike, project: str) -> str:
    """The chat Thoth sends this project's requests to, else ""."""
    return str(load(data_dir)["projects"].get(str(project or ""), ""))
