"""High-level Thoth operations shared by the Hermes plugin and the MCP server.

Every public method takes plain arguments, returns a JSON-serializable dict and
raises ``BrainError`` for anything the caller should show to the user.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import browser as web
from . import gitops, preview
from .agents import McpSpec, RunState, clean_env, driver_for
from .brain import NOTE_KINDS, TASK_KINDS, OPEN_RUN_STATUSES, Brain, BrainError, find_brain_root, parse_id, rid, tid
from .briefing import ago, board_lines, build_brief, parse_report, review_brief, strip_report
from .runner import RunManager, kill_group, pid_alive

logger = logging.getLogger(__name__)

WORKERS = ("claude", "codex", "hermes")  # hermes = the coordinator does the task itself
CLI_WORKERS = ("claude", "codex")
COORDINATORS = ("hermes", "claude", "codex")
TERMINAL_RUN_STATUSES = ("done", "failed", "cancelled", "timeout", "lost")
MAX_REPORT_DECISIONS = 3

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_agent": "claude",
    "claude_path": "",
    "codex_path": "",
    "claude_model": "",
    "codex_model": "",
    "claude_permission_mode": "acceptEdits",
    "claude_sandbox": True,
    "lean_workers": True,
    "claude_allowed_tools": [],
    "run_timeout_seconds": 1800,
    "max_concurrent_runs": 3,
    "attach_mcp": True,
    "resume_sessions": True,
    "resume_max_tokens": 600_000,
    "max_review_rounds": 3,
    "remove_worktree_on_merge": True,
    "env_passthrough": [],
    "notify_on_complete": True,
    "inject_board": True,
    "link_dependencies": True,
    "dependency_dirs": list(gitops.DEFAULT_DEPENDENCY_DIRS),
    "setup_timeout_seconds": 600,
}

Notifier = Callable[[str, dict[str, Any]], None]
McpLauncher = Callable[[Path, str, int, int], McpSpec | None]

_MANAGER = RunManager()  # one per host process: Hermes, or one MCP server


def default_mcp_launcher(root: Path, agent: str, run_id: int, task_id: int) -> McpSpec:
    """Launch this same package's MCP server with this same interpreter."""
    server = Path(__file__).resolve().parent.parent / "mcp_server.py"
    return McpSpec(
        command=sys.executable,
        args=["-I", str(server), "--project", str(root), "--as", agent, "--run", rid(run_id), "--task", tid(task_id)],
    )


AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "hermes": "Hermes"}


def _name(agent: str) -> str:
    return AGENT_NAMES.get(agent or "", agent or "an agent")


