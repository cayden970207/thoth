"""Where projects live, and which folders Thoth may turn into a project.

A project named by a short name ("bakery-site") goes in the projects folder (~/Thoth unless the user changed
it), never next to whichever process runs Thoth: Hermes's gateway works in ~/.hermes. A folder that is not in
git yet gets its own repository, so Thoth can keep every version, but never a folder that holds far more than
one project: the home folder, Desktop, Documents, Downloads, a settings folder or a folder of repositories.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = "~/Thoth"
BIG_FILE = 50 * 1024 * 1024  # a bigger file stays in the folder but out of git
MAX_FILES = 20_000  # what the first version of a folder may hold
MAX_BYTES = 1024 ** 3
# Kept out of the first version of a folder: secrets, system files, dependency folders (Thoth links those).
KEEP_OUT = (".DS_Store", ".env", ".env.*", "*.pem", ".npmrc", ".netrc", "id_rsa", "id_ecdsa", "id_ed25519",
            "node_modules/", ".venv/", "venv/", "__pycache__/")
_SKIP = {".git", ".thoth", "node_modules", ".venv", "venv", "__pycache__"}
_HOME_FOLDERS = {n.casefold() for n in ("Desktop", "Documents", "Downloads", "Library", "Applications", "Pictures",
                                         "Movies", "Music", "Public", "Sites", "Developer")}


def projects_home(setting: object = None) -> Path:
    """The folder new projects go in (the projects_dir setting)."""
    raw = str(setting or "").strip() or DEFAULT_HOME
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path.home() / path


def tilde(path: Path | str) -> str:
    """A path as people read it: ~/Thoth/bakery-site."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def resolved(path: Path | str) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser().absolute()


def personal(path: Path | str) -> str:
    """Why this folder holds far more than one project ('' when it doesn't): the home folder, a system folder
    or one of the big folders in it."""
    p, home = resolved(path), resolved(Path.home())
    if p == home:
        return "it is your home folder"
    if p == resolved(os.environ.get("HERMES_HOME") or home / ".hermes"):
        return "it is Hermes's own folder"
    if len(p.parts) <= 2 or p in (home.parent, Path("/Users/Shared"), Path("/Volumes")):
        return "it is a system folder"
    if p.parent == home and p.name.casefold() in _HOME_FOLDERS:
        return f"it is your {p.name} folder"
    return ""


def why_not_new(path: Path | str, setting: object = None) -> str:
    """Why Thoth must not make a repository of this folder ('' when it may)."""
    why = personal(path)
    if why:
        return why
    p, home = resolved(path), resolved(Path.home())
    if p == resolved(projects_home(setting)):
        return "it is the folder that holds your Thoth projects"
    try:
        inside = p.relative_to(home).parts
    except ValueError:
        inside = ()
    if inside and inside[0].startswith("."):
        return f"it is in ~/{inside[0]}, a folder of settings"
    try:
        repos = sum(1 for child in p.iterdir() if child.is_dir() and (child / ".git").exists())
    except OSError:
        repos = 0
    if repos >= 2:
        return f"it holds {repos} projects of its own"
    return ""


def survey(folder: Path) -> dict:
    """What a first version of this folder would hold: files, bytes and the big files left out. Stops as soon
    as the folder is clearly too much to keep versions of."""
    files = size = 0
    big: list[str] = []
    for top, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP)
        for name in sorted(names):
            path = Path(top) / name
            try:
                n = path.lstat().st_size
            except OSError:
                continue
            if n > BIG_FILE:
                big.append(path.relative_to(folder).as_posix())
            else:
                files += 1
                size += n
            if files > MAX_FILES or size > MAX_BYTES or len(big) > 200:
                return {"files": files, "bytes": size, "big": big, "too_big": True}
    return {"files": files, "bytes": size, "big": big, "too_big": False}
