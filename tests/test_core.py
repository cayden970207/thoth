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
    assert len(ctx.tools) == 12 and "coordinate" in ctx.skills
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
