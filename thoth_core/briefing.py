"""Turning the brain into words an agent can act on, and back.

``build_brief`` / ``review_brief`` compose the shared context every agent gets
before it starts: the goal, the board, decisions, handoffs, review comments and
what its dependencies produced. ``parse_report`` reads the structured report an
agent ends with, so its decisions flow back into the brain for the next agent.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from .brain import rid, tid

if TYPE_CHECKING:
    from .brain import Brain

REPORT_MARKER = "THOTH_REPORT"
MAX_BRIEF_NOTES = 15

_STATUS_ICON = {
    "todo": "○",
    "running": "◐",
    "built": "●",
    "reviewing": "◑",
    "approved": "✓",
    "changes": "↺",
    "merged": "⇢",
    "failed": "✗",
    "conflict": "⚠",
    "cancelled": "–",
}


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    delta = max(0, int(time.time() - float(ts)))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def board_lines(brain: "Brain") -> list[str]:
    lines = []
    for t in brain.tasks():
        deps = f" (after {', '.join(tid(d) for d in t['depends_on'])})" if t["depends_on"] else ""
        commit = f" @{t['head_commit'][:7]}" if t["head_commit"] else ""
        lines.append(
            f"{_STATUS_ICON.get(t['status'], '?')} {t['ref']} [{t['status']}] {t['agent'] or 'unassigned'}"
            f"{' as ' + t['role'] if t.get('role') else ''}{' (' + t['kind'] + ')' if t.get('kind', 'code') != 'code' else ''}: {t['title']}{deps}{commit}"
        )
    return lines or ["(no tasks yet)"]


def _note_line(n: dict) -> str:
    where = f" {tid(n['task_id'])}" if n.get("task_id") else ""
    return f"- [{n['kind']}{where}] {n['author']}: {n['body']}"


def _tools_section(has_mcp: bool) -> str:
    if not has_mcp:
        return ""
    return (
        "\n## Thoth tools you can use\n"
        "You are connected to the `thoth` MCP server. Use it to stay in sync with the team:\n"
        "- `thoth_status` — the live board and recent activity\n"
        "- `thoth_context` — the full brief for any task\n"
        "- `thoth_note` — record a decision, a handoff, a risk or a question for the others "
        "(decisions you record are shown to every agent that runs after you)\n\n"
        "Record a decision only when another agent must follow it (an interface, a file layout, a rule). "
        "One sentence each, no more than 25 words, and never restate a decision already listed above.\n"
    )


def _report_instructions(kind: str) -> str:
    if kind == "review":
        example = {
            "verdict": "approve | changes",
            "summary": "one paragraph on what you checked and what you found",
            "comments": ["[P1] file:line — a blocking problem the author must fix"],
            "suggestions": ["[P3] file:line — optional polish that does not block the merge"],
            "decisions": [],
        }
    else:
        example = {
            "status": "done | blocked",
            "summary": "one paragraph on what you built and how",
            "decisions": ["at most 3 NEW rules other agents must follow, one short sentence each (omit ones already recorded)"],
            "handoff": "what the next agent needs to know (empty if nothing)",
            "outputs": ["path/to/file — what it is (every file you produced for others to use)"],
            "risks": ["anything fragile or untested"],
        }
    return (
        "\n## When you finish\n"
        f"End your final message with the line `{REPORT_MARKER}` followed by a JSON object, exactly like:\n\n"
        f"{REPORT_MARKER}\n```json\n{json.dumps(example, indent=2)}\n```\n"
    )


KIND_GUIDE = {
    "code": "Write working code and verify it by running it where you can.",
    "asset": (
        "This is an asset task: produce the files themselves (images, sprites, audio, data). Put them under "
        "`assets/` unless the task names another place, use clear file names, and keep them at the sizes the task asks for. "
        "Codex can create images with its image generation tool and process them with ImageMagick (`magick`) or Python; "
        "check every file you produce (size, format, transparency) before you finish."
    ),
    "doc": "This is a writing task: produce the document files (Markdown unless the task says otherwise).",
    "research": "This is a research task: write your findings to a Markdown file under `notes/` with sources, then summarize.",
    "design": "This is a design task: produce the design files (specs, mockups, style guides) the builders will follow.",
    "qa": "This is a QA task: exercise the product, write what you found to `notes/qa.md`, and fix nothing unless asked.",
}


def _dep_files(brain: "Brain", dep_id: int) -> list[str]:
    """Files a dependency produced (its declared outputs, else what its builds changed)."""
    outputs, changed = [], []
    for r in reversed(brain.runs(task_id=dep_id, limit=20)):
        f = r.get("files") or {}
        outputs += [o for o in f.get("outputs") or [] if o not in outputs]
        changed += [c["path"] for c in f.get("changed") or [] if c.get("status") != "D" and c["path"] not in changed]
    return (outputs or changed)[:25]


def build_brief(brain: "Brain", task: dict, *, agent: str, instructions: str = "", has_mcp: bool = False, resumed: bool = False, self_run: bool = False) -> str:
    meta = brain.meta()
    coordinator = meta.get("coordinator", "hermes")
    parts: list[str] = []
    if resumed:
        parts.append(
            f"# Thoth: continue task {task['ref']} — {task['title']}\n"
            "You worked on this task before in this same session. Read the new information below and continue."
        )
    else:
        parts.append(
            f"# Thoth brief — task {task['ref']}: {task['title']}\n"
            f"You are **{agent}**, one member of a multi-agent team (Claude Code, Codex, Hermes) "
            f"coordinated by **{coordinator}** through Thoth, a shared project brain. "
            "Other agents work on other tasks in parallel, each in its own git worktree."
        )
        if meta.get("goal"):
            parts.append(f"## Project goal\n{meta['goal']}")
    role = (task.get("role") or "").strip()
    kind = task.get("kind") or "code"
    if role or kind != "code":
        parts.append("## Your role\n" + (f"On this task you are the team's **{role}**. " if role else "") + KIND_GUIDE.get(kind, ""))
    body = task.get("detail") or task["title"]
    if instructions.strip():
        body += f"\n\nCoordinator's instructions for this run:\n{instructions.strip()}"
    parts.append(f"## Your task\n{body}")
    parts.append(
        "## Workspace\n"
        f"Your current directory is an isolated git worktree on branch `{task['branch']}` "
        f"(based on `{task['base_ref']}` @ {task['base_commit'][:7]}). Work only inside it and do not switch branches. "
        "Thoth commits whatever you leave in the worktree when you finish, so you do not have to commit."
    )
    if not resumed:
        parts.append("## Team board\n" + "\n".join(board_lines(brain)))
    decisions = brain.notes(kinds=("decision",), limit=MAX_BRIEF_NOTES)
    if decisions:
        parts.append("## Decisions so far (follow these)\n" + "\n".join(_note_line(n) for n in decisions))
    context = brain.notes(project_wide=True, kinds=("context", "risk", "question"), limit=8)
    if context:
        parts.append("## Shared context\n" + "\n".join(_note_line(n) for n in context))
    task_notes = brain.notes(task_id=task["id"], kinds=("feedback", "handoff", "review", "question", "context", "risk"), limit=MAX_BRIEF_NOTES)
    if task_notes:
        parts.append("## Notes on this task (handoffs, review comments)\n" + "\n".join(_note_line(n) for n in task_notes))
    dep_lines = []
    for dep_id in task.get("depends_on") or []:
        dep = brain.task(dep_id)
        last = brain.runs(task_id=dep_id, limit=1)
        summary = last[0]["summary"] if last else ""
        dep_lines.append(f"- {dep['ref']} {dep['title']} [{dep['status']}] on `{dep['branch']}`: {summary or '(no summary)'}")
        files = _dep_files(brain, dep_id)
        if files:
            dep_lines.append("  Files it produced (already in your worktree when it is your base, otherwise on its branch):\n" + "\n".join(f"    - {f}" for f in files))
    if dep_lines:
        parts.append("## What your dependencies produced\n" + "\n".join(dep_lines))
    parts.append(_tools_section(has_mcp))
    if self_run:
        parts.append(
            "## How to finish\nYou are doing this task yourself, not through a CLI agent. Work inside the worktree path above, "
            "then call `thoth_complete` with this task id, a summary, the `outputs` you produced and any `decisions`."
        )
    else:
        parts.append(
            "## Delegating\nIf part of this task is better done by a specialist, you may hand it to your own sub-agents "
            "(Claude Code: the Agent/Task tool; Codex: spawn_agent). Thoth shows them to the team, so give each a clear role."
        )
        parts.append(_report_instructions("build"))
    return "\n\n".join(p for p in parts if p)


def review_brief(brain: "Brain", task: dict, *, reviewer: str, diff_text: str, log_text: str, has_mcp: bool = False) -> str:
    meta = brain.meta()
    author = task.get("agent") or "another agent"
    last = brain.runs(task_id=task["id"], limit=5)
    build_summary = next((r["summary"] for r in last if r["kind"] == "build" and r["summary"]), "")
    parts = [
        f"# Thoth review — task {task['ref']}: {task['title']}\n"
        f"You are **{reviewer}**, reviewing work that **{author}** did for the team. "
        "Do not edit any files: this is a read-only review.",
    ]
    if meta.get("goal"):
        parts.append(f"## Project goal\n{meta['goal']}")
    parts.append(f"## The task\n{task.get('detail') or task['title']}")
    if build_summary:
        parts.append(f"## The author's summary\n{build_summary}")
    decisions = brain.notes(kinds=("decision",), limit=MAX_BRIEF_NOTES)
    if decisions:
        parts.append("## Team decisions the work must respect\n" + "\n".join(_note_line(n) for n in decisions))
    parts.append(
        "## What to review\n"
        f"Your current directory is the task worktree on branch `{task['branch']}`. "
        f"Commits since `{task['base_ref']}`:\n```\n{log_text or '(none)'}\n```\n"
        f"Diff:\n```diff\n{diff_text or '(empty diff)'}\n```\n"
        "Check correctness, edge cases, consistency with the team decisions and with the rest of the code. "
        "Read surrounding files when you need more context, and run the code when that settles a question.\n\n"
        "Severity: P0 breaks the product, P1 a user will hit it, P2 a realistic edge case gives a wrong result, "
        "P3 polish or a far-fetched input. Put only P0–P2 in `comments`, and answer `changes` only if there is at "
        "least one. P3 items go in `suggestions` and do not block the merge."
    )
    rounds = sum(1 for r in brain.runs(task_id=task["id"], limit=50) if r["kind"] == "review" and r["status"] == "done")
    if rounds:
        parts.append(
            f"## Earlier reviews\nThis is review round {rounds + 1}. Earlier comments are in the notes above. "
            "Check whether they were fixed; do not raise new P3-level points as blocking."
        )
    parts.append(_tools_section(has_mcp))
    parts.append(_report_instructions("review"))
    return "\n\n".join(p for p in parts if p)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_report(text: str) -> dict[str, Any]:
    """Pull the JSON report out of an agent's final message. Never raises."""
    if not text:
        return {}
    tail = text
    idx = text.rfind(REPORT_MARKER)
    if idx != -1:
        tail = text[idx + len(REPORT_MARKER) :]
    candidates = _JSON_BLOCK.findall(tail) or _JSON_BLOCK.findall(text)
    if not candidates:
        brace = tail.find("{")
        if brace != -1:
            candidates = [tail[brace : tail.rfind("}") + 1]]
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def strip_report(text: str) -> str:
    idx = text.rfind(REPORT_MARKER)
    return text[:idx].strip() if idx != -1 else text.strip()


