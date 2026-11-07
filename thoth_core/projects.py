"""The projects Thoth has opened, most recent first, kept in the plugin's data folder (projects.json).

The Desktop page serves only these, so it can never be pointed at an arbitrary folder. Taking a
project off this list ("Remove from Thoth") leaves its files and its .thoth brain where they are;
starting Thoth in that folder again brings it back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_PROJECTS = 20


def _file(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / "projects.json"


def load(data_dir: str | os.PathLike) -> list[str]:
    """Every project root Thoth knows, most recent first."""
    try:
        data = json.loads(_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in data if isinstance(p, str) and p] if isinstance(data, list) else []


def _save(data_dir: str | os.PathLike, known: list[str]) -> None:
    path = _file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(known[:MAX_PROJECTS], indent=2), encoding="utf-8")
    tmp.replace(path)


def remember(data_dir: str | os.PathLike, root: str | os.PathLike) -> None:
    """Put a project at the top of the list (adding it when new)."""
    root = str(root or "").strip()
    if root:
        _save(data_dir, [root] + [p for p in load(data_dir) if p != root])


def forget(data_dir: str | os.PathLike, root: str | os.PathLike) -> bool:
    """Take a project off the list. True when it was there."""
    root = str(root or "").strip()
    known = load(data_dir)
    if root not in known:
        return False
    _save(data_dir, [p for p in known if p != root])
    return True
