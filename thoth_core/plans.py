"""How much of your Claude and Codex plans is used, as the CLIs themselves report it.

The team uses it to hand work to the other agent when one plan runs out, Hermes sees it every turn,
and the Thoth page draws it.

- Claude Code puts a ``rate_limit_event`` in every run's stream: the window that matters most
  (5-hour or weekly), how much of it is used and when it resets.
- Codex answers ``account/rateLimits/read`` on its app server, a read that uses no tokens.

Thoth keeps the latest of each in its data folder (``plans.json``). It never reads credentials:
each CLI asks its own service with its own login.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

FILE = "plans.json"
LOW = 0.85  # from here a plan is "running low"
NAMES = {"claude": "Claude Code", "codex": "Codex"}
_LOCK = threading.Lock()
# Claude's window ids -> (minutes, label)
_CLAUDE = {
    "five_hour": (300, "5-hour"),
    "seven_day": (10080, "Weekly"),
    "seven_day_opus": (10080, "Weekly · Opus"),
    "seven_day_sonnet": (10080, "Weekly · Sonnet"),
}
# What an agent says when its plan has run out ("Claude AI usage limit reached|1794484800",
# "You've hit your usage limit. ... try again in 2 days", "5-hour limit reached · resets 3pm").
_LIMIT_TEXT = re.compile(r"usage limit|hit your (?:\w+[ -])?limit|limit reached|out of (?:credits|usage)", re.I)
_EPOCH = re.compile(r"\|(\d{10})\b")


def default_data_dir() -> Path:
    if os.environ.get("THOTH_DATA_DIR"):  # tests and custom setups
        return Path(os.environ["THOTH_DATA_DIR"])
    try:
        from plugins.plugin_storage import plugin_data_dir

        return Path(plugin_data_dir("thoth"))
    except Exception:  # outside Hermes (an MCP server started without --data-dir): where Hermes keeps it
        home = os.environ.get("HERMES_HOME")
        return (Path(home).expanduser() if home else Path.home() / ".hermes") / "plugin-data" / "thoth"


def _int(value: Any) -> int | None:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _ratio(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, v / 100 if v > 2 else v)  # Claude says 0.14, Codex 14


def label(minutes: int | None, name: str = "") -> str:
    if name in _CLAUDE:
        return _CLAUDE[name][1]
    if minutes == 300:
        return "5-hour"
    if minutes == 10080:
        return "Weekly"
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}-day"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return name.replace("_", " ").capitalize() or "Plan"


def when(ts: float | None) -> str:
    """A reset time people read at a glance, in local time: "Thu 12 Nov 20:00"."""
    return time.strftime("%a %d %b %H:%M", time.localtime(ts)) if ts else "later"


# -- reading what the CLIs report -------------------------------------------------------------------


def from_claude(info: dict, now: float | None = None) -> dict:
    """A snapshot from Claude Code's ``rate_limit_info``."""
    windows = []
    sources = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
    if not sources and info.get("rateLimitType"):
        sources = {str(info["rateLimitType"]): {"utilization": info.get("utilization"), "resetsAt": info.get("resetsAt"), "status": info.get("status")}}
    for name, w in sources.items():
        if not isinstance(w, dict):
            continue
        minutes = _CLAUDE.get(name, (None, ""))[0]
        windows.append({"id": name, "label": label(minutes, name), "minutes": minutes, "used": _ratio(w.get("utilization")),
                        "resets_at": _int(w.get("resetsAt")), "limited": w.get("status") == "rejected"})
    on_overage = bool(info.get("isUsingOverage"))  # extra usage the user pays for keeps runs going
    limited = (info.get("status") == "rejected" or any(w["limited"] for w in windows)) and not on_overage
    until = None
    if limited:
        until = _int(info.get("resetsAt")) or max((w["resets_at"] or 0 for w in windows if w["limited"]), default=0) or None
    return {"agent": "claude", "plan": "", "windows": windows, "limited_until": until, "seen_at": now or time.time(), "source": "run"}


def from_codex(result: dict, now: float | None = None) -> dict:
    """A snapshot from Codex's ``account/rateLimits/read`` result."""
    rl = result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else {}
    windows = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if isinstance(w, dict) and isinstance(w.get("usedPercent"), (int, float)):
            minutes = _int(w.get("windowDurationMins"))
            used = float(w["usedPercent"]) / 100
            windows.append({"id": key, "label": label(minutes), "minutes": minutes, "used": max(0.0, used),
                            "resets_at": _int(w.get("resetsAt")), "limited": used >= 1})
    reached = bool(rl.get("rateLimitReachedType")) or result.get("ordinaryUsageAllowed") is False
    limited = reached or any(w["limited"] for w in windows)
    until = None
    if limited:
        full = [w["resets_at"] for w in windows if w["limited"] and w["resets_at"]]
        until = max(full) if full else min((w["resets_at"] for w in windows if w["resets_at"]), default=None)
    return {"agent": "codex", "plan": str(rl.get("planType") or ""), "windows": windows, "limited_until": until,
            "seen_at": now or time.time(), "source": "codex"}


def limit_in(text: str) -> tuple[bool, int | None]:
    """Whether an agent's error says its plan ran out, and the reset time when the text carries one."""
    text = str(text or "")
    if not _LIMIT_TEXT.search(text):
        return False, None
    m = _EPOCH.search(text)
    return True, int(m.group(1)) if m else None


