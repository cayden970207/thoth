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
    assert len(ctx.tools) == 17 and "coordinate" in ctx.skills
    init = json.loads(ctx.tools["thoth_init"]({"project": str(repo), "goal": "g"}, session_id="s1"))
    assert init["ok"]
    json.loads(ctx.tools["thoth_plan"]({"project": str(repo), "tasks": [{"title": "T", "agent": "codex"}]}, session_id="s1"))
    # Without the pre_tool_call hook (no approval) the handler refuses to start an agent.
    blocked = json.loads(ctx.tools["thoth_merge"]({"project": str(repo), "task": "T1"}, session_id="s1"))
    assert not blocked["ok"] and "approval" in blocked["error"]
    # The hook escalates to Hermes approval with a readable summary and a scoped rule key.
    decision = ctx.hooks["pre_tool_call"](tool_name="thoth_merge", args={"project": str(repo), "task": "T1"}, session_id="s1")
    assert decision["action"] == "approve" and "merge T1" in decision["message"]
    assert decision["rule_key"].startswith("thoth:merge:")
    # Ungated tools pass straight through.
    assert ctx.hooks["pre_tool_call"](tool_name="thoth_status", args={}, session_id="s1") is None
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
