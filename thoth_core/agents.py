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
    cwd: str = ""
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    # Sub-agents the agent delegated to: {id, role, title, status, activity, last, started, ended}
    delegations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def rel(self, path: str) -> str:
        """A path relative to the run's worktree, or "" for anything outside it."""
        if not path:
            return ""
        path = str(path).strip().strip("'\"")
        if self.cwd and os.path.isabs(path):
            root = os.path.realpath(self.cwd)
            full = os.path.realpath(path)
            if full == root or not full.startswith(root + os.sep):
                return ""
            path = os.path.relpath(full, root)
        while path.startswith("./"):
            path = path[2:]
        if path.startswith("../") or path.startswith(".git/") or path in (".git", ".", ""):
            return ""
        return path

    def saw_read(self, path: str) -> None:
        rel = self.rel(path)
        if rel and rel not in self.files_read and rel not in self.files_written:
            self.files_read.append(rel)
            del self.files_read[:-200]

    def saw_write(self, path: str) -> None:
        rel = self.rel(path)
        if rel and rel not in self.files_written:
            self.files_written.append(rel)
            del self.files_written[:-200]
        if rel in self.files_read:
            self.files_read.remove(rel)

    def delegate(self, key: str, **fields: Any) -> dict[str, Any]:
        import time as _time

        d = self.delegations.get(key)
        if d is None:
            d = {"id": key, "role": "", "title": "", "status": "running", "activity": 0, "last": "", "started": _time.time(), "ended": None}
            self.delegations[key] = d
        for k, v in fields.items():
            if v not in (None, ""):
                d[k] = v
        if d["status"] in ("completed", "failed", "cancelled") and not d["ended"]:
            d["ended"] = _time.time()
        return d

    def files(self) -> dict[str, Any]:
        return {"read": list(self.files_read), "written": list(self.files_written)}

    def delegation_list(self) -> list[dict[str, Any]]:
        return sorted(self.delegations.values(), key=lambda d: d["started"])


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
    EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
    # Claude Code's OS sandbox (Seatbelt on macOS, bubblewrap on Linux): shell commands run
    # without a prompt but can only write inside the worktree and temp dirs, and may not
    # fall back to running unsandboxed. This is the same footing Codex gets from
    # --sandbox workspace-write, and it lets Claude run the tests it writes.
    SANDBOX = {"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False}}

    def command(self, *, kind: str, session_id: str, mcp: McpSpec | None) -> list[str]:
        cmd = [self.resolve() or self.binary, "-p", "--output-format", "stream-json", "--verbose"]
        allowed: list[str] = []
        sandbox = bool(self.settings.get("claude_sandbox", True))
        lean = bool(self.settings.get("lean_workers", True))
        settings: dict[str, Any] = dict(self.SANDBOX) if sandbox else {}
        if lean:
            # Measured: 46.8k -> 25.2k prompt tokens per call. The task brief carries the context
            # the run needs; the user's plugins, hooks, skills and other MCP servers do not.
            settings["disableAllHooks"] = True
            cmd += ["--strict-mcp-config", "--disable-slash-commands"]
        if settings:
            cmd += ["--settings", json.dumps(settings)]
        if kind == "review":
            cmd += ["--permission-mode", "default", "--disallowedTools", ",".join(self.EDIT_TOOLS)]
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
        elif etype == "system" and event.get("subtype", "").startswith("task_") and event.get("tool_use_id"):
            # Sub-agent lifecycle: task_started / task_progress / task_updated / task_notification
            key = str(event["tool_use_id"])
            patch = event.get("patch") or {}
            status = str(event.get("status") or patch.get("status") or "")
            text = event.get("description") or ""
            state.delegate(key, title=text if event.get("subtype") == "task_started" else "", last=_short(text, 120) if event.get("subtype") == "task_progress" else "",
                           role=event.get("subagent_type") or "",
                           status={"completed": "completed", "failed": "failed", "killed": "cancelled", "cancelled": "cancelled"}.get(status, ""))
        elif etype == "assistant":
            parent = event.get("parent_tool_use_id")
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    if parent:
                        state.delegate(str(parent), last=_short(block["text"], 120))
                        continue
                    state.progress = _short(block["text"])
                    state.final_text = block["text"]
                elif block.get("type") == "tool_use":
                    name, args = block.get("name") or "", block.get("input") or {}
                    state.activity += 1
                    hint = _short(f"{name}: {_tool_hint(args)}")
                    if name in ("Read", "NotebookRead"):
                        state.saw_read(args.get("file_path") or args.get("notebook_path") or "")
                    elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                        state.saw_write(args.get("file_path") or args.get("notebook_path") or "")
                    if name in ("Agent", "Task"):
                        state.delegate(str(block.get("id")), role=args.get("subagent_type") or "agent", title=args.get("description") or "", status="running")
                        hint = _short(f"Delegated to {args.get('subagent_type') or 'a sub-agent'}: {args.get('description') or ''}")
                    if parent:
                        d = state.delegate(str(parent))
                        d["activity"] += 1
                        d["last"] = hint
                    else:
                        state.progress = hint
        elif etype == "user":
            for block in (event.get("message") or {}).get("content") or [] if isinstance((event.get("message") or {}).get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and str(block.get("tool_use_id")) in state.delegations:
                    state.delegate(str(block["tool_use_id"]), status="failed" if block.get("is_error") else "completed")
        elif etype == "result":
            state.final_text = str(event.get("result") or state.final_text)
            state.is_error = bool(event.get("is_error"))
            if state.is_error:
                state.error = _short(state.final_text, 400)
            usage = event.get("usage") or {}
            fresh = int(usage.get("input_tokens") or 0)
            written = int(usage.get("cache_creation_input_tokens") or 0)
            read = int(usage.get("cache_read_input_tokens") or 0)
            state.usage = {
                "prompt_tokens": fresh + written + read,
                "cached_tokens": read,
                "output_tokens": int(usage.get("output_tokens") or 0),
                "turns": event.get("num_turns"),
                "api_equivalent_usd": event.get("total_cost_usd"),
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
        if self.settings.get("lean_workers", True):
            # Measured: 35.7k -> 27.1k prompt tokens per call. Skips the user's config.toml
            # (their MCP servers, profiles, agent roles); auth still comes from CODEX_HOME.
            cmd.append("--ignore-user-config")
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
                command = str(item.get("command", ""))
                state.progress = _short(f"$ {_unwrap_shell(command)}")
                for token in _path_tokens(command):
                    if state.cwd and os.path.isfile(os.path.join(state.cwd, state.rel(token) or "\0")):
                        state.saw_read(token)
            elif itype == "file_change" and etype == "item.completed":
                state.activity += 1
                paths = [c.get("path", "") for c in item.get("changes") or []]
                for path in paths:
                    state.saw_write(path)
                state.progress = _short("edited " + ", ".join(os.path.basename(p) for p in paths if p))
            elif itype == "collab_tool_call":
                tool = item.get("tool") or ""
                receivers = item.get("receiver_thread_ids") or []
                if tool in ("spawn_agent", "spawn"):
                    prompt = str(item.get("prompt") or "")
                    role = str(item.get("agent_type") or item.get("role") or "") or "sub-agent"
                    key = receivers[0] if receivers else str(item.get("id"))
                    state.delegate(key, role=role, title=_short(prompt, 90), status="running" if item.get("status") != "failed" else "failed")
                    state.progress = _short(f"Delegated to {role}: {prompt}")
                for thread_id, info in (item.get("agents_states") or {}).items():
                    status = str((info or {}).get("status") if isinstance(info, dict) else info or "").lower()
                    mapped = "completed" if status in ("completed", "done", "finished", "shutdown") else "failed" if "error" in status or "fail" in status else ""
                    if thread_id in state.delegations or mapped:
                        state.delegate(thread_id, status=mapped)
            elif itype == "mcp_tool_call" and etype == "item.started":
                state.activity += 1
                state.progress = _short(f"{item.get('server', '')}.{item.get('tool', '')}")
            elif itype == "reasoning" and etype == "item.completed":
                text = item.get("text") or ""
                if text:
                    state.progress = _short(text)
        elif etype == "turn.completed":
            u = event.get("usage") or {}
            prev = state.usage or {}
            state.usage = {
                "prompt_tokens": int(prev.get("prompt_tokens", 0)) + int(u.get("input_tokens") or 0),
                "cached_tokens": int(prev.get("cached_tokens", 0)) + int(u.get("cached_input_tokens") or 0),
                "output_tokens": int(prev.get("output_tokens", 0)) + int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0),
                "turns": int(prev.get("turns", 0) or 0) + 1,
            }
        elif etype in ("turn.failed", "error"):
            err = event.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else event.get("message")
            state.is_error = True
            state.error = _short(str(message or "Codex reported an error"), 400)


_PATH_TOKEN = re.compile(r"(?<![\w/.-])((?:\.{0,2}/)?[\w@.-]+(?:/[\w@.-]+)*\.[A-Za-z0-9]{1,8})(?![\w/])")


def _path_tokens(command: str) -> list[str]:
    """File-looking tokens in a shell command (cat, sed, rg, magick, ...), for the "read" list."""
    return list(dict.fromkeys(m.group(1) for m in _PATH_TOKEN.finditer(command)))[:20]


def _unwrap_shell(command: str) -> str:
    m = re.match(r"^/bin/(?:ba|z)?sh -lc ['\"](.*)['\"]$", command.strip(), re.S)
    return m.group(1) if m else command


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
