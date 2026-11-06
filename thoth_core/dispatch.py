"""Map a tool call (name + arguments) onto the service. Shared by Hermes and MCP."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .brain import BrainError
from .service import Thoth


def mcp_server_path() -> Path:
    return Path(__file__).resolve().parent.parent / "mcp_server.py"


def coordinator_howto(agent: str, project: str) -> str:
    server = mcp_server_path()
    py = sys.executable
    if agent == "claude":
        return (
            "Claude Code now coordinates. One-time setup (registers the Thoth MCP server in Claude Code for this project):\n"
            f"  cd {project} && claude mcp add thoth -- {py} -I {server} --as claude\n"
            "Then open `claude` in the project and say, e.g.: \"You are the Thoth coordinator. Call thoth_status and continue the plan.\""
        )
    if agent == "codex":
        return (
            "Codex now coordinates. One-time setup (registers the Thoth MCP server in Codex):\n"
            f"  codex mcp add thoth -- {py} -I {server} --as codex\n"
            "Then run `codex` in the project and say, e.g.: \"You are the Thoth coordinator. Call thoth_status and continue the plan.\""
        )
    return "Hermes coordinates again. Ask Hermes for the Thoth status to pick up where the others left off."


def call(svc: Thoth, name: str, args: dict[str, Any], default_project: Callable[[], str]) -> dict:
    """Run one tool. Raises BrainError for user-facing problems."""
    args = dict(args or {})
    project = str(args.pop("project", "") or "").strip() or default_project()
    if not project:
        raise BrainError("No project folder: pass `project`")
    if name == "thoth_init":
        setup, run = args.get("setup"), args.get("run")
        return svc.init(project, goal=str(args.get("goal") or ""), setup=None if setup is None else str(setup), create=bool(args.get("create")),
                        run=None if run is None else str(run))
    if name == "thoth_preview":
        return svc.preview(project, task=args.get("task") or None)
    if name == "thoth_plan":
        return svc.plan(project, args.get("tasks") or [], goal=str(args.get("goal") or ""))
    if name == "thoth_assign":
        return svc.assign(project, args.get("task"), agent=str(args.get("agent") or ""), instructions=str(args.get("instructions") or ""),
                          model=str(args.get("model") or ""), effort=str(args.get("effort") or ""))
    if name == "thoth_review":
        return svc.review(project, args.get("task"), reviewer=str(args.get("reviewer") or ""), instructions=str(args.get("instructions") or ""),
                          model=str(args.get("model") or ""), effort=str(args.get("effort") or ""))
    if name == "thoth_wait":
        return svc.wait(project, args.get("runs") or None, timeout_seconds=int(args.get("timeout_seconds") or 600))
    if name == "thoth_status":
        return svc.status(project, task=args.get("task"))
    if name == "thoth_note":
        return svc.note(project, str(args.get("kind") or "context"), str(args.get("body") or ""), task=args.get("task"), run=args.get("run"))
    if name == "thoth_merge":
        return svc.merge(project, args.get("task"), override=bool(args.get("override")))
    if name == "thoth_context":
        return svc.context(project, task=args.get("task"), agent=svc.actor)
    if name == "thoth_cancel":
        return svc.cancel(project, args.get("run"))
    if name == "thoth_update_task":
        return svc.update_task(
            project,
            args.get("task"),
            title=str(args.get("title") or ""),
            detail=str(args.get("detail") or ""),
            agent=str(args.get("agent") or ""),
            status=str(args.get("status") or ""),
            kind=str(args.get("kind") or ""),
            role=str(args.get("role") or ""),
            model=args.get("model") if args.get("model") is not None else None,
            effort=args.get("effort") if args.get("effort") is not None else None,
        )
    if name == "thoth_revise":
        return svc.revise(project, args.get("task"), str(args.get("feedback") or ""), agent=str(args.get("agent") or ""), files=args.get("files") or [],
                          model=str(args.get("model") or ""), effort=str(args.get("effort") or ""))
    if name == "thoth_restore":
        return svc.restore(project, args.get("task"), str(args.get("commit") or ""))
    if name == "thoth_sync":
        return svc.sync(project, args.get("task"))
    if name == "thoth_models":
        return svc.models()
    if name == "thoth_complete":
        return svc.complete(
            project, args.get("task"), str(args.get("summary") or ""),
            outputs=[str(o) for o in args.get("outputs") or []], decisions=[str(x) for x in args.get("decisions") or []],
            handoff=str(args.get("handoff") or ""),
        )
    if name == "thoth_coordinator":
        result = svc.set_coordinator(project, str(args.get("agent") or ""))
        result["how_to_continue"] = coordinator_howto(result["coordinator"], result["project"])
        return result
    raise BrainError(f"Unknown Thoth tool {name}")
