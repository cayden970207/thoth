"""Offline tests: brain, git versions, report parsing, approval gate. No agent CLIs needed."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from thoth_core.brain import Brain, BrainError  # noqa: E402
from thoth_core.briefing import build_brief, parse_report  # noqa: E402
from thoth_core.service import Thoth  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path_factory, monkeypatch):
    """Tests never touch the user's real Thoth data (chats.json and friends)."""
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path_factory.mktemp("thoth-data")))
    monkeypatch.setenv("THOTH_NO_CONDUCTOR", "1")  # tests drive autopilot step by step
    monkeypatch.setenv("THOTH_NO_AGENTS", "1")  # and never start a real Claude Code or Codex run


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("print('hi')\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return tmp_path


def test_init_plan_and_brief(repo):
    svc = Thoth(mcp_launcher=None)
    out = svc.init(str(repo), goal="Ship a todo app")
    assert out["initialized"] and out["base_branch"] == "main"
    assert "/.thoth/" in (repo / ".git" / "info" / "exclude").read_text()
    plan = svc.plan(str(repo), [
        {"title": "Build UI", "agent": "claude"},
        {"title": "Write tests", "agent": "codex", "depends_on": [1]},
    ])
    assert [t["task"] for t in plan["created"]] == ["T1", "T2"]
    assert plan["created"][1]["depends_on"] == ["T1"]
    svc.note(str(repo), "decision", "Use SQLite for storage")
    brain = Brain.open(repo)
    task = {**brain.task(1), "branch": "thoth/T1-build-ui", "base_ref": "main", "base_commit": "abc1234"}
    brief = build_brief(brain, task, agent="claude")
    assert "Ship a todo app" in brief and "Use SQLite for storage" in brief and "THOTH_REPORT" in brief
    assert "## Decisions" in (repo / ".thoth" / "BRAIN.md").read_text()


def test_dependency_must_finish_first(repo):
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "A", "agent": "codex"}, {"title": "B", "agent": "codex", "depends_on": [1]}])
    with pytest.raises(BrainError, match="depends on T1"):
        svc._prepare_worktree(Brain.open(repo), Brain.open(repo).task(2))


def test_worktree_commit_merge(repo):
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "Add feature", "agent": "codex"}])
    brain = Brain.open(repo)
    task = svc._prepare_worktree(brain, brain.task(1))
    wt = Path(task["worktree"])
    (wt / "feature.py").write_text("x = 1\n")
    from thoth_core import gitops
    sha = gitops.commit_all(wt, "thoth(codex): Add feature")
    assert sha and gitops.head(wt) == sha
    brain.update_task(1, status="built", head_commit=sha)
    result = svc.merge(str(repo), "T1")
    assert result["merged_into"] == "main"
    assert (repo / "feature.py").exists()
    assert brain.task(1)["status"] == "merged"
    assert not wt.exists()


def test_parse_report_variants():
    text = 'Done.\nTHOTH_REPORT\n```json\n{"status": "done", "summary": "ok", "decisions": ["a"]}\n```'
    assert parse_report(text)["decisions"] == ["a"]
    assert parse_report('THOTH_REPORT {"verdict": "approve", "summary": "x"}')["verdict"] == "approve"
    assert parse_report("no report here") == {}


class FakeCtx:
    plugin_id = "thoth"

    def __init__(self):
        self.tools, self.hooks, self.commands, self.skills, self.injected = {}, {}, {}, {}, []
        self.state = self
        self._state = {}

    def register_tool(self, name, toolset, schema, handler, emoji=""):
        self.tools[name] = handler

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_cli_command(self, **kw):
        pass

    def register_skill(self, name, path):
        self.skills[name] = path

    def get_config(self, key, default=None):
        return default

    def get(self, key, default=None):
        return self._state.get(key, default)

    def set(self, key, value):
        self._state[key] = value

    def inject_message(self, content, role="user", session_key=None):
        self.injected.append(content)
        return True


def _load_plugin():
    spec = importlib.util.spec_from_file_location("thoth_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["thoth_plugin"] = module
    spec.loader.exec_module(module)
    return module


def test_gated_tools_need_the_hook(repo):
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    assert len(ctx.tools) == 19 and "coordinate" in ctx.skills
    init = json.loads(ctx.tools["thoth_init"]({"project": str(repo), "goal": "g"}, session_id="s1"))
    assert init["ok"]
    json.loads(ctx.tools["thoth_plan"]({"project": str(repo), "tasks": [{"title": "T", "agent": "codex"}]}, session_id="s1"))
    # Without the pre_tool_call hook (no approval) the handler refuses to start an agent.
    blocked = json.loads(ctx.tools["thoth_merge"]({"project": str(repo), "task": "T1"}, session_id="s1"))
    assert not blocked["ok"] and "approval" in blocked["error"]
    # The hook escalates to Hermes approval with a readable summary and a scoped rule key.
    decision = ctx.hooks["pre_tool_call"](tool_name="thoth_merge", args={"project": str(repo), "task": "T1"}, session_id="s1")
    assert decision["action"] == "approve" and decision["message"].startswith("Merge T1")
    assert decision["rule_key"].startswith("thoth:merge:")
    # Ungated tools pass straight through.
    assert ctx.hooks["pre_tool_call"](tool_name="thoth_status", args={}, session_id="s1") is None
    # A setup command runs outside the sandbox, so it needs approval too, and says exactly what will run.
    setup_args = {"project": str(repo), "setup": "npm ci"}
    assert not json.loads(ctx.tools["thoth_init"](setup_args, session_id="s1"))["ok"]
    decision = ctx.hooks["pre_tool_call"](tool_name="thoth_init", args=setup_args, session_id="s1")
    assert decision["action"] == "approve" and "`npm ci`" in decision["message"] and decision["rule_key"].startswith("thoth:setup:")
    assert json.loads(ctx.tools["thoth_init"](setup_args, session_id="s1"))["ok"]
    status = ctx.commands["thoth"]("status")
    assert "T1" in status


def test_claude_command_uses_os_sandbox_and_readonly_review():
    from thoth_core.agents import ClaudeDriver
    d = ClaudeDriver({})
    build = d.command(kind="build", session_id="", mcp=None)
    settings = json.loads(build[build.index("--settings") + 1])
    assert settings["sandbox"] == {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False}
    assert build[build.index("--permission-mode") + 1] == "acceptEdits"
    review = d.command(kind="review", session_id="", mcp=None)
    assert "Edit" in review[review.index("--disallowedTools") + 1]
    off = ClaudeDriver({"claude_sandbox": False, "lean_workers": False}).command(kind="build", session_id="", mcp=None)
    assert "--settings" not in off and "--strict-mcp-config" not in off
    assert settings.get("disableAllHooks") is True and "--strict-mcp-config" in build and "--disable-slash-commands" in build
    from thoth_core.agents import CodexDriver
    assert "--ignore-user-config" in CodexDriver({}).command(kind="build", session_id="", mcp=None)
    assert "--ignore-user-config" in CodexDriver({}).command(kind="build", session_id="abc", mcp=None)
    assert "--ignore-user-config" not in CodexDriver({"lean_workers": False}).command(kind="build", session_id="", mcp=None)
    assert not any("dangerously" in part for part in build + review)


def test_decisions_are_deduplicated_across_agents(repo):
    from thoth_core.service import _same_point
    assert _same_point("calculateTip returns { tip, total, perPerson }", "calculateTip returns {tip, total, perPerson} as numbers")
    assert not _same_point("Use SQLite for storage", "Money is rounded to cents only for display")
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    first = svc.note(str(repo), "decision", "tip.js exports calculateTip(bill, percent, people)", author="hermes")
    again = svc.note(str(repo), "decision", "tip.js exports calculateTip(bill, percent, people).", author="codex")
    assert again.get("duplicate_of") and again["note"] == first["note"]
    assert len(Brain.open(repo).notes(kinds=("decision",))) == 1


def test_needs_queue_and_review_cleanup(repo):
    from thoth_core import gitops
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "A", "agent": "claude"}, {"title": "B", "agent": "codex"}, {"title": "C", "agent": "codex"}])
    brain = Brain.open(repo)
    brain.update_task(1, status="approved")
    brain.update_task(2, status="changes")
    brain.update_task(3, status="built")
    kinds = [n["kind"] for n in svc.needs(str(repo))]
    assert kinds == ["merge", "fix", "review"]
    assert svc.snapshot(str(repo))["agents"]["claude"]["busy"] is False
    task = svc._prepare_worktree(brain, brain.task(1))
    wt = Path(task["worktree"])
    (wt / "scratch.txt").write_text("reviewer leftovers")
    (wt / "app.py").write_text("changed by reviewer\n")
    gitops.discard_changes(wt)
    assert not (wt / "scratch.txt").exists() and (wt / "app.py").read_text() == "print('hi')\n"


