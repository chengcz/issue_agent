import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from issue_agent.checks import CheckBaseline, failed_tests
from issue_agent.cli import format_report, format_status, parser
from issue_agent.config import load_config
from issue_agent.github import GitHub
from issue_agent.issue_log import IssueLog
from issue_agent.models import Issue, PlanTask, TaskStatus
from issue_agent.orchestrator import Orchestrator
from issue_agent.process import CommandError, Result, shell
from issue_agent.state import StateStore
from issue_agent.workspace import WorkspaceManager, slugify


def test_slugify_is_branch_safe():
    assert slugify("Add Regimen / API!") == "add-regimen-api"


def test_write_feedback_file_creates_agent_dir(tmp_path):
    workspace = tmp_path / "wt"
    workspace.mkdir()
    manager = WorkspaceManager(tmp_path, tmp_path / "worktrees", "main")
    manager.write_feedback_file(workspace, "review requested changes:\nMissing docs")
    feedback = (workspace / ".agent" / "feedback.md").read_text(encoding="utf-8")
    assert feedback == "review requested changes:\nMissing docs\n"


def test_workspace_status_uses_complete_porcelain_output(tmp_path, monkeypatch):
    calls = []

    async def fake_run(command, *, cwd, timeout=3600, stdin=None, check=True):
        calls.append((command, cwd))
        return Result(0, " M src/app.py\n?? new/file.py\n", "")

    monkeypatch.setattr("issue_agent.workspace.run", fake_run)
    app = _app(tmp_path, [])
    status = asyncio.run(app.workspaces.status(tmp_path))
    assert status == "M src/app.py\n?? new/file.py"
    assert calls == [
        (
            (
                "git", "status", "--porcelain", "--untracked-files=all", "--", ".",
                ":(exclude).agent",
            ),
            tmp_path,
        )
    ]


def test_workspace_git_mutations_preserve_and_exclude_agent_files(tmp_path, monkeypatch):
    calls = []

    async def fake_run(command, *, cwd, timeout=3600, stdin=None, check=True):
        calls.append(command)
        return Result(0, "", "")

    monkeypatch.setattr("issue_agent.workspace.run", fake_run)
    manager = WorkspaceManager(tmp_path, tmp_path / "worktrees", "main")

    asyncio.run(manager.commit(tmp_path, "message"))
    asyncio.run(manager.amend(tmp_path))
    asyncio.run(manager.clean(tmp_path))

    assert ("git", "add", "--all", "--", ".", ":(exclude).agent") in calls
    assert ("git", "clean", "-fd", "-e", ".agent/") in calls


def test_workspace_fetch_is_shared_within_ttl(tmp_path, monkeypatch):
    calls = 0

    async def fake_run(command, *, cwd, timeout=3600, stdin=None, check=True):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return Result(0, "", "")

    monkeypatch.setattr("issue_agent.workspace.run", fake_run)
    manager = WorkspaceManager(
        tmp_path,
        tmp_path / "worktrees",
        "main",
        fetch_ttl_seconds=30,
    )

    async def fetch_concurrently():
        await asyncio.gather(manager.fetch_base(), manager.fetch_base())
        await manager.fetch_base()

    asyncio.run(fetch_concurrently())
    assert calls == 1


def test_existing_worktree_reuses_its_actual_branch_after_issue_rename(tmp_path, monkeypatch):
    commands = []
    worktree = tmp_path / "worktrees" / "42"
    worktree.mkdir(parents=True)

    async def fake_run(command, *, cwd, timeout=3600, stdin=None, check=True):
        commands.append((command, cwd))
        if command == ("git", "branch", "--show-current"):
            return Result(0, "agent/42-original-title\n", "")
        return Result(0, "", "")

    monkeypatch.setattr("issue_agent.workspace.run", fake_run)
    manager = WorkspaceManager(tmp_path, tmp_path / "worktrees", "main")

    path, branch = asyncio.run(manager.create(Issue(42, "Renamed title", "Body")))

    assert path == worktree
    assert branch == "agent/42-original-title"
    assert (("git", "branch", "--show-current"), worktree) in commands


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


