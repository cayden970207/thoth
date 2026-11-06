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
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .thoth_core import __version__
from .thoth_core import browser as web
from .thoth_core import chats
from .thoth_core.agents import driver_for
from .thoth_core.brain import Brain, BrainError, find_brain_root, parse_id
from .thoth_core.briefing import board_lines
from .thoth_core.dispatch import call, coordinator_howto, mcp_server_path
from .thoth_core.schemas import EMOJI, TOOLS
from .thoth_core.service import DEFAULT_SETTINGS, Thoth

logger = logging.getLogger(__name__)

TOOLSET = "thoth"
# The last line the Thoth page adds to a request typed there, so Hermes knows which project it is about.
_PAGE_TAG = re.compile(r"\n\s*— via Thoth · ([^\n]+?)\s*$")
GATED = ("thoth_assign", "thoth_revise", "thoth_review", "thoth_merge")
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
        # Completion notices. Keys are (project root, run ref): run refs restart at R1 in every project.
        self._run_sessions: dict[tuple[str, str], str] = {}   # runs a coordinator tool call started -> its session
        self._waiting: dict[tuple[str, str], int] = {}        # runs an in-flight thoth_wait will report itself
        self._held: dict[tuple[str, str], list[list[str]]] = {}  # (root, session) -> notices held for one batch
        self._session_projects: dict[str, str] = {}           # session -> project it last used Thoth on
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

    def project_for(self, args: dict, scope: str = "") -> str:
        """The project a call is about: the one it names, else the one this chat is bound to, else the default."""
        named = str(args.get("project") or "").strip()
        if named:
            return named
        with self._lock:
            bound = self._session_projects.get(scope, "") if scope else ""
        if not bound and scope:
            bound = chats.project_of(self._data_dir(), scope)  # bound before Hermes last restarted
        return bound or self.default_project()

    @staticmethod
    def _data_dir() -> Path:
        if os.environ.get("THOTH_DATA_DIR"):  # tests and custom setups
            return Path(os.environ["THOTH_DATA_DIR"])
        try:
            from plugins.plugin_storage import plugin_data_dir

            return Path(plugin_data_dir("thoth"))
        except Exception:
            return Path.home() / ".hermes" / "plugin-data" / "thoth"

    def bind_chat(self, session: str, root: str) -> None:
        """This chat talks about this project: remembered in memory and on disk (for the Desktop side window)."""
        if not session or not root:
            return
        with self._lock:
            self._session_projects[session] = root
        try:
            chats.bind(self._data_dir(), session, root)
        except Exception:
            logger.debug("thoth: could not record the chat for %s", root, exc_info=True)

    @staticmethod
    def _known_projects() -> list[str]:
        try:
            from plugins.plugin_storage import plugin_data_dir

            path = plugin_data_dir("thoth") / "projects.json"
            return [str(p) for p in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []
        except Exception:
            return []

    def _page_project(self, message: Any) -> str:
        """The project named by a Thoth page request's last line ("— via Thoth · tiny-crm"), as a brain root, else ""."""
        if isinstance(message, list):  # content parts
            message = " ".join(str(p.get("text") or "") for p in message if isinstance(p, dict))
        m = _PAGE_TAG.search(message if isinstance(message, str) else "")
        if not m:
            return ""
        ref = m.group(1).strip()
        candidates = [str(Path(ref).expanduser())] if ref.startswith(("/", "~")) else [p for p in self._known_projects() if Path(p).name == ref]
        for path in candidates:
            root = find_brain_root(path)
            if root is not None:
                return str(root)
        return ""

    def remember(self, project: str) -> None:
        root = find_brain_root(project) if project else None
        if root is None:
            return
        try:
            self.ctx.state.set("last_project", str(root))
        except Exception:
            pass
        # The Desktop page only serves projects listed here (most recent first).
        try:
            from plugins.plugin_storage import plugin_data_dir

            path = plugin_data_dir("thoth") / "projects.json"
            known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            known = [str(root)] + [p for p in known if p != str(root)]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(known[:20], indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.debug("thoth: could not record the project", exc_info=True)

    # -- approval ----------------------------------------------------------------

    def _approval_text(self, name: str, args: dict, svc: Thoth, project: str) -> tuple[str, str]:
        """What the approval card says: first what will happen, in plain words; the technical details last."""
        if name == "thoth_init":
            setup = " ".join(str(args.get("setup") or "").split())
            run = " ".join(str(args.get("run") or "").split())
            where = str(Path(project).expanduser())
            lines = []
            if setup:
                lines.append(f"Before an agent starts on a task, Thoth will run `{setup}` in that task's folder.")
            if run:
                lines.append(f"To preview the app, Thoth will start it with `{run}` in a copy of the version you open.")
            lines.append("These run as you, outside the agents' sandbox and with network access.")
            lines.append(f"Details: project {where}.")
            return "\n".join(lines), f"thoth:setup:{_short_hash(where + setup + '|' + run)}"
        brain = svc.open(project)
        task = brain.task(parse_id(args.get("task"), "T"))
        what = f"{task['ref']} “{task['title']}”"
        if name == "thoth_merge":
            base = brain.meta().get("base_branch", "") or "main"
            builder = _AGENT_LABEL.get(task["agent"], task["agent"].capitalize())
            if args.get("override"):
                head = f"Merge {what} into {base}, although its review comments are still open."
            else:
                head = f"Merge {what} into {base}."
            body = f"{builder} built it" + (", and the review passed." if task["status"] == "approved" else ".") + " If anything conflicts, nothing changes."
            text = f"{head}\n{body}\nDetails: git merge --no-ff {task['branch']} in {brain.root}."
            key = f"thoth:merge{':override' if args.get('override') else ''}:{_short_hash(str(brain.root))}"
            return text, key
        if name == "thoth_review":
            agent = (args.get("reviewer") or svc.default_reviewer(task["agent"])).lower()
            kind = "review"
        else:
            agent = (args.get("agent") or task["agent"] or svc.settings["default_agent"]).lower()
            kind = "build"
        driver = driver_for(agent, svc.settings)
        ok, detail = driver.available()
        if not ok:
            raise BrainError(detail)
        label = _AGENT_LABEL[agent]
        note = _PERMISSION_NOTE[(agent, kind)].format(mode=svc.settings.get("claude_permission_mode", "acceptEdits"))
        minutes = max(1, round(int(svc.settings["run_timeout_seconds"]) / 60))
        if kind == "review":
            builder = _AGENT_LABEL.get(task["agent"], task["agent"].capitalize())
            fresh = " in a fresh session, since the other agent can't run on this machine" if agent == task["agent"] else ""
            head = f"{label} will review {what}, which {builder} built{fresh}. It only reads; it cannot change anything."
        elif name == "thoth_revise":
            fb = " ".join(str(args.get("feedback") or "").split())
            head = f"{label} will rework {what} with your feedback: “{fb[:300]}{'…' if len(fb) > 300 else ''}”"
        else:
            head = f"{label} will work on {what}."
        lines = [head]
        browse = ""
        if kind == "build":
            lines.append(f"It can only change this task's own copy of the project, and stops after {minutes} minutes at most.")
            browse = web.normalize(args["browser"] if args.get("browser") is not None else task.get("browser"))
            if browse:
                ok, why = web.ready(browse)
                if not ok:
                    raise BrainError(why)
                lines.append(web.CARD[browse])
        model = str(args.get("model") or task.get("model") or svc.settings.get(f"{agent}_model") or "").strip()
        effort = str(args.get("effort") or task.get("effort") or "").strip()
        lines.append("Model: " + (model or "default") + (f" · {effort} effort" if effort else "") + ".")
        excerpt = " ".join((task["detail"] or task["title"]).split())
        if excerpt and excerpt != task["title"]:
            lines.append("Task: " + (excerpt[:300] + "…" if len(excerpt) > 300 else excerpt))
        extra = " ".join(str(args.get("instructions") or "").split())
        if extra:
            lines.append("Extra instructions: " + (extra[:300] + "…" if len(extra) > 300 else extra))
        browser_detail = f" · browser: {web.PACKAGE} via npx, {'headless and temporary' if browse == 'clean' else 'attached to your Chrome'}" if browse else ""
        lines.append(f"Details: {detail} · {brain.root}/.thoth/worktrees/{task['ref']} · {note}{browser_detail} · uses your {label} login and plan.")
        # A browser is its own rule: "always allow" for plain builds never covers runs that browse, least of all in your Chrome.
        key = f"thoth:{kind}:{agent}{':web-' + browse if browse else ''}:{_short_hash(str(brain.root))}"
        return "\n".join(lines), key

    def _self_assign(self, name: str, args: dict, svc: Thoth, project: str) -> bool:
        """thoth_assign to hermes starts no process: the coordinator does the work in its own turn."""
        if name not in ("thoth_assign", "thoth_revise"):
            return False
        agent = str(args.get("agent") or "").strip().lower()
        if not agent:
            try:
                agent = svc.open(project).task(parse_id(args.get("task"), "T"))["agent"]
            except Exception:
                return False
        return agent == "hermes"

    @staticmethod
    def _gated(name: str, args: dict) -> bool:
        return name in GATED or (name == "thoth_init" and bool(str(args.get("setup") or args.get("run") or "").strip()))

    def pre_tool_call(self, tool_name: str = "", args: dict | None = None, task_id: str = "", session_id: str = "", **_: Any) -> dict | None:
        args = dict(args or {})
        if not self._gated(tool_name, args):
            return None
        svc = self.service()
        project = self.project_for(args, session_id or task_id)
        if self._self_assign(tool_name, args, svc, project):
            return None
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
        self._mark_waiting(project, text.split("\n", 1)[0], session_id or task_id)
        return {"action": "approve", "message": text, "rule_key": key}

    def post_tool_call(self, tool_name: str = "", args: dict | None = None, status: str = "", **_: Any) -> None:
        """A gated call the user rejected (or that was blocked) is no longer waiting for an OK."""
        args = dict(args or {})
        if status == "blocked" and self._gated(tool_name, args):
            project = self.project_for(args, str(_.get("session_id") or _.get("task_id") or ""))
            self._mark_waiting(project, "", "")

    @staticmethod
    def _mark_waiting(project: str, summary: str, session: str) -> None:
        """Let the Thoth page say that Hermes is waiting for the user's OK (and open that chat)."""
        try:
            root = find_brain_root(project) if project else None
            if root is not None:
                Brain(root).set_meta("pending_approval", json.dumps({"summary": summary[:220], "session": session, "at": time.time()}) if summary else "")
        except Exception:
            logger.debug("thoth: could not record the pending approval", exc_info=True)

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
            scope = str(kwargs.get("session_id") or kwargs.get("task_id") or "")
            project = self.project_for(args, scope)
            try:
                if self._gated(name, args) and not self._self_assign(name, args, svc, project):
                    if not self._consume_ticket(name, args, scope):
                        return json.dumps({"ok": False, "error": self._gate(name)})
                    self._mark_waiting(project, "", "")  # approved: nothing is waiting any more
                if name == "thoth_wait":
                    result = self._wait(svc, args, project)
                else:
                    result = call(svc, name, args, lambda: project)
                self.remember(project)
                root = find_brain_root(project) if project else None
                if root is not None and scope:
                    self.bind_chat(scope, _rootkey(root))
                if name in ("thoth_assign", "thoth_review", "thoth_revise") and result.get("run") and root is not None:
                    self._bind_run(_rootkey(root), result["run"])
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

    def _bind_run(self, root: str, run_ref: str) -> None:
        key = ""
        try:
            from gateway.session_context import get_session_env

            key = get_session_env("HERMES_SESSION_KEY") or ""
        except Exception:
            pass
        with self._lock:
            self._run_sessions[(root, run_ref)] = key

    def _wait(self, svc: Thoth, args: dict, project: str) -> dict:
        """thoth_wait, remembering which runs it will report so no separate notice repeats them."""
        brain = svc.open(project)
        root = _rootkey(brain.root)
        refs = [f"R{parse_id(r, 'R')}" for r in args.get("runs") or []] or [r["ref"] for r in brain.runs(open_only=True)]
        keys = [(root, ref) for ref in refs]
        with self._lock:
            for k in keys:
                self._waiting[k] = self._waiting.get(k, 0) + 1
        try:
            return call(svc, "thoth_wait", args, lambda: project)
        finally:
            with self._lock:
                for k in keys:
                    left = self._waiting.get(k, 0) - 1
                    if left > 0:
                        self._waiting[k] = left
                    else:
                        self._waiting.pop(k, None)

    @staticmethod
    def _notice(run: dict) -> list[str]:
        what = "review" if run["kind"] == "review" else "build"
        lines = [f"{run['agent']} finished the {what} of {run['task']} \"{run['title']}\": {run['status']} → task is {run['task_status']}."]
        if run.get("verdict"):
            lines.append(f"Verdict: {run['verdict']}.")
        if run.get("summary"):
            lines.append(f"Summary: {run['summary'][:400]}")
        if run.get("comments"):
            lines.append("Review comments: " + " | ".join(run["comments"])[:600])
        if run.get("error"):
            lines.append(f"Error: {run['error'][:300]}")
        return lines

    def _notify(self, project: str, run: dict) -> None:
        """Tell the coordinator's chat what finished, without waking it more than needed.

        A notice only goes to runs a coordinator tool call started (the Thoth page shows its own),
        never repeats what an in-flight thoth_wait reports, and waits for the session's other open
        runs so several results arrive as one message (each message starts a whole model turn).
        """
        if not self.settings().get("notify_on_complete", True):
            return
        time.sleep(0.5)  # let the tool call that started the run record its session first
        project = _rootkey(project)
        key = (project, run["run"])
        try:
            open_refs = {r["ref"] for r in Brain(project).runs(open_only=True)}
        except Exception:
            open_refs = set()
        with self._lock:
            if key not in self._run_sessions:
                return
            session_key = self._run_sessions.pop(key)
            if self._waiting.get(key):
                return
            batch_key = (project, session_key)
            self._held.setdefault(batch_key, []).append(self._notice(run))
            if any(k[0] == project and k[1] in open_refs and s == session_key for k, s in self._run_sessions.items()):
                return
            batch = self._held.pop(batch_key, [])
        if not batch:
            return
        head = "[Thoth] " + ("A run finished." if len(batch) == 1 else f"{len(batch)} runs finished.")
        body = "\n".join(("- " if len(batch) > 1 else "") + " ".join(lines) for lines in batch)
        tail = ("Decide the next step with the Thoth tools (review, fix, merge or the next task). "
                "Do not build, test, browse or start servers yourself: assign a task, or use thoth_preview to show a page.")
        try:
            self.ctx.inject_message(f"{head}\n{body}\n{tail}", role="user", session_key=session_key or None)
        except Exception:
            logger.debug("thoth: inject_message failed", exc_info=True)

    # -- ambient context ---------------------------------------------------------

    def pre_llm_call(self, session_id: str = "", is_first_turn: bool = False, user_message: Any = "", **_: Any) -> dict | None:
        """Tell the model a Thoth brain is active here, with a one-line board summary."""
        if not self.settings().get("inject_board", True):
            return None
        # A request typed on the Thoth page ends with "— via Thoth · <project>": bind this chat to that project.
        from_page = self._page_project(user_message)
        if from_page and session_id:
            self.bind_chat(session_id, from_page)
        here = self.session_dir()
        root = Path(from_page) if from_page else (find_brain_root(here) if here else None)
        if root is None:
            with self._lock:
                known = self._session_projects.get(session_id or "")
            known = known or (chats.project_of(self._data_dir(), session_id) if session_id else "")
            root = find_brain_root(known) if known else None
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
            f"[Thoth] Project {root.name} ({root}) has a shared multi-agent board (coordinator: {meta.get('coordinator', 'hermes')}). "
            f"Goal: {meta.get('goal') or '(not set)'}. Board: {summary}."
        ]
        if from_page:
            lines.append("The user wrote this on the Thoth page, which shows your reply in a small panel: answer in a few short lines, "
                         "in the user's language, and pass this project folder to Thoth tools.")
        try:
            ready = Thoth(settings=self.settings()).workers()
        except Exception:
            ready = {}
        missing = [f"{_AGENT_LABEL.get(a, a)} is {w.get('why')}" for a, w in ready.items() if not w.get("ready")]
        if missing:
            lines.append("On this machine " + "; ".join(missing) + ". Plan and review only with the agents that can run here "
                         "(an agent can review its own kind's work in a fresh session), and tell the user how to add the other if they ask.")
        if running:
            lines.append("Running: " + "; ".join(f"{r['ref']} {r['agent']} {r['kind']} {r['task_ref']}: {r['progress'][:80]}" for r in running))
        try:
            needs = Thoth(settings=self.settings()).needs(str(root))
        except Exception:
            needs = []
        if needs:
            lines.append("Waiting on the user or coordinator: " + "; ".join(f"{n['title']} ({n['tool']} {n['task']})" for n in needs[:4]))
        lines.append("You coordinate: plan, assign, review, merge, and talk to the user. Claude Code and Codex do the work. "
                     "Do not write code, run tests, start servers, open browsers or write outside the project yourself: assign a task "
                     "(kind qa for checks) and use thoth_preview to show a page. Record decisions with thoth_note. "
                     "A task that must look at websites gets browser=clean; browser=user (the user's own Chrome and logins) only when it needs their logins.")
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
                if data["needs_you"]:
                    out += ["", "Needs you:"] + [f"  • {n}" for n in data["needs_you"]]
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
    ready = Thoth(settings=settings).workers(fresh=True)
    for agent in ("claude", "codex"):
        ok, detail = driver_for(agent, settings).available()
        state = ready.get(agent, {})
        if ok and state.get("why") == "not signed in":
            lines.append(f"  ✗ {_AGENT_LABEL[agent]}: {detail} — not signed in ({state.get('hint')})")
        else:
            lines.append(f"  {'✓' if ok else '✗'} {_AGENT_LABEL[agent]}: {detail}" + (" · signed in" if state.get("signed_in") is True else ""))
    import shutil

    lines.append(f"  {'✓' if shutil.which('git') else '✗'} git: {shutil.which('git') or 'not found'}")
    lines.append(f"  MCP server: {mcp_server_path()}")
    ok, why = web.ready("clean")
    lines.append(f"  {'✓' if ok else '✗'} Web browsing (clean Chrome): {web.PACKAGE} via {web.npx() or 'npx'}" + ("" if ok else f" — {why}"))
    lines.append(f"  {'✓' if web.user_chrome_open() else '·'} Your Chrome: " + ("open to agents (remote debugging is on)" if web.user_chrome_open()
                                                                              else "closed to agents (turn on at chrome://inspect/#remote-debugging when a task needs your logins)"))
    return "\n".join(lines)


def _fingerprint(name: str, args: dict) -> str:
    relevant = {k: args.get(k) for k in ("project", "task", "agent", "reviewer", "instructions", "override", "feedback", "files", "model", "effort")}
    blob = json.dumps([name, relevant], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _rootkey(path: Any) -> str:
    """One spelling per project folder (/tmp and /private/tmp are the same place)."""
    return str(Path(path).expanduser().resolve())


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
    from .thoth_core import brain as _brain_mod

    def _push_change(root: str, type_: str) -> None:
        try:
            from hermes_cli.plugin_events import broadcast_plugin_event

            broadcast_plugin_event("thoth", "changed", {"project": root, "type": type_})
        except Exception:
            pass

    _brain_mod.CHANGE_LISTENERS.append(_push_change)
    ctx.register_hook("pre_tool_call", plugin.pre_tool_call)
    ctx.register_hook("post_tool_call", plugin.post_tool_call)
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
        from .thoth_core import preview as _preview

        _preview.stop_apps()
        _preview.stop_all()

    atexit.register(_at_exit)

    def _unload() -> None:
        atexit.unregister(_at_exit)
        if _push_change in _brain_mod.CHANGE_LISTENERS:
            _brain_mod.CHANGE_LISTENERS.remove(_push_change)
        _MANAGER.shutdown("the Thoth plugin was unloaded")
        from .thoth_core import preview as _preview

        _preview.stop_apps()
        _preview.stop_all()

    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        on_unload(_unload)
