"""What the user approved on this computer: the boards Thoth may use, and the project commands it may run.

A project's board (``.thoth/brain.db``) can arrive with its folder: a clone, a download, a copy. Thoth uses a board
only if it made that board on this computer, or the user said yes to it on a Hermes card. A board is known by its
folder and its database file (their inode numbers) and by a random id Thoth writes into it, so a board that was
cloned, copied, put in place of the original or written over it is a different board, whatever its path or the case
of its letters.

A board's setup and run commands run as the user, outside the agents' sandbox and with network access, so on top of
that Thoth runs a stored command only if the user approved that exact command for that board on Hermes's card for
thoth_init.

Both records are hashes in ``approvals.json`` in the plugin's data folder, never in the project, so nothing a project
brings with it can approve itself.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import unicodedata
from collections.abc import Callable
from pathlib import Path

from .brain import BRAIN_DIRNAME, Brain, BrainError, committed

FILE = "approvals.json"
MAX_KEPT = 2000  # the newest of each kind; an older one only means asking again
KINDS = {"setup": "setup command", "run": "command that starts its app"}
_HIDDEN = {"Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn"}  # control, invisible formatting, line breaks, private, unassigned


# -- commands as text -----------------------------------------------------------------------------------------------


def clean(kind: str, command: object) -> str:
    """The command exactly as Thoth will store, show and run it: one line, and nothing that doesn't show (control
    characters, invisible formatting, line separators), so an approval card always shows what runs."""
    text = str(command).strip()
    if any(unicodedata.category(c) in _HIDDEN and c != "\t" for c in text):
        raise BrainError(f"Give the {kind} command on one line, without control or invisible characters "
                         "(join several steps with &&).")
    return text


def shown(command: str) -> str:
    """A stored command for a message, with anything that could hide what it does written out (\\n, \\u202e)."""
    return "".join(c.encode("unicode_escape").decode("ascii") if unicodedata.category(c) in _HIDDEN and c != "\t" else c
                   for c in command)


def code(command: str) -> str:
    """The command as a code span that shows all of it, backticks included."""
    text = shown(command)
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


# -- the record -----------------------------------------------------------------------------------------------------


def _hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _read(folder: Path) -> dict:
    try:
        data = json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _keys(data: dict, name: str) -> list[str]:
    keys = data.get(name)
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def _add(data: dict, name: str, key: str) -> None:
    data[name] = ([k for k in _keys(data, name) if k != key] + [key])[-MAX_KEPT:]


def _change(data_dir: str | os.PathLike, change: Callable[[dict], None]) -> None:
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{FILE}.lock", "w") as lock:  # Hermes, the Thoth page and MCP servers may all write
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(folder)
        change(data)
        tmp = folder / f"{FILE}.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, folder / FILE)


# -- boards ---------------------------------------------------------------------------------------------------------


def identity(root: str | os.PathLike) -> str | None:
    """The board in ``root`` as this computer knows it: its folder's and its database file's inode numbers. None when
    there is no board, or when ``.thoth`` or its database is a link (Thoth never uses a board through a link)."""
    folder = Path(root)
    db = folder / BRAIN_DIRNAME / "brain.db"
    try:
        if stat.S_ISLNK(os.lstat(folder / BRAIN_DIRNAME).st_mode) or stat.S_ISLNK(os.lstat(db).st_mode):
            return None
        a, b = os.stat(folder), os.stat(db)
    except OSError:
        return None
    if not a.st_ino or not b.st_ino:  # a filesystem without inode numbers: the folder's own path
        return f"path:{folder.resolve()}"
    return f"{a.st_ino}:{b.st_ino}"


def _board_id(root: str | os.PathLike) -> str:
    """The random id Thoth wrote into the board, read without changing the file ("" when there is none)."""
    db = Path(os.path.abspath(Path(root) / BRAIN_DIRNAME / "brain.db"))
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='board_id'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    value = str(row[0]) if row and row[0] else ""
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""


def new_id(brain: Brain) -> None:
    """Give a board a new random id: a board Thoth makes, adopts, or finds in its own list for the first time."""
    brain.set_meta("board_id", secrets.token_hex(16))


def _board(root: str | os.PathLike) -> str | None:
    ident = identity(root)
    board_id = _board_id(root) if ident else ""
    return f"{ident}:{board_id}" if ident and board_id else None


def trusted(data_dir: str | os.PathLike, root: str | os.PathLike) -> bool:
    """Whether Thoth made this board here, or the user said yes to using it."""
    if identity(root) is None:
        return False
    data = _read(Path(data_dir))
    if not data.get("migrated"):
        _migrate(data_dir)
        data = _read(Path(data_dir))
    board = _board(root)
    return board is not None and _hash("board", board) in _keys(data, "boards")


def trust(data_dir: str | os.PathLike, root: str | os.PathLike) -> None:
    """Thoth made this board here, or the user just said yes to using it (it has its id by now)."""
    board = _board(root)
    if board is None:
        raise BrainError(f"Thoth won't use the board in {Path(root).name}: its .thoth folder or database is a link, or "
                         "it has no id.")
    _change(data_dir, lambda data: _add(data, "boards", _hash("board", board)))


def _migrate(data_dir: str | os.PathLike) -> None:
    """Once, the first time this record is read: the boards of the projects Thoth already lists (projects.json, which
    only Thoth writes) are the user's own, unless their .thoth folder is in git."""
    from . import projects

    def change(data: dict) -> None:
        if data.get("migrated"):
            return
        for root in projects.load(data_dir):
            try:
                if identity(root) is None or committed(root):
                    continue
                if not _board_id(root):
                    new_id(Brain(root))
                board = _board(root)
                if board:
                    _add(data, "boards", _hash("board", board))
            except Exception:
                continue  # asked again later, on a card
        data["migrated"] = True

    _change(data_dir, change)


def untrusted_message(root: str | os.PathLike) -> str:
    name = Path(root).name
    return (f"Thoth hasn't used the board in {name} on this computer before. It may have come with the folder (a clone, "
            f"a download or a copy), so Thoth won't use it until you say so: ask Hermes to open {name} (thoth_init for "
            "that folder), which asks you first.")


def require_board(data_dir: str | os.PathLike, root: str | os.PathLike) -> None:
    if not trusted(data_dir, root):
        raise BrainError(untrusted_message(root))


# -- commands -------------------------------------------------------------------------------------------------------


def _command_key(root: str | os.PathLike, kind: str, command: str) -> str | None:
    board = _board(root)
    return _hash("command", board, kind, command) if board else None


def approve(data_dir: str | os.PathLike, root: str | os.PathLike, kind: str, command: str) -> None:
    """Remember that the user approved ``command`` as this board's ``kind`` command (on Hermes's card)."""
    key = _command_key(root, kind, command) if command else None
    if key is not None:
        _change(data_dir, lambda data: _add(data, "commands", key))


def approved(data_dir: str | os.PathLike, root: str | os.PathLike, kind: str, command: str) -> bool:
    key = _command_key(root, kind, command) if command else None
    return key is not None and key in _keys(_read(Path(data_dir)), "commands")


def require(data_dir: str | os.PathLike, root: str | os.PathLike, kind: str, command: str) -> None:
    """Refuse to go on with a stored command the user has not approved for this board."""
    if command and not approved(data_dir, root, kind, command):
        raise BrainError(
            f"This project's {KINDS[kind]} won't run until you approve it on this computer: {code(command)}. Ask Hermes "
            f"to set it again (thoth_init with {kind} set to that command), which shows it to you for approval, or to "
            f"clear it ({kind} set to \"\").")
