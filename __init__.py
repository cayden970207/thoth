"""Thoth for Hermes Agent: one shared brain for Claude Code, Codex and Hermes.

Hermes is the default coordinator. It plans work onto a shared task board, starts
the real Claude Code and Codex CLIs on tasks (each in its own git worktree),
has them review each other, and merges the results. Context, progress and
versions live in ``<project>/.thoth/`` so any of the three tools can pick up
where another left off, and the coordinator role can move to Claude Code or
Codex through the bundled MCP server.

Every action that starts an agent or merges into the base branch goes through
Hermes's own approval gate (pre_tool_call -> {"action": "approve"}).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .thoth_core import __version__
from .thoth_core.agents import driver_for
from .thoth_core.brain import Brain, BrainError, find_brain_root, parse_id
from .thoth_core.briefing import board_lines
from .thoth_core.dispatch import call, coordinator_howto, mcp_server_path
from .thoth_core.schemas import EMOJI, TOOLS
from .thoth_core.service import DEFAULT_SETTINGS, Thoth

logger = logging.getLogger(__name__)

TOOLSET = "thoth"
GATED = ("thoth_assign", "thoth_review", "thoth_merge")
TICKET_TTL = 15 * 60

_AGENT_LABEL = {"claude": "Claude Code", "codex": "Codex"}
_PERMISSION_NOTE = {
    ("claude", "build"): "permission mode {mode}: file edits allowed, shell commands only from the allow-list",
    ("claude", "review"): "read-only review: no edits, read-only git commands",
    ("codex", "build"): "sandbox workspace-write: Codex writes only inside the task worktree",
    ("codex", "review"): "sandbox read-only",
}


class ThothPlugin:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self._tickets: dict[tuple[str, str], tuple[float, str]] = {}
        self._run_sessions: dict[str, str] = {}
        self._lock = threading.Lock()

    # -- settings & project -----------------------------------------------------

    def settings(self) -> dict[str, Any]:
        out = {}
        for key, default in DEFAULT_SETTINGS.items():
            try:
                out[key] = self.ctx.get_config(key, default=default)
            except Exception:
                out[key] = default
        return out

    def service(self) -> Thoth:
        return Thoth(settings=self.settings(), actor="hermes", notifier=self._notify)

    def session_dir(self) -> str:
        for getter in ("session_cwd",):
            fn = getattr(self.ctx, getter, None)
            if callable(fn):
                try:
                    value = fn()
                    if value:
                        return str(value)
                except Exception:
                    pass
        try:
            from agent.runtime_cwd import resolve_agent_cwd

            return str(resolve_agent_cwd())
        except Exception:
            return ""

    def default_project(self) -> str:
        here = self.session_dir()
        if here and find_brain_root(here):
            return here
        try:
            last = self.ctx.state.get("last_project", default="")
        except Exception:
            last = ""
        if last and Path(last).exists():
            return last
        return here

    def remember(self, project: str) -> None:
        root = find_brain_root(project) if project else None
        if root is not None:
            try:
                self.ctx.state.set("last_project", str(root))
            except Exception:
                pass

    # -- approval ----------------------------------------------------------------

    def _approval_text(self, name: str, args: dict, svc: Thoth, project: str) -> tuple[str, str]:
        brain = svc.open(project)
        task = brain.task(parse_id(args.get("task"), "T"))
        if name == "thoth_merge":
            base = brain.meta().get("base_branch", "")
            text = (
                f"Thoth: merge {task['ref']} \"{task['title']}\" (by {task['agent']}, status {task['status']}) "
                f"from branch {task['branch']} into {base} in {brain.root} with git merge --no-ff. "
                "Conflicts are aborted and leave the base branch unchanged."
            )
            key = f"thoth:merge:{_short_hash(str(brain.root))}"
            return text, key
        if name == "thoth_review":
            agent = (args.get("reviewer") or ("codex" if task["agent"] == "claude" else "claude")).lower()
            kind = "review"
        else:
            agent = (args.get("agent") or task["agent"] or svc.settings["default_agent"]).lower()
            kind = "build"
        driver = driver_for(agent, svc.settings)
        ok, detail = driver.available()
        if not ok:
            raise BrainError(detail)
        note = _PERMISSION_NOTE[(agent, kind)].format(mode=svc.settings.get("claude_permission_mode", "acceptEdits"))
        excerpt = " ".join((task["detail"] or task["title"]).split())
        if len(excerpt) > 600:
            excerpt = excerpt[:600] + f"… [{len(task['detail'])} characters in total]"
        extra = " ".join(str(args.get("instructions") or "").split())
        if len(extra) > 400:
            extra = extra[:400] + "…"
        verb = "review" if kind == "review" else "work on"
        text = (
            f"Thoth: start {_AGENT_LABEL[agent]} ({detail}) to {verb} {task['ref']} \"{task['title']}\" in "
            f"{brain.root}/.thoth/worktrees/{task['ref']} ({note}); timeout {svc.settings['run_timeout_seconds']}s. "
            f"Uses your existing {_AGENT_LABEL[agent]} login and plan. Task: {excerpt}"
            + (f" Extra instructions: {extra}" if extra else "")
        )
        key = f"thoth:{kind}:{agent}:{_short_hash(str(brain.root))}"
        return text, key

    def pre_tool_call(self, tool_name: str = "", args: dict | None = None, task_id: str = "", session_id: str = "", **_: Any) -> dict | None:
        if tool_name not in GATED:
            return None
        args = dict(args or {})
        svc = self.service()
        project = str(args.get("project") or "").strip() or self.default_project()
        try:
            text, key = self._approval_text(tool_name, args, svc, project)
        except BrainError as exc:
            return {"action": "block", "message": f"Thoth: {exc}"}
        except Exception as exc:
            logger.warning("thoth: pre_tool_call check failed: %s", exc)
            return {"action": "block", "message": f"Thoth could not check this call: {exc}"}
        with self._lock:
            now = time.time()
            self._tickets = {k: v for k, v in self._tickets.items() if now - v[0] < TICKET_TTL}
            self._tickets[(session_id or task_id, _fingerprint(tool_name, args))] = (now, key)
        return {"action": "approve", "message": text, "rule_key": key}

    def _consume_ticket(self, name: str, args: dict, scope: str) -> bool:
        with self._lock:
            entry = self._tickets.pop((scope, _fingerprint(name, args)), None)
        return entry is not None and time.time() - entry[0] < TICKET_TTL

    @staticmethod
    def _gate(name: str) -> str:
        """A gated tool reached its handler without passing our pre_tool_call hook (and so without
        Hermes's approval gate). Fail closed rather than start an agent nobody approved."""
        return (
            f"BLOCKED: {name} starts an agent or merges code and must go through Hermes approval; "
            "call it as a normal tool so the approval prompt can be shown."
        )

    # -- tools -------------------------------------------------------------------

    def handler(self, name: str):
        def run(args: dict, **kwargs: Any) -> str:
            args = dict(args or {})
            svc = self.service()
            project = str(args.get("project") or "").strip() or self.default_project()
            scope = str(kwargs.get("session_id") or kwargs.get("task_id") or "")
            try:
                if name in GATED and not self._consume_ticket(name, args, scope):
                    return json.dumps({"ok": False, "error": self._gate(name)})
                result = call(svc, name, args, lambda: project)
                self.remember(project)
                if name in ("thoth_assign", "thoth_review") and result.get("run"):
                    self._bind_run(result["run"])
                if name == "thoth_coordinator":
                    result["how_to_continue"] = coordinator_howto(result["coordinator"], result["project"])
                return json.dumps({"ok": True, **result}, ensure_ascii=False)
            except BrainError as exc:
                return json.dumps({"ok": False, "error": str(exc)})
            except Exception as exc:
                logger.exception("thoth: %s failed", name)
                return json.dumps({"ok": False, "error": f"internal error: {exc}"})

        return run

    # -- completion notices ------------------------------------------------------

    def _bind_run(self, run_ref: str) -> None:
        key = ""
        try:
            from gateway.session_context import get_session_env

            key = get_session_env("HERMES_SESSION_KEY") or ""
        except Exception:
            pass
        with self._lock:
            self._run_sessions[run_ref] = key

    def _notify(self, project: str, run: dict) -> None:
        if not self.settings().get("notify_on_complete", True):
            return
        time.sleep(0.5)  # let the tool call that started the run record its session first
        with self._lock:
            session_key = self._run_sessions.pop(run["run"], "")
        what = "review" if run["kind"] == "review" else "build"
        lines = [f"[Thoth] {run['agent']} finished the {what} of {run['task']} \"{run['title']}\": {run['status']} → task is {run['task_status']}."]
        if run.get("verdict"):
            lines.append(f"Verdict: {run['verdict']}.")
        if run.get("summary"):
            lines.append(f"Summary: {run['summary'][:600]}")
        if run.get("comments"):
            lines.append("Review comments: " + " | ".join(run["comments"])[:800])
        if run.get("diffstat"):
            lines.append(f"Changes: {run['diffstat']} (commit {run.get('commit', '')}).")
        if run.get("error"):
            lines.append(f"Error: {run['error'][:400]}")
        lines.append("Check thoth_status and decide the next step (review, fix, merge or the next task).")
        try:
            self.ctx.inject_message("\n".join(lines), role="user", session_key=session_key or None)
        except Exception:
            logger.debug("thoth: inject_message failed", exc_info=True)

    # -- ambient context ---------------------------------------------------------

    def pre_llm_call(self, session_id: str = "", is_first_turn: bool = False, **_: Any) -> dict | None:
        """Tell the model a Thoth brain is active here, with a one-line board summary."""
        if not self.settings().get("inject_board", True):
            return None
        here = self.session_dir()
        root = find_brain_root(here) if here else None
        if root is None:
            return None
        try:
            brain = Brain(root)
            meta = brain.meta()
            tasks = brain.tasks()
            running = brain.runs(open_only=True)
        except Exception:
            return None
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in counts.items()) or "no tasks yet"
        lines = [
            f"[Thoth] This project has a shared multi-agent brain (coordinator: {meta.get('coordinator', 'hermes')}). "
            f"Goal: {meta.get('goal') or '(not set)'}. Board: {summary}."
        ]
        if running:
            lines.append("Running: " + "; ".join(f"{r['ref']} {r['agent']} {r['kind']} {r['task_ref']}: {r['progress'][:80]}" for r in running))
        lines.append("Use thoth_status for details. Record important decisions with thoth_note so Claude Code and Codex see them.")
        return {"context": "\n".join(lines)}

    # -- slash command -----------------------------------------------------------

    def slash(self, raw_args: str, **_: Any) -> str:
        parts = raw_args.strip().split()
        sub = parts[0].lower() if parts else "status"
        svc = self.service()
        project = self.default_project()
        try:
            if sub in ("status", "board"):
                data = svc.status(project)
                out = [
                    f"Thoth · {data['project']}",
                    f"Goal: {data['goal'] or '(not set)'} · coordinator: {data['coordinator']} · base: {data['base_branch']}",
                    "",
                    *data["board"],
                ]
                if data["running"]:
                    out += ["", "Running:"] + [f"  {r['run']} {r['agent']} {r['kind']} {r['task']}: {r['progress']}" for r in data["running"]]
                if data["decisions"]:
                    out += ["", "Decisions:"] + [f"  • {d}" for d in data["decisions"]]
                return "\n".join(out)
            if sub == "task" and len(parts) > 1:
                return json.dumps(svc.status(project, parts[1]), indent=2, ensure_ascii=False)
            if sub == "cancel" and len(parts) > 1:
                return json.dumps(svc.cancel(project, parts[1]), ensure_ascii=False)
            if sub == "coordinator" and len(parts) > 1:
                result = svc.set_coordinator(project, parts[1])
                return f"Coordinator: {result['previous']} → {result['coordinator']}\n\n" + coordinator_howto(result["coordinator"], result["project"])
            if sub == "brain":
                root = find_brain_root(project)
                return str(root / ".thoth" / "BRAIN.md") if root else "No Thoth brain here. Ask Hermes to start one (thoth_init)."
            if sub == "doctor":
                return doctor_text(svc.settings)
        except BrainError as exc:
            return f"Thoth: {exc}"
        return "Usage: /thoth [status | task T1 | cancel R1 | coordinator hermes|claude|codex | brain | doctor]"


