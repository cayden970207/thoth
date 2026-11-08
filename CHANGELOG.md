# What's new in Thoth

Every version of Thoth, newest first, in plain words. Hermes tells you when an update is ready
(Settings → Plugins). Once it is installed, the Thoth page shows what's new, once.

<!-- One section per version: "## X.Y.Z — YYYY-MM-DD", then "- " lines written for the person using Thoth.
     The page shows the first three, so lead with what they will notice. See docs/releasing.md. -->

## 0.9.1 — 2026-11-08

- A project's setup and run commands run only once you have approved them on this computer. A project that
  already has one asks you once more; Hermes shows you the command first.
- Thoth uses a project's board only if it made it on this computer, or you said yes to it once. A board that came
  with a folder (a clone, a download or a copy) waits for that, and nothing in it starts on its own. Your projects
  from earlier versions carry on as before.
- Starting an app for a preview now asks you first, and the card shows the command that will run.
- Agents and project commands now get only the basics from your environment (where programs are, your home
  folder, your language), so other keys and tokens never reach them. Add any others they need under
  **Environment variables to pass to agents** in Settings → Plugins → Thoth.
- Thoth won't use a project whose `.thoth` folder is committed to git, and its own git commands (commits and
  merges of the agents' work) no longer run git hooks.

## 0.9.0 — 2026-11-08

- **New project** in the project menu: say what to build, and Hermes sets it up in a new chat.
- New projects get their own folder in `~/Thoth`. To keep them elsewhere, change **Folder for new
  projects** in Settings → Plugins → Thoth.
- A folder you already have can become a project: Thoth keeps every version of it and leaves secrets and
  very large files out.
- Agents now see the files your project keeps out of git, such as test builds, game images and big videos.
  Each task gets its own copy (never your secrets), and what an agent makes there arrives in your project
  folder when the task is merged. Your own files are never overwritten.
- After an update, Thoth shows what's new once. To read it again, click the version beside **Thoth** at the
  top of the page, or open the project menu.

## 0.8.0 — 2026-11-07

- **Autopilot**: approve a plan once and Thoth runs it to the end. It starts each task when what it builds on
  is ready, has the other agent review it, sends changes back and merges what passes. It writes in the chat
  only when you need to decide.
- **Your result**: once work is merged, the side window shows it with **Open**, **Download** (a zip in your
  Downloads folder) and its folder.
- An approval holds only for the plan you saw. If the plan changes before you answer, nothing starts and
  Hermes asks again.
- Autopilot keeps your Mac awake while it works, pauses when the plan changes and carries on after a restart.

## 0.7.1 — 2026-11-07

- The side window shows how much of your Claude and ChatGPT plans is used, next to the team: amber when one
  runs low, red with the time it's back when it runs out.

## 0.7.0 — 2026-11-07

- **Plan relay**: when your Claude or ChatGPT plan runs out, the work so far stays safe and the other agent
  can carry on from it.
- The Team card shows how much of each plan is used and when it resets.
- Shorter, clearer briefs for the agents, so runs lose less time.

## 0.6.2 — 2026-11-07

- An agent that did a task but couldn't check all of it now says so: the card shows what wasn't checked,
  and the reviewer sees it too.
- **Blocked** now means the agent couldn't do the work at all, and Needs you shows its reason or question
  in full.

## 0.6.1 — 2026-11-07

- A calmer ring around an agent while it works: the ring stays still and a soft light runs round it. With
  reduced motion it simply stays still.

## 0.6.0 — 2026-11-07

- **Thoth beside the chat**: a side window shows the project's work next to its Hermes chat. Pick the
  **Chat + Thoth** layout to set it up.
- A **Team & brain** tab: what needs you, who is doing what, and the shared brain of decisions, reviews and
  versions.
- Every Thoth project is also a Hermes project with a chat of its own, and the side window follows the chat
  you're in.
- The project menu shows each project's progress and what waits on you. **Remove from Thoth** takes a
  project off the list without touching its folder.

## 0.5.0 — 2026-11-06

- A task that needs the web can get a browser: a clean Chrome without your logins, or your own Chrome when
  it needs them (Chrome asks you to allow it).
- A new look: a calm page with floating cards, the agents as app icons, and what each task made peeking out
  from behind its card.

## 0.4.0 — 2026-11-06

- Calmer cards that show the work itself (the first image a task made) or the agent's own mark.
- Folding finished work into **Main**, unfolding it and **Fit & tidy** glide every card to its new place.
- Thoth no longer copies `.env` files into task folders, and never reads them.

## 0.1.0 — 2026-11-06

- The first Thoth: Hermes plans the work on a shared board and hands each task to the real Claude Code or
  Codex, each in its own copy of the project. They review each other's work, and Hermes merges what passes
  once you say yes.
- The Thoth page: the team's work on a canvas and a board, what needs you, and everything the team made in
  **Outputs** (images, video, sound, pages and documents).
- Send work back with your feedback, bring back any earlier version, and pick the model for each task.
- **Open preview** shows a page or game a task built, and approval cards say in plain words what will
  happen.
