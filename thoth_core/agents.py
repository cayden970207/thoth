"""Drivers for the real coding-agent CLIs: Claude Code and Codex.

Each driver turns a Thoth run into a command line, then reads the CLI's JSON
event stream line by line to track the session id (for resume), live progress,
the final message and token usage.

Permissions are deliberately narrow and are never chosen by a model:

* build runs: Claude Code ``--permission-mode acceptEdits`` (edits allowed,
  shell commands only from an allow-list); Codex ``--sandbox workspace-write``
  (its OS sandbox: writes only inside the task worktree).
* review runs: read-only (Claude Code default mode with a read-only git
  allow-list; Codex ``--sandbox read-only``).

No driver ever passes a "dangerously skip permissions" style flag.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

# Environment variables that must not leak from the host into an agent CLI:
# provider keys and tokens (agents use their own logins), Hermes internals, and
# variables set by a parent Claude Code / Agent SDK process that would make a
# nested ``claude`` talk to the wrong endpoint with the wrong token.
_ENV_DROP_PATTERNS = [
    re.compile(p)
    for p in (
        r".*_API_KEY$",
        r".*_TOKEN$",
        r".*_SECRET$",
        r".*_PASSWORD$",
        r"^HERMES_.*",
        r"^CLAUDE_CODE_.*",
        r"^CLAUDE_AGENT_SDK_.*",
        r"^CLAUDECODE$",
        r"^ANTHROPIC_.*",
        r"^OPENAI_.*",
        r"^CODEX_.*",
    )
]
# CODEX_HOME is how Codex finds its own login; keep it if the user set it.
_ENV_ALWAYS_KEEP = {"CODEX_HOME", "PATH", "HOME", "USER", "LANG", "SHELL", "TMPDIR"}


def clean_env(passthrough: list[str] | tuple[str, ...] = ()) -> dict[str, str]:
    keep = set(_ENV_ALWAYS_KEEP) | set(passthrough)
    env = {}
    for key, value in os.environ.items():
        if key in keep or not any(p.match(key) for p in _ENV_DROP_PATTERNS):
            env[key] = value
    env["THOTH_CHILD"] = "1"
    return env


@dataclass
class RunState:
    """What a driver learned from the event stream so far."""

    session_id: str = ""
    progress: str = ""
    activity: int = 0
    final_text: str = ""
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False


@dataclass
class McpSpec:
    """How the agent should launch the Thoth MCP server for this run."""

    command: str
    args: list[str]


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Driver:
    name = ""
    binary = ""
    label = ""

    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = settings or {}

    def resolve(self) -> str | None:
        configured = str(self.settings.get(f"{self.name}_path") or "").strip()
        if configured:
            return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
        return shutil.which(self.binary)

    def available(self) -> tuple[bool, str]:
        path = self.resolve()
        if not path:
            return False, f"{self.label} CLI ('{self.binary}') was not found on PATH; install it or set {self.name}_path"
        return True, path

    def command(self, *, kind: str, session_id: str, mcp: McpSpec | None) -> list[str]:
        raise NotImplementedError

    def parse(self, line: str, state: RunState) -> None:
        raise NotImplementedError


class ClaudeDriver(Driver):
    name = "claude"
    binary = "claude"
    label = "Claude Code"

    READ_ONLY_TOOLS = ["Read", "Glob", "Grep", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git status:*)"]

    def command(self, *, kind: str, session_id: str, mcp: McpSpec | None) -> list[str]:
        cmd = [self.resolve() or self.binary, "-p", "--output-format", "stream-json", "--verbose"]
        allowed: list[str] = []
        if kind == "review":
            cmd += ["--permission-mode", "default"]
            allowed += self.READ_ONLY_TOOLS
        else:
            mode = str(self.settings.get("claude_permission_mode") or "acceptEdits")
            if mode not in ("acceptEdits", "default", "plan"):
                mode = "acceptEdits"
            cmd += ["--permission-mode", mode]
            allowed += list(self.READ_ONLY_TOOLS)
            allowed += [str(t) for t in (self.settings.get("claude_allowed_tools") or [])]
        model = str(self.settings.get("claude_model") or "").strip()
        if model:
            cmd += ["--model", model]
        if session_id:
            cmd += ["--resume", session_id]
        if mcp is not None:
            config = {"mcpServers": {"thoth": {"type": "stdio", "command": mcp.command, "args": mcp.args}}}
            cmd += ["--mcp-config", json.dumps(config)]
            allowed.append("mcp__thoth")
        if allowed:
            cmd += ["--allowedTools", ",".join(dict.fromkeys(allowed))]
        return cmd

    def parse(self, line: str, state: RunState) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        etype = event.get("type")
        if event.get("session_id") and not state.session_id:
            state.session_id = str(event["session_id"])
        if etype == "system" and event.get("subtype") == "api_retry":
            state.progress = f"retrying API call ({event.get('error') or event.get('error_status')})"
        elif etype == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    state.progress = _short(block["text"])
                    state.final_text = block["text"]
                elif block.get("type") == "tool_use":
                    state.activity += 1
                    state.progress = _short(f"{block.get('name')}: {_tool_hint(block.get('input') or {})}")
        elif etype == "result":
            state.final_text = str(event.get("result") or state.final_text)
            state.is_error = bool(event.get("is_error"))
            if state.is_error:
                state.error = _short(state.final_text, 400)
            usage = event.get("usage") or {}
            state.usage = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cost_usd": event.get("total_cost_usd"),
                "turns": event.get("num_turns"),
            }


class CodexDriver(Driver):
    name = "codex"
    binary = "codex"
    label = "Codex"

    def command(self, *, kind: str, session_id: str, mcp: McpSpec | None) -> list[str]:
        exe = self.resolve() or self.binary
        sandbox = "read-only" if kind == "review" else "workspace-write"
        if session_id:
            cmd = [exe, "exec", "resume", session_id, "--json", "-c", f'sandbox_mode="{sandbox}"']
        else:
            cmd = [exe, "exec", "--json", "--sandbox", sandbox]
        model = str(self.settings.get("codex_model") or "").strip()
        if model:
            cmd += ["--model", model]
        if mcp is not None:
            # The worker-mode server exposes only thoth_status / thoth_context / thoth_note
            # (read the brain, write a note into it); it cannot launch agents or touch files.
            # codex exec runs with approval policy "never", which would otherwise refuse every
            # MCP call, so these three tools are pre-approved for this one server.
            cmd += [
                "-c", f"mcp_servers.thoth.command={json.dumps(mcp.command)}",
                "-c", f"mcp_servers.thoth.args={json.dumps(mcp.args)}",
                "-c", 'mcp_servers.thoth.default_tools_approval_mode="approve"',
            ]
        cmd.append("-")  # prompt from stdin
        return cmd

    def parse(self, line: str, state: RunState) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        etype = event.get("type", "")
        if etype == "thread.started":
            state.session_id = str(event.get("thread_id") or state.session_id)
        elif etype in ("item.started", "item.completed"):
            item = event.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message" and etype == "item.completed":
                state.final_text = str(item.get("text") or "")
                state.progress = _short(state.final_text)
            elif itype == "command_execution" and etype == "item.started":
                state.activity += 1
                state.progress = _short(f"$ {item.get('command', '')}")
            elif itype == "file_change" and etype == "item.completed":
                state.activity += 1
                paths = [c.get("path", "") for c in item.get("changes") or []]
                state.progress = _short("edited " + ", ".join(os.path.basename(p) for p in paths if p))
            elif itype == "mcp_tool_call" and etype == "item.started":
                state.activity += 1
                state.progress = _short(f"{item.get('server', '')}.{item.get('tool', '')}")
            elif itype == "reasoning" and etype == "item.completed":
                text = item.get("text") or ""
                if text:
                    state.progress = _short(text)
        elif etype == "turn.completed":
            state.usage = dict(event.get("usage") or {})
        elif etype in ("turn.failed", "error"):
            err = event.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else event.get("message")
            state.is_error = True
            state.error = _short(str(message or "Codex reported an error"), 400)


def _tool_hint(args: dict) -> str:
    for key in ("file_path", "path", "command", "pattern", "url", "description"):
        if args.get(key):
            value = str(args[key])
            return os.path.basename(value) if key in ("file_path", "path") else value
    return ""


DRIVERS = {"claude": ClaudeDriver, "codex": CodexDriver}


def driver_for(agent: str, settings: dict[str, Any] | None = None) -> Driver:
    try:
        return DRIVERS[agent](settings)
    except KeyError as exc:
        raise ValueError(f"Unknown agent '{agent}'. Workers: {', '.join(DRIVERS)}") from exc
