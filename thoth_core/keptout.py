"""What a project keeps out of git, in every task folder.

A task folder is a git worktree, so on its own it holds only what git tracks. What the project keeps out of
git on purpose (test builds, game images, big videos, private inputs) would be missing there, and an agent
could not use it. Thoth copies those files in as copy-on-write clones (APFS on macOS, reflinks on btrfs or
XFS): instant, no extra disk until one side changes, and nothing done in a task folder can touch the user's
originals. Secrets are never copied, dependency folders are linked instead (gitops.link_dependencies) and
caches stay behind. Where the disk cannot clone, a folder is linked read-only.

When a task is merged, the new files it made outside git go to the project folder; nothing there is ever
overwritten. Copies of the user's files that a task changed stay in its task folder.
"""

from __future__ import annotations

import filecmp
import fnmatch
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import gitops
from .brain import BRAIN_DIRNAME

SECRETS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx", ".npmrc", ".netrc",
           ".pypirc", ".git-credentials", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".ssh", ".aws", ".gnupg")
CACHES = (".DS_Store", "Thumbs.db", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".ruff_cache",
          ".tox", ".cache", ".parcel-cache", ".turbo", ".next", ".nuxt", ".svelte-kit")
MAX_FILES = 200_000  # a folder with more files than this stays behind rather than holding up the task


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def is_secret(name: str) -> bool:
    return _matches(name, SECRETS)


def _left_behind(rel: str, deps: set[str]) -> bool:
    """Never copied or moved: secrets, caches, dependency folders, Thoth's and git's own folders."""
    parts = Path(rel).parts
    return (not parts or parts[0] in (BRAIN_DIRNAME, ".git") or any(p in deps for p in parts)
            or any(_matches(p, SECRETS) or _matches(p, CACHES) for p in parts))


def _through_link(base: Path, rel: str) -> bool:
    """A path whose folders pass through a symlink: writing there would land somewhere else."""
    parts = Path(rel).parts
    return any(base.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts)))