def test_p3_only_review_does_not_block_and_rounds_escalate(repo):
    from thoth_core.service import _severity
    assert _severity("[P3] index.html:226 — polish") == 3
    assert _severity("[P1] crash on empty input") == 1
    assert _severity("index.html:10 no tag") == 2
    svc = Thoth(mcp_launcher=None, settings={"max_review_rounds": 1})
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "A", "agent": "claude"}])
    brain = Brain.open(repo)
    brain.update_task(1, status="changes")
    run = brain.add_run(task_id=1, agent="codex", kind="review", prompt="", actor="t")
    brain.update_run(run["id"], status="done", report={"verdict": "changes", "comments": ["[P2] x"]})
    assert svc.needs(str(repo))[0]["kind"] == "decide"
    with pytest.raises(BrainError, match="override"):
        svc.merge(str(repo), "T1")


def test_hermes_self_task_files_flow_to_dependents(repo):
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo), goal="A tiny game")
    svc.plan(str(repo), [
        {"title": "Make sprites", "agent": "hermes", "kind": "asset", "role": "Art pipeline"},
        {"title": "Build the game", "agent": "claude", "kind": "code", "role": "Game developer", "depends_on": [1]},
    ])
    started = svc.assign(str(repo), "T1")
    assert started["agent"] == "hermes" and "Art pipeline" in started["brief"] and "thoth_complete" in started["brief"]
    wt = Path(started["worktree"])
    (wt / "assets").mkdir()
    (wt / "assets" / "coin.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    done = svc.complete(str(repo), "T1", "Made a coin sprite", outputs=["assets/coin.png — 32x32 coin"])
    assert done["status"] == "done" and done["task_status"] == "built"
    assert done["files_changed"] == ["A assets/coin.png"] and done["outputs"] == ["assets/coin.png — 32x32 coin"]
    brain = Brain.open(repo)
    t2 = svc._prepare_worktree(brain, brain.task(2))
    from thoth_core.briefing import build_brief
    brief = build_brief(brain, t2, agent="claude")
    assert "Game developer" in brief and "assets/coin.png" in brief
    assert (Path(t2["worktree"]) / "assets" / "coin.png").exists()  # built on T1's branch, so the file is there


def test_stream_parsers_capture_files_and_delegations(tmp_path):
    from thoth_core.agents import ClaudeDriver, CodexDriver, RunState
    (tmp_path / "a.py").write_text("x")
    st = RunState(cwd=str(tmp_path))
    c = ClaudeDriver()
    c.parse(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Agent", "input": {"subagent_type": "playtester", "description": "Play the game"}}]}}), st)
    c.parse(json.dumps({"type": "assistant", "parent_tool_use_id": "t1", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": str(tmp_path / "a.py")}}]}}), st)
    c.parse(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t3", "name": "Write", "input": {"file_path": str(tmp_path / "b.py")}}]}}), st)
    c.parse(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}), st)
    d = st.delegation_list()[0]
    assert d["role"] == "playtester" and d["status"] == "completed" and d["activity"] == 1
    assert st.files() == {"read": ["a.py"], "written": ["b.py"]}
    st2 = RunState(cwd=str(tmp_path))
    x = CodexDriver()
    x.parse(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'cat a.py'"}}), st2)
    x.parse(json.dumps({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": str(tmp_path / "c.png"), "kind": "add"}]}}), st2)
    x.parse(json.dumps({"type": "item.started", "item": {"id": "i1", "type": "collab_tool_call", "tool": "spawn_agent", "receiver_thread_ids": ["th1"], "prompt": "Slice the sprite sheet", "agents_states": {}}}), st2)
    x.parse(json.dumps({"type": "item.completed", "item": {"id": "i2", "type": "collab_tool_call", "tool": "wait", "receiver_thread_ids": ["th1"], "agents_states": {"th1": {"status": "completed"}}}}), st2)
    assert st2.files() == {"read": ["a.py"], "written": ["c.png"]}
    assert st2.delegation_list()[0]["status"] == "completed" and "Slice" in st2.delegation_list()[0]["title"]


def test_revise_sync_and_restore_loop(repo):
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [
        {"title": "Sprite", "agent": "hermes", "kind": "asset"},
        {"title": "Game", "agent": "hermes", "kind": "code", "depends_on": [1]},
    ])
    wt1 = Path(svc.assign(str(repo), "T1")["worktree"])
    (wt1 / "hero.txt").write_text("red hair")
    svc.complete(str(repo), "T1", "v1", outputs=["hero.txt"])
    first = Brain.open(repo).task(1)["head_commit"]
    wt2 = Path(svc.assign(str(repo), "T2")["worktree"])
    assert (wt2 / "hero.txt").read_text() == "red hair"
    (wt2 / "game.txt").write_text("uses hero")
    svc.complete(str(repo), "T2", "game v1")
    # The user is not happy with the sprite: revise it with feedback.
    started = svc.revise(str(repo), "T1", "Make the hair blue", files=["hero.txt"])
    assert "Make the hair blue" in started["brief"] and "hero.txt" in started["brief"]
    (wt1 / "hero.txt").write_text("blue hair")
    svc.complete(str(repo), "T1", "v2")
    needs = svc.needs(str(repo))
    assert needs[0]["kind"] == "sync" and needs[0]["task"] == "T2"
    synced = svc.sync(str(repo), "T2")
    assert synced["updated"] == ["T1"] and (wt2 / "hero.txt").read_text() == "blue hair"
    assert not any(n["kind"] == "sync" for n in svc.needs(str(repo)))
    # Actually red was better: restore the first version as a new version.
    restored = svc.restore(str(repo), "T1", first)
    assert restored["changed"] and (wt1 / "hero.txt").read_text() == "red hair"
    assert any(n["kind"] == "sync" for n in svc.needs(str(repo)))
    assert [n["kind"] for n in Brain.open(repo).notes(task_id=1)] .count("feedback") == 1


def test_large_sessions_start_fresh(repo, monkeypatch):
    svc = Thoth(mcp_launcher=None, settings={"resume_max_tokens": 1000})
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "A", "agent": "codex"}])
    brain = Brain.open(repo)
    run = brain.add_run(task_id=1, agent="codex", kind="build", prompt="", actor="t")
    brain.update_run(run["id"], status="done", session_id="sess-1", usage={"prompt_tokens": 5000, "cached_tokens": 4000, "output_tokens": 10})
    brain.update_task(1, status="built")
    launched = {}
    monkeypatch.setattr(svc, "_driver", lambda agent: type("D", (), {"name": agent, "label": agent})())
    monkeypatch.setattr(svc, "_launch", lambda *a, **k: launched.update(k))
    out = svc.assign(str(repo), "T1")
    assert out["resumed_session"] is False and launched["session"] == ""
    svc2 = Thoth(mcp_launcher=None, settings={"resume_max_tokens": 10_000})
    brain.update_task(1, status="built")
    for r in brain.runs(task_id=1, open_only=True):
        brain.update_run(r["id"], status="done")
    monkeypatch.setattr(svc2, "_driver", lambda agent: type("D", (), {"name": agent, "label": agent})())
    monkeypatch.setattr(svc2, "_launch", lambda *a, **k: launched.update(k))
    assert svc2.assign(str(repo), "T1")["resumed_session"] is True


def test_model_and_effort_reach_the_cli():
    from thoth_core.agents import ClaudeDriver, CodexDriver
    c = ClaudeDriver({}).command(kind="build", session_id="", mcp=None, model="fable", effort="high", name="Thoth · T1 Build")
    assert c[c.index("--model") + 1] == "fable" and c[c.index("--effort") + 1] == "high" and c[c.index("--name") + 1] == "Thoth · T1 Build"
    assert "--effort" not in ClaudeDriver({}).command(kind="build", session_id="", mcp=None, effort="ultra")  # not a Claude level
    x = CodexDriver({"codex_model": "gpt-6-astra"}).command(kind="build", session_id="", mcp=None, effort="ultra")
    assert x[x.index("--model") + 1] == "gpt-6-astra" and 'model_reasoning_effort="ultra"' in x
    y = CodexDriver({"codex_model": "gpt-6-astra"}).command(kind="build", session_id="", mcp=None, model="gpt-6.1-sol")
    assert y[y.index("--model") + 1] == "gpt-6.1-sol"


def _built(svc, brain, ref, files, msg):
    """Give a task a worktree and a committed version, as if its agent had finished."""
    from thoth_core import gitops
    task = svc._prepare_worktree(brain, brain.task(int(ref[1:])))
    wt = Path(task["worktree"])
    for name, text in files.items():
        (wt / name).write_text(text)
    sha = gitops.commit_all(wt, msg)
    brain.update_task(task["id"], status="built", head_commit=sha)
    return wt


def test_task_with_two_unmerged_dependencies_starts_with_both(repo):
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "Art", "agent": "codex"}, {"title": "Page", "agent": "claude"},
                         {"title": "Integrate", "agent": "claude", "depends_on": [1, 2]}])
    brain = Brain.open(repo)
    _built(svc, brain, "T1", {"art.txt": "hero\n"}, "art")
    _built(svc, brain, "T2", {"page.html": "<h1>hi</h1>\n"}, "page")
    t3 = svc._prepare_worktree(brain, brain.task(3))
    wt = Path(t3["worktree"])
    assert (wt / "art.txt").exists() and (wt / "page.html").exists()
    from thoth_core import gitops
    assert t3["base_commit"] == gitops.head(wt)  # the merge is T3's starting point: T1/T2 stay out of its diff
    assert any("merged in" in n["body"] for n in brain.notes(task_id=3))


def test_conflicting_dependencies_leave_nothing_half_made(repo):
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "A", "agent": "codex"}, {"title": "B", "agent": "claude"},
                         {"title": "C", "agent": "claude", "depends_on": [1, 2]}])
    brain = Brain.open(repo)
    _built(svc, brain, "T1", {"app.py": "print('a')\n"}, "a")
    _built(svc, brain, "T2", {"app.py": "print('b')\n"}, "b")
    with pytest.raises(BrainError, match="change the same lines"):
        svc._prepare_worktree(brain, brain.task(3))
    assert not (repo / ".thoth" / "worktrees" / "T3").exists() and not brain.task(3)["worktree"]


