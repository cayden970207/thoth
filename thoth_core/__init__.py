"""Thoth core: a shared project brain that lets several coding agents work as one team.

This package is host-independent and uses only the Python standard library, so the
same code runs inside Hermes (the plugin) and inside the stdio MCP server that
Claude Code and Codex connect to.
"""

__version__ = "0.7.1"

AGENTS = ("claude", "codex", "hermes")
WORKER_AGENTS = ("claude", "codex")