def _ignored(folder: Path, collapse: bool) -> list[str]:
    """Ignored, untracked paths (NUL-separated, so names in any language arrive intact)."""
    args = ["ls-files", "-z", "--others", "--ignored", "--exclude-standard"] + (["--directory"] if collapse else [])
    return [p for p in gitops.git(folder, *args, check=False).split("\0") if p]


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def clone(src: Path, dst: Path) -> bool:
    """A copy-on-write copy of a file or folder at a path that does not exist yet (links stay links).
    False when this disk cannot clone; a plain copy is never made behind the caller's back."""
    if dst.exists() or dst.is_symlink():
        return False
    if sys.platform == "darwin":
        try:
            import ctypes

            fn = ctypes.CDLL(None, use_errno=True).clonefile
            fn.argtypes, fn.restype = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32), ctypes.c_int
            if fn(os.fsencode(src), os.fsencode(dst), 0x0001) == 0:  # CLONE_NOFOLLOW
                return True
        except (OSError, AttributeError):
            pass
    elif sys.platform.startswith("linux"):
        try:
            proc = subprocess.run(["cp", "-a", "--reflink=always", "--", str(src), str(dst)],
                                  capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    _remove(dst)  # whatever a failed attempt left behind (dst did not exist before)
    return False


def _copy(src: Path, dst: Path) -> bool:
    try:
        shutil.copy2(src, dst, follow_symlinks=False)
        return True
    except OSError:
        return False


def _scan(src: Path) -> tuple[int, list[str], bool]:
    """How many files a folder holds (links not followed), the secrets in it (relative paths, a secret folder
    as a whole) and whether it holds more than MAX_FILES."""
    if src.is_symlink() or not src.is_dir():
        return 1, [], False
    files, secrets = 0, []
    for top, dirs, names in os.walk(src):
        here = Path(top).relative_to(src)
        for d in [d for d in dirs if is_secret(d)]:
            secrets.append((here / d).as_posix())
            dirs.remove(d)
        for name in names:
            files += 1
            if is_secret(name):
                secrets.append((here / name).as_posix())
        if files > MAX_FILES:
            return files, secrets, True
    return files, secrets, False


def _same(a: Path, b: Path) -> bool:
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    if not stat.S_ISREG(sb.st_mode) or sa.st_size != sb.st_size:
        return False
    if sa.st_mtime_ns == sb.st_mtime_ns:  # a clone keeps the original's time
        return True
    return filecmp.cmp(a, b, shallow=False)


def changes(root: Path, folder: Path, deps: tuple[str, ...] | list[str] = gitops.DEFAULT_DEPENDENCY_DIRS) -> tuple[list[str], list[str]]:
    """Files a task folder holds outside git that the project folder lacks (new) or holds differently (changed)."""
    skip = set(deps)
    new, changed = [], []
    for rel in _ignored(folder, collapse=False):
        if _left_behind(rel, skip) or _through_link(folder, rel):
            continue
        src = folder / rel
        if src.is_symlink() or not src.is_file():
            continue
        dst = root / rel
        if not (dst.exists() or dst.is_symlink()):
            new.append(rel)
        elif not _same(src, dst):
            changed.append(rel)
    return new, changed


def bring_in(root: Path, worktree: Path, deps: tuple[str, ...] | list[str] = gitops.DEFAULT_DEPENDENCY_DIRS,
             sources: list[tuple[str, Path]] | None = None) -> dict:
    """Copy what the project keeps out of git into a new task folder, then what the tasks it builds on made
    outside git (``sources``: their ref and task folder), so a dependency's test build reaches the next task.
    Returns what happened, for the agent's brief."""
    skip = set(deps)
    out: dict = {"copied": [], "linked": [], "too_big": [], "held_back": [], "secrets": 0, "from": []}
    link: list[str] = []
    for entry in _ignored(root, collapse=True):
        rel = entry.rstrip("/")
        if not rel:
            continue
        if any(is_secret(p) for p in Path(rel).parts):
            out["secrets"] += 1
            continue
        if _left_behind(rel, skip):
            continue
        src, dst = root / rel, worktree / rel
        if dst.exists() or dst.is_symlink() or _through_link(worktree, rel):
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        files, secrets, too_many = _scan(src)
        if too_many:
            out["too_big"].append(rel)
        elif clone(src, dst):
            for secret in secrets:
                _remove(dst / secret)
            out["copied"].append((rel + ("/" if src.is_dir() and not src.is_symlink() else ""), files - len(secrets)))
            out["secrets"] += len(secrets)
        elif secrets:
            out["held_back"].append(rel)  # linking would hand the agent the secrets inside
        else:
            link.append(rel)
    if link:
        gitops._exclude_exactly(root, link)  # before linking, so a commit can never pick the links up
        for rel in link:
            (worktree / rel).symlink_to(root / rel, target_is_directory=(root / rel).is_dir())
        out["linked"] = link
    for ref, folder in sources or []:
        new, changed = changes(root, folder, deps)
        brought = 0
        for rel in new + changed:
            src, dst = folder / rel, worktree / rel
            if _through_link(worktree, rel):
                continue
            _remove(dst)  # the copy of the user's original: the dependency's version is newer
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            if clone(src, dst) or _copy(src, dst):
                brought += 1
        if brought:
            out["from"].append((ref, brought))
    return out


def bring_back(root: Path, worktree: Path, rels: list[str], keep: bool) -> list[str]:
    """Put the new files a merged task made outside git into the project folder; never overwrite anything.
    ``keep``: the task folder stays, so copy (a clone) instead of moving. Returns what arrived."""
    done = []
    for rel in rels:
        src, dst = worktree / rel, root / rel
        if dst.exists() or dst.is_symlink() or _through_link(root, rel) or not src.is_file():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if keep:
                ok = clone(src, dst) or _copy(src, dst)
            else:
                try:
                    os.replace(src, dst)  # the same disk: a rename
                    ok = True
                except OSError:
                    ok = clone(src, dst) or _copy(src, dst)
        except OSError:
            ok = False
        if ok:
            done.append(rel)
    return done


def describe(summary: dict) -> str:
    """The brief's line about what the task folder holds outside git."""
    parts = []
    if summary.get("copied"):
        shown = ", ".join(f"{rel} ({n:,} files)" if rel.endswith("/") else rel for rel, n in summary["copied"][:6])
        more = f" and {len(summary['copied']) - 6} more" if len(summary["copied"]) > 6 else ""
        parts.append(f"Copied in what the project keeps out of git: {shown}{more}. These are copy-on-write clones: "
                     "change them freely, they stay out of git and the user's originals never change. New files you "
                     "make outside git go to the project folder when this task is merged.")
    if summary.get("linked"):
        parts.append(f"Linked read-only, because this disk cannot make free copies (do not write inside): {', '.join(summary['linked'][:6])}.")
    if summary.get("from"):
        parts.append("Also copied what the tasks this one builds on made outside git: "
                     + ", ".join(f"{n} from {ref}" for ref, n in summary["from"]) + ".")
    if summary.get("too_big"):
        parts.append(f"Not copied, more than {MAX_FILES:,} files: {', '.join(summary['too_big'][:6])}.")
    if summary.get("held_back"):
        parts.append(f"Not copied, it holds secrets and this disk cannot make free copies: {', '.join(summary['held_back'][:6])}.")
    if summary.get("secrets"):
        parts.append("Secrets (.env files, keys) stay in the project folder.")
    return " ".join(parts)