def test_dependency_folders_are_linked_and_never_committed(repo):
    from thoth_core import gitops
    (repo / ".gitignore").write_text("node_modules/\n.env\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore")
    (repo / "node_modules" / "left-pad").mkdir(parents=True)
    (repo / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
    (repo / ".env").write_text("TOKEN=dev\n")
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "Use the package", "agent": "codex"}])
    brain = Brain.open(repo)
    wt = _built(svc, brain, "T1", {"main.js": "require('left-pad')\n"}, "use it")
    assert (wt / "node_modules").is_symlink() and (wt / "node_modules" / "left-pad" / "index.js").exists()
    assert not (wt / ".env").exists()  # secrets never leave the main checkout
    committed = gitops.git(wt, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["main.js"] and not gitops.is_dirty(wt)
    assert any("node_modules" in n["body"] for n in brain.notes(task_id=1))


def test_setup_command_replaces_links(repo):
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo), setup="mkdir -p build && echo ok > build/ready")
    svc.plan(str(repo), [{"title": "T", "agent": "codex"}])
    brain = Brain.open(repo)
    task = svc._prepare_worktree(brain, brain.task(1))
    assert (Path(task["worktree"]) / "build" / "ready").read_text().strip() == "ok"
    assert any("Setup `mkdir" in n["body"] for n in brain.notes(task_id=1))


def test_preview_serves_pages_but_never_hidden_files(repo):
    import urllib.error
    import urllib.request
    from thoth_core import preview
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "Site", "agent": "claude"}, {"title": "Art", "agent": "codex"}])
    brain = Brain.open(repo)
    wt = _built(svc, brain, "T1", {"index.html": "<h1>Brewbox</h1>\n"}, "site")
    (wt / ".env").write_text("SECRET=1\n")
    _built(svc, brain, "T2", {"hero.txt": "x\n"}, "art")
    try:
        r = svc.preview(str(repo), "T1")
        assert r["url"].startswith("http://127.0.0.1:")
        assert b"Brewbox" in urllib.request.urlopen(r["url"], timeout=5).read()
        base = r["url"].rsplit("/", 1)[0]
        for hidden in ("/.env", "/%2Eenv", "/.git/config"):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + hidden, timeout=5)
            assert err.value.code == 404
        with pytest.raises(BrainError, match="no web page"):
            svc.preview(str(repo), "T2")
    finally:
        preview.stop_all()


def test_init_can_create_a_new_project(tmp_path):
    svc = Thoth(mcp_launcher=None)
    target = tmp_path / "sites" / "brewbox"
    result = svc.init(str(target), goal="A coffee site", create=True)
    assert (target / ".git").exists() and (target / ".thoth" / "brain.db").exists()
    assert "new git repository" in result["created"]


def test_notices_batch_and_skip_what_wait_reports(repo, monkeypatch):
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    p = plugin._plugin
    monkeypatch.setattr(sys.modules["thoth_plugin"].time, "sleep", lambda s: None)
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "A", "agent": "codex"}, {"title": "B", "agent": "claude"}, {"title": "C", "agent": "claude"}])
    brain = Brain.open(repo)
    root = str(Path(repo).resolve())
    runs = [brain.add_run(task_id=i, agent="codex", kind="build", prompt="x", actor="hermes") for i in (1, 2, 3)]
    for r in runs:
        brain.update_run(r["id"], status="running")
    brief = lambda r, t: {"run": r["ref"], "kind": "build", "agent": "codex", "task": t, "title": t, "status": "done", "task_status": "built", "summary": "ok"}
    p._bind_run(root, runs[0]["ref"])
    p._bind_run(root, runs[1]["ref"])
    brain.update_run(runs[0]["id"], status="done")
    p._notify(root, brief(runs[0], "T1"))
    assert ctx.injected == []  # R2 from the same chat is still running: hold the notice
    brain.update_run(runs[1]["id"], status="done")
    p._notify(root, brief(runs[1], "T2"))
    assert len(ctx.injected) == 1 and "2 runs finished" in ctx.injected[0]
    p._bind_run(root, runs[2]["ref"])
    p._waiting[(root, runs[2]["ref"])] = 1  # a thoth_wait is about to report R3 itself
    brain.update_run(runs[2]["id"], status="done")
    p._notify(root, brief(runs[2], "T3"))
    p._notify(root, brief(runs[2], "T3"))  # and a run started from the Thoth page (never bound) wakes nobody
    assert len(ctx.injected) == 1


def test_preview_serves_byte_ranges_for_media(tmp_path):
    import urllib.request
    from thoth_core import preview
    (tmp_path / "clip.mp4").write_bytes(bytes(range(256)) * 4)
    try:
        port = preview.serve(tmp_path)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/clip.mp4", headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 206 and r.headers["Content-Range"] == "bytes 10-19/1024" and r.read() == bytes(range(10, 20))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/clip.mp4", headers={"Range": "bytes=-4"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.read() == bytes([252, 253, 254, 255])
    finally:
        preview.stop_all()


def test_reach_shows_who_has_the_newest_work(repo):
    import time as _time
    from thoth_core import gitops
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "API", "agent": "codex"}, {"title": "UI", "agent": "claude", "depends_on": [1]},
                         {"title": "Docs", "agent": "claude", "depends_on": [1]}])
    brain = Brain.open(repo)

    def version(task_id, files, msg):
        task = brain.task(task_id)
        wt = Path(task["worktree"])
        before = gitops.head(wt)
        for name, text in files.items():
            (wt / name).write_text(text)
        sha = gitops.commit_all(wt, msg)
        run = brain.add_run(task_id=task_id, agent=task["agent"], kind="build", prompt="x", actor="hermes")
        brain.update_run(run["id"], status="done", commit_before=before, commit_after=sha)
        brain.update_task(task_id, status="built", head_commit=sha)
        return sha

    svc._prepare_worktree(brain, brain.task(1))
    version(1, {"api.py": "v = 1\n"}, "api v1")
    svc._prepare_worktree(brain, brain.task(2))
    version(2, {"ui.js": "ui\n"}, "ui on api v1")
    assert [(e["agent"], e["task"], e["state"]) for e in svc.reach(brain)["T1"]] == [("claude", "T2", "ok")]
    version(1, {"api.py": "v = 2\n"}, "api v2")          # Codex ships a newer API; Claude's UI is still on v1
    t1 = svc.reach(brain)["T1"][0]
    assert t1["agent"] == "claude" and t1["state"] == "behind" and t1["have"] == 1 and t1["latest"] == 2
    svc.sync(str(repo), "T2")                            # Update brings v2 into the UI task
    assert svc.reach(brain)["T1"][0]["state"] == "ok"  # T3 has not started: it will start from v2, so nothing is missing
    _time.sleep(0.01)
    brain.add_note(kind="decision", author="codex", body="Amounts are stored in cents", task_id=1)
    assert svc.reach(brain)["T1"][0]["state"] == "notyet"  # Claude has open work and no run since that decision


