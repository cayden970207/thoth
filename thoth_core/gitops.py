"""Git plumbing: one branch and one worktree per task, commits as versions.

Every agent works in its own worktree under ``.thoth/worktrees/T<n>`` on branch
``thoth/T<n>-<slug>``, so two agents never write the same checkout. Thoth
commits whatever an agent leaves behind, which makes every run a version you
can diff, review, revert or merge.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .brain import BRAIN_DIRNAME, BrainError

GIT_TIMEOUT = 120
MAX_DIFF_CHARS = 60_000


class GitError(BrainError):
    pass


def git(cwd: str | Path, *args: str, check: bool = True, timeout: int = GIT_TIMEOUT) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:2])} timed out") from exc
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise GitError(f"git {' '.join(args)} failed: {msg[:800]}")
    return proc.stdout.strip()


def repo_root(path: str | Path) -> Path:
    out = git(path, "rev-parse", "--show-toplevel", check=False)
    if not out:
        raise GitError(f"{path} is not inside a git repository. Run `git init` there first (Thoth versions work with git).")
    return Path(out).resolve()


def current_branch(path: str | Path) -> str:
    name = git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if not name or name == "HEAD":
        raise GitError("The project is on a detached HEAD; check out a branch first")
    return name


def head(path: str | Path, ref: str = "HEAD") -> str:
    return git(path, "rev-parse", ref)


def has_commits(path: str | Path) -> bool:
    return bool(git(path, "rev-parse", "--verify", "HEAD", check=False))


def ensure_initial_commit(path: str | Path) -> None:
    """Worktrees need a commit to branch from; make an empty one in a brand-new repo."""
    if not has_commits(path):
        git(path, "commit", "--allow-empty", "-m", "thoth: initial commit")


def exclude_brain_dir(root: Path) -> None:
    """Keep ``.thoth/`` out of git without touching the user's .gitignore."""
    git_dir = Path(git(root, "rev-parse", "--git-common-dir"))
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = f"/{BRAIN_DIRNAME}/"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if line not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"# Thoth shared brain (local only)\n{line}\n")


def slugify(text: str, limit: int = 32) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "task"


def branch_exists(root: Path, branch: str) -> bool:
    return bool(git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False))


def create_worktree(root: Path, worktree: Path, branch: str, base_ref: str) -> str:
    """Create (or reuse) the task worktree. Returns the base commit."""
    base_commit = head(root, base_ref)
    if worktree.exists() and (worktree / ".git").exists():
        return base_commit
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        git(root, "worktree", "add", str(worktree), branch)
    else:
        git(root, "worktree", "add", "-b", branch, str(worktree), base_commit)
    return base_commit


def remove_worktree(root: Path, worktree: Path) -> None:
    if worktree.exists():
        git(root, "worktree", "remove", "--force", str(worktree), check=False)
    git(root, "worktree", "prune", check=False)


def is_dirty(path: str | Path) -> bool:
    return bool(git(path, "status", "--porcelain", "--untracked-files=all"))


def commit_all(worktree: Path, message: str) -> str | None:
    """Commit everything in the worktree. Returns the new commit, or None when clean."""
    if not is_dirty(worktree):
        return None
    git(worktree, "add", "-A")
    git(
        worktree,
        "-c", "user.name=Thoth",
        "-c", "user.email=thoth@localhost",
        "commit", "--no-verify", "-m", message,
    )
    return head(worktree)


def discard_changes(worktree: Path) -> None:
    """Reset tracked files to HEAD and delete untracked ones (never ignored files)."""
    git(worktree, "reset", "--hard", "HEAD")
    git(worktree, "clean", "-fd")


def name_status(path: str | Path, base: str, tip: str = "HEAD") -> list[dict]:
    """Files changed between two commits: [{path, status}] with status A, M, D or R."""
    if not base:
        return []
    out = git(path, "diff", "--name-status", "-M", f"{base}..{tip}", check=False)
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append({"path": parts[-1], "status": parts[0][:1]})
    return files[:200]


def diffstat(path: str | Path, base: str, tip: str = "HEAD") -> str:
    if not base:
        return ""
    return git(path, "diff", "--stat", f"{base}..{tip}", check=False)


def diff(path: str | Path, base: str, tip: str = "HEAD", limit: int = MAX_DIFF_CHARS) -> str:
    if not base:
        return ""
    text = git(path, "diff", f"{base}..{tip}", check=False)
    if len(text) > limit:
        text = text[:limit] + f"\n... [diff truncated: showing {limit} of {len(text)} characters]"
    return text


def log_oneline(path: str | Path, base: str, tip: str = "HEAD", limit: int = 20) -> str:
    if not base:
        return ""
    return git(path, "log", "--oneline", f"-{limit}", f"{base}..{tip}", check=False)


def is_ancestor(path: str | Path, commit: str, tip: str = "HEAD") -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, tip],
        cwd=str(path), capture_output=True, text=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def tracked_dirty(path: str | Path) -> bool:
    """Uncommitted changes to tracked files (untracked files do not block a merge)."""
    return bool(git(path, "status", "--porcelain", "--untracked-files=no"))


def restore_tree(worktree: Path, commit: str, message: str) -> str | None:
    """Make the worktree's files exactly those of ``commit`` and record that as a NEW commit.

    Nothing is rewritten: the newer versions stay in history and can be restored again.
    """
    git(worktree, "read-tree", "-u", "--reset", commit)
    git(worktree, "clean", "-fd")
    if not git(worktree, "status", "--porcelain", "--untracked-files=all"):
        return None
    git(worktree, "-c", "user.name=Thoth", "-c", "user.email=thoth@localhost", "commit", "--no-verify", "-m", message)
    return head(worktree)


def merge_into_worktree(worktree: Path, branch: str, message: str) -> tuple[bool, str]:
    """Bring ``branch`` into a task worktree (e.g. a dependency's newer version). Aborts on conflict."""
    proc = subprocess.run(
        ["git", "-c", "user.name=Thoth", "-c", "user.email=thoth@localhost", "merge", "--no-ff", "--no-edit", "-m", message, branch],
        cwd=str(worktree), capture_output=True, text=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL,
    )
    if proc.returncode == 0:
        return True, head(worktree)
    conflicts = git(worktree, "diff", "--name-only", "--diff-filter=U", check=False)
    git(worktree, "merge", "--abort", check=False)
    return False, ("Conflicts in: " + ", ".join(conflicts.splitlines())) if conflicts else (proc.stdout + proc.stderr).strip()[:600]


def merge_branch(root: Path, branch: str, message: str) -> tuple[bool, str]:
    """Merge ``branch`` into the checked-out branch with --no-ff.

    Returns (ok, merge_commit_or_error). Conflicts are aborted so the main
    checkout is never left half-merged.
    """
    proc = subprocess.run(
        ["git", "-c", "user.name=Thoth", "-c", "user.email=thoth@localhost", "merge", "--no-ff", "-m", message, branch],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode == 0:
        return True, head(root)
    conflicts = git(root, "diff", "--name-only", "--diff-filter=U", check=False)
    git(root, "merge", "--abort", check=False)
    detail = (proc.stdout + proc.stderr).strip()
    if conflicts:
        return False, "Merge conflicts in: " + ", ".join(conflicts.splitlines())
    return False, detail[:800] or "merge failed"