def _other(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


class Thoth:
    def __init__(
        self,
        *,
        settings: dict[str, Any] | None = None,
        actor: str = "hermes",
        notifier: Notifier | None = None,
        mcp_launcher: McpLauncher | None = default_mcp_launcher,
        manager: RunManager | None = None,
    ):
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.actor = actor
        self.notifier = notifier
        self.mcp_launcher = mcp_launcher
        self.manager = manager or _MANAGER

    # -- project -----------------------------------------------------------------

    def init(self, project_dir: str, goal: str = "", setup: str | None = None, create: bool = False, run: str | None = None) -> dict:
        folder = Path(project_dir).expanduser()
        made = made_repo = False
        if create and not folder.exists():
            folder.mkdir(parents=True)
            made = True
        # A new project gets its own repository (also when the new folder sits inside another one).
        if create and folder.is_dir() and (made or not gitops.git(folder, "rev-parse", "--show-toplevel", check=False)):
            gitops.git(folder, "init", "-q")
            made_repo = True
        root = gitops.repo_root(folder)
        created_commit = False
        if not gitops.has_commits(root):
            gitops.ensure_initial_commit(root)
            created_commit = True
        gitops.exclude_brain_dir(root)
        if find_brain_root(root) == root:
            brain = Brain.open(root)
            if goal:
                brain.set_meta("goal", goal)
                brain.event(self.actor, "goal", goal)
            existed = True
        else:
            brain = Brain.create(root, goal=goal, base_branch=gitops.current_branch(root), actor=self.actor)
            existed = False
        if setup is not None:
            brain.set_meta("setup", setup.strip())
        if run is not None:
            brain.set_meta("run", run.strip())
        brain.write_snapshot()
        result = self.status(str(root))
        result["initialized"] = not existed
        if made_repo:
            result["created"] = f"Made {'the folder and ' if made else ''}a new git repository at {root}."
        if created_commit:
            result["note"] = "The repository had no commits, so Thoth made an empty initial commit to branch from."
        return result

    def open(self, project_dir: str) -> Brain:
        brain = Brain.open(project_dir)
        self._sweep_orphans(brain)
        return brain

    def _sweep_orphans(self, brain: Brain) -> None:
        for run in brain.runs(open_only=True):
            if run["agent"] == "hermes":  # the coordinator's own work has no process to lose
                continue
            owner = run.get("owner_pid")
            if owner == os.getpid() or pid_alive(owner):
                continue
            if run.get("pid") and pid_alive(run["pid"]):
                kill_group(int(run["pid"]), grace=2.0)
            brain.update_run(run["id"], status="lost", error="the process that started this run exited", ended_at=time.time())
            brain.update_task(run["task_id"], status="failed")
            brain.event("thoth", "run_lost", f"{run['ref']} lost (its host exited)", task_id=run["task_id"], run_id=run["id"])

    # -- board -------------------------------------------------------------------

    def plan(self, project_dir: str, tasks: list[dict], goal: str = "") -> dict:
        brain = self.open(project_dir)
        if goal:
            brain.set_meta("goal", goal)
            brain.event(self.actor, "goal", goal)
        if not isinstance(tasks, list) or not tasks:
            raise BrainError("tasks must be a non-empty list of {title, detail, agent, depends_on}")
        created: list[dict] = []
        for index, spec in enumerate(tasks, start=1):
            if not isinstance(spec, dict) or not str(spec.get("title") or "").strip():
                raise BrainError(f"Task #{index} needs a title")
            agent = str(spec.get("agent") or "").strip().lower()
            if agent and agent not in WORKERS:
                raise BrainError(f"Task #{index}: agent must be one of {', '.join(WORKERS)} (or empty)")
            deps: list[int] = []
            for dep in spec.get("depends_on") or []:
                if isinstance(dep, int) or (isinstance(dep, str) and dep.strip().isdigit()):
                    pos = int(dep)
                    if not 1 <= pos < index:
                        raise BrainError(f"Task #{index}: depends_on {dep} must point to an earlier task in this list")
                    deps.append(created[pos - 1]["id"])
                else:
                    deps.append(parse_id(dep, "T"))
            kind = str(spec.get("kind") or "code").strip().lower()
            if kind not in TASK_KINDS:
                raise BrainError(f"Task #{index}: kind must be one of {', '.join(TASK_KINDS)}")
            try:
                browser = web.normalize(spec.get("browser"))
            except BrainError as exc:
                raise BrainError(f"Task #{index}: {exc}") from None
            created.append(
                brain.add_task(
                    title=str(spec["title"]).strip(),
                    detail=str(spec.get("detail") or "").strip(),
                    agent=agent,
                    depends_on=deps,
                    actor=self.actor,
                    kind=kind,
                    role=str(spec.get("role") or "").strip()[:60],
                    model=str(spec.get("model") or "").strip()[:60],
                    effort=str(spec.get("effort") or "").strip().lower()[:10],
                    browser=browser,
                )
            )
        return {"created": [self._task_brief(t) for t in created], "board": board_lines(brain)}

    def update_task(self, project_dir: str, task: Any, *, title: str = "", detail: str = "", agent: str = "", status: str = "", kind: str = "", role: str = "", model: str | None = None, effort: str | None = None,
                    browser: str | None = None) -> dict:
        brain = self.open(project_dir)
        task_id = parse_id(task, "T")
        current = brain.task(task_id)
        fields: dict[str, Any] = {}
        if title:
            fields["title"] = title
        if detail:
            fields["detail"] = detail
        if agent:
            if agent not in WORKERS:
                raise BrainError(f"agent must be one of {', '.join(WORKERS)}")
            fields["agent"] = agent
        if kind:
            if kind not in TASK_KINDS:
                raise BrainError(f"kind must be one of {', '.join(TASK_KINDS)}")
            fields["kind"] = kind
        if role:
            fields["role"] = role.strip()[:60]
        if model is not None:
            fields["model"] = model.strip()[:60]  # "" = back to the default
        if effort is not None:
            fields["effort"] = effort.strip().lower()[:10]
        if browser is not None:
            fields["browser"] = web.normalize(browser)  # applies from the next run
        if status:
            if status not in ("todo", "cancelled"):
                raise BrainError("Only 'todo' (reopen) or 'cancelled' can be set by hand")
            if current["status"] in ("running", "reviewing"):
                raise BrainError(f"{current['ref']} has an open run; cancel it first")
            fields["status"] = status
        updated = brain.update_task(task_id, **fields)
        brain.event(self.actor, "task_updated", f"{updated['ref']} {', '.join(fields)}", task_id=task_id)
        return self._task_brief(updated)

    # -- runs --------------------------------------------------------------------

    def assign(self, project_dir: str, task: Any, agent: str = "", instructions: str = "", model: str = "", effort: str = "",
               browser: str | None = None) -> dict:
        brain = self.open(project_dir)
        task_id = parse_id(task, "T")
        current = brain.task(task_id)
        agent = (agent or current["agent"] or self.settings["default_agent"]).strip().lower()
        if agent not in WORKERS:
            raise BrainError(f"agent must be one of {', '.join(WORKERS)}")
        if current["status"] in ("running", "reviewing"):
            raise BrainError(f"{current['ref']} is already {current['status']}")
        if current["status"] == "merged":
            raise BrainError(f"{current['ref']} is already merged; add a new task for follow-up work")
        if browser is not None and web.normalize(browser) != current.get("browser", ""):
            current = brain.update_task(task_id, browser=web.normalize(browser))  # kept for later runs and revisions
        if agent == "hermes":
            return self._assign_to_coordinator(brain, current, instructions)
        driver = self._driver(agent)
        browse = current.get("browser") or ""
        ok, why = web.ready(browse)
        if not ok:
            raise BrainError(why)
        self._check_capacity(brain)
        current = self._prepare_worktree(brain, current)
        session = brain.last_session(task_id, agent) if self.settings.get("resume_sessions") else ""
        if session:
            # Resuming re-sends the whole earlier session on every call. When that session is already
            # large, a fresh run with the brief (which carries the summary, feedback and review notes) is cheaper.
            last = next((r for r in brain.runs(task_id=task_id, limit=20) if r["agent"] == agent and r["kind"] == "build" and r["session_id"] == session), None)
            limit = int(self.settings.get("resume_max_tokens") or 0)
            if last is not None and limit and self._usage(last)["prompt"] > limit:
                session = ""
        has_mcp = bool(self.settings.get("attach_mcp")) and self.mcp_launcher is not None
        prompt = build_brief(brain, current, agent=agent, instructions=instructions, has_mcp=has_mcp, resumed=bool(session), browser=browse)
        run = brain.add_run(task_id=task_id, agent=agent, kind="build", prompt=prompt, actor=self.actor)
        use_model = model or (current["model"] if agent == (current["agent"] or agent) else "")
        use_effort = effort or (current["effort"] if agent == (current["agent"] or agent) else "")
        brain.update_run(run["id"], model=use_model or self.settings.get(f"{agent}_model") or "", effort=use_effort)
        brain.update_task(task_id, status="running", agent=agent)
        brain.event(self.actor, "run_started", f"{agent} builds {current['ref']} {current['title']}" + (" (resumed session)" if session else "")
                    + {"clean": " with a clean Chrome", "user": " with your Chrome"}.get(browse, ""), task_id=task_id, run_id=run["id"])
        self._launch(brain, run["id"], current, driver, kind="build", prompt=prompt, session=session, cwd=Path(current["worktree"]), model=use_model, effort=use_effort,
                     browser=browse)
        return {"run": run["ref"], "task": current["ref"], "agent": agent, "branch": current["branch"], "resumed_session": bool(session), "status": "running",
                "model": use_model or "default", "effort": use_effort or "default", "browser": browse or "off"}

    def _assign_to_coordinator(self, brain: Brain, task: dict, instructions: str) -> dict:
        """Hermes (or whoever coordinates) does this task itself, in the task worktree, then calls complete()."""
        task = self._prepare_worktree(brain, task)
        prompt = build_brief(brain, task, agent="hermes", instructions=instructions, has_mcp=False, self_run=True)
        run = brain.add_run(task_id=task["id"], agent="hermes", kind="build", prompt=prompt, actor=self.actor)
        brain.update_run(run["id"], status="running", started_at=time.time(), owner_pid=None, commit_before=gitops.head(Path(task["worktree"])), progress="Working on it")
        brain.update_task(task["id"], status="running", agent="hermes")
        brain.event("hermes", "run_started", f"hermes takes {task['ref']} {task['title']}", task_id=task["id"], run_id=run["id"])
        return {
            "run": run["ref"], "task": task["ref"], "agent": "hermes", "status": "running", "worktree": task["worktree"],
            "next": f"Do this task yourself inside {task['worktree']} (write files there), then call thoth_complete with task={task['ref']}.",
            "brief": prompt,
        }

    def complete(self, project_dir: str, task: Any, summary: str, outputs: list[str] | None = None, decisions: list[str] | None = None, handoff: str = "") -> dict:
        """Finish a task the coordinator did itself: commit its worktree and record the result like any run."""
        brain = self.open(project_dir)
        current = brain.task(parse_id(task, "T"))
        run = next((r for r in brain.runs(task_id=current["id"], open_only=True) if r["agent"] == "hermes"), None)
        if run is None:
            raise BrainError(f"{current['ref']} has no open Hermes run; assign it with agent='hermes' first")
        report = {"status": "done", "summary": summary, "outputs": outputs or [], "decisions": decisions or [], "handoff": handoff}
        state = RunState(final_text="THOTH_REPORT\n" + __import__("json").dumps(report), cwd=current["worktree"], progress=summary[:160])
        self._finalize(brain, run["id"], state, 0, "")
        final = brain.run(run["id"])
        return self._run_brief(final, brain.task(current["id"]))

    def review(self, project_dir: str, task: Any, reviewer: str = "", instructions: str = "", model: str = "", effort: str = "") -> dict:
        brain = self.open(project_dir)
        task_id = parse_id(task, "T")
        current = brain.task(task_id)
        if current["status"] not in ("built", "approved", "changes", "failed"):
            raise BrainError(f"{current['ref']} is {current['status']}; review needs a finished build")
        if not current["worktree"] or not Path(current["worktree"]).exists():
            raise BrainError(f"{current['ref']} has no worktree to review")
        reviewer = (reviewer or _other(current["agent"] or "claude")).strip().lower()
        if reviewer not in CLI_WORKERS:
            raise BrainError(f"reviewer must be one of {', '.join(CLI_WORKERS)}")
        driver = self._driver(reviewer)
        self._check_capacity(brain)
        worktree = Path(current["worktree"])
        diff_text = gitops.diff(worktree, current["base_commit"])
        if not diff_text.strip():
            raise BrainError(f"{current['ref']} has no changes against {current['base_ref']} to review")
        log_text = gitops.log_oneline(worktree, current["base_commit"])
        has_mcp = bool(self.settings.get("attach_mcp")) and self.mcp_launcher is not None
        prompt = review_brief(brain, current, reviewer=reviewer, diff_text=diff_text, log_text=log_text, has_mcp=has_mcp)
        if instructions.strip():
            prompt += f"\n\nCoordinator's instructions for this review:\n{instructions.strip()}"
        run = brain.add_run(task_id=task_id, agent=reviewer, kind="review", prompt=prompt, actor=self.actor)
        brain.update_run(run["id"], model=model or self.settings.get(f"{reviewer}_model") or "", effort=effort)
        brain.update_task(task_id, status="reviewing")
        brain.event(self.actor, "review_started", f"{reviewer} reviews {current['agent']}'s {current['ref']}", task_id=task_id, run_id=run["id"])
        self._launch(brain, run["id"], current, driver, kind="review", prompt=prompt, session="", cwd=worktree, model=model, effort=effort)
        return {"run": run["ref"], "task": current["ref"], "reviewer": reviewer, "author": current["agent"], "status": "running"}

    def _driver(self, agent: str):
        driver = driver_for(agent, self.settings)
        ok, detail = driver.available()
        if not ok:
            raise BrainError(detail)
        return driver

    def _check_capacity(self, brain: Brain) -> None:
        limit = int(self.settings.get("max_concurrent_runs") or 3)
        open_runs = brain.runs(open_only=True)
        if len(open_runs) >= limit:
            names = ", ".join(f"{r['ref']} ({r['agent']} on {r['task_ref']})" for r in open_runs)
            raise BrainError(f"{len(open_runs)} runs are already open (limit {limit}): {names}. Wait or cancel one.")

    def _prepare_worktree(self, brain: Brain, task: dict) -> dict:
        if task["worktree"] and Path(task["worktree"]).exists():
            return task
        meta = brain.meta()
        base_ref = meta.get("base_branch") or "HEAD"
        unmerged = []
        for dep_id in task["depends_on"]:
            dep = brain.task(dep_id)
            if dep["status"] == "merged":
                continue
            if dep["status"] in ("built", "approved", "changes") and dep["branch"]:
                unmerged.append(dep)
                continue
            raise BrainError(f"{task['ref']} depends on {dep['ref']}, which is {dep['status']}; finish it first")
        if unmerged:
            base_ref = unmerged[0]["branch"]
        branch = f"thoth/{task['ref']}-{gitops.slugify(task['title'])}"
        worktree = brain.worktrees_dir / task["ref"]
        base_commit = gitops.create_worktree(brain.root, worktree, branch, base_ref)
        # Several dependencies still on their own branches: start from the first and merge the others in,
        # so every file the task builds on is in its worktree. The merge becomes the task's starting point,
        # which keeps the dependencies' changes out of this task's diff and review.
        for dep in unmerged[1:]:
            ok, result = gitops.merge_into_worktree(worktree, dep["branch"], f"thoth: start {task['ref']} with {dep['ref']}'s work")
            if not ok:
                gitops.remove_worktree(brain.root, worktree)
                gitops.delete_branch(brain.root, branch)
                refs = ", ".join(d["ref"] for d in unmerged)
                brain.add_note(kind="risk", author="thoth", body=f"{task['ref']} could not combine {refs}: {result}", task_id=task["id"])
                raise BrainError(f"{task['ref']} builds on {refs}, but they change the same lines ({result}). "
                                 f"Merge one of them first, or have its agent align it with the other.")
            base_commit = gitops.head(worktree)
        notes = []
        if len(unmerged) > 1:
            notes.append(f"Starts from {unmerged[0]['ref']} with {', '.join(d['ref'] for d in unmerged[1:])} merged in, so every dependency's files are here.")
        notes += self._prepare_environment(brain, worktree)
        if notes:
            brain.add_note(kind="handoff", author="thoth", body=" ".join(notes), task_id=task["id"])
        return brain.update_task(task["id"], branch=branch, worktree=str(worktree), base_ref=base_ref, base_commit=base_commit, head_commit=base_commit)

    def _prepare_environment(self, brain: Brain, worktree: Path) -> list[str]:
        """Give a new worktree what a clean checkout lacks: the project's setup command, or links to its
        dependency folders. Agents have no network to install them. Never .env files or other secrets."""
        notes: list[str] = []
        setup = (brain.meta().get("setup") or "").strip()
        if setup:
            started = time.time()
            try:
                proc = subprocess.run(setup, shell=True, cwd=str(worktree), env=clean_env(self.settings.get("env_passthrough") or ()),
                                      capture_output=True, text=True, timeout=int(self.settings.get("setup_timeout_seconds") or 600),
                                      stdin=subprocess.DEVNULL)
                ok, tail = proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()[-300:]
            except subprocess.TimeoutExpired:
                ok, tail = False, "timed out"
            took = round(time.time() - started)
            notes.append(f"Setup `{setup}` ran in {took}s." if ok else f"Setup `{setup}` failed after {took}s: {tail}")
        elif self.settings.get("link_dependencies", True):
            linked = gitops.link_dependencies(brain.root, worktree, tuple(self.settings.get("dependency_dirs") or gitops.DEFAULT_DEPENDENCY_DIRS))
            if linked:
                notes.append(f"Linked from the main checkout (read-only for you, no network to reinstall): {', '.join(linked)}.")
        return notes

    def _launch(self, brain: Brain, run_id: int, task: dict, driver, *, kind: str, prompt: str, session: str, cwd: Path, model: str = "", effort: str = "",
                browser: str = "") -> None:
        mcp = None
        if self.settings.get("attach_mcp") and self.mcp_launcher is not None:
            try:
                mcp = self.mcp_launcher(brain.root, driver.name, run_id, task["id"])
            except Exception:
                logger.warning("thoth: could not prepare the MCP server for R%s", run_id, exc_info=True)
        title = f"Thoth · {task['ref']} {'review' if kind == 'review' else task['title']}"
        tools = web.spec(browser) if browser and kind == "build" else None
        cmd = driver.command(kind=kind, session_id=session, mcp=mcp, model=model, effort=effort, name=title, browser=tools)
        env = clean_env(self.settings.get("env_passthrough") or [])
        if tools is not None:
            env = web.with_node(env)
        log_path = brain.runs_dir / f"R{run_id}.jsonl"
        brain.update_run(run_id, log_path=str(log_path), commit_before=gitops.head(cwd))

        def on_progress(state: RunState, extra: dict[str, Any]) -> None:
            fields: dict[str, Any] = {"progress": state.progress, "activity": state.activity, "files": state.files(), "delegations": state.delegation_list()}
            if state.session_id:
                fields["session_id"] = state.session_id
            fields.update(extra)
            brain.update_run(run_id, **fields)

        def on_exit(state: RunState, exit_code: int | None, error: str) -> None:
            self._finalize(brain, run_id, state, exit_code, error)

        try:
            self.manager.start(
                key=str(brain.root),
                run_id=run_id,
                driver=driver,
                cmd=cmd,
                prompt=prompt,
                cwd=cwd,
                env=env,
                log_path=log_path,
                timeout=int(self.settings.get("run_timeout_seconds") or 1800),
                on_progress=on_progress,
                on_exit=on_exit,
            )
        except Exception as exc:
            self._finalize(brain, run_id, RunState(error=str(exc), is_error=True), None, f"could not start {driver.label}: {exc}")
            raise BrainError(f"Could not start {driver.label}: {exc}") from exc

    def _finalize(self, brain: Brain, run_id: int, state: RunState, exit_code: int | None, error: str) -> None:
        run = brain.run(run_id)
        task = brain.task(run["task_id"])
        if run["error"].startswith("cancelled"):
            error = run["error"]
        if error.startswith("cancelled"):
            status = "cancelled"
        elif error.startswith("timed out"):
            status = "timeout"
        elif error:
            status = "failed"
        elif exit_code == 0 and not state.is_error:
            status = "done"
        else:
            status = "failed"
            error = state.error or f"exit code {exit_code}"
        report = parse_report(state.final_text)
        summary = str(report.get("summary") or strip_report(state.final_text))[:2000]
        fields: dict[str, Any] = {
            "files": state.files(),
            "delegations": [{**d, "status": d["status"] if d["status"] != "running" else ("completed" if status == "done" else "cancelled")} for d in state.delegation_list()],
            "status": status,
            "exit_code": exit_code,
            "error": error or state.error if status != "done" else "",
            "summary": summary,
            "report": report,
            "usage": state.usage,
            "session_id": state.session_id or run["session_id"],
            "progress": state.progress,
            "activity": state.activity,
            "ended_at": time.time(),
        }
        author = run["agent"]
        commit = ""
        if run["kind"] == "build":
            worktree = Path(task["worktree"])
            try:
                label = "" if status == "done" else f" [{status}]"
                commit = gitops.commit_all(
                    worktree,
                    f"thoth({author}): {task['title']}{label}\n\n{summary[:600]}\n\nThoth-Task: {task['ref']}\nThoth-Run: {rid(run_id)}\nThoth-Agent: {author}",
                ) or ""
                tip = gitops.head(worktree)
                fields["commit_after"] = tip
                fields["diffstat"] = gitops.diffstat(worktree, run["commit_before"] or task["base_commit"], tip)
                changed = gitops.name_status(worktree, run["commit_before"] or task["base_commit"], tip)
                outputs = [str(o) for o in _as_list(report.get("outputs"))]
                fields["files"] = {
                    "changed": changed,
                    "read": [f for f in state.files_read if f not in {c["path"] for c in changed}][:80],
                    "outputs": outputs[:40],
                }
                brain.update_task(task["id"], head_commit=tip)
                commit = commit or (tip if tip != run["commit_before"] else "")
            except Exception as exc:
                logger.warning("thoth: commit after R%s failed: %s", run_id, exc)
                fields["error"] = (fields["error"] + f"; commit failed: {exc}").strip("; ")
            if status == "done" and str(report.get("status", "done")).lower() != "blocked":
                new_status = "built"
            elif status == "cancelled":
                new_status = "cancelled"
            else:
                new_status = "failed"
            brain.update_task(task["id"], status=new_status)
            for decision in _new_only(brain, run_id, "decision", _as_list(report.get("decisions"))):
                brain.add_note(kind="decision", author=author, body=decision, task_id=task["id"], run_id=run_id)
            if str(report.get("handoff") or "").strip():
                brain.add_note(kind="handoff", author=author, body=str(report["handoff"]), task_id=task["id"], run_id=run_id)
            for risk in _as_list(report.get("risks")):
                brain.add_note(kind="risk", author=author, body=risk, task_id=task["id"], run_id=run_id)
            if str(report.get("status", "")).lower() == "blocked":
                brain.add_note(kind="question", author=author, body=f"Blocked: {summary[:500]}", task_id=task["id"], run_id=run_id)
        else:
            # A review must not change the task. Anything the reviewer left in the worktree
            # (scratch scripts, test output) is discarded so it can't ride along in the next build commit.
            try:
                worktree = Path(task["worktree"])
                if worktree.exists() and gitops.is_dirty(worktree):
                    gitops.discard_changes(worktree)
                    brain.event("thoth", "review_cleanup", f"discarded files {author} left in {task['ref']}'s worktree during review", task_id=task["id"], run_id=run_id)
            except Exception as exc:
                logger.warning("thoth: cleanup after review R%s failed: %s", run_id, exc)
            verdict = str(report.get("verdict") or "").lower()
            blocking = [c for c in _as_list(report.get("comments")) if _severity(c) <= 2]
            if verdict.startswith("change") and not blocking:
                # Only polish was raised: it does not block the merge.
                report["suggestions"] = _as_list(report.get("suggestions")) + _as_list(report.get("comments"))
                report["comments"] = []
                report["verdict"] = verdict = "approve"
            fields["report"] = report
            if status != "done":
                new_status = "built"
            elif verdict.startswith("approve"):
                new_status = "approved"
            elif verdict.startswith("change"):
                new_status = "changes"
            else:
                new_status = "built"
                fields["error"] = "the reviewer did not give a verdict"
            brain.update_task(task["id"], status=new_status)
            comments = _as_list(report.get("comments"))
            suggestions = _as_list(report.get("suggestions"))
            if comments or (status == "done" and verdict):
                body = f"Review by {author} ({verdict or 'no verdict'}): {summary[:600]}"
                if comments:
                    body += "\n" + "\n".join(f"  • {c}" for c in comments)
                if suggestions:
                    body += "\nOptional suggestions:\n" + "\n".join(f"  ◦ {c}" for c in suggestions[:6])
                brain.add_note(kind="review", author=author, body=body, task_id=task["id"], run_id=run_id)
            for decision in _as_list(report.get("decisions")):
                brain.add_note(kind="decision", author=author, body=decision, task_id=task["id"], run_id=run_id)
        brain.update_run(run_id, **fields)
        final = brain.run(run_id)
        verb = "built" if run["kind"] == "build" else "reviewed"
        outcome = new_status if run["kind"] == "build" else (new_status if status == "done" else status)
        brain.event(author, f"run_{status}", f"{author} {verb} {task['ref']}: {outcome}", task_id=task["id"], run_id=run_id, commit=commit if run["kind"] == "build" else "")
        if self.notifier is not None:
            try:
                self.notifier(str(brain.root), self._run_brief(final, brain.task(task["id"])))
            except Exception:
                logger.debug("thoth: notifier failed", exc_info=True)

    def wait(self, project_dir: str, runs: list[Any] | None = None, timeout_seconds: int = 600) -> dict:
        brain = self.open(project_dir)
        if runs:
            ids = [parse_id(r, "R") for r in runs]
        else:
            ids = [r["id"] for r in brain.runs(open_only=True)]
        if not ids:
            return {"runs": [], "message": "No open runs."}
        deadline = time.time() + max(1, min(int(timeout_seconds or 600), 3600))
        while True:
            current = [brain.run(i) for i in ids]
            if all(r["status"] in TERMINAL_RUN_STATUSES for r in current) or time.time() >= deadline:
                break
            time.sleep(2)
        done = all(r["status"] in TERMINAL_RUN_STATUSES for r in current)
        return {
            "all_finished": done,
            "runs": [self._run_brief(r, brain.task(r["task_id"])) for r in current],
            "board": board_lines(brain),
        }

    def cancel(self, project_dir: str, run: Any) -> dict:
        brain = self.open(project_dir)
        run_id = parse_id(run, "R")
        current = brain.run(run_id)
        if current["status"] not in OPEN_RUN_STATUSES:
            raise BrainError(f"{current['ref']} is already {current['status']}")
        reason = f"cancelled by {self.actor}"
        brain.update_run(run_id, error=reason)
        if not self.manager.cancel(str(brain.root), run_id, reason):
            if current.get("pid") and pid_alive(current["pid"]):
                kill_group(int(current["pid"]))
            if not pid_alive(current.get("owner_pid")) or current.get("owner_pid") == os.getpid():
                brain.update_run(run_id, status="cancelled", ended_at=time.time())
                brain.update_task(current["task_id"], status="cancelled")
        brain.event(self.actor, "run_cancel", f"{current['ref']} {reason}", task_id=current["task_id"], run_id=run_id)
        return {"run": current["ref"], "status": "cancelling"}

    # -- versions ----------------------------------------------------------------

    # -- iterate on a task ------------------------------------------------------

    def revise(self, project_dir: str, task: Any, feedback: str, agent: str = "", files: list[str] | None = None, model: str = "", effort: str = "") -> dict:
        """Send a task back with the user's feedback. The same agent resumes its session; the feedback
        stays on the task so later runs and reviewers see it too."""
        brain = self.open(project_dir)
        current = brain.task(parse_id(task, "T"))
        feedback = (feedback or "").strip()
        if not feedback:
            raise BrainError("Say what should change")
        if current["status"] == "merged":
            raise BrainError(f"{current['ref']} is merged; add a follow-up task instead")
        focus = [str(f) for f in files or [] if str(f).strip()]
        body = feedback + (("\nFiles to redo: " + ", ".join(focus)) if focus else "")
        brain.add_note(kind="feedback", author=self.actor if self.actor != "hermes" else "user", body=body, task_id=current["id"])
        instructions = f"The user reviewed your work and wants changes:\n{feedback}"
        if focus:
            instructions += "\nRedo only these files unless the feedback says otherwise: " + ", ".join(focus)
        instructions += "\nKeep everything else as it is, and say in your report what you changed."
        result = self.assign(project_dir, current["ref"], agent=agent, instructions=instructions, model=model, effort=effort)
        brain.update_run(parse_id(result["run"], "R"), started_by="feedback")
        return result

    def restore(self, project_dir: str, task: Any, commit: str) -> dict:
        """Bring back an earlier version of a task as a new version (history is kept)."""
        brain = self.open(project_dir)
        current = brain.task(parse_id(task, "T"))
        if current["status"] in ("running", "reviewing", "merged"):
            raise BrainError(f"{current['ref']} is {current['status']}; it can't be restored now")
        if not current["worktree"] or not Path(current["worktree"]).exists():
            raise BrainError(f"{current['ref']} has no worktree")
        wt = Path(current["worktree"])
        full = gitops.git(wt, "rev-parse", "--verify", f"{commit}^{{commit}}", check=False)
        if not full or not gitops.is_ancestor(wt, full, "HEAD"):
            raise BrainError(f"{commit} is not a version of {current['ref']}")
        before = gitops.head(wt)
        tip = gitops.restore_tree(wt, full, f"thoth: restore {current['ref']} to {full[:7]}\n\nThoth-Task: {current['ref']}")
        if tip is None:
            return {"task": current["ref"], "restored": full[:7], "changed": False}
        run = brain.add_run(task_id=current["id"], agent=current["agent"] or "hermes", kind="build", prompt=f"restore {full[:7]}", actor="restore")
        brain.update_run(run["id"], status="done", summary=f"Restored the version from {full[:7]}", commit_before=before, commit_after=tip,
                         files={"changed": gitops.name_status(wt, before, tip), "read": [], "outputs": []},
                         diffstat=gitops.diffstat(wt, before, tip), started_at=time.time(), ended_at=time.time())
        brain.update_task(current["id"], head_commit=tip, status="built")
        brain.event(self.actor, "restored", f"{current['ref']} restored to {full[:7]}", task_id=current["id"], run_id=run["id"], commit=tip)
        return {"task": current["ref"], "restored": full[:7], "commit": tip[:7], "changed": True}

    def stale(self, brain: Brain, task: dict) -> list[dict]:
        """Dependencies with newer versions than the ones this task was built on."""
        if not task.get("worktree") or not Path(task["worktree"]).exists() or task["status"] in ("merged", "running", "reviewing"):
            return []
        out = []
        for dep_id in task["depends_on"]:
            dep = brain.task(dep_id)
            if dep["head_commit"] and dep["head_commit"] != dep["base_commit"] and not gitops.is_ancestor(Path(task["worktree"]), dep["head_commit"], "HEAD"):
                out.append(dep)
        return out

    def sync(self, project_dir: str, task: Any) -> dict:
        """Pull the newest versions of a task's dependencies into its worktree."""
        brain = self.open(project_dir)
        current = brain.task(parse_id(task, "T"))
        deps = self.stale(brain, current)
        if not deps:
            return {"task": current["ref"], "updated": [], "message": "Already up to date with its dependencies"}
        wt = Path(current["worktree"])
        if gitops.is_dirty(wt):
            gitops.commit_all(wt, f"thoth: save {current['ref']} before syncing")
        before = gitops.head(wt)
        merged = []
        for dep in deps:
            ok, result = gitops.merge_into_worktree(wt, dep["branch"], f"thoth: bring {dep['ref']}'s newest version into {current['ref']}")
            if not ok:
                brain.add_note(kind="risk", author="thoth", body=f"Could not bring {dep['ref']}'s new version into {current['ref']}: {result}", task_id=current["id"])
                raise BrainError(f"{current['ref']} conflicts with {dep['ref']}'s new version ({result}). Ask {current['agent']} to reconcile them.")
            merged.append(dep["ref"])
        tip = gitops.head(wt)
        changed = gitops.name_status(wt, before, tip)
        brain.update_task(current["id"], head_commit=tip)
        brain.add_note(kind="handoff", author="thoth", body=f"Updated with the newest {', '.join(merged)}: " + ", ".join(f"{c['status']} {c['path']}" for c in changed[:12]), task_id=current["id"])
        brain.event(self.actor, "synced", f"{current['ref']} now uses the newest {', '.join(merged)}", task_id=current["id"], commit=tip)
        return {"task": current["ref"], "updated": merged, "files": [f"{c['status']} {c['path']}" for c in changed[:20]]}

    def preview(self, project_dir: str, task: Any = None) -> dict:
        """Serve what a task built (or the main checkout) on localhost and return the link to open.

        Agents cannot open browsers in their sandboxes and coordinators should not start servers of their
        own, so this is the one way to look at a web page or a browser game a task made.
        """
        brain = self.open(project_dir)
        current = brain.task(parse_id(task, "T")) if task not in (None, "", "main") else None
        run = (brain.meta().get("run") or "").strip()
        if run:
            return self._run_preview(brain, current, run)
        if current and current["worktree"] and Path(current["worktree"]).is_dir():
            folder, what = Path(current["worktree"]), f"{current['ref']} {current['title']}"
        else:
            folder, what = brain.root, f"{brain.meta().get('base_branch') or 'main'}, with everything merged so far"
        candidates: list[str] = []
        if current:
            for r in brain.runs(task_id=current["id"], limit=20):
                f = r.get("files") or {}
                candidates += [re.split(r"\s+[—–-]\s+", str(o))[0].strip() for o in f.get("outputs") or []]
                candidates += [c["path"] for c in f.get("changed") or [] if c.get("status") != "D"]
        page = preview.entry_page(folder, candidates)
        if not page and not (folder / "index.html").is_file():
            raise BrainError(f"{current['ref'] if current else 'The project'} has no web page to preview yet. Its files are in {folder}.")
        port = preview.serve(folder)
        return {"url": f"http://127.0.0.1:{port}/{page}", "what": what, "folder": str(folder), "page": page or "index.html"}

    def _run_preview(self, brain: Brain, current: dict | None, run: str) -> dict:
        """Start the project's app (its ``run`` command) for a task's newest version, or for main."""
        if current is not None and current["status"] != "merged":
            if not current["head_commit"] or current["head_commit"] == current["base_commit"]:
                raise BrainError(f"{current['ref']} has no version to run yet")
            commit, key, what = current["head_commit"], current["ref"], f"{current['ref']} {current['title']}"
        else:
            base = brain.meta().get("base_branch") or "main"
            commit, key, what = gitops.head(brain.root, base), "main", f"{base}, with everything merged so far"
        try:
            port = preview.run_app(brain.root, commit, run, f"{brain.root.name}-{key}", clean_env(self.settings.get("env_passthrough") or ()),
                                   brain.root / ".thoth" / "previews", link_from=brain.root,
                                   links=gitops.dependency_dirs(brain.root, tuple(self.settings.get("dependency_dirs") or gitops.DEFAULT_DEPENDENCY_DIRS)))
        except RuntimeError as exc:
            raise BrainError(str(exc)) from exc
        return {"url": f"http://127.0.0.1:{port}/", "what": what, "folder": str(brain.root), "page": "", "mode": "run", "command": run}

    def output_url(self, project_dir: str, task: Any, path: str) -> dict:
        """A link to one file a task produced (a video, a sound, a page, a PDF), for viewing it in place."""
        brain = self.open(project_dir)
        current = brain.task(parse_id(task, "T")) if task not in (None, "", "main") else None
        folder = Path(current["worktree"]) if current and current["worktree"] and Path(current["worktree"]).is_dir() else brain.root
        rel = Path(str(path or ""))
        if not str(path or "").strip() or rel.is_absolute() or ".." in rel.parts or any(p.startswith(".") for p in rel.parts):
            raise BrainError("Only files inside the task can be opened")
        target = (folder / rel).resolve()
        if not target.is_file() or folder.resolve() not in target.parents:
            raise BrainError(f"{rel.as_posix()} is not in {current['ref'] if current else 'the project'} any more")
        port = preview.serve(folder)
        return {"url": f"http://127.0.0.1:{port}/{urllib.parse.quote(rel.as_posix())}", "folder": str(folder),
                "path": rel.as_posix(), "bytes": target.stat().st_size}

    def merge(self, project_dir: str, task: Any, override: bool = False) -> dict:
        brain = self.open(project_dir)
        task_id = parse_id(task, "T")
        current = brain.task(task_id)
        allowed = ("built", "approved", "changes") if override else ("built", "approved")
        if current["status"] not in allowed:
            hint = " (pass override to merge despite open review comments)" if current["status"] == "changes" else ""
            raise BrainError(f"{current['ref']} is {current['status']}; only built or approved tasks can be merged{hint}")
        if override and current["status"] == "changes":
            brain.add_note(kind="decision", author=self.actor, body=f"Merged {current['ref']} with review comments still open, by the user's decision.", task_id=task_id)
        if not current["branch"]:
            raise BrainError(f"{current['ref']} has no branch")
        base = brain.meta().get("base_branch", "")
        on = gitops.current_branch(brain.root)
        if on != base:
            raise BrainError(f"The project checkout is on '{on}', not the base branch '{base}'; switch back first")
        if gitops.tracked_dirty(brain.root):
            raise BrainError("The project checkout has uncommitted changes to tracked files; commit or stash them first")
        ok, result = gitops.merge_branch(
            brain.root,
            current["branch"],
            f"thoth: merge {current['ref']} {current['title']} ({current['agent']})\n\nThoth-Task: {current['ref']}",
        )
        if not ok:
            brain.update_task(task_id, status="conflict")
            brain.add_note(kind="risk", author="thoth", body=f"Merge of {current['ref']} failed: {result}", task_id=task_id)
            brain.event(self.actor, "merge_failed", f"{current['ref']}: {result}", task_id=task_id)
            raise BrainError(f"Merge failed and was aborted, the base branch is unchanged. {result}")
        brain.update_task(task_id, status="merged", merge_commit=result)
        brain.event(self.actor, "merged", f"{current['ref']} {current['title']} merged into {base}", task_id=task_id, commit=result)
        merged = [current]
        # A task built on top of another unmerged task carries that task's commits with it.
        for other in brain.tasks():
            if other["id"] == task_id or other["status"] not in ("built", "approved", "changes", "failed") or not other["head_commit"]:
                continue
            if other["head_commit"] != other["base_commit"] and gitops.is_ancestor(brain.root, other["head_commit"], "HEAD"):
                brain.update_task(other["id"], status="merged", merge_commit=result)
                brain.event(self.actor, "merged", f"{other['ref']} {other['title']} merged into {base} along with {current['ref']}", task_id=other["id"], commit=result)
                merged.append(other)
        if self.settings.get("remove_worktree_on_merge"):
            for task_row in merged:
                if task_row["worktree"]:
                    gitops.remove_worktree(brain.root, Path(task_row["worktree"]))
                    brain.update_task(task_row["id"], worktree="")
        return {
            "task": current["ref"],
            "merged_into": base,
            "commit": result[:12],
            "also_merged": [t["ref"] for t in merged[1:]],
            "board": board_lines(brain),
        }

    # -- notes & context ---------------------------------------------------------

    def note(self, project_dir: str, kind: str, body: str, task: Any = None, run: Any = None, author: str = "") -> dict:
        brain = self.open(project_dir)
        task_id = parse_id(task, "T") if task not in (None, "") else None
        run_id = parse_id(run, "R") if run not in (None, "") else None
        kind = (kind or "context").strip().lower()
        if kind not in NOTE_KINDS:
            raise BrainError(f"kind must be one of {', '.join(NOTE_KINDS)}")
        if kind == "decision":
            for existing in brain.notes(kinds=("decision",), limit=300):
                if _same_point(body, existing["body"]):
                    return {"note": f"N{existing['id']}", "kind": kind, "task": tid(existing.get("task_id")), "duplicate_of": f"{existing['author']}: {existing['body'][:160]}"}
        note = brain.add_note(kind=kind, author=author or self.actor, body=body, task_id=task_id, run_id=run_id)
        return {"note": f"N{note['id']}", "kind": kind, "task": tid(task_id)}

    def context(self, project_dir: str, task: Any = None, agent: str = "") -> dict:
        brain = self.open(project_dir)
        if task in (None, ""):
            return {"brain": (brain.dir / "BRAIN.md").read_text(encoding="utf-8") if (brain.dir / "BRAIN.md").exists() else ""}
        current = brain.task(parse_id(task, "T"))
        if not current["branch"]:
            current = {**current, "branch": "(not started)", "base_ref": brain.meta().get("base_branch", ""), "base_commit": ""}
        return {"task": current["ref"], "brief": build_brief(brain, current, agent=agent or self.actor, has_mcp=True)}

    def set_coordinator(self, project_dir: str, agent: str) -> dict:
        agent = (agent or "").strip().lower()
        if agent not in COORDINATORS:
            raise BrainError(f"coordinator must be one of {', '.join(COORDINATORS)}")
        brain = self.open(project_dir)
        previous = brain.meta().get("coordinator", "hermes")
        brain.set_meta("coordinator", agent)
        brain.event(self.actor, "coordinator", f"coordinator {previous} -> {agent}")
        return {"coordinator": agent, "previous": previous, "project": str(brain.root)}

    # -- views -------------------------------------------------------------------

    def status(self, project_dir: str, task: Any = None) -> dict:
        brain = self.open(project_dir)
        meta = brain.meta()
        if task not in (None, ""):
            current = brain.task(parse_id(task, "T"))
            runs = brain.runs(task_id=current["id"], limit=10)
            return {
                "task": self._task_brief(current),
                "detail": current["detail"],
                "runs": [self._run_brief(r, current) for r in runs],
                "notes": [f"[{n['kind']}] {n['author']}: {n['body']}" for n in brain.notes(task_id=current["id"], limit=20)],
            }
        open_runs = brain.runs(open_only=True)
        return {
            "project": str(brain.root),
            "goal": meta.get("goal", ""),
            "coordinator": meta.get("coordinator", "hermes"),
            "base_branch": meta.get("base_branch", ""),
            "board": board_lines(brain),
            "needs_you": [f"{n['title']} — {n['detail']} (next: {n['tool']} {n['task']})" for n in self.needs(project_dir)],
            "running": [self._run_brief(r, brain.task(r["task_id"])) for r in open_runs],
            "decisions": [f"{n['author']}: {n['body']}" for n in brain.notes(kinds=("decision",), limit=10)],
            "recent": [f"{ago(e['ts'])} {e['actor']}: {e['message']}" for e in brain.events(limit=10)],
            "tokens": (lambda us: f"{us['total']['prompt']:,} prompt tokens across agents ({round(us['cache_rate'] * 100)}% cache hits); about {us['kept_out_tokens']:,} tokens of agent transcripts stayed out of the coordinator's context")(self.usage(brain)),
            "brain_file": str(brain.dir / "BRAIN.md"),
        }

    def models(self) -> dict[str, Any]:
        """What each worker can run on, for pickers: models and reasoning efforts."""
        out = {}
        for agent in CLI_WORKERS:
            drv = driver_for(agent, self.settings)
            out[agent] = {"models": drv.models() if drv.resolve() else [], "efforts": list(drv.EFFORTS),
                          "default": str(self.settings.get(f"{agent}_model") or "")}
        return out

    def needs(self, project_dir: str) -> list[dict]:
        """What is waiting on a person (or the coordinator), most urgent first."""
        brain = self.open(project_dir)
        out: list[dict] = []
        for t in brain.tasks():
            behind = self.stale(brain, t)
            if behind:
                names = ", ".join(d["ref"] for d in behind)
                out.append({"id": f"sync-{t['ref']}", "kind": "sync", "task": t["ref"], "who": behind[0]["agent"] or "hermes", "priority": 1,
                            "title": f"{t['ref']} uses an older {names}", "detail": f"{names} has a newer version. Bring it in so {_name(t['agent'])} works with it.", "tool": "thoth_sync"})
            runs = brain.runs(task_id=t["id"], limit=10)
            last_review = next((r for r in runs if r["kind"] == "review" and r["status"] == "done"), None)
            reviewer = last_review["agent"] if last_review else _other(t["agent"] or "claude")
            last = runs[0] if runs else None
            if t["status"] == "conflict":
                out.append({"id": f"conflict-{t['ref']}", "kind": "conflict", "task": t["ref"], "who": t["agent"], "priority": 0,
                            "title": f"Merge conflict on {t['ref']}", "detail": "The base branch is unchanged. Reconcile the branches, then merge again.", "tool": "thoth_assign"})
            elif t["status"] == "approved":
                out.append({"id": f"merge-{t['ref']}", "kind": "merge", "task": t["ref"], "who": reviewer, "priority": 1,
                            "title": f"{t['ref']} is ready to merge", "detail": f"Approved by {_name(reviewer)} · {t['title']}", "tool": "thoth_merge"})
            elif t["status"] == "changes":
                comments = _as_list((last_review or {}).get("report", {}).get("comments")) if last_review else []
                n = len(comments)
                rounds = sum(1 for r in runs if r["kind"] == "review" and r["status"] == "done")
                if rounds >= int(self.settings.get("max_review_rounds") or 3):
                    out.append({"id": f"decide-{t['ref']}", "kind": "decide", "task": t["ref"], "who": reviewer, "priority": 1,
                                "title": f"{rounds} review rounds on {t['ref']} — your call",
                                "detail": f"{n} point{'s' if n != 1 else ''} still open. Merge as it is, or send it back once more.", "tool": "thoth_merge"})
                else:
                    out.append({"id": f"fix-{t['ref']}", "kind": "fix", "task": t["ref"], "who": reviewer, "priority": 2,
                                "title": f"{n or 'Some'} change{'s' if n != 1 else ''} asked on {t['ref']}", "detail": f"{_name(t['agent'])} can pick them up in its last session", "tool": "thoth_assign"})
            elif t["status"] == "failed":
                err = (last or {}).get("error") or (last or {}).get("summary") or ""
                out.append({"id": f"retry-{t['ref']}", "kind": "retry", "task": t["ref"], "who": t["agent"], "priority": 2,
                            "title": f"{t['ref']} needs another try", "detail": err[:160] or "The last run did not finish", "tool": "thoth_assign"})
            elif t["status"] == "built":
                out.append({"id": f"review-{t['ref']}", "kind": "review", "task": t["ref"], "who": t["agent"], "priority": 3,
                            "title": f"{t['ref']} is built and waiting for review", "detail": f"Ask {_name(_other(t['agent'] or 'claude'))} to review it", "tool": "thoth_review"})
        out.sort(key=lambda n: n["priority"])
        return out

    def presence(self, brain: Brain) -> dict[str, dict]:
        """What each agent is doing right now, from its open run."""
        out = {a: {"busy": False, "say": "", "task": "", "kind": "", "run": ""} for a in ("hermes", "claude", "codex")}
        for r in brain.runs(open_only=True):
            out[r["agent"]] = {"busy": True, "say": r["progress"] or "Starting", "task": r["task_ref"], "kind": r["kind"], "run": r["ref"]}
        return out

    @staticmethod
    def _usage(run: dict) -> dict[str, int]:
        """Token use of one run in one shape (older runs stored the CLIs' raw fields)."""
        u = run.get("usage") or {}
        if "prompt_tokens" in u:
            return {"prompt": int(u.get("prompt_tokens") or 0), "cached": int(u.get("cached_tokens") or 0), "output": int(u.get("output_tokens") or 0)}
        if run["agent"] == "claude":
            fresh, read = int(u.get("input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0)
            return {"prompt": fresh + read, "cached": read, "output": int(u.get("output_tokens") or 0)}
        return {"prompt": int(u.get("input_tokens") or 0), "cached": int(u.get("cached_input_tokens") or 0), "output": int(u.get("output_tokens") or 0)}

    def usage(self, brain: Brain) -> dict[str, Any]:
        """Tokens per agent, cache hits, and how much agent work stayed out of the coordinator's context."""
        import json as _json

        per: dict[str, dict[str, int]] = {}
        transcript_bytes = handed_bytes = 0
        for run in brain.runs(limit=500):
            if run["status"] in OPEN_RUN_STATUSES:
                continue
            u = self._usage(run)
            a = per.setdefault(run["agent"], {"prompt": 0, "cached": 0, "output": 0, "runs": 0})
            for k in ("prompt", "cached", "output"):
                a[k] += u[k]
            a["runs"] += 1
            if run.get("log_path") and os.path.exists(run["log_path"]):
                transcript_bytes += os.path.getsize(run["log_path"])
                handed_bytes += len(_json.dumps(self._run_brief(run, brain.task(run["task_id"])), ensure_ascii=False))
        total = {k: sum(a[k] for a in per.values()) for k in ("prompt", "cached", "output")}
        return {
            "per_agent": per,
            "total": total,
            "cache_rate": round(total["cached"] / total["prompt"], 3) if total["prompt"] else 0,
            # What the coordinator would have had to read if it did the work itself, vs what it received.
            "transcript_bytes": transcript_bytes,
            "handed_bytes": handed_bytes,
            "kept_out_tokens": max(0, (transcript_bytes - handed_bytes) // 4),
        }

    def reach(self, brain: Brain, tasks: list[dict] | None = None) -> dict[str, list[dict]]:
        """Which other agents have each task's newest work, per task ref.

        One entry per agent that needs it: through a task that builds on it (``ok`` when that task
        is built on the newest version, ``behind`` when on an older one, ``waiting`` when it has not
        started and will start from the newest), or through a decision it recorded that an agent
        with unfinished work has not received yet (``notyet``: no run of theirs started after it).
        """
        tasks = tasks if tasks is not None else brain.tasks()
        runs = sorted(brain.runs(limit=500), key=lambda r: r["id"])
        versions: dict[int, list[str]] = {}
        for r in runs:
            if r["kind"] == "build" and r["status"] == "done" and r["commit_after"] and r["commit_after"] != r["commit_before"]:
                versions.setdefault(r["task_id"], []).append(r["commit_after"])
        last_run: dict[str, float] = {}
        for r in runs:
            last_run[r["agent"]] = max(last_run.get(r["agent"], 0.0), float(r["created_at"] or 0))
        busy = {t["agent"] for t in tasks if t["status"] not in ("merged", "cancelled")}
        decisions = [n for n in brain.notes(kinds=("decision",), limit=200) if n.get("task_id")]
        rank = {"behind": 3, "notyet": 2, "ok": 1, "waiting": 0}  # waiting is not a problem: it starts from the newest
        out: dict[str, list[dict]] = {}
        for x in tasks:
            if x["status"] == "cancelled":
                continue
            vers, author, entries = versions.get(x["id"], []), x["agent"], {}

            def keep(entry: dict) -> None:
                old = entries.get(entry["agent"])
                if old is None or rank[entry["state"]] > rank[old["state"]]:
                    entries[entry["agent"]] = entry

            for d in tasks:
                if x["id"] not in d["depends_on"] or d["status"] == "cancelled" or d["agent"] == author:
                    continue
                if not d["branch"] or not d["head_commit"] or not vers:
                    keep({"agent": d["agent"], "task": d["ref"], "state": "waiting" if not d["branch"] else "ok", "latest": len(vers)})
                    continue
                have = next((i + 1 for i in range(len(vers) - 1, -1, -1) if self._has(brain.root, vers[i], d["head_commit"])), 0)
                keep({"agent": d["agent"], "task": d["ref"], "state": "ok" if have == len(vers) else "behind", "have": have, "latest": len(vers)})
            for n in decisions:
                if n["task_id"] != x["id"]:
                    continue
                for agent in busy - {author}:
                    if last_run.get(agent, 0.0) < float(n["created_at"] or 0):
                        keep({"agent": agent, "state": "notyet", "note": n["body"][:120]})
                    elif agent not in entries:
                        keep({"agent": agent, "state": "ok", "note": n["body"][:120]})
            if entries:
                out[x["ref"]] = list(entries.values())
        return out

    _ancestry: dict[tuple[str, str, str], bool] = {}

    @classmethod
    def _has(cls, root: Path, commit: str, tip: str) -> bool:
        """Is ``commit`` part of ``tip``'s history? Commits never change, so answers are cached."""
        key = (str(root), commit, tip)
        if key not in cls._ancestry:
            if len(cls._ancestry) > 5000:
                cls._ancestry.clear()
            cls._ancestry[key] = commit == tip or gitops.is_ancestor(root, commit, tip)
        return cls._ancestry[key]

    def snapshot(self, project_dir: str) -> dict:
        """Everything a dashboard needs in one call."""
        brain = self.open(project_dir)
        meta = brain.meta()
        tasks = brain.tasks()
        runs = brain.runs(limit=60)
        return {
            "reach": self.reach(brain, tasks),
            "project": str(brain.root),
            "meta": meta,
            "tasks": tasks,
            "runs": [{k: v for k, v in r.items() if k != "prompt"} for r in runs],
            "notes": brain.notes(limit=60),
            "events": brain.events(limit=80),
            "needs": self.needs(project_dir),
            "agents": self.presence(brain),
            "usage": self.usage(brain),
            "now": time.time(),
        }

    @staticmethod
    def _task_brief(task: dict) -> dict:
        return {
            "task": task["ref"],
            "title": task["title"],
            "agent": task["agent"],
            "status": task["status"],
            "depends_on": [tid(d) for d in task["depends_on"]],
            "branch": task["branch"],
            "commit": task["head_commit"][:7] if task["head_commit"] else "",
            **({"browser": task["browser"]} if task.get("browser") else {}),
        }

    @staticmethod
    def _run_brief(run: dict, task: dict) -> dict:
        out = {
            "run": run["ref"],
            "task": task["ref"],
            "title": task["title"],
            "agent": run["agent"],
            "kind": run["kind"],
            "status": run["status"],
            "task_status": task["status"],
            "progress": run["progress"],
            "activity": run["activity"],
        }
        if run.get("model") or run.get("effort"):
            out["model"] = " · ".join(x for x in (run.get("model"), run.get("effort")) if x)
        files = run.get("files") or {}
        if files.get("changed"):
            out["files_changed"] = [f"{c['status']} {c['path']}" for c in files["changed"][:20]]
        if files.get("outputs"):
            out["outputs"] = files["outputs"][:20]
        if run.get("delegations"):
            out["delegated"] = [f"{d.get('role') or 'sub-agent'}: {d.get('title') or ''} ({d.get('status')})" for d in run["delegations"][:10]]
        if run["status"] in TERMINAL_RUN_STATUSES:
            out["summary"] = run["summary"]
            if run["error"]:
                out["error"] = run["error"]
            if run["diffstat"]:
                out["diffstat"] = run["diffstat"].splitlines()[-1].strip()
            if run["commit_after"]:
                out["commit"] = run["commit_after"][:7]
            if run["kind"] == "review" and isinstance(run.get("report"), dict):
                out["verdict"] = run["report"].get("verdict", "")
                out["comments"] = _as_list(run["report"].get("comments"))
            if run["started_at"] and run["ended_at"]:
                out["seconds"] = round(run["ended_at"] - run["started_at"])
        return out


def _severity(comment: str) -> int:
    """P0..P3 from a "[P2] ..." tag; untagged comments count as blocking (P2)."""
    import re

    m = re.search(r"\[?\bP([0-3])\b\]?", comment[:12])
    return int(m.group(1)) if m else 2


def _norm(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.lower()).split())


def _same_point(a: str, b: str) -> bool:
    """True when two notes say the same thing (rewording, one contains the other, or mostly the same words)."""
    import difflib

    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() > 0.72:
        return True
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) / max(1, min(len(wa), len(wb))) > 0.8


def _new_only(brain: Brain, run_id: int, kind: str, items: list[str]) -> list[str]:
    """Drop report items that repeat a note already in the brain, from any agent or run."""
    seen = [n["body"] for n in brain.notes(kinds=(kind,), limit=300)]
    fresh = []
    for item in items:
        if any(_same_point(item, s) for s in seen):
            continue
        fresh.append(item)
        seen.append(item)
    return fresh[:MAX_REPORT_DECISIONS]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]