def test_run_command_previews_the_app_in_a_clean_copy(repo):
    import urllib.request
    from thoth_core import gitops, preview
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo), run=f"{sys.executable} -m http.server {{port}} --bind 127.0.0.1")
    svc.plan(str(repo), [{"title": "CRM", "agent": "claude"}])
    brain = Brain.open(repo)
    wt = _built(svc, brain, "T1", {"index.html": "<h1>Tiny CRM</h1>\n"}, "page")
    try:
        r = svc.preview(str(repo), "T1")
        assert r["mode"] == "run" and b"Tiny CRM" in urllib.request.urlopen(r["url"], timeout=5).read()
        assert svc.preview(str(repo), "T1")["url"] == r["url"]   # same version: the running app is reused
        assert not gitops.is_dirty(wt)                            # it ran in an export, not in the agent's worktree
        assert (repo / ".thoth" / "previews").is_dir()
    finally:
        preview.stop_apps()


def test_waiting_banner_appears_and_clears_when_rejected(repo):
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    json.loads(ctx.tools["thoth_init"]({"project": str(repo)}, session_id="s1"))
    json.loads(ctx.tools["thoth_plan"]({"project": str(repo), "tasks": [{"title": "T", "agent": "codex"}]}, session_id="s1"))
    args = {"project": str(repo), "task": "T1"}
    ctx.hooks["pre_tool_call"](tool_name="thoth_merge", args=args, session_id="s1")
    waiting = json.loads(Brain.open(repo).meta()["pending_approval"])
    assert waiting["summary"].startswith("Merge T1") and waiting["session"] == "s1"
    ctx.hooks["post_tool_call"](tool_name="thoth_merge", args=args, status="blocked")   # the user said no
    assert Brain.open(repo).meta()["pending_approval"] == ""


def test_a_new_preview_stops_apps_left_by_an_earlier_process(repo):
    import subprocess as sp
    from thoth_core import preview
    previews = repo / ".thoth" / "previews" / "old-app"
    previews.mkdir(parents=True)
    orphan = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    (previews / ".thoth-run.pid").write_text(str(orphan.pid))
    preview._sweep_orphans(repo / ".thoth" / "previews")
    assert orphan.wait(5) is not None and not (previews / ".thoth-run.pid").exists()


def test_browser_reaches_the_cli_and_the_brief_but_never_a_review(repo):
    from thoth_core import browser as web
    from thoth_core.agents import ClaudeDriver, CodexDriver
    assert [web.normalize(v) for v in (None, "off", "Clean", "user's chrome", "my chrome")] == ["", "", "clean", "user", "user"]
    with pytest.raises(BrainError):
        web.normalize("firefox")
    clean, user = web.spec("clean"), web.spec("user")
    assert web.PACKAGE in clean.args and {"--headless", "--isolated", "--no-usage-statistics", "--redactNetworkHeaders"} <= set(clean.args)
    assert "--autoConnect" in user.args and "--headless" not in user.args and "--no-performance-crux" in user.args
    c = ClaudeDriver({}).command(kind="build", session_id="", mcp=None, browser=clean)
    assert json.loads(c[c.index("--mcp-config") + 1])["mcpServers"]["browser"]["args"] == clean.args
    assert "mcp__browser" in c[c.index("--allowedTools") + 1].split(",")
    assert "--mcp-config" not in ClaudeDriver({}).command(kind="review", session_id="", mcp=None, browser=clean)
    x = " ".join(CodexDriver({}).command(kind="build", session_id="", mcp=None, browser=user))
    assert "mcp_servers.browser.args=" in x and "--autoConnect" in x and 'mcp_servers.browser.default_tools_approval_mode="approve"' in x
    assert "mcp_servers.browser" not in " ".join(CodexDriver({}).command(kind="review", session_id="", mcp=None, browser=user))
    # Stored on the task and explained in its brief.
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    out = svc.plan(str(repo), [{"title": "Study the current site", "agent": "codex", "kind": "research", "browser": "clean"}, {"title": "Build", "agent": "claude"}])
    assert out["created"][0]["browser"] == "clean" and "browser" not in out["created"][1]
    brain = Brain.open(repo)
    task = {**brain.task(1), "branch": "thoth/T1-x", "base_ref": "main", "base_commit": "abc1234"}
    assert "fresh headless Chrome" in build_brief(brain, task, agent="codex", browser="clean")
    assert "## Web browser" not in build_brief(brain, task, agent="codex")
    mine = build_brief(brain, task, agent="codex", browser="user")
    assert "Open your own tab" in mine and "Only look" in mine and "close_page" in mine
    assert svc.update_task(str(repo), "T1", browser="user")["browser"] == "user"
    assert "browser" not in svc.update_task(str(repo), "T1", browser="off")


def test_your_chrome_must_be_open_before_anything_starts(repo, monkeypatch, tmp_path):
    import socket
    from thoth_core import browser as web
    port_file = tmp_path / "DevToolsActivePort"
    monkeypatch.setattr(web, "devtools_port_file", lambda: port_file)
    assert not web.user_chrome_open()  # the switch was never turned on
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_file.write_text(f"{srv.getsockname()[1]}\n/devtools/browser/abc\n")
    assert web.user_chrome_open()
    srv.close()
    assert not web.user_chrome_open()  # a stale file from a Chrome that has quit
    monkeypatch.setattr(web, "npx", lambda: "/opt/node/bin/npx")
    monkeypatch.setattr(web, "_node_ok", lambda folder: (True, ""))
    monkeypatch.setattr(web, "chrome_installed", lambda: True)
    assert web.ready("clean") == (True, "")
    monkeypatch.setattr(Thoth, "_driver", lambda self, agent: object())
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "Read my store's theme settings", "agent": "codex"}])
    with pytest.raises(BrainError, match="chrome://inspect/#remote-debugging"):
        svc.assign(str(repo), "T1", browser="user")
    assert Brain.open(repo).runs() == [] and Brain.open(repo).task(1)["browser"] == "user"  # nothing started; the choice is kept


def test_browser_runs_say_so_on_the_approval_card(repo, monkeypatch):
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)

    class FakeDriver:
        def available(self):
            return True, "/usr/local/bin/codex"

    monkeypatch.setattr(plugin, "driver_for", lambda agent, settings=None: FakeDriver())
    monkeypatch.setattr(plugin.web, "ready", lambda mode: (True, ""))
    json.loads(ctx.tools["thoth_init"]({"project": str(repo)}, session_id="s1"))
    json.loads(ctx.tools["thoth_plan"]({"project": str(repo), "tasks": [{"title": "Study the site", "agent": "codex", "browser": "clean"}]}, session_id="s1"))
    hook = ctx.hooks["pre_tool_call"]
    plain = hook(tool_name="thoth_assign", args={"project": str(repo), "task": "T1", "browser": "off"}, session_id="s1")
    assert "Chrome" not in plain["message"] and ":web-" not in plain["rule_key"]
    clean = hook(tool_name="thoth_assign", args={"project": str(repo), "task": "T1"}, session_id="s1")
    assert "fresh Chrome without your logins" in clean["message"] and ":web-clean:" in clean["rule_key"]
    mine = hook(tool_name="thoth_assign", args={"project": str(repo), "task": "T1", "browser": "user"}, session_id="s1")
    assert "your own Chrome" in mine["message"] and "titles of your other tabs" in mine["message"] and ":web-user:" in mine["rule_key"]
    monkeypatch.setattr(plugin.web, "ready", lambda mode: (False, "Your Chrome is not open to agents yet."))
    blocked = hook(tool_name="thoth_assign", args={"project": str(repo), "task": "T1", "browser": "user"}, session_id="s1")
    assert blocked["action"] == "block" and "not open to agents" in blocked["message"]


