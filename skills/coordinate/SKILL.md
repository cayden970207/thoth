---
name: coordinate
description: Playbook for coordinating Claude Code and Codex on one project through the Thoth shared brain — planning, parallel assignment, cross-review, fixes and merging.
---

# Coordinating a team with Thoth

You are the coordinator. Claude Code and Codex are your workers. Thoth gives all of you one
shared brain: a task board, decisions, handoffs, run history and a git branch per task.

## The loop

1. **Start the brain.** `thoth_init(goal=...)` in the project (a git repo). Reopening is safe.
2. **Record ground rules first.** Anything every agent must follow (stack, style, API shape,
   file ownership) goes in with `thoth_note(kind="decision")`. Every brief includes decisions.
3. **Plan.** `thoth_plan(tasks=[...])` with small tasks that touch different files, so they can
   run in parallel. Use `depends_on` only for real dependencies. Suggested split:
   - **claude**: UI and UX, product copy, refactors, documentation, wide-context changes.
   - **codex**: algorithms, tests, scripts, data handling, precise edits.
4. **Run in parallel.** Call `thoth_assign` for each independent task, then `thoth_wait` once
   for all of them. Each agent works in its own worktree; they cannot overwrite each other.
5. **Cross-review.** `thoth_review(task)` puts the *other* agent on a read-only review.
   - `approved` → `thoth_merge(task)`.
   - `changes` → `thoth_assign(task)` again. The same agent resumes its session and the review
     comments are already in its brief. Then review again.
6. **Merge in dependency order.** Merging a task built on another also merges that one.
7. **Report** to the user from `thoth_status`: what merged, what is open, decisions made.

## Rules

- Never assign the same task twice at once; check `thoth_status` when unsure.
- Keep tasks reviewable: one concern each, clear acceptance criteria in `detail`.
- If a run fails, read its error in `thoth_status(task=...)` before retrying. Re-assigning
  resumes the agent's session with the error context.
- If a merge conflicts, the base branch is untouched. Add a task (often for the agent that
  built the later branch) to rebase or reconcile, then merge again.
- To hand over coordination, call `thoth_coordinator(agent="claude" | "codex")` and pass the
  setup command it returns to the user.