def read_codex(exe: str, env: dict | None = None, timeout: float = 15.0, version: str = "") -> dict | None:
    """Ask Codex's app server for this login's rate limits (a read that uses no tokens). None when it can't say."""
    try:
        proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, env=env, start_new_session=True)
    except OSError:
        return None
    try:
        messages = [
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "thoth", "title": "Thoth", "version": version or "0"}}},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}},
        ]
        assert proc.stdin is not None and proc.stdout is not None
        lines: queue.Queue = queue.Queue()
        out = proc.stdout

        def pump() -> None:  # a thread, so a reply already in the pipe's buffer is never missed
            for line in out:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, name="thoth-codex-limits", daemon=True).start()
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in messages))
        proc.stdin.flush()
        deadline = time.time() + timeout
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == 2:
                return msg["result"] if isinstance(msg.get("result"), dict) else None
    except (OSError, ValueError, AssertionError):
        return None
    finally:
        for step in (lambda: proc.stdin and proc.stdin.close(), proc.terminate, lambda: proc.wait(timeout=3)):
            try:
                step()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


# -- keeping them ---------------------------------------------------------------------------------------


def load(data_dir: str | os.PathLike) -> dict[str, dict]:
    try:
        data = json.loads((Path(data_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data_dir: str | os.PathLike, agent: str, snap: dict) -> None:
    """Keep the newest snapshot per agent. A limit hit that came without numbers keeps the last numbers."""
    path = Path(data_dir) / FILE
    with _LOCK:
        data = load(data_dir)
        prev = data.get(agent) if isinstance(data.get(agent), dict) else {}
        merged = dict(snap)
        if not merged.get("windows") and prev.get("windows"):
            merged["windows"] = prev["windows"]
        if not merged.get("plan") and prev.get("plan"):
            merged["plan"] = prev["plan"]
        data[agent] = merged
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)


def mark_limited(data_dir: str | os.PathLike, agent: str, until: int | None, now: float | None = None) -> None:
    """A run stopped at the plan limit: remember it until the reset (or an hour, when nobody said when)."""
    now = now or time.time()
    snap = dict(load(data_dir).get(agent) or {"agent": agent, "windows": []})
    snap.update(limited_until=until or int(now + 3600), seen_at=now, source="run")
    save(data_dir, agent, snap)


# -- reading them back ------------------------------------------------------------------------------------


def view(data: dict, now: float | None = None) -> dict[str, dict]:
    """Each agent's plan as of now: a window whose reset has passed counts as fresh, and how much of
    each window's time has gone by (to compare with how much of it is used)."""
    now = now or time.time()
    out: dict[str, dict] = {}
    for agent, snap in data.items():
        if agent not in NAMES or not isinstance(snap, dict):
            continue
        windows = []
        for w in snap.get("windows") or []:
            reset, minutes = w.get("resets_at"), w.get("minutes")
            rolled = bool(reset and reset <= now)
            elapsed = None
            if reset and minutes and not rolled:
                elapsed = round(min(1.0, max(0.0, 1 - (reset - now) / (minutes * 60))), 3)
            windows.append({"id": w.get("id"), "label": w.get("label") or "Plan", "minutes": minutes,
                            "used": 0.0 if rolled else round(min(1.0, max(0.0, float(w.get("used") or 0))), 3),
                            "resets_at": None if rolled else reset, "rolled": rolled, "elapsed": elapsed})
        until = snap.get("limited_until")
        limited = bool(until and until > now)
        top = max((w["used"] for w in windows), default=0.0)
        out[agent] = {"agent": agent, "plan": snap.get("plan") or "", "windows": windows,
                      "limited_until": until if limited else None,
                      "level": "limited" if limited else "low" if top >= LOW else "ok",
                      "seen_at": snap.get("seen_at"), "source": snap.get("source") or ""}
    return out


def limited_until(data: dict, agent: str, now: float | None = None) -> int | None:
    return (view(data, now).get(agent) or {}).get("limited_until")


def hint(views: dict[str, dict]) -> str:
    """One line for the coordinator: how much of each plan is left, and who should take new work."""
    parts, advice = [], []
    out = {a for a, v in views.items() if v.get("limited_until")}
    low = {a for a, v in views.items() if v.get("level") == "low"}
    for agent in ("claude", "codex"):
        v = views.get(agent)
        if not v:
            continue
        other = "codex" if agent == "claude" else "claude"
        if v["limited_until"]:
            parts.append(f"{NAMES[agent]} has hit its plan limit until {when(v['limited_until'])}")
            if other not in out:
                advice.append(f"give {NAMES[agent]}'s tasks and reviews to {NAMES[other]}")
        elif v["windows"]:
            w = max(v["windows"], key=lambda x: x["used"])
            parts.append(f"{NAMES[agent]} has used {round(w['used'] * 100)}% of its {w['label'].lower()} limit"
                         + (f" (resets {when(w['resets_at'])})" if w["resets_at"] else ""))
            if agent in low and other not in out and other not in low:  # only steer work to an agent with room
                advice.append(f"give big new tasks to {NAMES[other]} rather than {NAMES[agent]}")
    if not parts:
        return ""
    both = all((views.get(a) or {}).get("limited_until") for a in NAMES)
    tail = " Both plans are out: tell the user when work can go on." if both else (" So " + "; ".join(advice) + "." if advice else "")
    return "Plan usage: " + "; ".join(parts) + "." + tail
