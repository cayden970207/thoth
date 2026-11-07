"""Thoth core: a shared project brain that lets several coding agents work as one team.

This package is host-independent and uses only the Python standard library, so the
same code runs inside Hermes (the plugin) and inside the stdio MCP server that
Claude Code and Codex connect to.
"""

import re as _re
from pathlib import Path as _Path


def _version() -> str:
    """The version in plugin.yaml, the one place it is written (CHANGELOG.md says what each one brings)."""
    try:
        text = (_Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return "0+unknown"
    found = _re.search(r"^version:\s*[\"']?([0-9][^\"'\s#]*)", text, _re.M)
    return found.group(1) if found else "0+unknown"


__version__ = _version()

AGENTS = ("claude", "codex", "hermes")
WORKER_AGENTS = ("claude", "codex")
