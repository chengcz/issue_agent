import asyncio
from pathlib import Path

from coding_agent_orchestrator.config import load_config
from coding_agent_orchestrator.models import Issue, TaskStatus
from coding_agent_orchestrator.orchestrator import Orchestrator
from coding_agent_orchestrator.process import shell
from coding_agent_orchestrator.state import StateStore
from coding_agent_orchestrator.workspace import slugify


def test_slugify_is_branch_safe():
    assert slugify("Add Regimen / API!") == "add-regimen-api"


def test_state_claim_is_idempotent(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(42, "Task", "Body")
    assert state.claim(issue, "codex") is True
    assert state.claim(issue, "claude") is False
    state.update(42, TaskStatus.FAILED, last_error="boom")
    assert state.claim(issue, "claude") is True


def test_state_recovery_makes_interrupted_task_claimable(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(7, "Restart", "Body")
    assert state.claim(issue, "codex")
    state.update(7, TaskStatus.TESTING)
    assert state.recover_interrupted() == 1
    assert state.claim(issue, "codex")


def test_shell_uses_platform_shell(tmp_path: Path):
    result = asyncio.run(shell("echo available", cwd=tmp_path))
    assert "available" in result.stdout


def test_config_and_label_routing(tmp_path: Path):
    config_file = tmp_path / "autocode.toml"
    config_file.write_text('''
[runtime]
repo = "."
state_db = "state.db"
default_agent = "codex"
[github]
repo = "a/b"
[agents.codex]
command = "codex exec -"
[agents.claude]
command = "claude -p"
''')
    app = Orchestrator(load_config(config_file))
    assert app.select_agent(Issue(1, "x", "", ("agent:claude",))) == "claude"
