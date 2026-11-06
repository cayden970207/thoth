# Thoth — one brain for Claude Code, Codex and Hermes

You use Claude Code, Codex and Hermes. Each is good at different things, but they
don't know about each other: no shared context, no shared progress, and they
overwrite each other's files when they work on the same project.

**Thoth** turns them into one team. Hermes coordinates: it plans the work onto a
shared board, starts the **real** Claude Code and Codex CLIs on tasks in parallel,
has each one review the other's work, and merges the result. Everything the team
knows lives in one shared brain in your project, and the coordinator role can move
to Claude Code or Codex at any time.

```
                ┌──────────────── Hermes (coordinator) ────────────────┐
   you ──▶      │  thoth_plan → thoth_assign → thoth_wait → thoth_review │
                └───────┬──────────────────────────────┬───────────────┘
                        │ brief from the brain          │
             ┌──────────▼─────────┐          ┌──────────▼─────────┐
             │  Claude Code CLI   │          │     Codex CLI      │
             │ worktree T1, branch│          │ worktree T2, branch│
             └──────────┬─────────┘          └──────────┬─────────┘
                        │  decisions, handoffs (MCP)     │
                        └──────────▶  .thoth/brain  ◀────┘
                           goal · board · decisions · notes · runs · versions
```

## What it does

| Problem | What Thoth does |
|---|---|
| Agents don't share context | Every run starts with a **brief built from the brain**: goal, board, team decisions, handoffs, review comments and what dependencies produced. Agents write decisions back through the bundled MCP server while they work. |
| No shared progress | A **task board** (todo → running → built → reviewing → approved/changes → merged) with live progress of every running agent. |
| Agents overwrite each other | Each task gets its **own git branch and worktree**, so Claude Code and Codex can work at the same time. |
| No versions | Every run's result is a **commit** on its task branch; merges are `--no-ff` merge commits; the timeline records which agent made which commit. |
| One agent checks its own work | **Cross-review**: the other agent reviews the diff read-only and returns approve/changes with comments. Re-assigning a task resumes the original agent's session with the review comments in its brief. |
| Stuck with one coordinator | `thoth_coordinator` hands the coordinator role to Claude Code or Codex; they drive the same brain through the Thoth MCP server. |

## Requirements

- Hermes Agent ≥ 0.22
- `git`
- [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) and/or [Codex CLI](https://github.com/openai/codex) (`codex`), installed and **logged in**. Thoth uses your existing logins and plans; it never handles their credentials.

## Install

```bash
hermes plugins install thoth
hermes thoth doctor        # checks claude, codex and git
```

## Use it

Open Hermes in a git project and talk to it:

> Start a Thoth brain for this project: a tip calculator web page. Plan it so
> Claude builds the UI and Codex writes the calculation logic with tests, run
> them in parallel, then have each review the other's work and merge what passes.

Hermes will call the Thoth tools; you approve each agent launch and each merge.

| Tool | What it does |
|---|---|
| `thoth_init` | Create/reopen the brain (`.thoth/`, kept out of git) |
| `thoth_plan` | Add tasks with agent and dependencies |
| `thoth_assign` | Start Claude Code or Codex on a task in the background (**needs your approval**) |
| `thoth_wait` | Wait for runs and get summary, commit and diffstat |
| `thoth_review` | Cross-review by the other agent, read-only (**needs your approval**) |
| `thoth_merge` | Merge a task branch into the base branch (**needs your approval**) |
| `thoth_status` / `thoth_context` | Board, live progress, decisions; full brief for a task |
| `thoth_note` | Record a decision / handoff / risk / question for every agent |
| `thoth_cancel`, `thoth_update_task`, `thoth_coordinator` | Stop a run, edit a task, hand over coordination |

Slash command: `/thoth [status | task T1 | cancel R1 | coordinator claude|codex|hermes | brain | doctor]`.

### Let Claude Code or Codex coordinate

```
/thoth coordinator claude
```

prints a one-time setup command, e.g.

```bash
claude mcp add thoth -- <hermes python> -I <plugin dir>/mcp_server.py --as claude
```

Then in Claude Code: *"You are the Thoth coordinator. Call thoth_status and continue the plan."*
Claude Code now sees the same board, decisions and history, and can start Codex
(or another Claude Code) through `thoth_assign`. Claude Code's own permission
prompts decide which Thoth tools it may call. Runs started from Claude Code live
as long as that Claude Code session.

### Agents without the MCP server

`.thoth/BRAIN.md` is a readable snapshot of the whole brain, regenerated on every
change, so any tool can catch up by reading one file.

## Settings

Hermes Desktop → Settings → Plugins → Thoth, or `plugins.entries.thoth.settings` in `config.yaml`:
default worker, Claude Code permission mode and extra allowed tools, models, CLI
paths, run timeout (default 30 min), max parallel runs (default 3), whether agents
get the Thoth MCP tools, session resume, worktree cleanup, chat notifications,
environment passthrough.

## What Thoth does on your machine (disclosure)

- **Starts agent CLIs as subprocesses**: `claude -p` and `codex exec` (and `codex exec resume`), in a
  task worktree under `<project>/.thoth/worktrees/`, only after you approve each launch in Hermes.
  They run under **your** Claude Code / Codex login and plan, talk to Anthropic / OpenAI servers,
  and use your usage allowance.
- **Permissions it gives them** (never chosen by a model):
  build runs — Claude Code `--permission-mode acceptEdits` (file edits; shell only from read-only git
  commands plus your `claude_allowed_tools`), Codex `--sandbox workspace-write` (Codex's OS sandbox:
  writes only inside the worktree); review runs — read-only for both. Thoth never passes any
  "skip permissions" or "bypass sandbox" flag.
- **Pre-approves its own three worker tools inside Codex runs**: `codex exec` cannot ask anyone, so
  the run-scoped Thoth MCP server is started with `default_tools_approval_mode="approve"`. In worker
  mode that server exposes only `thoth_status`, `thoth_context` and `thoth_note` (read the brain,
  write a note); it cannot start agents or touch files.
- **Strips the environment** passed to agents: variables ending in `_API_KEY`, `_TOKEN`, `_SECRET`,
  `_PASSWORD`, and `HERMES_*`, `ANTHROPIC_*`, `OPENAI_*`, `CLAUDE_CODE_*`, `CODEX_*` (except `CODEX_HOME`)
  are removed unless you list them in `env_passthrough`.
- **Writes into your project**: `.thoth/` (SQLite brain, `BRAIN.md`, run logs with the agents' full
  JSON output, worktrees), one line in `.git/info/exclude`, git branches `thoth/T<n>-…`, commits
  authored `Thoth <thoth@localhost>` on those branches, and merge commits on your base branch when
  you approve a merge. In a repository with no commits it makes one empty initial commit.
- **Background processes**: runs continue after the tool call returns, up to the timeout; they are
  killed (whole process group) on cancel, timeout, plugin unload and Hermes exit.
- No network calls of its own, no telemetry, no credential reads, no self-updating code.

## Development

```bash
uvx pytest tests            # offline tests (no agent CLIs needed)
hermes plugins validate .   # catalog checks
hermes plugins doctor . --ci
```

`thoth_core/` is plain Python with no third-party dependencies, shared by the Hermes plugin
(`__init__.py`) and the MCP server (`mcp_server.py`).

MIT License.