def test_npx_runs_with_the_node_next_to_it(tmp_path, monkeypatch):
    import os
    from thoth_core import browser as web
    node_bin = tmp_path / "hermes-node" / "bin"
    node_bin.mkdir(parents=True)
    (node_bin / "node").write_text("")
    (node_bin / "npx").write_text("")
    (tmp_path / "local-bin").mkdir()
    (tmp_path / "local-bin" / "npx").symlink_to(node_bin / "npx")
    assert web.node_dir(str(tmp_path / "local-bin" / "npx")) == str(node_bin)
    monkeypatch.setattr(web, "npx", lambda: str(tmp_path / "local-bin" / "npx"))
    env = web.with_node({"PATH": os.pathsep.join(["/usr/bin", "/bin"])})
    assert env["PATH"].split(os.pathsep)[0] == str(node_bin) and web.with_node(env)["PATH"] == env["PATH"]


def test_thoth_plans_and_reviews_around_a_missing_agent(repo, tmp_path, monkeypatch):
    from thoth_core.agents import ClaudeDriver, CodexDriver
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ClaudeDriver, "signed_in", lambda self: (True, ""))
    svc = Thoth(mcp_launcher=None, settings={"claude_path": str(fake), "codex_path": str(tmp_path / "no-codex")})
    w = svc.workers(fresh=True)
    assert w["claude"]["ready"] and not w["codex"]["ready"] and w["codex"]["why"] == "not installed"
    svc.init(str(repo))
    out = svc.plan(str(repo), [{"title": "UI", "agent": "claude"}, {"title": "Tests", "agent": "codex"}])
    assert out["warnings"] == ["T2 is for Codex, which is not installed on this machine: give it to another agent"]
    assert svc.default_reviewer("claude") == "claude"  # no Codex here: Claude reviews in a fresh session
    with pytest.raises(BrainError, match="isn't installed"):
        svc.assign(str(repo), "T2")
    # Signed out is reported by the CLI itself, never by reading its credentials.
    monkeypatch.setattr(CodexDriver, "_ask", lambda self, *a: (1, "Not logged in\n"))
    monkeypatch.setattr(ClaudeDriver, "_ask", lambda self, *a: (0, '{"loggedIn": false}'))
    assert CodexDriver({})._ask() and CodexDriver({}).signed_in() == (False, "run `codex login`")
    monkeypatch.undo()
    monkeypatch.setattr(ClaudeDriver, "_ask", lambda self, *a: (0, '{"loggedIn": false}'))
    assert ClaudeDriver({}).signed_in()[0] is False


def test_a_request_from_the_thoth_page_binds_its_chat_to_the_project(repo, monkeypatch):
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    json.loads(ctx.tools["thoth_init"]({"project": str(repo), "goal": "A tiny CRM"}, session_id="s0"))
    hook = ctx.hooks["pre_llm_call"]
    instance = hook.__self__
    monkeypatch.setattr(instance, "_known_projects", lambda: [str(repo)])
    monkeypatch.setattr(instance, "session_dir", lambda: "")
    out = hook(session_id="chat-9", is_first_turn=True, user_message=f"下一步可以做什么？\n\n— via Thoth · {repo.name}")
    assert out and f"({repo})" in out["context"] and "few short lines" in out["context"]
    assert instance.project_for({}, "chat-9") == str(repo)
    by_path = hook(session_id="chat-10", user_message=f"Status?\n\n— via Thoth · {repo}")
    assert by_path and "A tiny CRM" in by_path["context"]
    assert hook(session_id="chat-11", user_message="no tag here") is None or "chat-11" not in instance._session_projects


def test_each_project_keeps_one_chat_across_restarts(tmp_path):
    from thoth_core import chats
    chats.bind(tmp_path, "stored-1", "/p/crm", primary=True)
    chats.bind(tmp_path, "runtime-1", "/p/crm")
    chats.bind(tmp_path, "stored-2", "/p/crm")  # another chat that talked about it does not take over
    assert chats.project_of(tmp_path, "nope", "runtime-1") == "/p/crm"
    assert chats.chat_of(tmp_path, "/p/crm") == "stored-1"
    chats.bind(tmp_path, "stored-3", "/p/crm", primary=True)
    assert chats.chat_of(tmp_path, "/p/crm") == "stored-3" and chats.project_of(tmp_path, "") == ""
    for i in range(chats.MAX_SESSIONS + 20):
        chats.bind(tmp_path, f"s{i}", "/p/other")
    assert len(chats.load(tmp_path)["sessions"]) == chats.MAX_SESSIONS


def test_the_plugin_remembers_which_chat_talks_about_which_project(repo, tmp_path, monkeypatch):
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    json.loads(ctx.tools["thoth_init"]({"project": str(repo), "goal": "g"}, session_id="s0"))
    hook = ctx.hooks["pre_llm_call"]
    inst = hook.__self__
    monkeypatch.setattr(type(inst), "_data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(inst, "_known_projects", lambda: [str(repo)])
    monkeypatch.setattr(inst, "session_dir", lambda: "")
    hook(session_id="chat-1", user_message=f"Status?\n\n— via Thoth · {repo.name}")
    from thoth_core import chats
    assert chats.project_of(tmp_path, "chat-1") == str(repo)
    inst._session_projects.clear()  # as after a restart: the chat still knows its project
    assert inst.project_for({}, "chat-1") == str(repo)


def test_thoth_lists_its_projects_most_recent_first_and_can_forget_one(tmp_path):
    from thoth_core import projects as registry
    registry.remember(tmp_path, "/p/a")
    registry.remember(tmp_path, "/p/b")
    registry.remember(tmp_path, "/p/a")
    assert registry.load(tmp_path) == ["/p/a", "/p/b"]
    assert registry.forget(tmp_path, "/p/b") and not registry.forget(tmp_path, "/p/b")
    assert registry.load(tmp_path) == ["/p/a"]
    for i in range(registry.MAX_PROJECTS + 5):
        registry.remember(tmp_path, f"/p/{i}")
    assert len(registry.load(tmp_path)) == registry.MAX_PROJECTS


def test_the_page_shows_each_projects_progress_and_removes_one_without_touching_it(repo):
    pytest.importorskip("fastapi")
    import asyncio
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    json.loads(ctx.tools["thoth_init"]({"project": str(repo), "goal": "A tiny CRM"}, session_id="s0"))
    Thoth(mcp_launcher=None).plan(str(repo), [{"title": "Build UI", "agent": "claude"}, {"title": "Write tests", "agent": "codex"}])
    spec = importlib.util.spec_from_file_location("thoth_dash_api_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    listed = asyncio.run(api.projects())["projects"]
    assert [p["path"] for p in listed] == [str(repo)]
    assert (listed[0]["total"], listed[0]["merged"], listed[0]["waiting"], listed[0]["working"]) == (2, 0, 0, 0)
    asyncio.run(api.action({"action": "forget_project", "project": str(repo)}))
    assert asyncio.run(api.projects())["projects"] == []
    assert (repo / ".thoth" / "brain.db").is_file()  # off the list only: the brain stays


def test_a_chat_that_names_a_project_is_about_that_project(repo, tmp_path, monkeypatch):
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    json.loads(ctx.tools["thoth_init"]({"project": str(repo), "goal": "A tiny CRM"}, session_id="s0"))
    hook = ctx.hooks["pre_llm_call"]
    inst = hook.__self__
    other = tmp_path / "ab"  # a short, plain name never counts as a mention
    monkeypatch.setattr(inst, "_known_projects", lambda: [str(repo), str(other)])
    monkeypatch.setattr(inst, "session_dir", lambda: "")
    out = hook(session_id="chat-n1", user_message=f"Quick update on {repo.name.upper()}: what's left?")
    assert out and f"({repo})" in out["context"] and "few short lines" not in out["context"]
    assert inst.project_for({}, "chat-n1") == str(repo)
    assert hook(session_id="chat-n2", user_message=f"is {repo.name}x related?") is None  # part of a longer word
    assert hook(session_id="chat-n3", user_message="what about ab?") is None


def test_partial_runs_go_to_review_with_their_limits_and_blocked_ones_say_why(repo):
    from thoth_core.agents import RunState
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "Check the game in a browser", "agent": "codex"}, {"title": "Build the page", "agent": "claude"}])
    brain = Brain.open(repo)
    for tid in (1, 2):
        svc._prepare_worktree(brain, brain.task(tid))
        brain.update_task(tid, status="running")
    # finished the check, but its tools could not send real touch or held keys
    r1 = brain.add_run(task_id=1, agent="codex", kind="build", prompt="", actor="t")
    report = {"status": "partial", "summary": "Played it in a clean Chrome; no defects.", "limits": ["native touch input", "held arrow keys"]}
    svc._finalize(brain, r1["id"], RunState(final_text="THOTH_REPORT " + json.dumps(report)), 0, "")
    assert brain.task(1)["status"] == "built"
    assert "Not checked: native touch input" in [n["body"] for n in brain.notes(task_id=1, kinds=("risk",))]
    need = next(n for n in svc.needs(str(repo)) if n["task"] == "T1")
    assert need["kind"] == "review" and "couldn't check 2 things" in need["detail"]
    told = svc._run_brief(brain.run(r1["id"]), brain.task(1))
    assert told["outcome"] == "partial" and told["limits"] == report["limits"]
    # could not do the task at all: blocked, with its reason, not "needs another try"
    r2 = brain.add_run(task_id=2, agent="claude", kind="build", prompt="", actor="t")
    svc._finalize(brain, r2["id"], RunState(final_text='THOTH_REPORT {"status": "blocked", "summary": "The spec names a file that does not exist."}'), 0, "")
    assert brain.task(2)["status"] == "failed"
    need = next(n for n in svc.needs(str(repo)) if n["task"] == "T2")
    assert need["title"] == "T2 is blocked" and "does not exist" in need["detail"]
    # the brief offers the middle way
    assert "partial" in build_brief(brain, brain.task(1), agent="codex")