def doctor_text(settings: dict[str, Any]) -> str:
    lines = [f"Thoth {__version__}"]
    for agent in ("claude", "codex"):
        ok, detail = driver_for(agent, settings).available()
        lines.append(f"  {'✓' if ok else '✗'} {_AGENT_LABEL[agent]}: {detail}")
    import shutil

    lines.append(f"  {'✓' if shutil.which('git') else '✗'} git: {shutil.which('git') or 'not found'}")
    lines.append(f"  MCP server: {mcp_server_path()}")
    return "\n".join(lines)


def _fingerprint(name: str, args: dict) -> str:
    relevant = {k: args.get(k) for k in ("project", "task", "agent", "reviewer", "instructions")}
    blob = json.dumps([name, relevant], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# -- CLI: hermes thoth ... -----------------------------------------------------------


def _cli_setup(subparser) -> None:
    subs = subparser.add_subparsers(dest="thoth_command")
    subs.add_parser("doctor", help="Check that Claude Code, Codex and git are ready")
    mcp = subs.add_parser("mcp", help="Print the command that connects Claude Code or Codex to the Thoth brain")
    mcp.add_argument("agent", choices=["claude", "codex"])
    st = subs.add_parser("status", help="Show the Thoth board for a project")
    st.add_argument("project", nargs="?", default=".")
    subparser.set_defaults(func=_cli_handler)


def _cli_handler(args) -> None:
    sub = getattr(args, "thoth_command", None)
    if sub == "doctor":
        print(doctor_text(dict(DEFAULT_SETTINGS)))
    elif sub == "mcp":
        print(coordinator_howto(args.agent, "<your project>"))
    elif sub == "status":
        try:
            print("\n".join(board_lines(Brain.open(args.project))))
        except BrainError as exc:
            print(f"Thoth: {exc}")
    else:
        print("Usage: hermes thoth <doctor | mcp claude|codex | status [project]>")


_plugin: ThothPlugin | None = None


def register(ctx: Any) -> None:
    global _plugin
    plugin = ThothPlugin(ctx)
    _plugin = plugin
    for name, (description, parameters) in TOOLS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema={"name": name, "description": description, "parameters": parameters},
            handler=plugin.handler(name),
            emoji=EMOJI.get(name, ""),
        )
    ctx.register_hook("pre_tool_call", plugin.pre_tool_call)
    ctx.register_hook("pre_llm_call", plugin.pre_llm_call)
    ctx.register_command(
        "thoth",
        plugin.slash,
        description="Thoth shared brain: board, runs, coordinator",
        args_hint="[status | task T1 | cancel R1 | coordinator <agent> | brain | doctor]",
    )
    try:
        ctx.register_cli_command(name="thoth", help="Thoth multi-agent brain", setup_fn=_cli_setup, handler_fn=_cli_handler)
    except Exception as exc:
        logger.debug("thoth: CLI command not registered: %s", exc)
    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            if (child / "SKILL.md").is_file():
                ctx.register_skill(child.name, child / "SKILL.md")

    import atexit

    from .thoth_core.service import _MANAGER

    def _at_exit() -> None:
        _MANAGER.shutdown("Hermes exited")

    atexit.register(_at_exit)

    def _unload() -> None:
        atexit.unregister(_at_exit)
        _MANAGER.shutdown("the Thoth plugin was unloaded")

    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        on_unload(_unload)
