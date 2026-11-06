"""The shared project brain.

Everything the team knows lives in ``<project>/.thoth/``:

* ``brain.db``  SQLite (WAL) with the goal, the task board, runs, notes and a
  timeline of events. Hermes, the MCP server inside Claude Code / Codex, and
  background runs all read and write it concurrently.
* ``BRAIN.md``  a human- and AI-readable snapshot regenerated on every change,
  so an agent that has no Thoth tools can still catch up by reading one file.
* ``runs/``     raw JSONL event logs of every agent run.
* ``worktrees/`` one git worktree per task.

The ``.thoth/`` folder is added to ``.git/info/exclude`` so it never shows up as
an untracked change and is never committed by accident.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

BRAIN_DIRNAME = ".thoth"

TASK_STATUSES = (
    "todo",  # on the board, nobody started it
    "running",  # an agent is building it
    "built",  # the build run finished and its work is committed on the task branch
    "reviewing",  # another agent is reviewing it
    "approved",  # review passed
    "changes",  # review asked for changes
    "merged",  # merged into the base branch
    "failed",  # the last build run failed
    "conflict",  # merge hit conflicts and was aborted
    "cancelled",
)
OPEN_RUN_STATUSES = ("queued", "running")
NOTE_KINDS = ("decision", "context", "handoff", "progress", "question", "review", "risk", "feedback")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    depends_on TEXT NOT NULL DEFAULT '[]',
    branch TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    base_ref TEXT NOT NULL DEFAULT '',
    base_commit TEXT NOT NULL DEFAULT '',
    head_commit TEXT NOT NULL DEFAULT '',
    merge_commit TEXT NOT NULL DEFAULT '',
    last_run_id INTEGER,
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'build',
    status TEXT NOT NULL DEFAULT 'queued',
    started_by TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    progress TEXT NOT NULL DEFAULT '',
    activity INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    report TEXT NOT NULL DEFAULT '{}',
    exit_code INTEGER,
    error TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    owner_pid INTEGER,
    log_path TEXT NOT NULL DEFAULT '',
    usage TEXT NOT NULL DEFAULT '{}',
    commit_before TEXT NOT NULL DEFAULT '',
    commit_after TEXT NOT NULL DEFAULT '',
    diffstat TEXT NOT NULL DEFAULT '',
    started_at REAL,
    ended_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    author TEXT NOT NULL,
    task_id INTEGER,
    run_id INTEGER,
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    task_id INTEGER,
    run_id INTEGER,
    message TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_notes_task ON notes(task_id);
"""


TASK_KINDS = ("code", "asset", "doc", "research", "design", "qa")

# Columns added after the first release; created on open for older brains.
_MIGRATIONS = {
    "tasks": [("kind", "TEXT NOT NULL DEFAULT 'code'"), ("role", "TEXT NOT NULL DEFAULT ''"),
              ("model", "TEXT NOT NULL DEFAULT ''"), ("effort", "TEXT NOT NULL DEFAULT ''")],
    "runs": [("files", "TEXT NOT NULL DEFAULT '{}'"), ("delegations", "TEXT NOT NULL DEFAULT '[]'"),
             ("model", "TEXT NOT NULL DEFAULT ''"), ("effort", "TEXT NOT NULL DEFAULT ''")],
}


class BrainError(Exception):
    """A user-facing error: bad id, wrong state, missing brain."""


# Hosts (the Hermes plugin) register callbacks here to hear about every change,
# e.g. to push a live update to Hermes Desktop. Called with (project_root, event_type).
CHANGE_LISTENERS: list = []


def _notify_change(root: Path, type_: str) -> None:
    for listener in list(CHANGE_LISTENERS):
        try:
            listener(str(root), type_)
        except Exception:
            pass


def tid(n: int | None) -> str:
    return f"T{n}" if n else ""


def rid(n: int | None) -> str:
    return f"R{n}" if n else ""


def parse_id(value: Any, prefix: str) -> int:
    """Accept 3, "3", "T3" or "t3"."""
    text = str(value or "").strip().upper()
    if text.startswith(prefix.upper()):
        text = text[len(prefix) :]
    if not text.isdigit():
        raise BrainError(f"'{value}' is not a valid {prefix} id (expected like {prefix}1)")
    return int(text)


