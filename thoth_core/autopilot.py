"""Autopilot: the user approves a plan once and Thoth runs it to the end.

After one approval of a plan card, Thoth starts each task once what it builds on is merged, has the other agent
review it, sends changes back (up to the review-round limit), retries a failed run once, gives a task to the other
agent when a plan runs out, and merges what passes review. It stops and asks, through the chat that started it,
when an agent is stuck, a review keeps asking for changes, a merge conflicts, the plan changes, or the time limit
is reached. It never uses more of a plan than the user keeps back for themselves.

An approval covers the plan as the card showed it. Any change to the goal or the tasks (added, removed, what they
say, who does them, what they build on) pauses autopilot until the user approves the new plan. What Thoth does
under the approved rules (giving a task to the other agent) is written into the approved plan as it happens.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from . import plans
from .brain import BrainError, tid

if TYPE_CHECKING:
    from .brain import Brain

KEY = "autopilot"
FIELDS = ("title", "detail", "agent", "kind", "role", "browser", "model", "effort")
NAMES = {"claude": "Claude Code", "codex": "Codex", "hermes": "Hermes"}
_WORDS = {
    "title": "{ref} was renamed",
    "detail": "{ref}'s instructions changed",
    "agent": "{ref} went to another agent",
    "kind": "{ref}'s kind changed",
    "role": "{ref}'s role changed",
    "browser": "{ref}'s web access changed",
    "model": "{ref}'s model changed",
    "effort": "{ref}'s effort changed",
    "after": "what {ref} builds on changed",
}


def _other(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


# -- the plan an approval covers ----------------------------------------------------------------------


def snapshot(brain: "Brain") -> dict:
    """The plan as a person reads it: the goal, and each task's words, agent and what it builds on."""
    tasks = {}
    for t in brain.tasks():
        if t["status"] == "cancelled":
            continue
        tasks[t["ref"]] = {**{k: t.get(k) or "" for k in FIELDS}, "after": [tid(d) for d in t["depends_on"]]}
    return {"goal": brain.meta().get("goal", ""), "tasks": tasks}


