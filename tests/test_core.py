import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from issue_agent.cli import format_status, parser
from issue_agent.config import load_config
from issue_agent.github import GitHub
from issue_agent.issue_log import IssueLog
from issue_agent.models import Issue, PlanTask, TaskStatus
from issue_agent.orchestrator import Orchestrator, failed_tests
from issue_agent.process import CommandError, Result, shell
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


def test_state_reset_makes_parked_issue_claimable_again(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(4, "Task", "Body")
    state.claim(issue, "codex")
    state.save_plan(4, [PlanTask("One", "D"), PlanTask("Two", "D")])
    state.update_plan_task(4, 0, status=TaskStatus.DONE, commit_hash="aaaa1111")
    state.update_plan_task(4, 1, status=TaskStatus.REVIEWING, last_error="stuck")
    state.record_failure(4, TaskStatus.BLOCKED, "database unavailable")
    state.record_failure(4, TaskStatus.BLOCKED, "database unavailable")
    # budget exhausted -> parked, claim refused until reset
    assert state.claim(issue, "codex", max_attempts=2) is False

    old = state.reset(4)

    assert old == str(TaskStatus.BLOCKED)
    row = state.rows()[0]
    assert row["status"] == str(TaskStatus.PENDING)
    assert row["failures"] == 0
    assert row["attempts"] == 0
    # DONE plan items survive; unfinished ones return to pending
    assert state.plan_task_statuses(4) == [TaskStatus.DONE, TaskStatus.PENDING]
    assert state.claim(issue, "codex", max_attempts=2) is True


def test_state_reset_returns_none_for_unknown_issue(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    assert state.reset(99) is None


def test_cli_reset_requeues_and_guards_running_tasks(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        """\
[runtime]
repo = "."
state_db = "state.db"
log_dir = "logs"
dry_run = true
[github]
repo = "a/b"
"""
    )
    config = load_config(config_file)
    state = StateStore(config.state_db)
    issue = Issue(4, "Task", "Body")
    state.claim(issue, "codex")
    state.record_failure(4, TaskStatus.BLOCKED, "boom")
    state.record_failure(4, TaskStatus.BLOCKED, "boom")

    from issue_agent.cli import reset_issue

    exit_code = asyncio.run(reset_issue(config, 4, no_label=False))
    assert exit_code == 0
    row = state.rows()[0]
    assert row["status"] == str(TaskStatus.PENDING)
    assert row["failures"] == 0

    # a running task must not be reset
    state.claim(issue, "codex")
    state.update(4, TaskStatus.CODING)
    exit_code = asyncio.run(reset_issue(config, 4, no_label=True))
    assert exit_code == 1
    assert state.rows()[0]["status"] == str(TaskStatus.CODING)


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


# --- baseline-aware checks -------------------------------------------------


def test_failed_tests_extracts_pytest_summary():
    output = (
        "backend/schemas/lit.py:311: PydanticDeprecatedSince20: ...\n"
        "FAILED backend/tests/test_teams.py::test_list_create_and_member_crud - AttributeError: 'Depends'\n"
        "FAILED backend/tests/test_iam.py::test_non_admin_cannot_assign_admin_role - AssertionError\n"
        "1 failed, 201 passed, 37 warnings in 3.71s\n"
    )
    assert failed_tests(output) == {
        "backend/tests/test_teams.py::test_list_create_and_member_crud",
        "backend/tests/test_iam.py::test_non_admin_cannot_assign_admin_role",
    }
    assert failed_tests("200 passed, 37 warnings in 3.71s\n") == set()
    assert failed_tests("") == set()


def _app(tmp_path: Path, checks: list[str]) -> Orchestrator:
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        f"""
[runtime]
repo = "."
state_db = "state.db"
log_dir = "logs"
dry_run = true
[github]
repo = "a/b"
[checks]
commands = {json.dumps(checks)}
"""
    )
    return Orchestrator(load_config(config_file))


class _Log:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


def test_run_checks_tolerates_pre_existing_failures(tmp_path, monkeypatch):
    app = _app(tmp_path, ["pytest"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(1, "FAILED backend/tests/test_teams.py::test_a - x\n", "")

    monkeypatch.setattr("issue_agent.orchestrator.shell", fake_shell)
    log = _Log()
    asyncio.run(
        app._run_checks(tmp_path, log, {"backend/tests/test_teams.py::test_a"})
    )
    assert log.events[0][0] == "check_passed_pre_existing"
    assert log.events[0][1]["pre_existing"] == ["backend/tests/test_teams.py::test_a"]


def test_run_checks_flags_new_failures_and_blames_only_them(tmp_path, monkeypatch):
    app = _app(tmp_path, ["pytest"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(
            1,
            "FAILED backend/tests/test_teams.py::test_a - x\n"
            "FAILED backend/tests/test_new.py::test_b - y\n",
            "",
        )

    monkeypatch.setattr("issue_agent.orchestrator.shell", fake_shell)
    with pytest.raises(CommandError) as exc:
        asyncio.run(app._run_checks(tmp_path, _Log(), {"backend/tests/test_teams.py::test_a"}))
    assert "1 new failure(s)" in str(exc.value)
    # the summary block only names the new failure, never the pre-existing one
    summary = str(exc.value).split("\n\n")[0]
    assert "test_new.py::test_b" in summary
    assert "test_teams.py" not in summary


def test_run_checks_still_fails_without_failed_lines(tmp_path, monkeypatch):
    app = _app(tmp_path, ["compileall"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(2, "SyntaxError: bad input\n", "")

    monkeypatch.setattr("issue_agent.orchestrator.shell", fake_shell)
    with pytest.raises(CommandError):
        asyncio.run(app._run_checks(tmp_path, _Log(), set()))


def test_capture_baseline_collects_pre_existing_failures(tmp_path, monkeypatch):
    app = _app(tmp_path, ["compileall", "pytest"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        if "compileall" in command:
            return Result(0, "", "")
        return Result(1, "FAILED backend/tests/test_teams.py::test_a - x\n", "")

    monkeypatch.setattr("issue_agent.orchestrator.shell", fake_shell)
    baseline = asyncio.run(app._capture_baseline(tmp_path))
    assert baseline == {"backend/tests/test_teams.py::test_a"}


def test_run_task_cleans_state_after_failure(tmp_path, monkeypatch):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        """
[runtime]
repo = "."
state_db = "state.db"
log_dir = "logs"
default_agent = "codex"
max_attempts = 2
dry_run = true
[github]
repo = "a/b"
[checks]
commands = ["pytest"]
[agents.codex]
command = "fake -"
"""
    )
    app = Orchestrator(load_config(config_file))
    issue = Issue(1, "Task", "Body")
    app.state.claim(issue, "codex")
    app.state.save_plan(1, [PlanTask("Implement", "Description")])
    app.state.update(1, TaskStatus.PLANNED, current_seq=0)

    async def fake_execute(workspace, prompt, *, review=False):
        return Result(0, "done", "")

    app.agents["codex"] = SimpleNamespace(execute=fake_execute)

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(1, "FAILED backend/tests/test_new.py::test_b - y\n", "")

    monkeypatch.setattr("issue_agent.orchestrator.shell", fake_shell)
    issue_log = IssueLog(tmp_path / "logs", 1)

    with pytest.raises(CommandError):
        asyncio.run(
            app._run_task(
                tmp_path, issue, [PlanTask("Implement", "Description")], 0, "codex", issue_log, set()
            )
        )

    # the task is no longer stuck on CODING: plan row is retryable, cursor reset
    assert app.state.plan_task_statuses(1) == [TaskStatus.PENDING]
    row = app.state.rows()[0]
    assert row["current_seq"] == -1
    assert "test_new.py::test_b" in str(row["last_error"])