def find_brain_root(start: str | os.PathLike) -> Path | None:
    """Walk up from ``start`` to the folder that holds ``.thoth/brain.db``."""
    path = Path(start).expanduser().resolve()
    for candidate in (path, *path.parents):
        if (candidate / BRAIN_DIRNAME / "brain.db").is_file():
            return candidate
    return None


class Brain:
    def __init__(self, project_dir: str | os.PathLike):
        self.root = Path(project_dir).expanduser().resolve()
        self.dir = self.root / BRAIN_DIRNAME
        self.db_path = self.dir / "brain.db"
        self.runs_dir = self.dir / "runs"
        self.worktrees_dir = self.dir / "worktrees"

    # -- lifecycle ---------------------------------------------------------------

    @classmethod
    def create(cls, project_dir: str | os.PathLike, *, goal: str, base_branch: str, actor: str) -> "Brain":
        brain = cls(project_dir)
        brain.dir.mkdir(parents=True, exist_ok=True)
        brain.runs_dir.mkdir(exist_ok=True)
        brain.worktrees_dir.mkdir(exist_ok=True)
        brain._ensure_schema()
        with brain._tx() as db:
            now = time.time()
            for key, value in (
                ("goal", goal),
                ("base_branch", base_branch),
                ("coordinator", "hermes"),
                ("created_at", str(now)),
                ("schema", "1"),
            ):
                db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, value))
            if goal:
                db.execute("UPDATE meta SET value=? WHERE key='goal'", (goal,))
        brain.event(actor, "init", f"Thoth brain created (base branch {base_branch})")
        return brain

    @classmethod
    def open(cls, project_dir: str | os.PathLike) -> "Brain":
        root = find_brain_root(project_dir)
        if root is None:
            raise BrainError(
                f"No Thoth brain found at or above {project_dir}. Run thoth_init in the project first."
            )
        brain = cls(root)
        brain._ensure_schema()
        return brain

    def _ensure_schema(self) -> None:
        # executescript() manages its own transaction, so it must not run inside _tx().
        with self._conn() as db:
            db.executescript(_SCHEMA)
            for table, columns in _MIGRATIONS.items():
                have = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, decl in columns:
                    if name not in have:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            yield db
        finally:
            db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    # -- meta --------------------------------------------------------------------

    def meta(self) -> dict[str, str]:
        with self._conn() as db:
            return {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM meta")}

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- tasks -------------------------------------------------------------------

    def add_task(self, *, title: str, detail: str, agent: str, depends_on: list[int], actor: str, kind: str = "code", role: str = "", model: str = "", effort: str = "") -> dict:
        now = time.time()
        with self._tx() as db:
            for dep in depends_on:
                if db.execute("SELECT 1 FROM tasks WHERE id=?", (dep,)).fetchone() is None:
                    raise BrainError(f"Dependency {tid(dep)} does not exist")
            cur = db.execute(
                "INSERT INTO tasks(title, detail, agent, depends_on, created_by, created_at, updated_at, kind, role, model, effort)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, detail, agent, json.dumps(depends_on), actor, now, now, kind if kind in TASK_KINDS else "code", role, model, effort),
            )
            task_id = int(cur.lastrowid)
        self.event(actor, "task_added", f"{tid(task_id)} {title} -> {agent or 'unassigned'}", task_id=task_id)
        return self.task(task_id)

    def task(self, task_id: int) -> dict:
        with self._conn() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise BrainError(f"Task {tid(task_id)} does not exist")
        return _task_dict(row)

    def tasks(self) -> list[dict]:
        with self._conn() as db:
            return [_task_dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY id")]

    def update_task(self, task_id: int, **fields: Any) -> dict:
        if not fields:
            return self.task(task_id)
        if "depends_on" in fields:
            fields["depends_on"] = json.dumps(fields["depends_on"])
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as db:
            db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
        return self.task(task_id)

    # -- runs --------------------------------------------------------------------

    def add_run(self, *, task_id: int, agent: str, kind: str, prompt: str, actor: str) -> dict:
        now = time.time()
        with self._tx() as db:
            busy = db.execute(
                "SELECT id FROM runs WHERE task_id=? AND status IN ('queued','running')", (task_id,)
            ).fetchone()
            if busy is not None:
                raise BrainError(f"{tid(task_id)} already has an open run ({rid(busy['id'])}); wait for it or cancel it")
            cur = db.execute(
                "INSERT INTO runs(task_id, agent, kind, prompt, started_by, owner_pid, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, agent, kind, prompt, actor, os.getpid(), now),
            )
            run_id = int(cur.lastrowid)
            db.execute("UPDATE tasks SET last_run_id=?, updated_at=? WHERE id=?", (run_id, now, task_id))
        return self.run(run_id)

    def run(self, run_id: int) -> dict:
        with self._conn() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise BrainError(f"Run {rid(run_id)} does not exist")
        return _run_dict(row)

    def runs(self, *, task_id: int | None = None, open_only: bool = False, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM runs"
        clauses, params = [], []
        if task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
        if open_only:
            clauses.append("status IN ('queued','running')")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as db:
            return [_run_dict(r) for r in db.execute(sql, params)]

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        for key in ("report", "usage", "files", "delegations"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as db:
            db.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def last_session(self, task_id: int, agent: str) -> str:
        """The agent CLI session id of the latest build run of ``agent`` on this task (for resume)."""
        with self._conn() as db:
            row = db.execute(
                "SELECT session_id FROM runs WHERE task_id=? AND agent=? AND kind='build' AND session_id!=''"
                " ORDER BY id DESC LIMIT 1",
                (task_id, agent),
            ).fetchone()
        return row["session_id"] if row else ""

    # -- notes & events ----------------------------------------------------------

    def add_note(self, *, kind: str, author: str, body: str, task_id: int | None = None, run_id: int | None = None) -> dict:
        if kind not in NOTE_KINDS:
            raise BrainError(f"Note kind must be one of {', '.join(NOTE_KINDS)}")
        body = body.strip()
        if not body:
            raise BrainError("Note body is empty")
        now = time.time()
        with self._tx() as db:
            if task_id is not None and db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
                raise BrainError(f"Task {tid(task_id)} does not exist")
            cur = db.execute(
                "INSERT INTO notes(kind, author, task_id, run_id, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, author, task_id, run_id, body, now),
            )
            note_id = int(cur.lastrowid)
        self.event(author, f"note_{kind}", body[:160], task_id=task_id, run_id=run_id)
        return {"id": note_id, "kind": kind, "author": author, "task_id": task_id, "body": body, "created_at": now}

    def notes(self, *, task_id: int | None = None, kinds: tuple[str, ...] = (), limit: int = 30, project_wide: bool = False) -> list[dict]:
        sql = "SELECT * FROM notes"
        clauses, params = [], []
        if project_wide:
            clauses.append("task_id IS NULL")
        elif task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
        if kinds:
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as db:
            rows = [dict(r) for r in db.execute(sql, params)]
        rows.reverse()
        return rows

    def event(self, actor: str, type_: str, message: str = "", *, task_id: int | None = None, run_id: int | None = None, commit: str = "") -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO events(ts, actor, type, task_id, run_id, message, commit_sha) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), actor, type_, task_id, run_id, message, commit),
            )
        try:
            self.write_snapshot()
        except Exception:
            pass
        _notify_change(self.root, type_)

    def events(self, *, limit: int = 40, since_id: int = 0) -> list[dict]:
        with self._conn() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?", (since_id, limit)
                )
            ]
        rows.reverse()
        return rows

    # -- snapshot ----------------------------------------------------------------

    def write_snapshot(self) -> Path:
        from .briefing import render_brain_md  # local import: briefing imports brain

        path = self.dir / "BRAIN.md"
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(render_brain_md(self), encoding="utf-8")
        os.replace(tmp, path)
        return path


def _task_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        data["depends_on"] = json.loads(data.get("depends_on") or "[]")
    except ValueError:
        data["depends_on"] = []
    data["ref"] = tid(data["id"])
    return data


def _run_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key, empty in (("report", "{}"), ("usage", "{}"), ("files", "{}"), ("delegations", "[]")):
        try:
            data[key] = json.loads(data.get(key) or empty)
        except ValueError:
            data[key] = json.loads(empty)
    data["ref"] = rid(data["id"])
    data["task_ref"] = tid(data["task_id"])
    return data