def fingerprint(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def changes(before: dict, now: dict) -> list[str]:
    """What moved between two plans, in plain words; empty when nothing did."""
    out = []
    if before.get("goal") != now.get("goal"):
        out.append("the goal changed")
    old, new = before.get("tasks") or {}, now.get("tasks") or {}
    out += [f"{ref} “{new[ref]['title']}” was added" for ref in new if ref not in old]
    out += [f"{ref} “{old[ref]['title']}” was dropped" for ref in old if ref not in new]
    for ref in (r for r in old if r in new):
        out += [words.format(ref=ref) for key, words in _WORDS.items() if old[ref].get(key) != new[ref].get(key)]
    return out


# -- state ------------------------------------------------------------------------------------------------


def load(brain: "Brain") -> dict:
    try:
        data = json.loads(brain.meta().get(KEY) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save(brain: "Brain", ap: dict) -> None:
    brain.set_meta(KEY, json.dumps(ap, ensure_ascii=False))


def _log(ap: dict, text: str, now: float) -> None:
    ap.setdefault("log", []).append({"at": now, "text": text})
    del ap["log"][:-30]


def _say(ap: dict, key: str, text: str, out: list[str]) -> None:
    """A message for the coordinator, once per thing worth saying."""
    said = ap.setdefault("said", [])
    if key not in said:
        said.append(key)
        del said[:-80]
        out.append(text)


def start(brain: "Brain", *, hours: float, reserve: float, session: str = "", now: float | None = None) -> dict:
    now = now or time.time()
    plan = snapshot(brain)
    scope = [ref for ref, t in plan["tasks"].items() if brain.task(int(ref[1:]))["status"] != "merged"]
    if not scope:
        raise BrainError("Everything on the plan is already merged: there is nothing for autopilot to run.")
    ap = {"state": "running", "plan": plan, "scope": scope, "started_at": now, "until": now + hours * 3600, "hours": hours,
          "reserve": reserve, "session": session, "reason": "", "changes": [], "held": {}, "retried": {}, "waiting": "",
          "said": [], "log": []}
    _log(ap, f"Started on {len(scope)} task{'s' if len(scope) != 1 else ''} for up to {hours:g} hours", now)
    save(brain, ap)
    return ap


def pause(brain: "Brain", reason: str, now: float | None = None) -> dict:
    ap = load(brain)
    if ap.get("state") == "running":
        ap.update(state="paused", reason=reason, changes=[], waiting="")
        _log(ap, f"Paused: {reason}", now or time.time())
        save(brain, ap)
    return ap


def stop(brain: "Brain", now: float | None = None) -> dict:
    ap = load(brain)
    if ap:
        ap.update(state="off", reason="stopped by you", waiting="")
        _log(ap, "Stopped", now or time.time())
        save(brain, ap)
    return ap


# -- one step ---------------------------------------------------------------------------------------------


def _unavailable(svc: Any, reserve: float) -> dict[str, str]:
    """Agents autopilot must not start now, and why: not runnable here, out of plan, or into what the user keeps."""
    out = {}
    workers = svc.workers()
    for agent in ("claude", "codex"):
        if not workers.get(agent, {}).get("ready"):
            out[agent] = "can't run on this machine"
    for agent, v in svc.plan_usage(refresh=False).items():
        if agent in out:
            continue
        if v.get("limited_until"):
            out[agent] = f"its plan is out until {plans.when(v['limited_until'])}"
        else:
            w = max(v.get("windows") or [{"used": 0}], key=lambda x: x["used"])
            if reserve and w["used"] >= 1 - reserve:
                back = f", back after {plans.when(w['resets_at'])}" if w.get("resets_at") else ""
                out[agent] = f"it has used {round(w['used'] * 100)}% of its {w.get('label', 'plan').lower()} limit and you keep {round(reserve * 100)}%{back}"
    return out


def _pick(preferred: str, unavailable: dict[str, str]) -> str:
    """The agent to use: the preferred one, else the other one, else none."""
    if preferred in ("claude", "codex") and preferred not in unavailable:
        return preferred
    other = _other(preferred if preferred in ("claude", "codex") else "codex")
    return other if other not in unavailable else ""


def step(svc: Any, brain: "Brain", now: float | None = None) -> list[str]:
    """Move the plan forward by what can be done now. Returns messages for the coordinator (things only a person
    can decide, or news worth a turn); routine progress stays on the board."""
    now = now or time.time()
    ap = load(brain)
    if ap.get("state") != "running":
        return []
    project, out, was = str(brain.root), [], json.dumps(ap, sort_keys=True)
    moved = changes(ap["plan"], snapshot(brain))
    if moved:
        ap.update(state="paused", reason="the plan changed", changes=moved, waiting="")
        _log(ap, "Paused: the plan changed (" + "; ".join(moved) + ")", now)
        save(brain, ap)
        return [f"Autopilot paused: the plan changed ({'; '.join(moved)}). Nothing new starts until the user approves the plan as it is now: "
                "tell them what changed and call thoth_autopilot with action start to show the updated plan."]
    tasks = {t["ref"]: t for t in brain.tasks()}
    scope = [tasks[ref] for ref in ap["scope"] if ref in tasks]
    left = [t for t in scope if t["status"] not in ("merged", "cancelled")]
    if not left:
        ap.update(state="done", reason="", waiting="", ended_at=now)
        _log(ap, "Done: everything is merged", now)
        save(brain, ap)
        base = brain.meta().get("base_branch") or "main"
        return [f"Autopilot finished: {'its task is' if len(scope) == 1 else f'all {len(scope)} tasks are'} merged into {base}. "
                "Tell the user their result is ready, in a few lines (thoth_preview gives them a link)."]
    if now >= float(ap.get("until") or now + 1):
        done = len(scope) - len(left)
        ap.update(state="stopped", reason=f"it reached its {ap.get('hours', 8):g}-hour limit", waiting="")
        _log(ap, "Stopped at its time limit", now)
        save(brain, ap)
        return [f"Autopilot stopped at its {ap.get('hours', 8):g}-hour limit: {done} of {len(scope)} merged; "
                f"{', '.join(t['ref'] for t in left)} still open. Ask the user whether to run the rest (thoth_autopilot start)."]
    room = max(0, int(svc.settings.get("max_concurrent_runs") or 3) - len(brain.runs(open_only=True)))
    unavailable = _unavailable(svc, float(ap.get("reserve") or 0))
    rounds_limit = int(svc.settings.get("max_review_rounds") or 3)
    held, waiting = ap.setdefault("held", {}), ""

    def last_run(t: dict, kind: str = "") -> dict:
        runs = brain.runs(task_id=t["id"], limit=20)
        return next((r for r in runs if not kind or r["kind"] == kind), {}) if runs else {}

    # A hold lasts while the task stays as it was: once someone acts on it, autopilot carries on with it.
    for ref in list(held):
        t = tasks.get(ref)
        if t is None or t["status"] != held[ref]["status"] or (last_run(t) or {}).get("id") != held[ref]["run"]:
            del held[ref]

    def use(t: dict, agent: str, why: str = "") -> None:
        """Autopilot gives this task to `agent`: written into the approved plan, so it is not taken for a change."""
        if agent != (t["agent"] or ""):
            ap["plan"]["tasks"][t["ref"]]["agent"] = agent
            if t["agent"] and why:
                _log(ap, f"Gave {t['ref']} to {NAMES[agent]}: {why}", now)

    def hold(t: dict, why: str, ask: str) -> None:
        held[t["ref"]] = {"why": why, "status": t["status"], "run": (last_run(t) or {}).get("id"), "at": now}
        _log(ap, f"Waiting for you on {t['ref']}: {why}", now)
        _say(ap, f"hold:{t['ref']}:{held[t['ref']]['run']}", f"Autopilot needs the user on {t['ref']} “{t['title']}”: {why}. {ask} "
             "Autopilot keeps running the other tasks.", out)

    for t in left:
        ref, st = t["ref"], t["status"]
        if ref in held or st in ("running", "reviewing"):
            continue
        try:
            if st == "approved":
                svc.merge(project, ref)
                _log(ap, f"Merged {ref}", now)
            elif st == "conflict":
                hold(t, "merging it conflicts with what is already merged", "Plan a task to reconcile it (the user approves the change).")
            elif st == "built":
                author = t["agent"] or "claude"
                reviewer = _pick(_other(author), unavailable) or ("" if author in unavailable else author)
                if not reviewer:
                    waiting = waiting or f"no agent can review {ref} now ({'; '.join(f'{NAMES[a]}: {w}' for a, w in unavailable.items())})"
                elif room:
                    svc.review(project, ref, reviewer=reviewer)
                    room -= 1
                    _log(ap, f"{NAMES[reviewer]} reviews {ref}", now)
            elif st == "changes":
                rounds = sum(1 for r in brain.runs(task_id=t["id"], limit=50) if r["kind"] == "review" and r["status"] == "done")
                if rounds >= rounds_limit:
                    hold(t, f"after {rounds} reviews the reviewer still asks for changes", "Ask the user: merge it as it is (thoth_merge with override) or send it back once more (thoth_assign).")
                elif room:
                    agent = _pick(t["agent"] or "claude", unavailable)
                    if not agent:
                        waiting = waiting or "no agent has plan left to make the changes asked on " + ref
                    else:
                        use(t, agent, f"{NAMES.get(t['agent'], 'its agent')} {unavailable.get(t['agent'], 'is unavailable')}")
                        svc.assign(project, ref, agent=agent)
                        room -= 1
                        _log(ap, f"{NAMES[agent]} makes the changes asked on {ref}", now)
            elif st == "todo":
                deps = [tasks.get(tid(d)) for d in t["depends_on"]]
                if any(d is None or d["status"] not in ("merged", "cancelled") for d in deps):
                    continue
                if (t.get("browser") or "") == "user":
                    hold(t, "it browses with the user's own Chrome, so autopilot leaves starting it to the user", "Start it when the user is there (thoth_assign).")
                    continue
                if t["agent"] == "hermes":
                    hold(t, "it is yours to do (assigned to Hermes)", "Do it now with thoth_assign and thoth_complete.")
                    continue
                agent = _pick(t["agent"] or str(svc.settings.get("default_agent") or "claude"), unavailable)
                if not agent:
                    waiting = waiting or "both plans are unavailable (" + "; ".join(f"{NAMES[a]}: {w}" for a, w in unavailable.items()) + ")"
                elif room:
                    use(t, agent, f"{NAMES.get(t['agent'], 'its agent')} {unavailable.get(t['agent'], 'is unavailable')}")
                    svc.assign(project, ref, agent=agent)
                    room -= 1
                    _log(ap, f"{NAMES[agent]} starts {ref}", now)
            elif st == "failed":
                build = last_run(t, "build")
                report = build.get("report") if isinstance(build.get("report"), dict) else {}
                if str(build.get("error") or "").startswith("plan limit"):
                    agent = _pick(_other(build.get("agent") or t["agent"] or "claude"), unavailable)
                    if not agent:
                        waiting = waiting or f"{ref} stopped at a plan limit and no other agent has plan left"
                    elif room:
                        use(t, agent, f"{NAMES.get(build.get('agent'), 'its agent')}'s plan ran out")
                        svc.assign(project, ref, agent=agent)
                        room -= 1
                elif build.get("status") == "done" and str(report.get("status") or "").lower() == "blocked":
                    hold(t, f"{NAMES.get(t['agent'], t['agent'])} is stuck: {(build.get('summary') or '')[:300]}",
                         "Ask the user, record the answer with thoth_note, then thoth_assign it again.")
                elif int(ap.setdefault("retried", {}).get(ref, 0)) < 1:
                    if room:
                        agent = _pick(t["agent"] or "claude", unavailable)
                        if agent:
                            ap["retried"][ref] = int(ap["retried"].get(ref, 0)) + 1
                            use(t, agent, f"{NAMES.get(t['agent'], 'its agent')} {unavailable.get(t['agent'], 'is unavailable')}")
                            svc.assign(project, ref, agent=agent)
                            room -= 1
                            _log(ap, f"Retried {ref} with {NAMES[agent]}", now)
                else:
                    hold(t, f"its run failed twice ({(build.get('error') or 'no reason given')[:200]})", "Look at the error with the user, then fix the task or try again (thoth_assign).")
        except BrainError as exc:
            hold(t, f"autopilot could not move it: {exc}", "Sort it out with the user, then autopilot carries on.")
    if waiting:
        _say(ap, "wait:" + waiting[:60], f"Autopilot is waiting: {waiting}. It carries on by itself when that changes.", out)
    ap["waiting"] = waiting
    if json.dumps(ap, sort_keys=True) != was:
        save(brain, ap)
    return out


def view(brain: "Brain", now: float | None = None) -> dict | None:
    """What the page shows: the state, how far the plan is, what is happening, and why it waits or paused."""
    ap = load(brain)
    if not ap or ap.get("state") in (None, "off"):
        return None
    tasks = {t["ref"]: t for t in brain.tasks()}
    scope = [tasks[r] for r in ap.get("scope") or [] if r in tasks]
    doing = [f"{NAMES.get(r['agent'], r['agent'])} {'reviews' if r['kind'] == 'review' else 'builds'} {r['task_ref']}" for r in brain.runs(open_only=True)]
    return {
        "state": ap.get("state"),
        "reason": ap.get("reason") or "",
        "changes": ap.get("changes") or [],
        "waiting": ap.get("waiting") or "",
        "until": ap.get("until"),
        "started_at": ap.get("started_at"),
        "hours": ap.get("hours"),
        "scope": len(scope),
        "merged": sum(1 for t in scope if t["status"] == "merged"),
        "doing": doing,
        "held": [{"task": ref, "why": h.get("why", "")} for ref, h in (ap.get("held") or {}).items()],
        "log": (ap.get("log") or [])[-6:],
        "now": now or time.time(),
    }


def hint(brain: "Brain") -> str:
    """One line for the coordinator's turn."""
    v = view(brain)
    if not v:
        return ""
    if v["state"] == "running":
        line = (f"Autopilot is running the approved plan: {v['merged']} of {v['scope']} merged"
                + (f", now {', '.join(v['doing'])}" if v["doing"] else "") + f", until {plans.when(v['until'])}. "
                "Thoth starts, reviews and merges its tasks: do not do those steps yourself. Answer what it asks; "
                "any change you make to the plan pauses it until the user approves the new plan.")
        if v["held"]:
            line += " Waiting for the user on: " + "; ".join(f"{h['task']} ({h['why'][:120]})" for h in v["held"]) + "."
        if v["waiting"]:
            line += f" It is waiting: {v['waiting']}."
        return line
    if v["state"] == "paused":
        return (f"Autopilot is paused: {v['reason']}" + (f" ({'; '.join(v['changes'])})" if v["changes"] else "")
                + ". To carry on, call thoth_autopilot with action start so the user approves the plan as it is now.")
    if v["state"] == "stopped":
        return f"Autopilot stopped: {v['reason']}. {v['merged']} of {v['scope']} merged; thoth_autopilot start runs the rest after the user approves."
    return ""
