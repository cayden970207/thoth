"""High-level Thoth operations shared by the Hermes plugin and the MCP server.

Every public method takes plain arguments, returns a JSON-serializable dict and
raises ``BrainError`` for anything the caller should show to the user.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import gitops
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
    "max_review_rounds": 3,
    "remove_worktree_on_merge": True,
    "env_passthrough": [],
    "notify_on_complete": True,
    "inject_board": True,
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

    def init(self, project_dir: str, goal: str = "") -> dict:
        root = gitops.repo_root(project_dir)
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
        brain.write_snapshot()
        result = self.status(str(root))
        result["initialized"] = not existed
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
            created.append(
                brain.add_task(
                    title=str(spec["title"]).strip(),
                    detail=str(spec.get("detail") or "").strip(),
                    agent=agent,
                    depends_on=deps,
                    actor=self.actor,
                    kind=kind,
                    role=str(spec.get("role") or "").strip()[:60],
                )
            )
        return {"created": [self._task_brief(t) for t in created], "board": board_lines(brain)}

    def update_task(self, project_dir: str, task: Any, *, title: str = "", detail: str = "", agent: str = "", status: str = "", kind: str = "", role: str = "") -> dict:
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

    def assign(self, project_dir: str, task: Any, agent: str = "", instructions: str = "") -> dict:
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
        if agent == "hermes":
            return self._assign_to_coordinator(brain, current, instructions)
        driver = self._driver(agent)
        self._check_capacity(brain)
        current = self._prepare_worktree(brain, current)
        session = brain.last_session(task_id, agent) if self.settings.get("resume_sessions") else ""
        has_mcp = bool(self.settings.get("attach_mcp")) and self.mcp_launcher is not None
        prompt = build_brief(brain, current, agent=agent, instructions=instructions, has_mcp=has_mcp, resumed=bool(session))
        run = brain.add_run(task_id=task_id, agent=agent, kind="build", prompt=prompt, actor=self.actor)
        brain.update_task(task_id, status="running", agent=agent)
        brain.event(self.actor, "run_started", f"{agent} builds {current['ref']} {current['title']}" + (" (resumed session)" if session else ""), task_id=task_id, run_id=run["id"])
        self._launch(brain, run["id"], current, driver, kind="build", prompt=prompt, session=session, cwd=Path(current["worktree"]))
        return {"run": run["ref"], "task": current["ref"], "agent": agent, "branch": current["branch"], "resumed_session": bool(session), "status": "running"}

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

    def review(self, project_dir: str, task: Any, reviewer: str = "", instructions: str = "") -> dict:
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
        brain.update_task(task_id, status="reviewing")
        brain.event(self.actor, "review_started", f"{reviewer} reviews {current['agent']}'s {current['ref']}", task_id=task_id, run_id=run["id"])
        self._launch(brain, run["id"], current, driver, kind="review", prompt=prompt, session="", cwd=worktree)
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
        if len(unmerged) > 1:
            raise BrainError(
                f"{task['ref']} depends on several unmerged tasks ({', '.join(d['ref'] for d in unmerged)}); merge them first"
            )
        if unmerged:
            base_ref = unmerged[0]["branch"]
        branch = f"thoth/{task['ref']}-{gitops.slugify(task['title'])}"
        worktree = brain.worktrees_dir / task["ref"]
        base_commit = gitops.create_worktree(brain.root, worktree, branch, base_ref)
        return brain.update_task(task["id"], branch=branch, worktree=str(worktree), base_ref=base_ref, base_commit=base_commit, head_commit=base_commit)

    def _launch(self, brain: Brain, run_id: int, task: dict, driver, *, kind: str, prompt: str, session: str, cwd: Path) -> None:
        mcp = None
        if self.settings.get("attach_mcp") and self.mcp_launcher is not None:
            try:
                mcp = self.mcp_launcher(brain.root, driver.name, run_id, task["id"])
            except Exception:
                logger.warning("thoth: could not prepare the MCP server for R%s", run_id, exc_info=True)
        cmd = driver.command(kind=kind, session_id=session, mcp=mcp)
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
                env=clean_env(self.settings.get("env_passthrough") or []),
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

    def needs(self, project_dir: str) -> list[dict]:
        """What is waiting on a person (or the coordinator), most urgent first."""
        brain = self.open(project_dir)
        out: list[dict] = []
        for t in brain.tasks():
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

    def snapshot(self, project_dir: str) -> dict:
        """Everything a dashboard needs in one call."""
        brain = self.open(project_dir)
        meta = brain.meta()
        tasks = brain.tasks()
        runs = brain.runs(limit=60)
        return {
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