def test_briefs_size_the_work_and_say_what_the_sandbox_refuses(repo):
    # What agents lost time on in real runs: test harnesses nobody needed, refused commands, writes to /tmp,
    # launching Chrome without a browser, and fighting browser tools that have no touch input.
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [
        {"title": "Build the game", "agent": "claude"},
        {"title": "Make the sprites", "agent": "codex", "kind": "asset"},
        {"title": "Play it", "agent": "codex", "kind": "qa", "browser": "clean", "depends_on": [1, 2]},
    ])
    brain = Brain.open(repo)
    t = {i: {**brain.task(i), "branch": f"thoth/T{i}", "base_ref": "main", "base_commit": "abc1234"} for i in (1, 2, 3)}
    claude = build_brief(brain, t[1], agent="claude")
    assert "## How to work here" in claude and ".thoth/scratch/" in claude and "$TMPDIR included" in claude
    assert "T3 tests the whole product after you" in claude
    assert "no network" in claude and "don't launch Chrome" in claude
    assert "`sed -i`" in claude and "with Write and Edit" in claude
    codex = build_brief(brain, t[2], agent="codex")
    assert ".thoth/scratch/" in codex and "T3 tests the whole product" in codex
    assert "`sed -i`" not in codex and "$TMPDIR included" not in codex  # Codex's sandbox refuses neither
    qa = build_brief(brain, t[3], agent="codex", browser="clean")
    assert "a few screenshots" in qa and "no touch input" in qa
    assert "don't launch Chrome" not in qa and "tests the whole product" not in qa
    assert "## How to work here" not in build_brief(brain, t[1], agent="hermes", self_run=True)


def test_scratch_files_stay_out_of_the_commit_and_the_run_files(repo):
    from thoth_core import gitops
    from thoth_core.agents import RunState
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "Build", "agent": "claude"}])
    brain = Brain.open(repo)
    wt = Path(svc._prepare_worktree(brain, brain.task(1))["worktree"])
    (wt / ".thoth" / "scratch").mkdir(parents=True)
    (wt / ".thoth" / "scratch" / "check.py").write_text("print(1)\n")
    (wt / "game.js").write_text("// game\n")
    gitops.commit_all(wt, "thoth(claude): Build")
    assert gitops.git(wt, "show", "--name-only", "--format=", "HEAD").split() == ["game.js"]
    st = RunState(cwd=str(wt))
    st.saw_write(str(wt / ".thoth" / "scratch" / "check.py"))
    st.saw_write(".thoth/scratch/run.js")
    st.saw_write(str(wt / "game.js"))
    assert st.files()["written"] == ["game.js"]


def test_plan_view_counts_resets_and_pace():
    from thoth_core import plans
    now = 1_800_000_000
    snap = plans.from_codex({"rateLimits": {"planType": "plus",
                                            "primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": now + 3600},
                                            "secondary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": now + 86400}}}, now=now)
    assert snap["plan"] == "plus" and snap["limited_until"] == now + 86400
    v = plans.view({"codex": snap}, now=now)["codex"]
    assert v["level"] == "limited" and [w["label"] for w in v["windows"]] == ["5-hour", "Weekly"]
    assert v["windows"][0]["used"] == 0.4 and v["windows"][0]["elapsed"] == 0.8  # 4 of its 5 hours have passed
    later = plans.view({"codex": snap}, now=now + 2 * 86400)["codex"]  # both windows have reset since
    assert later["level"] == "ok" and all(w["used"] == 0 and w["rolled"] and w["resets_at"] is None for w in later["windows"])
    claude = plans.from_claude({"status": "allowed", "rateLimitType": "seven_day", "isUsingOverage": False,
                                "unifiedWindows": {"seven_day": {"utilization": 0.9, "resetsAt": now + 86400, "status": "allowed"}}}, now=now)
    cv = plans.view({"claude": claude}, now=now)["claude"]
    assert cv["level"] == "low" and cv["windows"][0]["label"] == "Weekly" and cv["limited_until"] is None
    line = plans.hint(plans.view({"claude": claude, "codex": snap}, now=now))
    assert "Claude Code has used 90% of its weekly limit" in line and "Codex has hit its plan limit" in line
    assert "give Codex's tasks and reviews to Claude Code" in line
    assert "big new tasks to Codex" not in line  # never steer work to an agent whose plan is out
    fresh = plans.from_codex({"rateLimits": {"primary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": now + 86400}}}, now=now)
    assert "give big new tasks to Codex rather than Claude Code" in plans.hint(plans.view({"claude": claude, "codex": fresh}, now=now))


def test_agents_report_when_their_plan_runs_out():
    from thoth_core.agents import ClaudeDriver, CodexDriver, RunState
    st = RunState()
    CodexDriver().parse(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Upgrade to Pro or try again in 2 days 4 hours."}}), st)
    assert st.plan_limit and st.is_error
    other = RunState()
    CodexDriver().parse(json.dumps({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}}), other)
    assert other.is_error and not other.plan_limit
    st = RunState()
    ClaudeDriver().parse(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1800003600"}), st)
    assert st.plan_limit and st.plan_reset == 1800003600


def test_an_agent_that_runs_out_of_plan_hands_its_task_to_the_other(repo):
    import time
    from thoth_core import plans
    from thoth_core.agents import ClaudeDriver, RunState
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    svc.plan(str(repo), [{"title": "Build the page", "agent": "claude"}])
    brain = Brain.open(repo)
    svc._prepare_worktree(brain, brain.task(1))
    brain.update_task(1, status="running")
    run = brain.add_run(task_id=1, agent="claude", kind="build", prompt="", actor="t")
    back = int(time.time()) + 3 * 3600
    st, drv = RunState(), ClaudeDriver()
    drv.parse(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": back, "rateLimitType": "five_hour",
                                                                          "unifiedWindows": {"five_hour": {"utilization": 0.97, "resetsAt": back, "status": "allowed"}}}}), st)
    drv.parse(json.dumps({"type": "result", "is_error": True, "result": f"Claude AI usage limit reached|{back}"}), st)
    assert st.plan_limit and st.plan_reset == back and st.plan["windows"][0]["label"] == "5-hour"
    svc._finalize(brain, run["id"], st, 1, "")
    assert brain.run(run["id"])["error"].startswith("plan limit") and brain.task(1)["status"] == "failed"
    assert any("Codex can carry on" in n["body"] for n in brain.notes(task_id=1, kinds=("handoff",)))
    need = next(n for n in svc.needs(str(repo)) if n["task"] == "T1")
    assert need["kind"] == "relay" and need["agent"] == "codex" and need["title"] == "Claude Code ran out of plan on T1"
    assert "carry on from Claude Code's work" in need["detail"]
    svc.workers = lambda fresh=False: {"claude": {"ready": True}, "codex": {"ready": True}}
    assert svc.default_reviewer("codex") == "codex"  # Claude's plan is out: Codex reviews in a fresh session
    with pytest.raises(BrainError, match="Codex can take T1 instead"):
        svc.assign(str(repo), "T1", agent="claude")
    assert "agent=codex" in svc._run_brief(brain.run(run["id"]), brain.task(1))["next"]
    view = svc.plan_usage(refresh=False)["claude"]
    assert view["level"] == "limited" and view["limited_until"] == back and view["windows"][0]["used"] == 0.97
    assert svc.snapshot(str(repo))["plans"]["claude"]["limited_until"] == back
    assert "Claude Code has hit its plan limit" in svc.status(str(repo))["plans"]
    off = Thoth(mcp_launcher=None, settings={"plan_usage": False})  # turned off: no numbers, no guard
    assert "plans" not in off.snapshot(str(repo)) and off.plan_usage() == {}
    off._check_plan("claude", "T1")
    assert plans.limited_until(plans.load(svc.data_dir), "claude") == back


