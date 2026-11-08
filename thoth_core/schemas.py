"""Tool schemas shared by the Hermes plugin and the MCP server.

Each entry is ``name -> (description, parameters)``. Hermes wraps them as
``{"name", "description", "parameters"}``; MCP as ``{"name", "description", "inputSchema"}``.
"""

from __future__ import annotations

_PROJECT = {"type": "string"}  # the project folder; defaults to the chat's folder (see thoth_init)
_TASK = {"type": "string", "description": "Task id, e.g. T3"}
_AGENT = {"type": "string", "enum": ["claude", "codex"]}
_WORKER = {"type": "string", "enum": ["claude", "codex", "hermes"], "description": "hermes = you do it yourself"}
_KIND = {"type": "string", "enum": ["code", "asset", "doc", "research", "design", "qa"]}
_ROLE = {"type": "string", "description": "e.g. Game developer, Art pipeline"}
_MODEL = {"type": "string", "description": "fable|opus|sonnet|haiku, or a Codex slug (thoth_models)"}
_EFFORT = {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "ultra"]}
_BROWSER = {"type": "string", "enum": ["off", "clean", "user"],
            "description": "Web browser for the agent: clean = a fresh Chrome without logins; user = the user's own Chrome and logins, only when the task needs them"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    out = {"type": "object", "properties": {"project": _PROJECT, **props}}
    if required:
        out["required"] = required
    return out


# Short on purpose: every coordinator call carries these. The playbook lives in the coordinate skill.
TOOLS: dict[str, tuple[str, dict]] = {
    "thoth_init": (
        "Start or reopen the shared brain for a project (board, decisions, history in .thoth/). For a new project pass "
        "create=true and a short folder name as project: Thoth makes it in the user's projects folder (~/Thoth by "
        "default). A folder of theirs: its full path; Thoth keeps versions there itself. A board Thoth hasn't used on this "
        "computer before needs the user's yes.",
        _obj({
            "project": {"type": "string", "description": "A short folder name or a full path; other tools default to the chat's folder"},
            "goal": {"type": "string", "description": "What the team builds"},
            "create": {"type": "boolean"},
            "run": {"type": "string", "description": "One-line command that starts the app for previews, with {port}. Runs only after the user approves it for this folder"},
            "setup": {"type": "string", "description": "One-line command run in each new task folder before an agent starts, e.g. npm ci. Runs only after the user approves it for this folder"},
        }),
    ),
    "thoth_plan": (
        "Add tasks. Give each an agent (claude: product, UI, apps, docs; codex: images, algorithms, tests, scripts; "
        "hermes: you), a kind and a role. Split for parallel work; depends_on hands one task's files to the next.",
        _obj({
            "goal": {"type": "string", "description": "Set or update the project goal"},
            "tasks": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string", "description": "Short imperative"},
                "detail": {"type": "string", "description": "What to do and what must be true when done, in a few lines (not a test plan)"},
                "agent": _WORKER, "kind": _KIND, "role": _ROLE, "model": _MODEL, "effort": _EFFORT, "browser": _BROWSER,
                "depends_on": {"type": "array", "items": {"type": ["string", "integer"]}, "description": "Task ids (\"T2\") or 1-based positions in this list"},
            }, "required": ["title"]}},
        }, ["tasks"]),
    ),
    "thoth_assign": (
        "Start an agent on a task in the background, in the task's own worktree, with a brief from the brain. "
        "Its work is committed when it ends; re-assigning resumes its session. Returns at once: then call thoth_wait.",
        _obj({"task": _TASK, "agent": _WORKER, "instructions": {"type": "string", "description": "Extra instructions for this run"},
              "model": _MODEL, "effort": _EFFORT, "browser": _BROWSER}, ["task"]),
    ),
    "thoth_review": (
        "Read-only cross-review of a built task by another agent (default: the other one). Verdict and comments go on the task.",
        _obj({"task": _TASK, "reviewer": _AGENT, "instructions": {"type": "string", "description": "What to focus on"},
              "model": _MODEL, "effort": _EFFORT}, ["task"]),
    ),
    "thoth_wait": (
        "Wait for runs to finish (default: every open run); returns status, summary, verdict, commit and diffstat.",
        _obj({"runs": {"type": "array", "items": {"type": "string"}, "description": "Run ids, e.g. [\"R4\"]"},
              "timeout_seconds": {"type": "integer", "description": "Default 600, max 3600"}}),
    ),
    "thoth_status": (
        "The board: goal, tasks, live progress, decisions, activity. With a task: its runs, notes and review comments.",
        _obj({"task": _TASK}),
    ),
    "thoth_note": (
        "Record something every agent will see: decision (a rule to follow), handoff, risk, question, context, progress.",
        _obj({"kind": {"type": "string", "enum": ["decision", "context", "handoff", "progress", "question", "risk"]},
              "body": {"type": "string"}, "task": {"type": "string", "description": "Attach to a task"}}, ["kind", "body"]),
    ),
    "thoth_merge": (
        "Merge a task's branch into the base branch. A conflict aborts and changes nothing.",
        _obj({"task": _TASK, "override": {"type": "boolean", "description": "Merge despite open review comments, only if the user decided so"}}, ["task"]),
    ),
    "thoth_context": ("The brief an agent gets for a task, or BRAIN.md without a task.", _obj({"task": _TASK})),
    "thoth_cancel": ("Stop a run; its partial work is committed.", _obj({"run": {"type": "string", "description": "Run id, e.g. R4"}}, ["run"])),
    "thoth_update_task": (
        "Edit a task, reopen it (status todo) or cancel it.",
        _obj({"task": _TASK, "title": {"type": "string"}, "detail": {"type": "string"}, "agent": _WORKER, "kind": _KIND,
              "role": _ROLE, "model": _MODEL, "effort": _EFFORT, "browser": _BROWSER,
              "status": {"type": "string", "enum": ["todo", "cancelled"]}}, ["task"]),
    ),
    "thoth_complete": (
        "Finish a task you did yourself (assigned to hermes): commits it and records your summary, outputs and decisions.",
        _obj({"task": _TASK, "summary": {"type": "string"},
              "outputs": {"type": "array", "items": {"type": "string"}, "description": "Files you made, e.g. [\"assets/logo.png — logo\"]"},
              "decisions": {"type": "array", "items": {"type": "string"}}, "handoff": {"type": "string"}}, ["task", "summary"]),
    ),
    "thoth_revise": (
        "Send a task back with the user's feedback; the agent continues in its session and makes a new version.",
        _obj({"task": _TASK, "feedback": {"type": "string", "description": "What should change, in the user's words"},
              "files": {"type": "array", "items": {"type": "string"}, "description": "Files to redo"},
              "agent": _WORKER, "model": _MODEL, "effort": _EFFORT}, ["task", "feedback"]),
    ),
    "thoth_restore": (
        "Bring back an earlier version of a task, as a new version.",
        _obj({"task": _TASK, "commit": {"type": "string", "description": "Commit id of that version"}}, ["task", "commit"]),
    ),
    "thoth_sync": ("Bring the newest versions of a task's dependencies into it.", _obj({"task": _TASK}, ["task"])),
    "thoth_preview": (
        "Show the user what a task built (page, game, app) or, without a task, everything merged: returns a localhost link. "
        "When the project has a run command, the user approves starting the app. Never start servers or open browsers yourself.",
        _obj({"task": {"type": "string", "description": "Omit for everything merged"}}),
    ),
    "thoth_models": ("Models and efforts Claude Code and Codex can use here.", _obj({})),
    "thoth_coordinator": (
        "Hand the coordinator role to hermes, claude or codex; returns how to continue there.",
        _obj({"agent": {"type": "string", "enum": ["hermes", "claude", "codex"]}}, ["agent"]),
    ),
    "thoth_autopilot": (
        "Run the whole plan on its own after the user approves it once: Thoth starts each task when what it builds on "
        "is merged, cross-reviews it, sends changes back, merges what passes, and writes to this chat when it needs "
        "the user. Use it when the user wants the job done end to end. start shows the plan card (also to resume "
        "after a pause); pause, stop and status need no approval.",
        _obj({"action": {"type": "string", "enum": ["start", "pause", "stop", "status"]},
              "hours": {"type": "number", "description": "Time limit in hours, up to 24 (default 8, from settings)"}}, ["action"]),
    ),
}

# Only Hermes runs autopilot: it drives the plan from Hermes's own process and asks through Hermes's chat.
HERMES_ONLY = ("thoth_autopilot",)

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
    "thoth_complete": "✅",
    "thoth_revise": "🎨",
    "thoth_restore": "⏪",
    "thoth_sync": "🔄",
    "thoth_models": "🧠",
    "thoth_preview": "👀",
    "thoth_autopilot": "🛫",
}