def render_brain_md(brain: "Brain") -> str:
    meta = brain.meta()
    lines = [
        "# Thoth brain",
        "",
        "Shared memory for every agent on this project. Generated by Thoth; do not edit by hand.",
        "Agents with the Thoth MCP server should use its tools; others can read this file.",
        "",
        f"- **Goal:** {meta.get('goal') or '(not set)'}",
        f"- **Coordinator:** {meta.get('coordinator', 'hermes')}",
        f"- **Base branch:** {meta.get('base_branch', '')}",
        "",
        "## Board",
        "",
        *board_lines(brain),
        "",
    ]
    open_runs = brain.runs(open_only=True)
    if open_runs:
        lines += ["## Running now", ""]
        for r in open_runs:
            lines.append(f"- {r['ref']} {r['agent']} {r['kind']} on {r['task_ref']}: {r['progress'] or 'starting'}")
        lines.append("")
    decisions = brain.notes(kinds=("decision",), limit=30)
    if decisions:
        lines += ["## Decisions", "", *(_note_line(n) for n in decisions), ""]
    others = brain.notes(kinds=("context", "handoff", "risk", "question", "review"), limit=20)
    if others:
        lines += ["## Notes", "", *(_note_line(n) for n in others), ""]
    events = brain.events(limit=25)
    if events:
        lines += ["## Timeline", ""]
        for e in events:
            where = " ".join(x for x in (tid(e.get("task_id")), rid(e.get("run_id"))) if x)
            commit = f" `{e['commit_sha'][:7]}`" if e.get("commit_sha") else ""
            stamp = time.strftime("%m-%d %H:%M", time.localtime(e["ts"]))
            lines.append(f"- {stamp} {e['actor']} {e['type']} {where}{commit}: {e['message']}")
        lines.append("")
    return "\n".join(lines)


def ago(ts: float | None) -> str:
    return _ago(ts)