def test_codex_limits_come_from_its_app_server(tmp_path):
    import textwrap
    reset = 1_900_000_000
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f"""
        import json, sys
        if sys.argv[1:] != ["app-server"]:
            sys.exit(2)
        for line in sys.stdin:
            m = json.loads(line)
            if m.get("method") == "initialize":
                print(json.dumps({{"id": m["id"], "result": {{"userAgent": "fake"}}}}), flush=True)
                print(json.dumps({{"method": "account/updated", "params": {{}}}}), flush=True)
            elif m.get("method") == "account/rateLimits/read":
                print(json.dumps({{"id": m["id"], "result": {{"ordinaryUsageAllowed": True, "rateLimits": {{"planType": "pro",
                    "primary": {{"usedPercent": 92, "windowDurationMins": 300, "resetsAt": {reset}}},
                    "secondary": {{"usedPercent": 61, "windowDurationMins": 10080, "resetsAt": {reset + 86400}}}}}}}}}), flush=True)
    """))
    fake.chmod(0o755)
    svc = Thoth(mcp_launcher=None, settings={"codex_path": str(fake)})
    snap = svc.refresh_codex_plan()
    assert snap["plan"] == "pro" and [(w["label"], w["used"]) for w in snap["windows"]] == [("5-hour", 0.92), ("Weekly", 0.61)]
    from thoth_core import plans
    assert plans.load(svc.data_dir)["codex"]["source"] == "codex"
    assert plans.read_codex(str(tmp_path / "missing")) is None