def test_state_recovery_closes_open_metric_runs(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(42, "Task", "Body")
    state.claim(issue, "codex")
    state.save_plan(42, [PlanTask("Implement", "Details")])
    state.start_run(42, "implementation")
    state.start_plan_task(42, 0)
    state.update(42, TaskStatus.CODING, current_seq=0)

    assert state.recover_interrupted() == 1

    report = state.report_rows(42)[0]
    assert report["runs"][0]["status"] == "interrupted"
    assert report["runs"][0]["finished_at"] is not None
    assert report["tasks"][0]["finished_at"] is not None


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


# ---------------------------------------------------------------------------
# usage accumulation tests
# ---------------------------------------------------------------------------

def test_accumulate_usage_stores_tokens_and_cost(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(7, "T", "B"), "codex")
    state.accumulate_usage(
        7,
        {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 20,
         "cache_creation_input_tokens": 5, "cost_usd": 0.01},
        duration_ms=1200,
    )
    row = next(r for r in state.rows() if r["issue_number"] == 7)
    assert row["total_input_tokens"] == 100
    assert row["total_output_tokens"] == 50
    assert row["total_cache_read_tokens"] == 20
    assert row["total_cache_creation_tokens"] == 5
    assert row["total_cost_usd"] == 0.01
    assert row["total_duration_ms"] == 1200


def test_accumulate_usage_sums_across_calls(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(7, "T", "B"), "codex")
    state.accumulate_usage(7, {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01}, duration_ms=1000)
    state.accumulate_usage(7, {"input_tokens": 200, "output_tokens": 80, "cost_usd": 0.02}, duration_ms=2000)
    row = next(r for r in state.rows() if r["issue_number"] == 7)
    assert row["total_input_tokens"] == 300
    assert row["total_output_tokens"] == 130
    assert abs(row["total_cost_usd"] - 0.03) < 1e-9
    assert row["total_duration_ms"] == 3000


def test_accumulate_usage_treats_missing_keys_as_zero(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(7, "T", "B"), "codex")
    state.accumulate_usage(7, {"input_tokens": 42}, duration_ms=None)
    row = next(r for r in state.rows() if r["issue_number"] == 7)
    assert row["total_input_tokens"] == 42
    assert row["total_output_tokens"] == 0
    assert row["total_cost_usd"] == 0.0
    assert row["total_duration_ms"] == 0


def test_accumulate_usage_ignores_unknown_issue(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    # no row for issue 99 — must not raise
    state.accumulate_usage(99, {"input_tokens": 10}, duration_ms=5)
    assert all(r["issue_number"] != 99 for r in state.rows())


def test_status_rows_include_usage_fields(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(7, "T", "B"), "codex")
    state.accumulate_usage(7, {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.005}, duration_ms=500)
    rows = state.status_rows()
    row = next(r for r in rows if r["issue_number"] == 7)
    assert row["total_input_tokens"] == 10
    assert row["total_output_tokens"] == 5
    assert row["total_cost_usd"] == 0.005
    assert row["total_duration_ms"] == 500


def test_usage_columns_migrate_on_existing_db(tmp_path: Path):
    """A DB created before usage columns exist gains them on reopen."""
    import sqlite3

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE tasks (
            issue_number INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
            agent TEXT, branch TEXT, worktree TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, pr_url TEXT, plan TEXT,
            current_seq INTEGER NOT NULL DEFAULT -1, final_commit_hash TEXT,
            final_last_error TEXT, updated_at TEXT NOT NULL
        )""")
        db.execute(
            "INSERT INTO tasks(issue_number,title,status,updated_at) VALUES(1,'Old','pending','2026-01-01')"
        )

    state = StateStore(db_path)  # triggers migration
    state.accumulate_usage(1, {"input_tokens": 7}, duration_ms=99)
    row = next(r for r in state.rows() if r["issue_number"] == 1)
    assert row["total_input_tokens"] == 7
    assert row["total_duration_ms"] == 99


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


def test_shell_can_return_nonzero_result_for_baseline_checks(tmp_path: Path):
    result = asyncio.run(shell("exit 7", cwd=tmp_path, check=False))
    assert result.returncode == 7


def test_shell_timeout_terminates_the_command(tmp_path: Path):
    with pytest.raises(CommandError, match="timed out"):
        asyncio.run(shell("sleep 10", cwd=tmp_path, timeout=0.01))


def test_shell_cancellation_terminates_the_command(tmp_path: Path):
    async def cancel_command():
        task = asyncio.create_task(shell("sleep 10", cwd=tmp_path, timeout=30))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_command())


def test_result_has_duration_and_usage_fields():
    """Result dataclass exposes optional duration_ms and usage for token tracking."""
    r = Result(returncode=0, stdout="ok", stderr="")
    assert r.duration_ms is None
    assert r.usage is None

    r2 = Result(returncode=0, stdout="ok", stderr="", duration_ms=123, usage={"input_tokens": 10})
    assert r2.duration_ms == 123
    assert r2.usage == {"input_tokens": 10}


def test_run_measures_duration(tmp_path: Path):
    """process.run() always populates duration_ms with wall-clock milliseconds."""
    result = asyncio.run(shell("sleep 0.05", cwd=tmp_path))
    assert result.duration_ms is not None
    assert result.duration_ms >= 40  # allow small timing slack


def test_run_duration_present_on_failure(tmp_path: Path):
    """duration_ms is populated even when the command fails (check=False)."""
    result = asyncio.run(shell("exit 3", cwd=tmp_path, check=False))
    assert result.returncode == 3
    assert result.duration_ms is not None
    assert result.duration_ms >= 0


# ---------------------------------------------------------------------------
# JSON envelope unwrap tests (CliAgent.execute)
# ---------------------------------------------------------------------------

CLAUDE_JSON_ENVELOPE = json.dumps({
    "type": "result",
    "subtype": "success",
    "cost_usd": 0.0123,
    "is_error": False,
    "duration_ms": 5000,
    "duration_api_ms": 4500,
    "num_turns": 3,
    "result": "VERDICT: APPROVE\nAll checks passed.",
    "session_id": "sess_abc123",
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 1500,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 800,
        "output_tokens": 350,
    },
})


def test_unwrap_claude_json_extracts_result_and_usage():
    """Claude CLI JSON envelope: stdout becomes result text, usage/duration extracted."""
    from issue_agent.agents import _unwrap_agent_output

    raw = Result(returncode=0, stdout=CLAUDE_JSON_ENVELOPE, stderr="", duration_ms=5100)
    unwrapped = _unwrap_agent_output(raw)
    assert unwrapped.stdout == "VERDICT: APPROVE\nAll checks passed."
    assert unwrapped.usage is not None
    assert unwrapped.usage["input_tokens"] == 1500
    assert unwrapped.usage["output_tokens"] == 350
    assert unwrapped.usage["cache_read_input_tokens"] == 800
    assert unwrapped.usage["cost_usd"] == 0.0123
    # duration from envelope preferred over wall-clock when present
    assert unwrapped.duration_ms == 5000


def test_unwrap_codex_jsonl_extracts_final_message_and_usage():
    from issue_agent.agents import _unwrap_agent_output

    output = (
        '{"type":"thread.started","thread_id":"thread-7"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":80,'
        '"output_tokens":20,"reasoning_output_tokens":5}}'
    )
    unwrapped = _unwrap_agent_output(Result(0, output, "", duration_ms=123))

    assert unwrapped.stdout == "done"
    assert unwrapped.duration_ms == 123
    assert unwrapped.usage == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 80,
        "reasoning_output_tokens": 5,
        "session_id": "thread-7",
    }


def test_unwrap_plain_text_passthrough():
    """Non-JSON stdout is returned unchanged with usage=None."""
    from issue_agent.agents import _unwrap_agent_output

    raw = Result(returncode=0, stdout="VERDICT: APPROVE\n", stderr="", duration_ms=1200)
    unwrapped = _unwrap_agent_output(raw)
    assert unwrapped.stdout == "VERDICT: APPROVE\n"
    assert unwrapped.usage is None
    assert unwrapped.duration_ms == 1200


def test_unwrap_ignores_json_without_result_envelope():
    """JSON that is not a Claude result envelope (e.g. planner fenced output) is not unwrapped."""
    from issue_agent.agents import _unwrap_agent_output

    planner_output = '```json\n[{"title": "A", "description": "B"}]\n```'
    raw = Result(returncode=0, stdout=planner_output, stderr="", duration_ms=900)
    unwrapped = _unwrap_agent_output(raw)
    assert unwrapped.stdout == planner_output
    assert unwrapped.usage is None


def test_unwrap_handles_malformed_json_gracefully():
    """Truncated or invalid JSON falls back to plain text, never raises."""
    from issue_agent.agents import _unwrap_agent_output

    raw = Result(returncode=0, stdout='{"type": "result", "result": "trunc', stderr="", duration_ms=100)
    unwrapped = _unwrap_agent_output(raw)
    assert unwrapped.stdout == '{"type": "result", "result": "trunc'
    assert unwrapped.usage is None


def test_unwrap_preserves_stderr_and_returncode():
    """Unwrap only transforms stdout; stderr and returncode pass through."""
    from issue_agent.agents import _unwrap_agent_output

    raw = Result(returncode=1, stdout=CLAUDE_JSON_ENVELOPE, stderr="warning: x", duration_ms=300)
    unwrapped = _unwrap_agent_output(raw)
    assert unwrapped.returncode == 1
    assert unwrapped.stderr == "warning: x"


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
[checks]
commands = ["pytest -q"]
timeout_seconds = 17
''')
    app = Orchestrator(load_config(config_file))
    assert app.select_agent(Issue(1, "x", "", ("agent:claude",))) == "claude"
    assert app.config.auto_plan_unlabeled is True
    assert app.config.auto_plan_limit == 7
    assert app.config.check_timeout_seconds == 17
    assert app.config.baseline_cache_ttl_seconds == 300


def test_config_parses_codegraph_and_parallel_checks(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text('''
[runtime]
repo = "."
state_db = "state.db"
dry_run = true
[github]
repo = "a/b"
[checks]
commands = ["pytest -q"]
parallel = false
[codegraph]
enabled = false
''')
    config = load_config(config_file)
    assert config.checks_parallel is False
    assert config.codegraph.enabled is False


def test_config_parses_task_checks_limits_and_resume_commands(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text('''
[runtime]
repo = "."
max_workers = 4
[github]
repo = "a/b"
[checks]
commands = ["pytest -q"]
task_commands = ["ruff check ."]
max_workers = 2
baseline_cache_max_entries = 5
[agents.codex]
command = "codex exec --json -"
resume_command = "codex exec resume --json {session_id} -"
''')
    config = load_config(config_file)

    assert config.task_checks == ("ruff check .",)
    assert config.max_check_workers == 2
    assert config.baseline_cache_max_entries == 5
    assert config.agents["codex"].resume_command == (
        "codex", "exec", "resume", "--json", "{session_id}", "-"
    )


def test_config_rejects_resume_command_without_session_placeholder(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text('''
[runtime]
repo = "."
[github]
repo = "a/b"
[agents.codex]
command = "codex exec -"
resume_command = "codex exec resume --last -"
''')

    with pytest.raises(ValueError, match="must contain.*session_id"):
        load_config(config_file)


def test_config_defaults_to_codegraph_enabled_and_parallel_checks(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text('''
[runtime]
repo = "."
[github]
repo = "a/b"
''')
    config = load_config(config_file)
    assert config.checks_parallel is True
    assert config.codegraph.enabled is True
    assert config.review_task_mode == "formal"


def test_config_parses_review_task_mode(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text('''
[runtime]
repo = "."
[github]
repo = "a/b"
[review]
task_mode = "full"
''')
    config = load_config(config_file)
    assert config.review_task_mode == "full"


def test_config_rejects_invalid_review_task_mode(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text('''
[runtime]
repo = "."
[github]
repo = "a/b"
[review]
task_mode = "invalid"
''')
    with pytest.raises(ValueError, match="review.task_mode"):
        load_config(config_file)


def test_config_rejects_unknown_reviewer_and_zero_limits(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        """
[runtime]
repo = "."
reviewer_agent = "missing"
[github]
repo = "a/b"
[agents.codex]
command = "codex exec -"
"""
    )
    with pytest.raises(ValueError, match="reviewer_agent"):
        load_config(config_file)

    config_file.write_text(
        """
[runtime]
repo = "."
max_workers = 0
[github]
repo = "a/b"
"""
    )
    with pytest.raises(ValueError, match="max_workers"):
        load_config(config_file)


def test_github_unassigned_issues_keeps_product_labels(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(
        return_value='''[
            {"number": 1, "title": "Plan me", "body": "", "labels": [], "url": "u1"},
            {"number": 2, "title": "Plan me too", "body": "", "labels": [{"name": "bug"}], "url": "u2"},
            {"number": 3, "title": "Already queued", "body": "", "labels": [{"name": "agent-ready"}], "url": "u3"}
        ]'''
    )

    issues = asyncio.run(github.unassigned_issues())

    assert [issue.number for issue in issues] == [1, 2]
    assert "--search" not in github._gh.await_args.args


def test_create_pr_reuses_existing_branch_pr(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(return_value='[{"url": "https://example.test/pr/42"}]')

    url = asyncio.run(github.create_pr(42, "agent/42-task", "main", "Task", ("pytest",)))

    assert url == "https://example.test/pr/42"
    assert github._gh.await_count == 1
    assert github._gh.await_args.args[:2] == ("pr", "list")


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


def test_report_parser_and_human_format():
    args = parser().parse_args(["report", "--issue", "7", "--json"])
    assert args.issue == 7
    assert args.json is True
    output = format_report(
        [
            {
                "issue_number": 7,
                "title": "Improve runner",
                "status": "done",
                "total_input_tokens": 100,
                "total_output_tokens": 20,
                "total_duration_ms": 1000,
                "total_check_duration_ms": 2000,
                "total_wall_duration_ms": 4000,
                "total_cost_usd": 0.01,
                "tasks": [
                    {
                        "seq": 0,
                        "title": "Add metrics",
                        "status": "done",
                        "attempts": 1,
                        "total_input_tokens": 100,
                        "total_output_tokens": 20,
                        "total_duration_ms": 1000,
                        "total_check_duration_ms": 2000,
                        "total_wall_duration_ms": 3500,
                    }
                ],
            }
        ]
    )
    assert "#7 Improve runner [done]" in output
    assert "Add metrics" in output
    assert "tokens=120" in output


def test_state_records_issue_task_and_failed_call_metrics(tmp_path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(7, "Task", "Body")
    state.claim(issue, "codex")
    state.save_plan(7, [PlanTask("Implement", "Details")])
    run_id = state.start_run(7, "implementation")
    state.start_plan_task(7, 0)
    state.record_agent_call(
        7,
        run_id=run_id,
        seq=0,
        attempt=1,
        agent="codex",
        role="worker",
        success=False,
        duration_ms=1500,
        usage={"input_tokens": 90, "output_tokens": 10, "reasoning_output_tokens": 4},
        error="failed",
    )
    state.record_check_duration(7, duration_ms=250, seq=0)
    state.finish_plan_task(7, 0, wall_duration_ms=2000)
    state.finish_run(run_id, 7, "failed", wall_duration_ms=2200)

    row = state.report_rows(7)[0]
    task = row["tasks"][0]
    assert row["total_input_tokens"] == task["total_input_tokens"] == 90
    assert row["total_reasoning_tokens"] == task["total_reasoning_tokens"] == 4
    assert row["total_duration_ms"] == task["total_duration_ms"] == 1500
    assert row["total_check_duration_ms"] == task["total_check_duration_ms"] == 250
    assert row["total_wall_duration_ms"] == 2200
    assert task["total_wall_duration_ms"] == 2000
    assert row["runs"][0]["status"] == "failed"


def test_format_status_shows_token_cost_time_columns():
    """Status table includes TOKENS, COST, TIME columns from accumulated usage."""
    output = format_status(
        [
            {
                "issue_number": 7,
                "status": "testing",
                "title": "Check CLI",
                "agent": "codex",
                "updated_at": "2026-08-28T12:34:56+00:00",
                "total_input_tokens": 1200,
                "total_output_tokens": 350,
                "total_cache_read_tokens": 800,
                "total_cache_creation_tokens": 50,
                "total_cost_usd": 0.0123,
                "total_duration_ms": 95000,
            }
        ]
    )
    assert "TOKENS" in output
    assert "COST" in output
    assert "TIME" in output
    # tokens shown as combined in+out (1200+350=1550 -> "1.6k")
    assert "1.6k" in output or "1550" in output
    # cost formatted with dollar sign (0.0123 -> "$0.01" at 2 decimals)
    assert "$0.01" in output
    # duration formatted human-readable (95000ms -> 1m35s)
    assert "1m35s" in output or "95s" in output


def test_format_status_handles_zero_usage_gracefully():
    """Issues with no agent calls yet show dashes or zeros, not errors."""
    output = format_status(
        [
            {
                "issue_number": 8,
                "status": "pending",
                "title": "Fresh",
                "agent": None,
                "updated_at": "2026-08-28T12:34:56+00:00",
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_read_tokens": 0,
                "total_cache_creation_tokens": 0,
                "total_cost_usd": 0.0,
                "total_duration_ms": 0,
            }
        ]
    )
    assert "#8" in output
    assert "TOKENS" in output
    # zero usage should render as "-" or "0", not crash
    assert "-" in output or "0" in output


def test_format_status_handles_missing_usage_keys():
    """Rows without usage keys (old DB rows) still render without KeyError."""
    output = format_status(
        [
            {
                "issue_number": 9,
                "status": "pending",
                "title": "Legacy",
                "agent": "codex",
                "updated_at": "2026-08-28T12:34:56+00:00",
            }
        ]
    )
    assert "#9" in output
    assert "TOKENS" in output


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
    app = Orchestrator(load_config(config_file))
    app._baseline_cache = None
    return app


class _Log:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


def test_run_checks_tolerates_pre_existing_failures(tmp_path, monkeypatch):
    app = _app(tmp_path, ["pytest"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(1, "FAILED backend/tests/test_teams.py::test_a - x\n", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    log = _Log()
    asyncio.run(
        app._run_checks(
            tmp_path,
            log,
            {
                "pytest": CheckBaseline(
                    1,
                    frozenset({"backend/tests/test_teams.py::test_a"}),
                    "FAILED backend/tests/test_teams.py::test_a - x",
                )
            },
        )
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

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    with pytest.raises(CommandError) as exc:
        asyncio.run(
            app._run_checks(
                tmp_path,
                _Log(),
                {
                    "pytest": CheckBaseline(
                        1,
                        frozenset({"backend/tests/test_teams.py::test_a"}),
                        "FAILED backend/tests/test_teams.py::test_a - x",
                    )
                },
            )
        )
    assert "1 new failure(s)" in str(exc.value)
    # the summary block only names the new failure, never the pre-existing one
    summary = str(exc.value).split("\n\n")[0]
    assert "test_new.py::test_b" in summary
    assert "test_teams.py" not in summary


def test_run_checks_still_fails_without_failed_lines(tmp_path, monkeypatch):
    app = _app(tmp_path, ["compileall"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(2, "SyntaxError: bad input\n", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    with pytest.raises(CommandError):
        asyncio.run(app._run_checks(tmp_path, _Log(), {}))


def test_run_checks_tolerates_unchanged_non_pytest_failure(tmp_path, monkeypatch):
    app = _app(tmp_path, ["python -m compileall src"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(1, "SyntaxError: existing bad input\n", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    baseline = asyncio.run(app._capture_baseline(tmp_path))
    log = _Log()
    asyncio.run(app._run_checks(tmp_path, log, baseline))
    assert log.events[0][0] == "check_passed_pre_existing"


def test_run_checks_flags_changed_non_pytest_failure(tmp_path, monkeypatch):
    app = _app(tmp_path, ["python -m compileall src"])
    results = iter(
        [
            Result(1, "SyntaxError: existing bad input\n", ""),
            Result(1, "SyntaxError: new bad input\n", ""),
        ]
    )

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return next(results)

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    baseline = asyncio.run(app._capture_baseline(tmp_path))
    with pytest.raises(CommandError, match="new bad input"):
        asyncio.run(app._run_checks(tmp_path, _Log(), baseline))


def test_run_checks_keeps_baselines_isolated_by_command(tmp_path, monkeypatch):
    app = _app(tmp_path, ["pytest unit", "pytest integration"])
    baseline = {
        "pytest unit": CheckBaseline(1, frozenset({"tests/test_api.py::test_a"}), "old"),
    }

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        if command == "pytest unit":
            return Result(0, "", "")
        return Result(1, "FAILED tests/test_api.py::test_a - new\n", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    with pytest.raises(CommandError, match="1 new failure"):
        asyncio.run(app._run_checks(tmp_path, _Log(), baseline))


def test_capture_baseline_collects_pre_existing_failures(tmp_path, monkeypatch):
    app = _app(tmp_path, ["compileall", "pytest"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        if "compileall" in command:
            return Result(0, "", "")
        return Result(1, "FAILED backend/tests/test_teams.py::test_a - x\n", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    baseline = asyncio.run(app._capture_baseline(tmp_path))
    assert baseline == {
        "pytest": CheckBaseline(
            1,
            frozenset({"backend/tests/test_teams.py::test_a"}),
            "FAILED backend/tests/test_teams.py::test_a - x",
        )
    }


def test_capture_baseline_is_reused_for_same_anchor(tmp_path, monkeypatch):
    app = _app(tmp_path, ["pytest"])
    app._baseline_cache = {}
    calls = 0

    async def fake_head_commit(path):
        return "anchor123"

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        nonlocal calls
        calls += 1
        return Result(1, "FAILED tests/test_api.py::test_a - existing\n", "")

    monkeypatch.setattr(app.workspaces, "head_commit", fake_head_commit)
    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)

    first = asyncio.run(app._capture_baseline(tmp_path))
    second = asyncio.run(app._capture_baseline(tmp_path))

    assert first == second
    assert calls == 1


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
max_task_attempts = 1
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
    assert app.config.max_attempts == 2
    assert app.config.max_task_attempts == 1
    issue = Issue(1, "Task", "Body")
    app.state.claim(issue, "codex")
    app.state.save_plan(1, [PlanTask("Implement", "Description")])
    app.state.update(1, TaskStatus.PLANNED, current_seq=0)

    async def fake_execute(workspace, prompt, *, review=False):
        return Result(0, "done", "")

    app.agents["codex"] = SimpleNamespace(execute=fake_execute)
    app.workspaces.changed = AsyncMock(return_value=True)

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(1, "FAILED backend/tests/test_new.py::test_b - y\n", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    issue_log = IssueLog(tmp_path / "logs", 1)

    with pytest.raises(CommandError):
        asyncio.run(
            app._run_task(
                tmp_path, issue, [PlanTask("Implement", "Description")], 0, "codex", issue_log, {}
            )
        )

    # the task is no longer stuck on CODING: plan row is retryable, cursor reset
    assert app.state.plan_task_statuses(1) == [TaskStatus.PENDING]
    row = app.state.rows()[0]
    assert row["current_seq"] == -1
    assert "test_new.py::test_b" in str(row["last_error"])


def test_plan_task_last_error_roundtrip(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(1, "Task", "Body"), "codex")
    state.save_plan(1, [PlanTask("One", "D")])
    assert state.plan_task_last_error(1, 0) == ""
    state.update_plan_task(1, 0, last_error="boom")
    assert state.plan_task_last_error(1, 0) == "boom"


def test_final_context_roundtrip(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(1, "Task", "Body"), "codex")
    assert state.final_context(1) == (None, "")
    state.update_final_context(1, commit_hash="abc1234", last_error="final review failed")
    assert state.final_context(1) == ("abc1234", "final review failed")
    state.update_final_context(1, last_error="")
    assert state.final_context(1) == ("abc1234", "")


def test_run_task_seeds_first_prompt_with_persisted_error(tmp_path, monkeypatch):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        """
[runtime]
repo = "."
state_db = "state.db"
log_dir = "logs"
default_agent = "codex"
dry_run = true
[github]
repo = "a/b"
[checks]
commands = ["pytest"]
[review]
task_mode = "off"
[agents.codex]
command = "fake -"
"""
    )
    app = Orchestrator(load_config(config_file))
    issue = Issue(1, "Task", "Body")
    app.state.claim(issue, "codex")
    app.state.save_plan(1, [PlanTask("Implement", "Description")])
    app.state.update(1, TaskStatus.PLANNED, current_seq=0)
    app.state.update_plan_task(1, 0, last_error="persisted boom")

    prompts = []

    async def fake_execute(workspace, prompt, *, review=False):
        prompts.append(prompt)
        return Result(0, "done", "")

    app.agents["codex"] = SimpleNamespace(execute=fake_execute)

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(0, "", "")

    async def fake_changed(path):
        return True

    async def fake_commit(path, message):
        return None

    async def fake_head_commit(path):
        return "abc1234"

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    monkeypatch.setattr(app.workspaces, "changed", fake_changed)
    monkeypatch.setattr(app.workspaces, "commit", fake_commit)
    monkeypatch.setattr(app.workspaces, "head_commit", fake_head_commit)

    issue_log = IssueLog(tmp_path / "logs", 1)
    asyncio.run(
        app._run_task(
            tmp_path, issue, [PlanTask("Implement", "Description")], 0, "codex", issue_log, {}
        )
    )
    assert "persisted boom" in prompts[0]
