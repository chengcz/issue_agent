import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from issue_agent.cli import format_status, parser
from issue_agent.config import load_config
from issue_agent.github import GitHub
from issue_agent.models import Issue, PlanTask, TaskStatus
from issue_agent.orchestrator import Orchestrator
from issue_agent.process import shell
from issue_agent.state import StateStore
from issue_agent.workspace import slugify


def test_slugify_is_branch_safe():
    assert slugify("Add Regimen / API!") == "add-regimen-api"


def test_state_claim_is_idempotent(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(42, "Task", "Body")
    assert state.claim(issue, "codex") is True
    assert state.claim(issue, "claude") is False
    state.update(42, TaskStatus.FAILED, last_error="boom")
    assert state.claim(issue, "claude") is True


def test_unlabeled_issue_is_planned_only_once(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(8, "Needs a plan", "Vague request")
    assert state.claim_for_planning(issue, "planner") is True
    state.save_plan(8, [PlanTask("Clarify implementation", "Acceptance: reviewed")])
    state.update(8, TaskStatus.PLANNED)
    assert state.claim_for_planning(issue, "planner") is False


def test_claim_gates_failed_issue_by_attempt_budget(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(1, "Task", "Body")
    state.claim(issue, "codex")
    state.record_failure(1, TaskStatus.FAILED, "boom")
    # one failure stays below the budget -> reclaimable
    assert state.claim(issue, "codex", max_attempts=2) is True
    state.record_failure(1, TaskStatus.FAILED, "boom")
    # budget exhausted -> parked, needs a human reset
    assert state.claim(issue, "codex", max_attempts=2) is False


def test_claim_allows_blocked_under_attempt_budget(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(1, "Task", "Body")
    state.claim(issue, "codex")
    state.record_failure(1, TaskStatus.BLOCKED, "crash")
    assert state.claim(issue, "codex", max_attempts=2) is True
    state.record_failure(1, TaskStatus.BLOCKED, "crash")
    assert state.claim(issue, "codex", max_attempts=2) is False


def test_record_failure_increments_failures_and_sets_status(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(1, "Task", "Body"), "codex")
    assert state.record_failure(1, TaskStatus.FAILED, "boom") == 1
    assert state.record_failure(1, TaskStatus.BLOCKED, "crash") == 2
    row = state.rows()[0]
    assert row["status"] == str(TaskStatus.BLOCKED)
    assert row["last_error"] == "crash"
    assert row["failures"] == 2


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
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text('''
[runtime]
repo = "."
state_db = "state.db"
default_agent = "codex"
auto_plan_unlabeled = true
auto_plan_limit = 7
[github]
repo = "a/b"
[agents.codex]
command = "codex exec -"
[agents.claude]
command = "claude -p"
''')
    app = Orchestrator(load_config(config_file))
    assert app.select_agent(Issue(1, "x", "", ("agent:claude",))) == "claude"
    assert app.config.auto_plan_unlabeled is True
    assert app.config.auto_plan_limit == 7


def test_github_unlabeled_issues_filters_any_labeled_issue(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(
        return_value='''[
            {"number": 1, "title": "Plan me", "body": "", "labels": [], "url": "u1"},
            {"number": 2, "title": "Skip me", "body": "", "labels": [{"name": "bug"}], "url": "u2"}
        ]'''
    )

    issues = asyncio.run(github.unlabeled_issues())

    assert [issue.number for issue in issues] == [1]
    assert "--label" not in github._gh.await_args.args


def test_status_rows_include_current_plan_task_and_filter_active(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(1, "Active issue", "Body"), "codex")
    state.save_plan(1, [PlanTask("Implement status", "Description")])
    state.update(1, TaskStatus.CODING, current_seq=0)
    state.claim(Issue(2, "Finished issue", "Body"), "codex")
    state.update(2, TaskStatus.DONE)
    state.claim(Issue(3, "Awaiting approval", "Body"), "codex")
    state.update(3, TaskStatus.PLANNED)

    rows = state.status_rows(active_only=True)

    assert [row["issue_number"] for row in rows] == [1]
    assert rows[0]["current_task"] == "Implement status"


def test_status_parser_and_human_format():
    args = parser().parse_args(["status", "--active", "--json"])
    assert args.active is True
    assert args.json is True
    output = format_status(
        [
            {
                "issue_number": 7,
                "status": "testing",
                "title": "Check CLI",
                "agent": "codex",
                "updated_at": "2026-08-28T12:34:56+00:00",
            }
        ]
    )
    assert "#7" in output
    assert "testing" in output
    assert "Check CLI" in output


def test_cli_uses_public_issue_agent_name():
    command = parser()
    assert command.prog == "issue-agent"
    assert command.parse_args(["status"]).config == "issue-agent.toml"
