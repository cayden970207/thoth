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

## Not only code

Tasks have a **kind** (code, asset, doc, research, design, qa) and a **role**, and `depends_on`
passes one task's files to the next. Codex can generate and process images (its image tool plus
ImageMagick), Claude Code builds, and Hermes can take a task itself (`thoth_assign` to `hermes`,
then `thoth_complete`). The Thoth page shows what every agent read, changed and produced (images as
thumbnails), which files moved between tasks, and every sub-agent an agent delegated to.

## Context, tokens and memory

Measured on real runs (see `thoth_status` → `tokens`, or the Team activity card):

- **The coordinator stays small.** Hermes receives each run's summary, diffstat and verdict, not the
  transcript. On the tip-calculator demo the agents' transcripts were 674 KB; Hermes received 9.6 KB
  (98.6 % kept out, about 166k tokens).
- **Every agent starts focused.** A run gets a 2–18 KB brief (goal, decisions, handoffs, review
  comments, the files its dependencies produced) instead of the whole conversation.
- **Lean workers** (on by default) start agents without your personal plugins, hooks, skills and extra
  MCP servers: one call's fixed prompt drops from 46.8k to 25.2k tokens for Claude Code and from 35.7k
  to 27.1k for Codex. Every call re-sends that prefix, so a 10–30 call run saves 200k–600k tokens.
- **Caching does the rest:** 82–95 % of prompt tokens in our runs were cache hits.
- **Memory:** the brain (`.thoth/brain.db`, `BRAIN.md`) keeps goal, decisions, handoffs, reviews and
  versions across sessions and across tools, and re-assigning a task resumes the agent's own session.

What it does not do: total tokens across all agents are not lower than one agent doing everything —
each agent pays its own fixed prompt and cross-review reads the diff again. The gain is that no single
context has to hold everything, the expensive coordinator context stays tiny, and the work is spread
across your Claude and ChatGPT plans.

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
| `thoth_preview` | Open what a task built (a web page, a browser game) in your browser, served read-only on localhost |
| `thoth_revise`, `thoth_restore`, `thoth_sync` | Send work back with feedback, bring back an earlier version, pull a dependency's newer version |
| `thoth_cancel`, `thoth_update_task`, `thoth_coordinator` | Stop a run, edit a task, hand over coordination |

For a brand-new project, ask for one: Hermes calls `thoth_init(create=true)` and Thoth makes the folder
and its git repository. A task that depends on several unfinished tasks starts with all of their work
merged into its branch.

**Seeing the result.** Agents cannot open a browser inside their sandbox, so every task that made a web
page or a browser game gets an **Open preview** button on the Thoth page (and Hermes has
`thoth_preview`). Thoth serves that task's folder on `127.0.0.1`, read-only, never hidden files such
as `.env`. The **Outputs** view collects everything the team made that you can look at — images, video, sound,
web pages and documents — and plays or shows each one in place. On the canvas, shipped work folds into
the Main card once there is more than a little of it, so a long project stays readable.

For an app with a backend (say a CRM whose server also serves its pages), give the project a run command —
`thoth_init(run="python3 backend/server.py --port {port}")`, approved by you. **Run & open** then starts the
app for the version you are looking at, in a clean copy under `.thoth/previews/`, so the database or logs it
writes never end up in an agent's work or your checkout.

**Dependencies.** A task worktree is a clean checkout, and the agents' sandboxes have no network, so they
cannot reinstall packages. Thoth links ignored `node_modules`, `.venv` and `venv` folders from your main
checkout into each new worktree (agents can read them, not change them; the links are excluded from git).
For a full install per task, give the project a setup command — `thoth_init(setup="npm ci")` — which
Thoth runs in each new worktree before the agent starts, outside the sandbox and only after you approve
it. `copy_env_files: true` also copies ignored `.env` files.

**Hearing back in chat.** When an agent finishes, Thoth can tell the chat that started it, with one
message for a batch of runs and nothing for results `thoth_wait` already returned. Hermes keeps this off
until you allow it for Thoth:

```yaml
plugins:
  entries:
    thoth:
      allow_gateway_injection: true
```

Without it Hermes learns about finished runs through `thoth_wait`, and the Thoth page shows them anyway.

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
  build runs — Claude Code `--permission-mode acceptEdits` with its OS sandbox on
  (`sandbox.enabled`, `autoAllowBashIfSandboxed`, `allowUnsandboxedCommands: false`: shell commands such
  as tests run without prompts but can only write inside the worktree and temp folders; turn it off
  with `claude_sandbox: false`, after which shell commands are limited to read-only git commands plus
  your `claude_allowed_tools`), Codex `--sandbox workspace-write` (Codex's OS sandbox: writes only
  inside the worktree); review runs — no edit tools for Claude Code, `--sandbox read-only` for Codex.
  Anything a reviewer leaves in the worktree is discarded (`git reset --hard` + `git clean -fd` in that
  task's worktree only) so it can't slip into the next build commit. Thoth never passes any
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
- **Run previews**: with a project run command (approved by you), Thoth exports the version you open to
  `.thoth/previews/` and starts that command there as you, outside the agents' sandbox, on a free localhost
  port. At most 4 apps run at once; they stop when Hermes exits, and leftovers are stopped on the next start.
- **Previews**: when you (or Hermes) open a preview, a static file server listens on `127.0.0.1` on a
  free port for that task's folder, inside the Hermes process. It only answers GET/HEAD and never serves
  hidden paths. At most 8 run at once.
- **Worktree setup**: links ignored dependency folders (`node_modules`, `.venv`, `venv`) from your main
  checkout into new worktrees and adds exact-path lines for them to `.git/info/exclude`; with
  `copy_env_files` it copies ignored `.env` files; with a project setup command (approved by you) it runs
  that command in the new worktree as you, with network access.
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

## Trademarks

The Claude, Codex (OpenAI) and Hermes marks on the Thoth page only identify which tool did what. They come from the
vendors' own packages (Anthropic's Claude Code extension, OpenAI's Codex extension and Hermes Agent's assets) and
remain trademarks of their owners. Thoth is an independent community plugin, not affiliated with Anthropic or OpenAI.