def test_download_zips_what_is_merged_and_never_env_files(repo):
    import zipfile
    svc = Thoth(mcp_launcher=None)
    svc.init(str(repo))
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "site").mkdir()
    (repo / "site" / "index.html").write_text("<h1>hi</h1>\n")
    (repo / "site" / ".env.local").write_text("X=1\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "site")
    out = repo.parent / (repo.name + "-downloads")
    out.mkdir()
    first = svc.export(str(repo), to=out)
    names = sorted(i.filename for i in zipfile.ZipFile(first["path"]).infolist() if not i.is_dir())
    assert names == [f"{repo.name}/app.py", f"{repo.name}/site/index.html"]  # committed files, in the project's folder
    assert first["files"] == 2 and first["name"] == f"{repo.name}.zip" and first["branch"] == "main"
    assert svc.export(str(repo), to=out)["name"] == f"{repo.name} 2.zip"  # never overwrites a copy you kept
    with pytest.raises(BrainError):
        svc.export(str(repo), to=out / "missing")


def test_an_approval_holds_only_for_the_plan_it_showed(repo):
    # "Invalidate pending approvals whenever the shared plan changes" (@escship): a yes covers the plan as the
    # card showed it. If the task changes while the card waits, nothing starts and Hermes asks again.
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    p = str(repo)
    ctx.tools["thoth_init"]({"project": p, "goal": "A landing page"}, session_id="s1")
    ctx.tools["thoth_plan"]({"project": p, "tasks": [{"title": "Build the page", "agent": "codex", "detail": "Hero and signup"}]}, session_id="s1")
    merge = {"project": p, "task": "T1"}
    assert ctx.hooks["pre_tool_call"](tool_name="thoth_merge", args=merge, session_id="s1")["action"] == "approve"
    Thoth(mcp_launcher=None).update_task(p, "T1", detail="Hero, pricing table and signup")  # edited while the card waits
    out = json.loads(ctx.tools["thoth_merge"](merge, session_id="s1"))
    assert not out["ok"] and out["plan_changed"] == ["T1's instructions changed"] and "Nothing was started" in out["error"]
    assert not Brain.open(repo).meta().get("pending_approval")  # the page no longer says Hermes waits for an OK
    # Asked again, the same call goes through the gate (and on to merge, which says T1 isn't built yet).
    ctx.hooks["pre_tool_call"](tool_name="thoth_merge", args=merge, session_id="s1")
    again = json.loads(ctx.tools["thoth_merge"](merge, session_id="s1"))
    assert not again["ok"] and "plan_changed" not in again and "BLOCKED" not in again["error"]
    # A changed goal counts too.
    ctx.hooks["pre_tool_call"](tool_name="thoth_merge", args=merge, session_id="s1")
    Brain.open(repo).set_meta("goal", "A landing page in French")
    assert json.loads(ctx.tools["thoth_merge"](merge, session_id="s1"))["plan_changed"] == ["the project goal changed"]


def _fake_hands(svc, brain, monkeypatch):
    """Stand-ins for starting, reviewing and merging: they only move the task, and remember what was asked."""
    calls = []

    def assign(project, ref, agent="", **_):
        calls.append(("assign", ref, agent))
        brain.update_task(int(ref[1:]), status="running", agent=agent)
        return {}

    def review(project, ref, reviewer="", **_):
        calls.append(("review", ref, reviewer))
        brain.update_task(int(ref[1:]), status="reviewing")
        return {}

    def merge(project, ref, **_):
        calls.append(("merge", ref))
        brain.update_task(int(ref[1:]), status="merged")
        return {}

    monkeypatch.setattr(svc, "assign", assign)
    monkeypatch.setattr(svc, "review", review)
    monkeypatch.setattr(svc, "merge", merge)
    monkeypatch.setattr(svc, "workers", lambda fresh=False: {"claude": {"ready": True}, "codex": {"ready": True}})
    return calls


def test_autopilot_runs_the_plan_and_stops_only_for_what_a_person_decides(repo, monkeypatch):
    from thoth_core import autopilot
    svc, p = Thoth(mcp_launcher=None), str(repo)
    svc.init(p, goal="A landing page")
    svc.plan(p, [{"title": "Make the artwork", "agent": "codex", "kind": "asset"},
                 {"title": "Build the page", "agent": "claude", "depends_on": [1]},
                 {"title": "Check it", "agent": "codex", "kind": "qa", "depends_on": [2]}])
    brain = Brain.open(repo)
    calls = _fake_hands(svc, brain, monkeypatch)
    svc.autopilot_start(p, hours=2, session="s1")
    step = lambda: svc.autopilot_step(p)  # noqa: E731
    assert step() == [] and calls == [("assign", "T1", "codex")]  # T2 waits until T1 is merged
    brain.update_task(1, status="built")
    assert [n["task"] for n in svc.needs(p)] == []  # autopilot reviews it itself: nothing to ask the user
    step()
    assert calls[-1] == ("review", "T1", "claude")  # the other agent reviews
    brain.update_task(1, status="approved")
    step()
    assert calls[-1] == ("merge", "T1")
    step()
    assert calls[-1] == ("assign", "T2", "claude")
    # Claude's plan runs out on T2: Codex carries on from its work, under the approved rules (not a plan change).
    run = brain.add_run(task_id=2, agent="claude", kind="build", prompt="", actor="t")
    brain.update_run(run["id"], status="failed", error="plan limit: Claude Code hit its plan limit; it resets later")
    brain.update_task(2, status="failed")
    step()
    assert calls[-1] == ("assign", "T2", "codex") and autopilot.load(brain)["state"] == "running"
    brain.update_task(2, status="approved")
    step()
    step()
    assert ("merge", "T2") in calls and calls[-1] == ("assign", "T3", "codex")
    # Codex is stuck on T3 and asks a question: autopilot asks the user through the chat, once.
    run = brain.add_run(task_id=3, agent="codex", kind="build", prompt="", actor="t")
    brain.update_run(run["id"], status="done", summary="Which browsers must it support?", report={"status": "blocked"})
    brain.update_task(3, status="failed")
    told = step()
    assert len(told) == 1 and "T3" in told[0] and "stuck" in told[0] and "Which browsers" in told[0]
    assert step() == [] and autopilot.view(brain)["held"][0]["task"] == "T3"
    assert [n["task"] for n in svc.needs(p)] == ["T3"]  # what it waits for the user on still shows
    # Answered and finished: everything is merged, and autopilot says the result is ready.
    brain.update_task(3, status="approved")
    step()
    told = step()
    assert autopilot.load(brain)["state"] == "done" and "finished" in told[0]
    assert "Autopilot" not in (svc.status(p).get("autopilot") or "")  # nothing left to say once it is done


def test_autopilot_pauses_on_a_plan_change_and_keeps_its_limits(repo, monkeypatch):
    import time
    from thoth_core import autopilot, plans
    svc, p = Thoth(mcp_launcher=None), str(repo)
    svc.init(p, goal="g")
    svc.plan(p, [{"title": "A", "agent": "codex"}, {"title": "B", "agent": "claude"}])
    brain = Brain.open(repo)
    calls = _fake_hands(svc, brain, monkeypatch)
    svc.autopilot_start(p, hours=1, session="s1")
    svc.update_task(p, "T2", detail="B, in French")  # the plan changed after the user approved it
    told = svc.autopilot_step(p)
    assert autopilot.load(brain)["state"] == "paused" and calls == [] and "T2's instructions changed" in told[0]
    assert "thoth_autopilot" in svc.status(p)["autopilot"]
    # Approved again; the time limit passes before anything else happens.
    svc.autopilot_start(p, hours=1, session="s1")
    ap = autopilot.load(brain)
    ap["until"] = time.time() - 1
    autopilot.save(brain, ap)
    told = svc.autopilot_step(p)
    assert autopilot.load(brain)["state"] == "stopped" and "limit" in told[0] and calls == []
    # The user keeps 20% of each plan: Codex at 85% means its task goes to Claude, and with both at it, it waits.
    plans.save(svc.data_dir, "codex", plans.from_codex({"rateLimits": {"primary": {"usedPercent": 85, "windowDurationMins": 10080, "resetsAt": int(time.time()) + 3600}}}))
    svc.autopilot_start(p, hours=1, session="s1")
    svc.autopilot_step(p)
    assert calls == [("assign", "T1", "claude"), ("assign", "T2", "claude")] and autopilot.load(brain)["state"] == "running"
    plans.save(svc.data_dir, "claude", plans.from_claude({"status": "allowed", "unifiedWindows": {"seven_day": {"utilization": 0.9, "resetsAt": int(time.time()) + 3600}}}))
    brain.update_task(2, status="todo")  # say T2 had not started yet
    told = svc.autopilot_step(p)
    assert "waiting" in told[0] and "keep 20%" in told[0] and len(calls) == 2 and svc.autopilot_step(p) == []
    svc.autopilot_stop(p)
    assert autopilot.view(brain) is None and svc.autopilot_step(p) == []


def test_a_plan_card_shows_the_whole_plan_and_lapses_when_it_changes(repo):
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    p = str(repo)
    ctx.tools["thoth_init"]({"project": p, "goal": "A landing page"}, session_id="s1")
    ctx.tools["thoth_plan"]({"project": p, "tasks": [{"title": "Make the artwork", "agent": "codex"},
                                                     {"title": "Build the page", "agent": "claude", "depends_on": [1]}]}, session_id="s1")
    start = {"project": p, "action": "start"}
    assert not json.loads(ctx.tools["thoth_autopilot"](start, session_id="s1"))["ok"]  # never without the card
    card = ctx.hooks["pre_tool_call"](tool_name="thoth_autopilot", args=start, session_id="s1")
    assert card["action"] == "approve" and card["rule_key"].startswith("thoth:autopilot:")
    assert "• T1 Codex · Make the artwork" in card["message"] and "after T1" in card["message"] and "merges what passes review" in card["message"]
    assert ctx.hooks["pre_tool_call"](tool_name="thoth_autopilot", args={"project": p, "action": "status"}, session_id="s1") is None
    Thoth(mcp_launcher=None).plan(p, [{"title": "Write the FAQ", "agent": "claude"}])  # added while the card waits
    out = json.loads(ctx.tools["thoth_autopilot"](start, session_id="s1"))
    assert not out["ok"] and out["plan_changed"] == ["T3 “Write the FAQ” was added"]
    card2 = ctx.hooks["pre_tool_call"](tool_name="thoth_autopilot", args=start, session_id="s1")
    assert card2["rule_key"] != card["rule_key"]  # a new plan is a new card: "always allow" never carries over
    ran = json.loads(ctx.tools["thoth_autopilot"](start, session_id="s1"))
    assert ran["ok"] and ran["autopilot"] == "running" and ran["tasks"] == ["T1", "T2", "T3"]
    assert json.loads(ctx.tools["thoth_autopilot"]({"project": p, "action": "pause"}, session_id="s1"))["autopilot"] == "paused"


def test_the_conductor_drives_a_plan_to_the_end_and_tells_the_chat(repo, monkeypatch):
    # The whole loop as Hermes runs it: the conductor thread's loop, the registry, the lock, and the message at the
    # end. Agents are stand-ins that finish at once (a real run would spend the user's plans).
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    p = str(repo)
    ctx.tools["thoth_init"]({"project": p, "goal": "A landing page"}, session_id="s1")
    ctx.tools["thoth_plan"]({"project": p, "tasks": [{"title": "Make the artwork", "agent": "codex"},
                                                     {"title": "Build the page", "agent": "claude", "depends_on": [1]}]}, session_id="s1")
    brain = Brain.open(repo)
    calls = []

    def assign(self, project, ref, agent="", **_):
        calls.append(("assign", ref, agent))
        Brain.open(project).update_task(int(ref[1:]), status="built", agent=agent)  # finishes at once
        return {}

    def review(self, project, ref, reviewer="", **_):
        calls.append(("review", ref, reviewer))
        Brain.open(project).update_task(int(ref[1:]), status="approved")
        return {}

    def merge(self, project, ref, **_):
        calls.append(("merge", ref))
        Brain.open(project).update_task(int(ref[1:]), status="merged")
        return {}

    plugin_thoth = sys.modules["thoth_plugin.thoth_core.service"].Thoth  # the plugin's own copy of the service
    monkeypatch.setattr(plugin_thoth, "assign", assign)
    monkeypatch.setattr(plugin_thoth, "review", review)
    monkeypatch.setattr(plugin_thoth, "merge", merge)
    monkeypatch.setattr(plugin_thoth, "workers", lambda self, fresh=False: {"claude": {"ready": True}, "codex": {"ready": True}})
    monkeypatch.setattr(sys.modules["thoth_plugin"], "CONDUCTOR_TICK", 0.01)
    monkeypatch.setattr(sys.modules["thoth_plugin"], "_keep_awake", lambda: None)
    ctx.hooks["pre_tool_call"](tool_name="thoth_autopilot", args={"project": p, "action": "start"}, session_id="s1")
    assert json.loads(ctx.tools["thoth_autopilot"]({"project": p, "action": "start"}, session_id="s1"))["autopilot"] == "running"
    plugin._plugin._drive()  # what the conductor thread runs; it returns once no autopilot is running
    assert calls == [("assign", "T1", "codex"), ("review", "T1", "claude"), ("merge", "T1"),
                     ("assign", "T2", "claude"), ("review", "T2", "codex"), ("merge", "T2")]
    assert [m for m in ctx.injected if m.startswith("[Thoth autopilot")] == [
        f"[Thoth autopilot · {repo.name}] Autopilot finished: all 2 tasks are merged into main. "
        "Tell the user their result is ready, in a few lines (thoth_preview gives them a link)."]
    assert json.loads(ctx.tools["thoth_autopilot"]({"project": p, "action": "status"}, session_id="s1"))["autopilot"]["state"] == "done"


def test_offline_tests_can_never_start_a_real_agent(tmp_path):
    # Every test runs with THOTH_NO_AGENTS: even a test that forgets a stand-in cannot spend anyone's plan.
    from thoth_core.runner import RunManager
    with pytest.raises(RuntimeError, match="THOTH_NO_AGENTS"):
        RunManager().start(key="k", run_id=1, driver=None, cmd=["true"], prompt="", cwd=tmp_path, env={}, log_path=tmp_path / "R1.jsonl",
                           timeout=1, on_progress=lambda *a: None, on_exit=lambda *a: None)


def test_autopilot_runs_up_to_8_hours_by_default_and_24_at_most():
    svc = Thoth(mcp_launcher=None)
    assert svc._autopilot_hours() == 8 and svc._autopilot_hours(30) == 24 and svc._autopilot_hours(3) == 3
    assert Thoth(mcp_launcher=None, settings={"autopilot_hours": 12})._autopilot_hours() == 12
