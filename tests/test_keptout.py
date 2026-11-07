"""What a project keeps out of git reaches every task folder, and what a task makes there comes back on merge."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from thoth_core import gitops, keptout  # noqa: E402
from thoth_core.brain import Brain  # noqa: E402
from thoth_core.service import Thoth  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _offline(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path_factory.mktemp("thoth-data")))
    monkeypatch.setenv("THOTH_NO_CONDUCTOR", "1")
    monkeypatch.setenv("THOTH_NO_AGENTS", "1")  # never start a real Claude Code or Codex run


def _can_clone(tmp_path) -> bool:
    src, dst = tmp_path / "probe-src", tmp_path / "probe-dst"
    src.write_text("x")
    ok = keptout.clone(src, dst)
    src.unlink()
    dst.unlink(missing_ok=True)
    return ok


@pytest.fixture()
def game(tmp_path):
    """A project shaped like a ROM-hack repo: tools in git, the private game image and builds kept out."""
    repo = tmp_path / "game"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("/local/\n/artifacts/\n*.zip\n.env\n*.key\nnode_modules/\n__pycache__/\n.DS_Store\n")
    (repo / "tools.py").write_text("print('patch')\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (repo / "local" / "font").mkdir(parents=True)
    (repo / "local" / "game.bin").write_bytes(b"original image")
    (repo / "local" / "font" / "字库.png").write_bytes(b"glyphs")
    (repo / "数码宝贝世界3 欧版.zip").write_bytes(b"zip")
    (repo / ".env").write_text("TOKEN=1\n")
    (repo / "node_modules" / "pad").mkdir(parents=True)
    (repo / "node_modules" / "pad" / "index.js").write_text("module.exports = 1\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "tools.cpython-312.pyc").write_bytes(b"\0")
    (repo / ".DS_Store").write_bytes(b"\0")
    return repo


def _start(repo, tasks=None):
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), tasks or [{"title": "Patch the font", "agent": "codex"}])
    return svc, Brain.open(repo)


def _built(svc, brain, ref, tracked=None):
    task = svc._prepare_worktree(brain, brain.task(int(ref[1:])))
    wt = Path(task["worktree"])
    for name, text in (tracked or {"notes.md": f"{ref}\n"}).items():
        (wt / name).write_text(text)
    sha = gitops.commit_all(wt, f"{ref} work")
    brain.update_task(task["id"], status="built", head_commit=sha)
    return wt


def test_ignored_names_in_any_language_are_listed_intact(game):
    entries = gitops.ignored_entries(game)
    assert "数码宝贝世界3 欧版.zip" in entries and "local/" in entries


def test_a_task_folder_gets_what_the_project_keeps_out_of_git_but_never_secrets(game, tmp_path):
    if not _can_clone(tmp_path):
        pytest.skip("this disk cannot make copy-on-write clones")
    (game / "local" / ".env").write_text("SECRET=1\n")  # a secret inside a copied folder stays behind too
    (game / "local" / "keys").mkdir()
    (game / "local" / "keys" / "signing.key").write_text("k")
    svc, brain = _start(game)
    task = svc._prepare_worktree(brain, brain.task(1))
    wt = Path(task["worktree"])
    assert (wt / "local").is_dir() and not (wt / "local").is_symlink()
    assert (wt / "local" / "game.bin").read_bytes() == b"original image"
    assert (wt / "local" / "font" / "字库.png").read_bytes() == b"glyphs"
    assert (wt / "数码宝贝世界3 欧版.zip").exists()
    assert not (wt / ".env").exists() and not (wt / "local" / ".env").exists()
    assert not (wt / "local" / "keys" / "signing.key").exists()
    assert not (wt / "__pycache__").exists() and not (wt / ".DS_Store").exists()
    assert (wt / "node_modules").is_symlink()  # dependencies are still linked, not copied
    # The copy is the agent's own: changing it never touches the user's original.
    (wt / "local" / "game.bin").write_bytes(b"patched")
    assert (game / "local" / "game.bin").read_bytes() == b"original image"
    # Nothing kept out of git sneaks into the task's commit.
    (wt / "patch.py").write_text("x = 1\n")
    gitops.commit_all(wt, "patch")
    assert gitops.git(wt, "show", "--name-only", "--format=", "HEAD").split() == ["patch.py"]
    note = " ".join(n["body"] for n in brain.notes(task_id=1))
    assert "Copied in what the project keeps out of git" in note and "local/ (2 files)" in note
    assert "Secrets" in note and ".env" not in note.replace("Secrets (.env files, keys)", "")


def test_new_files_come_back_on_merge_and_changed_copies_keep_their_folder(game, tmp_path):
    if not _can_clone(tmp_path):
        pytest.skip("this disk cannot make copy-on-write clones")
    svc, brain = _start(game)
    wt = _built(svc, brain, "T1")
    (wt / "local" / "build").mkdir()
    (wt / "local" / "build" / "test copy.bin").write_bytes(b"patched image")
    (wt / "artifacts").mkdir()
    (wt / "artifacts" / "frame 1.png").write_bytes(b"png")
    (wt / "local" / "game.bin").write_bytes(b"changed by mistake")
    result = svc.merge(str(game), "T1")
    assert (game / "local" / "build" / "test copy.bin").read_bytes() == b"patched image"
    assert (game / "artifacts" / "frame 1.png").read_bytes() == b"png"
    assert (game / "local" / "game.bin").read_bytes() == b"original image"  # never overwritten
    report = result["outside_git"]["T1"]
    assert report["moved"] == 2 and report["kept"] == ["local/game.bin"]
    assert wt.is_dir() and brain.task(1)["worktree"]  # it still holds the changed copy
    assert (wt / "local" / "game.bin").read_bytes() == b"changed by mistake"
    # The merged files stay out of git in the project folder as well.
    assert not gitops.git(game, "status", "--porcelain")


def test_a_task_folder_without_changed_copies_goes_after_the_merge(game, tmp_path):
    if not _can_clone(tmp_path):
        pytest.skip("this disk cannot make copy-on-write clones")
    svc, brain = _start(game)
    wt = _built(svc, brain, "T1")
    (wt / "local" / "render.png").write_bytes(b"png")
    result = svc.merge(str(game), "T1")
    assert (game / "local" / "render.png").read_bytes() == b"png"
    assert result["outside_git"]["T1"]["moved"] == 1 and not result["outside_git"]["T1"]["kept"]
    assert not wt.exists() and not brain.task(1)["worktree"]


def test_the_next_task_gets_what_its_dependency_made_outside_git(game, tmp_path):
    if not _can_clone(tmp_path):
        pytest.skip("this disk cannot make copy-on-write clones")
    svc, brain = _start(game, [{"title": "Build the test image", "agent": "codex"},
                               {"title": "Play it in the emulator", "agent": "claude", "depends_on": [1]}])
    t1 = _built(svc, brain, "T1")
    (t1 / "local" / "build").mkdir()
    (t1 / "local" / "build" / "test.img").write_bytes(b"new image")
    (t1 / "local" / "game.bin").write_bytes(b"T1's version")
    task = svc._prepare_worktree(brain, brain.task(2))  # T1 is built, not merged
    t2 = Path(task["worktree"])
    assert (t2 / "local" / "build" / "test.img").read_bytes() == b"new image"
    assert (t2 / "local" / "game.bin").read_bytes() == b"T1's version"
    assert (game / "local" / "game.bin").read_bytes() == b"original image"
    assert any("2 from T1" in n["body"] for n in brain.notes(task_id=2))


def test_without_free_copies_folders_are_linked_read_only_and_secrets_held_back(game, monkeypatch):
    monkeypatch.setattr(keptout, "clone", lambda src, dst: False)
    (game / "private").mkdir()
    (game / "private" / ".env").write_text("SECRET=1\n")
    with (game / ".gitignore").open("a") as fh:
        fh.write("/private/\n")
    _git(game, "add", ".gitignore")
    _git(game, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore private")
    svc, brain = _start(game)
    wt = _built(svc, brain, "T1", {"patch.py": "x = 1\n"})
    assert (wt / "local").is_symlink() and (wt / "local" / "game.bin").read_bytes() == b"original image"
    assert not (wt / "private").exists()  # linking it would hand over the secret inside
    assert gitops.git(wt, "show", "--name-only", "--format=", "HEAD").split() == ["patch.py"]
    assert not gitops.is_dirty(wt)  # the links are ignored, as files too
    note = " ".join(n["body"] for n in brain.notes(task_id=1))
    assert "Linked read-only" in note and "private" in note


def test_a_folder_with_too_many_files_stays_behind(game, monkeypatch, tmp_path):
    if not _can_clone(tmp_path):
        pytest.skip("this disk cannot make copy-on-write clones")
    monkeypatch.setattr(keptout, "MAX_FILES", 1)
    svc, brain = _start(game)
    task = svc._prepare_worktree(brain, brain.task(1))
    wt = Path(task["worktree"])
    assert not (wt / "local").exists() and (wt / "数码宝贝世界3 欧版.zip").exists()
    assert any("more than 1 files: local" in n["body"] for n in brain.notes(task_id=1))


def test_the_setting_turns_it_off(game):
    svc = Thoth(mcp_launcher=None, settings={"copy_kept_out_files": False})
    svc.init(str(game))
    svc.plan(str(game), [{"title": "T", "agent": "codex"}])
    brain = Brain.open(game)
    task = svc._prepare_worktree(brain, brain.task(1))
    assert not (Path(task["worktree"]) / "local").exists()
