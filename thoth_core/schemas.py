"""Tool schemas shared by the Hermes plugin and the MCP server.

Each entry is ``name -> (description, parameters)``. Hermes wraps them as
``{"name", "description", "parameters"}``; MCP as ``{"name", "description", "inputSchema"}``.
"""

from __future__ import annotations

_PROJECT = {
    "type": "string",
    "description": "Project folder (any path inside the git repo). Defaults to the current session's folder.",
}
_TASK = {"type": "string", "description": "Task id, e.g. T3"}
_AGENT = {"type": "string", "enum": ["claude", "codex"], "description": "claude = Claude Code CLI, codex = Codex CLI"}

TOOLS: dict[str, tuple[str, dict]] = {
    "thoth_init": (
        "Start (or reopen) the Thoth shared brain for a git project so Claude Code, Codex and Hermes can work on it "
        "as one team. Creates .thoth/ in the repo (kept out of git) with the task board, decisions, notes and run history. "
        "Call this once per project before planning.",
        {
            "type": "object",
            "properties": {
                "project": _PROJECT,
                "goal": {"type": "string", "description": "What the team is building, in one or two sentences"},
            },
        },
    ),
    "thoth_plan": (
        "Add tasks to the shared board. Split the goal into tasks that can run in parallel where possible and give each "
        "one to the agent that suits it (claude: UI, product sense, refactors, docs; codex: algorithms, tests, scripts, "
        "careful edits). Every task gets its own branch and worktree when it starts.",
        {
            "type": "object",
            "properties": {
                "project": _PROJECT,
                "goal": {"type": "string", "description": "Optionally set or update the project goal"},
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Short imperative title"},
                            "detail": {"type": "string", "description": "Full instructions and acceptance criteria"},
                            "agent": _AGENT,
                            "depends_on": {
                                "type": "array",
                                "items": {"type": ["string", "integer"]},
                                "description": "Existing task ids (\"T2\") or 1-based positions of earlier tasks in this list (1, 2)",
                            },
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["tasks"],
        },
    ),
    "thoth_assign": (
        "Start an agent on a task in the background. The agent (Claude Code or Codex CLI) runs in the task's own git "
        "worktree with a brief built from the shared brain (goal, board, decisions, handoffs, review comments, dependency "
        "results). Its work is committed to the task branch when it finishes and its decisions are written back to the brain. "
        "Re-assigning a task the same agent worked on resumes that agent's session. Returns at once; use thoth_wait.",
        {
            "type": "object",
            "properties": {
                "project": _PROJECT,
                "task": _TASK,
                "agent": _AGENT,
                "instructions": {"type": "string", "description": "Extra instructions for this run only"},
            },
            "required": ["task"],
        },
    ),
    "thoth_review": (
        "Cross-review: start a different agent (by default the other one) on a read-only review of a built task's diff. "
        "The verdict (approve / changes) and comments land on the task, so a follow-up thoth_assign gets them automatically.",
        {
            "type": "object",
            "properties": {
                "project": _PROJECT,
                "task": _TASK,
                "reviewer": _AGENT,
                "instructions": {"type": "string", "description": "What to focus on"},
            },
            "required": ["task"],
        },
    ),
    "thoth_wait": (
        "Wait until runs finish (or the timeout passes) and return their results: status, summary, verdict, commit and diffstat. "
        "With no runs given, waits for every open run.",
        {
            "type": "object",
            "properties": {
                "project": _PROJECT,
                "runs": {"type": "array", "items": {"type": "string"}, "description": "Run ids, e.g. [\"R4\", \"R5\"]"},
                "timeout_seconds": {"type": "integer", "description": "Max seconds to wait (default 600, max 3600)"},
            },
        },
    ),
    "thoth_status": (
        "Show the shared brain: goal, coordinator, task board with branches and commits, live progress of running agents, "
        "recent decisions and activity. Pass a task to see its runs, notes and review comments.",
        {"type": "object", "properties": {"project": _PROJECT, "task": _TASK}},
    ),
    "thoth_note": (
        "Write to the shared brain so every other agent sees it. kinds: decision (rules everyone must follow), context "
        "(background), handoff (what the next agent needs), risk, question, progress.",
        {
            "type": "object",
            "properties": {
                "project": _PROJECT,
                "kind": {"type": "string", "enum": ["decision", "context", "handoff", "progress", "question", "risk"]},
                "body": {"type": "string"},
                "task": {"type": "string", "description": "Attach to a task (optional)"},
            },
            "required": ["kind", "body"],
        },
    ),
    "thoth_merge": (
        "Merge a built or approved task's branch into the base branch (git merge --no-ff). Conflicts are aborted and "
        "reported, leaving the base branch unchanged. The merge commit is recorded as a version in the brain.",
        {"type": "object", "properties": {"project": _PROJECT, "task": _TASK}, "required": ["task"]},
    ),
    "thoth_context": (
        "Get the full brief an agent would receive for a task (or the whole BRAIN.md when no task is given).",
        {"type": "object", "properties": {"project": _PROJECT, "task": _TASK}},
    ),
    "thoth_cancel": (
        "Stop a running agent run (kills its process tree). Its partial work is still committed to the task branch.",
        {"type": "object", "properties": {"project": _PROJECT, "run": {"type": "string", "description": "Run id, e.g. R4"}}, "required": ["run"]},
    ),
    "thoth_update_task": (
        "Edit a task on the board: title, detail, agent, or status 'todo' (reopen) / 'cancelled'.",
        {
            "type": "object",
            "properties": {
                "project": _PROJECT,
                "task": _TASK,
                "title": {"type": "string"},
                "detail": {"type": "string"},
                "agent": _AGENT,
                "status": {"type": "string", "enum": ["todo", "cancelled"]},
            },
            "required": ["task"],
        },
    ),
    "thoth_coordinator": (
        "Hand the coordinator role to another AI (hermes, claude or codex). The brain records who coordinates; the "
        "result explains how to continue from that tool, which reads and drives the same brain through the Thoth MCP server.",
        {
            "type": "object",
            "properties": {
                "project": _PROJECT,
                "agent": {"type": "string", "enum": ["hermes", "claude", "codex"]},
            },
            "required": ["agent"],
        },
    ),
}

# What an agent gets while it works on a run: read and write the brain, never launch other agents.
WORKER_TOOLS = ("thoth_status", "thoth_context", "thoth_note")

EMOJI = {
    "thoth_init": "🪶",
    "thoth_plan": "🗺️",
    "thoth_assign": "🚀",
    "thoth_review": "🔍",
    "thoth_wait": "⏳",
    "thoth_status": "📜",
    "thoth_note": "✍️",
    "thoth_merge": "🔀",
    "thoth_context": "📖",
    "thoth_cancel": "🛑",
    "thoth_update_task": "📝",
    "thoth_coordinator": "👑",
}
