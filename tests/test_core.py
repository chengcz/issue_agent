import asyncio
import json
import os
import re
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from issue_agent.checks import CheckBaseline, failed_tests
from issue_agent.cli import format_report, format_status, parser
from issue_agent.config import load_config
from issue_agent.github import GitHub
from issue_agent.issue_log import IssueLog
from issue_agent.models import (
    Blocker,
    Comment,
    Issue,
    PlanOutcome,
    PlanTask,
    RecordedChild,
    SplitChild,
    TaskStatus,
)
from issue_agent.orchestrator import Orchestrator
from issue_agent.process import CommandError, Result, run, shell
from issue_agent.state import StateStore
from issue_agent.workspace import WorkspaceManager, slugify

AWS_KEY = "AKIAABCDEFGHIJKLMNOP"


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

    assert ("git", "add", "--all", "--", ".") in calls
    assert ("git", "clean", "-fd", "-e", ".agent/") in calls


def _init_git_repo(tmp_path: Path, gitignore: str) -> None:
    import subprocess

    (tmp_path / ".gitignore").write_text(gitignore, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-f", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)


def _tracked(tmp_path: Path) -> list[str]:
    import subprocess

    return subprocess.run(
        ["git", "ls-files"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.splitlines()


def _commit_with_agent_dir(tmp_path: Path, gitignore: str) -> list[str]:
    manager = WorkspaceManager(tmp_path, tmp_path / "worktrees", "main")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    manager.write_task_file(tmp_path, Issue(1, "T", "B"), PlanTask("t", "d"))
    asyncio.run(manager.commit(tmp_path, "feat: add main"))
    return _tracked(tmp_path)


def test_workspace_commit_excludes_agent_even_when_the_repo_ignores_dotfiles(tmp_path):
    """`git add` rejects a `:(exclude).agent` pathspec in a dotfile-ignoring repo."""
    _init_git_repo(tmp_path, ".*\n")

    tracked = _commit_with_agent_dir(tmp_path, ".*\n")

    assert "src/main.py" in tracked
    assert not any(name.startswith(".agent") for name in tracked)


def test_workspace_commit_excludes_agent_when_the_repo_does_not_ignore_it(tmp_path):
    """The self-ignore must keep `.agent` out even without a repo `.gitignore` rule."""
    _init_git_repo(tmp_path, ".venv/\n")

    tracked = _commit_with_agent_dir(tmp_path, ".venv/\n")

    assert "src/main.py" in tracked
    assert not any(name.startswith(".agent") for name in tracked)


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


def test_planned_issue_with_a_plan_is_reclaimed_only_to_republish(tmp_path: Path):
    """claim_for_planning admits a PLANNED row so a plan whose publication was
    cut short by a crash gets republished from the persisted JSON. The plan is
    never regenerated: production keeps fully published plans out of the
    planning pool via the ``agent-planned`` label, and plan_only skips the LLM
    when a plan already exists."""
    state = StateStore(tmp_path / "state.db")
    issue = Issue(8, "Needs a plan", "Vague request")
    assert state.claim_for_planning(issue, "planner") is True
    state.save_plan(8, [PlanTask("Clarify implementation", "Acceptance: reviewed")])
    state.update(8, TaskStatus.PLANNED)
    assert state.claim_for_planning(issue, "planner") is True


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


def test_record_failure_creates_a_row_for_an_unknown_issue(tmp_path: Path):
    """The returned count must always reflect the database: a caller firing
    before any claim still gets a persisted failure, never a phantom count."""
    state = StateStore(tmp_path / "state.db")
    assert state.record_failure(42, TaskStatus.FAILED, "preflight blew up") == 1
    row = state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)
    assert row["failures"] == 1
    assert row["last_error"] == "preflight blew up"


def test_record_clarify_round_creates_a_row_for_an_unknown_issue(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    marker = "2026-09-10T00:00:00+00:00"
    assert state.record_clarify_round(42, marker) == 1
    assert state.clarify_state(42) == (1, marker)


def test_load_plan_survives_a_corrupted_payload(tmp_path: Path):
    """Same posture as load_split: a hand-edited database falls back to 'no
    plan' (fresh planning overwrites it) instead of wedging the poll loop."""
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(4, "T", "B"), "planner")
    state.save_plan(4, [PlanTask("One", "D")])
    import sqlite3

    db = sqlite3.connect(state.path)
    db.execute("UPDATE tasks SET plan='{not json' WHERE issue_number=4")
    db.commit()
    db.close()

    assert state.load_plan(4) is None


def test_plan_task_statuses_read_unknown_status_as_pending(tmp_path: Path):
    import sqlite3

    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(4, "T", "B"), "planner")
    state.save_plan(4, [PlanTask("One", "D")])
    db = sqlite3.connect(state.path)
    db.execute("UPDATE plan_tasks SET status='frobnicated' WHERE issue_number=4")
    db.commit()
    db.close()

    assert state.plan_task_statuses(4) == [TaskStatus.PENDING]


def test_claim_refreshes_an_issue_title(tmp_path: Path):
    """Retitled issues must not keep their stale name in status/report output:
    every re-claim (e.g. a retry after a failure) carries the current title."""
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(4, "Old title", "B"), "worker")
    state.record_failure(4, TaskStatus.FAILED, "boom")
    state.claim(Issue(4, "New title", "B"), "worker", max_attempts=3)

    assert state.rows()[0]["title"] == "New title"


def test_save_plan_prunes_ghost_rows_from_a_shorter_plan(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(4, "T", "B"), "planner")
    state.save_plan(4, [PlanTask("One", "D"), PlanTask("Two", "D"), PlanTask("Three", "D")])
    state.save_plan(4, [PlanTask("Only", "D")])

    assert state.plan_task_statuses(4) == [TaskStatus.PENDING]


def test_issue_log_structural_keys_cannot_be_shadowed(tmp_path: Path):
    import json as json_module

    log = IssueLog(tmp_path, 4)
    log.event("plan_failed", event="sneaky", issue_number=999, error="boom")
    record = json_module.loads(log.execution_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["event"] == "plan_failed"
    assert record["issue_number"] == 4
    assert record["error"] == "boom"


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


def test_state_blocker_notices_round_trip_without_a_task_row(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")

    # The dependency gate records notices before the issue is ever claimed, so
    # the record must not depend on a tasks row existing.
    assert state.load_blocker_notices(4) == {}

    state.save_blocker_notices(4, {"pending": [2, 3]})

    assert state.load_blocker_notices(4) == {"pending": [2, 3]}
    assert state.rows() == []


def test_state_blocker_notices_read_malformed_or_foreign_values_as_empty(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.save_blocker_notices(4, {"pending": [2]})
    state.save_blocker_notices(5, {"pending": [2]})
    with state.connect() as db:
        db.execute("UPDATE blocker_notices SET blockers_notified='{' WHERE issue_number=4")
        db.execute("UPDATE blocker_notices SET blockers_notified='[2]' WHERE issue_number=5")

    # A hand-edited database must not wedge the poll loop; the next comment
    # simply rewrites the record.
    assert state.load_blocker_notices(4) == {}
    assert state.load_blocker_notices(5) == {}


def test_state_reset_forgets_blocker_notices(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(4, "T", "B"), "codex")
    state.save_blocker_notices(4, {"pending": [2]})

    state.reset(4)

    assert state.load_blocker_notices(4) == {}


def test_state_clarify_round_counts_up_and_moves_the_marker(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(4, "T", "B"), "planner")

    assert state.clarify_state(4) == (0, "")

    assert state.record_clarify_round(4, "2026-09-10T00:00:00+00:00") == 1
    assert state.record_clarify_round(4, "2026-09-10T01:00:00+00:00") == 2

    assert state.clarify_state(4) == (2, "2026-09-10T01:00:00+00:00")


def test_state_clear_clarify_keeps_the_round_budget(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(4, "T", "B"), "planner")
    state.record_clarify_round(4, "2026-09-10T00:00:00+00:00")

    state.clear_clarify(4)

    # The budget bounds how often the planner may ask over the issue's life, not
    # how many answers it is allowed to receive.
    assert state.clarify_state(4) == (1, "")


def test_state_reset_restores_the_whole_clarify_budget(tmp_path: Path):
    """The ask budget resets so the planner may ask again, but the marker stays:
    the Q&A transcript is read from it, and a re-plan after the reset must still
    see the answers the human already gave."""
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(4, "T", "B"), "planner")
    state.record_clarify_round(4, "2026-09-10T00:00:00+00:00")

    state.reset(4)

    assert state.clarify_state(4) == (0, "2026-09-10T00:00:00+00:00")


def test_status_rows_include_usage_fields(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(7, "T", "B"), "codex")
    state.record_agent_call(
        7, run_id=None, seq=None, attempt=None, agent="codex", role="worker",
        success=True, duration_ms=500,
        usage={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.005},
    )
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
    state.record_agent_call(
        1, run_id=None, seq=None, attempt=None, agent="codex", role="worker",
        success=True, duration_ms=99, usage={"input_tokens": 7},
    )
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
[agents.codex]
command = "codex exec -"
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
    result = asyncio.run(run([sys.executable, "-c", "import time; time.sleep(0.05)"], cwd=tmp_path))
    assert result.duration_ms is not None
    assert result.duration_ms >= 40  # allow small timing slack


def test_run_caps_captured_output_for_runaway_commands(tmp_path: Path):
    """A command streaming ~11 MiB is truncated at the capture cap with an
    explicit marker instead of buffering gigabytes in memory."""

    result = asyncio.run(run(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 11534336)"],
        cwd=tmp_path, check=False, timeout=120,
    ))

    assert result.returncode == 0
    assert len(result.stdout) < 11 * 1024 * 1024
    assert "output truncated" in result.stdout


def test_run_feeds_stdin_and_reads_output(tmp_path: Path):
    """The bounded-capture rewrite still pumps stdin and collects stdout."""
    result = asyncio.run(
        run(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
            cwd=tmp_path,
            stdin="hello issue-agent",
            timeout=30,
        )
    )

    assert result.stdout.strip() == "HELLO ISSUE-AGENT"


def test_serial_execution_stops_at_the_first_failure(tmp_path, monkeypatch):
    """The serial path propagates the first check exception immediately instead
    of running the remaining commands (parallel mode collects instead)."""
    from issue_agent.checks import _execute

    calls: list[str] = []

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        calls.append(command)
        if command == "first":
            raise CommandError("command timed out: first")
        return Result(0, "", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)

    with pytest.raises(CommandError, match="first"):
        asyncio.run(_execute(tmp_path, ("first", "second"), timeout=10, parallel=False))

    assert calls == ["first"]


def test_cross_repo_blocker_parsing():
    from issue_agent.orchestrator import cross_repo_blockers, declared_blockers

    body = "Blocked by: other/repo#12 and #13, plus org/other-repo#14.\n"
    assert cross_repo_blockers(body) == ("org/other-repo#14", "other/repo#12")
    # Plain #N stays a same-repo declaration; owner/repo#N does not leak into it.
    assert declared_blockers(body) == (13,)


def test_instance_lock_blocks_a_second_instance(tmp_path):
    """The lock refuses a second live instance but breaks its own stale file."""
    state_db = tmp_path / "state.sqlite3"
    first = Orchestrator.__new__(Orchestrator)
    first.config = SimpleNamespace(state_db=state_db)
    second = Orchestrator.__new__(Orchestrator)
    second.config = SimpleNamespace(state_db=state_db)

    assert first.acquire_instance_lock() is True
    assert second.acquire_instance_lock() is False
    assert second._lock_held_by == os.getpid()

    first.release_instance_lock()
    assert second.acquire_instance_lock() is True
    second.release_instance_lock()


def test_instance_lock_breaks_a_stale_lock_file(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    lock = tmp_path / "state.sqlite3.lock"
    lock.write_text("999999999")  # a pid that cannot exist

    app = Orchestrator.__new__(Orchestrator)
    app.config = SimpleNamespace(state_db=state_db)

    assert app.acquire_instance_lock() is True
    app.release_instance_lock()


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
    assert app.config.ready_poll_limit == 20
    assert app.config.max_active_issues == 1
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
[agents.codex]
command = "codex exec -"
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
[agents.codex]
command = "codex exec -"
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
[agents.codex]
command = "codex exec -"
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
[agents.codex]
command = "codex exec -"
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


def test_config_defaults_leave_new_workflow_features_off(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text('''
[runtime]
repo = "."
[github]
repo = "a/b"
[agents.codex]
command = "codex exec -"
''')
    config = load_config(config_file)
    assert config.auto_ready_with_plan is False
    assert config.allow_split is False
    assert config.max_split_children == 5
    assert config.max_clarify_rounds == 2
    assert config.clarify_ignore_authors == ()


def test_config_parses_new_workflow_features(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text('''
[runtime]
repo = "."
auto_ready_with_plan = true
allow_split = true
max_split_children = 3
max_clarify_rounds = 4
clarify_ignore_authors = ["Dependabot[bot]", "ci-bot"]
[github]
repo = "a/b"
[agents.codex]
command = "codex exec -"
''')
    config = load_config(config_file)
    assert config.auto_ready_with_plan is True
    assert config.allow_split is True
    assert config.max_split_children == 3
    assert config.max_clarify_rounds == 4
    assert config.clarify_ignore_authors == ("dependabot[bot]", "ci-bot")


def test_config_rejects_nonpositive_new_limits(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    for key in ("max_split_children", "max_clarify_rounds"):
        config_file.write_text(f'''
[runtime]
repo = "."
{key} = 0
[github]
repo = "a/b"
''')
        with pytest.raises(ValueError, match=key):
            load_config(config_file)


def test_config_parses_quoted_boolean_flags_as_written(tmp_path: Path):
    """bool("false") is True: before strict parsing a quoted "false" left
    dry_run ON when the user meant OFF (and kept a "disabled" agent enabled).
    Quoted forms now parse to the value they spell; garbage strings fail."""
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        """
[runtime]
repo = "."
dry_run = "false"
auto_plan_unlabeled = "true"
[github]
repo = "a/b"
[agents.codex]
command = "codex exec -"
enabled = "true"
"""
    )
    config = load_config(config_file)
    assert config.dry_run is False
    assert config.auto_plan_unlabeled is True
    assert "codex" in config.agents


def test_config_rejects_a_non_boolean_flag_value(tmp_path: Path):
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        """
[runtime]
repo = "."
dry_run = "nope"
[github]
repo = "a/b"
[agents.codex]
command = "codex exec -"
"""
    )
    with pytest.raises(ValueError, match="dry_run"):
        load_config(config_file)


def test_config_rejects_a_scalar_command_string(tmp_path: Path):
    """tuple("pytest -q") would split the command into single characters, each
    then run as its own shell command."""
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        """
[runtime]
repo = "."
[agents.codex]
command = "codex exec -"
[checks]
commands = "pytest -q"
"""
    )
    with pytest.raises(ValueError, match="checks.commands"):
        load_config(config_file)


def test_config_rejects_an_empty_agent_table(tmp_path: Path):
    """Without agents every ready issue is silently rejected forever — the
    orchestrator spins with no failure signal. Fail fast at load time."""
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        """
[runtime]
repo = "."
[github]
repo = "a/b"
"""
    )
    with pytest.raises(ValueError, match="no agents configured"):
        load_config(config_file)


def test_parser_accepts_global_flags_after_the_subcommand():
    args = parser().parse_args(["status", "--config", "x.toml", "--verbose"])
    assert args.config == "x.toml" and args.verbose and args.command == "status"

    args = parser().parse_args(["--config", "y.toml", "once"])
    assert args.config == "y.toml"


def test_format_report_aligns_cjk_titles_by_display_width():
    rows = [
        {
            "issue_number": 4,
            "title": "实现解析器",
            "status": "done",
            "tasks": [
                {
                    "seq": 0,
                    "status": "done",
                    "attempts": 1,
                    "title": "实现解析器核心",
                    "total_input_tokens": 10,
                    "total_output_tokens": 5,
                    "total_cost_usd": 0.0,
                    "total_duration_ms": 1000,
                    "total_check_duration_ms": 0,
                    "total_wall_duration_ms": 2000,
                }
            ],
        }
    ]
    from issue_agent.cli import _display_width

    table = format_report(rows).splitlines()
    # Every line of the task table shares one display width: CJK titles
    # occupy two terminal cells each, so raw len() padding misaligned them.
    widths = {_display_width(line) for line in table[1:]}
    assert len(widths) == 1
    assert any("实现解析器核心" in line for line in table)


def test_config_errors_exit_with_a_one_line_message(tmp_path, capsys):
    """Every subcommand pays the same clean error: no raw tracebacks for a
    missing file, broken TOML, or invalid values (status/report need the
    config too, so they cannot be exempt)."""
    from issue_agent import cli

    def run(command):
        return asyncio.run(
            cli.async_main(
                Namespace(command=command, config=str(path), active=False, json=False)
            )
        )

    path = tmp_path / "missing.toml"
    assert run("status") == 2
    assert "config file not found" in capsys.readouterr().err

    path = tmp_path / "broken.toml"
    path.write_text("[runtime\n")
    assert run("status") == 2
    assert "not valid TOML" in capsys.readouterr().err

    path = tmp_path / "invalid.toml"
    path.write_text(
        '[runtime]\nrepo = "."\nmax_workers = 0\n'
        '[agents.codex]\ncommand = "codex exec -"\n'
    )
    assert run("status") == 2
    assert "invalid configuration" in capsys.readouterr().err


def test_issue_defaults_have_no_blockers_or_parent():
    issue = Issue(number=1, title="T", body="B")
    assert issue.blocked_by == ()
    assert issue.parent is None


def test_split_child_carries_planner_proposed_sibling_dependencies():
    assert SplitChild(title="First", body="Body", depends_on=(0, 2)).depends_on == (0, 2)
    assert SplitChild(title="Second", body="Body").depends_on == ()


def test_plan_outcome_represents_one_planner_shape_at_a_time():
    planned = PlanOutcome(tasks=(PlanTask(title="t", description="d"),))
    asked = PlanOutcome(questions=("Which module?",))
    split = PlanOutcome(split=(SplitChild(title="c", body="b"),))

    assert planned.tasks and not planned.questions and not planned.split
    assert asked.questions and not asked.tasks and not asked.split
    assert split.split and not split.tasks and not split.questions


def test_split_status_is_not_claimable(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(number=7, title="Split parent", body="")
    assert state.claim(issue, "codex") is True

    state.update(7, TaskStatus.SPLIT)

    assert state.claim(issue, "codex") is False


def test_recorded_child_starts_uncreated_and_unlinked():
    child = RecordedChild.from_child(SplitChild(title="First", body="Body", depends_on=(0,)))

    assert child.title == "First"
    assert child.depends_on == (0,)
    # 0 marks "not created yet", which is what makes a retry create only the
    # children a previous attempt failed to create.
    assert child.number == 0
    assert child.linked is False


def test_recorded_child_survives_a_json_round_trip():
    child = RecordedChild(
        title="First", body="Body", depends_on=(0, 2), number=12, url="u12", linked=True
    )

    assert RecordedChild.from_dict(child.to_dict()) == child


def test_state_split_round_trips_the_created_children(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(number=7, title="Split parent", body="")
    state.claim_for_planning(issue, "planner")

    state.save_split(
        7,
        [RecordedChild(title="First", body="B1"), RecordedChild(title="Second", body="B2")],
    )
    state.update_split_child(7, 1, number=22, url="u22")

    children = state.load_split(7)
    assert [child.title for child in children] == ["First", "Second"]
    # The first child is still uncreated; a retry has to create exactly that one.
    assert [(child.number, child.url) for child in children] == [(0, ""), (22, "u22")]


def test_state_split_reads_missing_or_malformed_records_as_empty(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(number=7, title="Split parent", body="")
    state.claim_for_planning(issue, "planner")

    assert state.load_split(7) == []

    with state.connect() as db:
        db.execute("UPDATE tasks SET split=? WHERE issue_number=?", ("{not json", 7))
    assert state.load_split(7) == []


def test_state_split_child_update_refuses_an_unknown_child(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(number=7, title="Split parent", body="")
    state.claim_for_planning(issue, "planner")
    state.save_split(7, [RecordedChild(title="First", body="B1")])

    # Silently dropping the number would orphan an issue that really exists on
    # GitHub, so an out-of-range index has to be loud.
    with pytest.raises(ValueError, match="index 3"):
        state.update_split_child(7, 3, number=22)


def test_state_reset_forgets_a_split(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(number=7, title="Split parent", body="")
    state.claim_for_planning(issue, "planner")
    state.save_split(7, [RecordedChild(title="First", body="B1")])
    state.update(7, TaskStatus.SPLIT)

    assert state.reset(7) == str(TaskStatus.SPLIT)

    # A human resetting a split parent wants it planned again as one unit, so
    # the record must not survive and resume the old proposal.
    assert state.load_split(7) == []
    assert state.rows()[0]["status"] == str(TaskStatus.PENDING)


def test_open_issues_reads_native_blockers_and_parent(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(
        return_value=json.dumps([
            {
                "number": 4,
                "title": "Blocked",
                "body": "",
                "labels": [],
                "url": "u4",
                "blockedBy": {"nodes": [{"number": 2}, {"number": 3}], "totalCount": 2},
                "parent": {"number": 1},
            },
            {
                "number": 5,
                "title": "Free",
                "body": "",
                "labels": [],
                "url": "u5",
                "blockedBy": {"nodes": [], "totalCount": 0},
                "parent": None,
            },
        ])
    )

    issues = asyncio.run(github.open_issues())

    assert issues[0].blocked_by == (2, 3)
    assert issues[0].parent == 1
    assert issues[1].blocked_by == ()
    assert issues[1].parent is None


def test_blocker_states_reports_closed_blockers_and_their_titles(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(
        side_effect=[
            '{"number": 2, "title": "First", "state": "CLOSED"}',
            '{"number": 3, "title": "Second", "state": "OPEN"}',
        ]
    )

    states = asyncio.run(github.blocker_states([3, 2]))

    assert states == {2: Blocker(2, "First", True), 3: Blocker(3, "Second", False)}
    assert github._gh.await_args_list[0].args == (
        "issue",
        "view",
        "2",
        "--json",
        "number,title,state",
    )
    assert github._gh.await_count == 2


def test_blocker_states_treats_a_failed_lookup_as_missing(tmp_path: Path):
    """One deleted/unreadable blocker must not raise the lookup away."""
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(
        side_effect=[
            CommandError("command failed (1): gh\nissue #3 not found"),
            '{"number": 2, "title": "First", "state": "OPEN"}',
        ]
    )

    states = asyncio.run(github.blocker_states([3, 2]))

    assert states == {2: Blocker(2, "First", False)}


def test_gh_omits_the_repo_flag_when_told_to(tmp_path: Path, monkeypatch):
    github = GitHub("owner/repo", tmp_path)
    commands: list[list[str]] = []

    async def fake_run(command, *, cwd, check=True):
        commands.append(list(command))
        return Result(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("issue_agent.github.run", fake_run)

    asyncio.run(github._gh("label", "list", "--json", "name"))
    # `gh api` accepts no --repo, so extracting the login needs the flag off.
    asyncio.run(github._gh("api", "user", "--jq", ".login", repo=False))

    assert commands[0] == ["gh", "label", "list", "--json", "name", "--repo", "owner/repo"]
    assert commands[1] == ["gh", "api", "user", "--jq", ".login"]


def test_viewer_login_reads_the_authenticated_user(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(return_value="octocat\n")

    assert asyncio.run(github.viewer_login()) == "octocat"
    assert github._gh.await_args.args == ("api", "user", "--jq", ".login")
    assert github._gh.await_args.kwargs == {"repo": False}


def test_viewer_login_stays_offline_in_dry_run(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path, dry_run=True)
    github._gh = AsyncMock()

    assert asyncio.run(github.viewer_login()) == ""
    github._gh.assert_not_awaited()


def test_comments_reads_author_timestamp_and_body(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(
        return_value=json.dumps(
            {
                "comments": [
                    {
                        "author": {"login": "octocat"},
                        "createdAt": "2026-09-10T01:00:00Z",
                        "body": "Use the parser module.",
                    },
                    {"author": None, "createdAt": "2026-09-10T02:00:00Z", "body": "Ghost"},
                ]
            }
        )
    )

    comments = asyncio.run(github.comments(4))

    assert comments == [
        Comment("octocat", "2026-09-10T01:00:00Z", "Use the parser module."),
        Comment("", "2026-09-10T02:00:00Z", "Ghost"),
    ]


def test_create_issue_reads_the_number_out_of_the_returned_url(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(return_value="https://github.com/owner/repo/issues/42\n")

    number, url = asyncio.run(github.create_issue("Child", "Body", labels=("enhancement",)))

    assert (number, url) == (42, "https://github.com/owner/repo/issues/42")
    assert github._gh.await_args.args == (
        "issue",
        "create",
        "--title",
        "Child",
        "--body",
        "Body",
        "--label",
        "enhancement",
    )


def test_create_issue_redacts_secrets_before_posting(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(return_value="https://github.com/owner/repo/issues/42")

    asyncio.run(github.create_issue("Child", f"key {AWS_KEY}"))

    assert AWS_KEY not in github._gh.await_args.args[-1]


def test_create_issue_fails_loudly_when_gh_returns_no_url(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(return_value="  \n")

    # Without a number the caller cannot record what it created, and inventing
    # one would make the next attempt skip a child that never existed.
    with pytest.raises(CommandError, match="issue number"):
        asyncio.run(github.create_issue("Child", "Body"))


def test_create_issue_makes_no_request_in_dry_run(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path, dry_run=True)
    github._gh = AsyncMock()

    number, url = asyncio.run(github.create_issue("Child", "Body"))
    second, _ = asyncio.run(github.create_issue("Child two", "Body two"))

    # Fake numbers from a range GitHub can never hand out, distinct per child,
    # so a dry-run can exercise the whole split flow (spec §6.2).
    assert number > 1_000_000_000 and second == number + 1
    assert url.endswith(f"/{number}")
    github._gh.assert_not_awaited()


def test_child_links_use_the_native_parent_and_blocked_by_flags(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(return_value="")

    asyncio.run(github.link_parent(12, 7))
    asyncio.run(github.add_blocked_by(13, 12))

    assert github._gh.await_args_list[0].args == ("issue", "edit", "12", "--parent", "7")
    assert github._gh.await_args_list[1].args == ("issue", "edit", "13", "--add-blocked-by", "12")


def test_child_links_are_skipped_in_dry_run(tmp_path: Path):
    github = GitHub("owner/repo", tmp_path, dry_run=True)
    github._gh = AsyncMock()

    asyncio.run(github.link_parent(12, 7))
    asyncio.run(github.add_blocked_by(13, 12))

    github._gh.assert_not_awaited()


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


def test_unassigned_issues_excludes_a_custom_ready_label(tmp_path: Path):
    """The ready label is configurable and need not be agent-prefixed: a ready
    issue must not also surface in the auto-plan pool and get processed twice
    in the same poll."""
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(
        return_value="""[
            {"number": 1, "title": "Ready custom", "body": "", "labels": [{"name": "todo"}], "url": "u1"},
            {"number": 2, "title": "Plain", "body": "", "labels": [{"name": "bug"}], "url": "u2"},
            {"number": 3, "title": "Workflow", "body": "", "labels": [{"name": "agent-planned"}], "url": "u3"}
        ]"""
    )

    issues = asyncio.run(github.unassigned_issues(ready_label="todo"))

    assert [issue.number for issue in issues] == [2]


def test_runnable_issues_merges_and_sorts_by_issue_number(tmp_path: Path):
    """gh lists newest-first, so the merge is sorted locally: with more ready
    issues than the limit, the oldest backlog still drains first."""
    github = GitHub("owner/repo", tmp_path)
    github._gh = AsyncMock(
        side_effect=[
            json.dumps([
                {"number": 9, "title": "New ready", "body": "", "labels": [{"name": "agent-ready"}], "url": "u9"},
                {"number": 3, "title": "Old ready", "body": "", "labels": [{"name": "agent-ready"}], "url": "u3"},
            ]),
            json.dumps([
                {"number": 5, "title": "Interrupted", "body": "", "labels": [{"name": "agent-running"}], "url": "u5"},
            ]),
        ]
    )

    issues = asyncio.run(github.runnable_issues("agent-ready", limit=20))

    assert [issue.number for issue in issues] == [3, 5, 9]


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


ANSI_SGR = re.compile(r"\033\[[0-9;]*m")


def strip_color(text: str) -> str:
    return ANSI_SGR.sub("", text)


def painted_codes(text: str, status: str) -> list[str]:
    """The SGR codes wrapped around `status` where the STATUS column rendered it."""
    return [
        code
        for code, value in re.findall(r"\033\[([0-9;]+)m(.*?)\033\[0m", text)
        if value.strip() == status
    ]


def status_row(status: str, number: int = 7) -> dict[str, object]:
    return {
        "issue_number": number,
        "status": status,
        "title": "Check CLI",
        "agent": "codex",
        "updated_at": "2026-08-28T12:34:56+00:00",
    }


def report_row(status: str = "done", task_status: str = "done") -> dict[str, object]:
    return {
        "issue_number": 7,
        "title": "Improve runner",
        "status": status,
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
                "status": task_status,
                "attempts": 1,
                "total_input_tokens": 100,
                "total_output_tokens": 20,
                "total_duration_ms": 1000,
                "total_check_duration_ms": 2000,
                "total_wall_duration_ms": 3500,
            }
        ],
    }


def test_status_color_is_off_unless_it_is_asked_for():
    assert "\033[" not in format_status([status_row("coding")])
    assert painted_codes(format_status([status_row("coding")], color=True), "coding")


def test_status_color_groups_the_workflow_states_by_meaning():
    statuses = [str(status) for status in TaskStatus]
    text = format_status(
        [status_row(status, number) for number, status in enumerate(statuses, 1)], color=True
    )
    codes = {status: painted_codes(text, status) for status in statuses}

    assert all(len(found) == 1 for found in codes.values()), codes
    for group in (
        ("claimed", "planning", "coding", "testing", "reviewing", "pushing"),
        ("pending", "planned"),
        ("human_review", "split"),
        ("done",),
        ("failed", "blocked"),
    ):
        assert len({codes[status][0] for status in group}) == 1, group
    # Five meanings must be five distinguishable colors, or the column says nothing.
    assert len({codes[status][0] for status in ("coding", "pending", "human_review", "done", "failed")}) == 5


def test_status_color_never_moves_a_column():
    rows = [status_row(status, number) for number, status in enumerate(("pending", "coding", "done"), 1)]
    assert strip_color(format_status(rows, color=True)) == format_status(rows)


def test_status_color_survives_the_narrow_layout():
    rows = [status_row("failed")]
    colored = format_status(rows, terminal_width=20, color=True)

    assert strip_color(colored) == format_status(rows, terminal_width=20)
    assert painted_codes(colored, "failed")


def test_report_colors_the_summary_and_the_task_table():
    rows = [report_row()]
    colored = format_report(rows, color=True)

    assert strip_color(colored) == format_report(rows)
    # The `[status]` bracket in the summary plus the STATUS cell in the task table.
    assert len(painted_codes(colored, "done")) == 2


def test_color_choice_and_environment_decide_whether_codes_are_emitted():
    from issue_agent.cli import _color_enabled

    tty = SimpleNamespace(isatty=lambda: True)
    pipe = SimpleNamespace(isatty=lambda: False)

    assert _color_enabled("auto", stream=tty, environ={}) is True
    assert _color_enabled("auto", stream=pipe, environ={}) is False
    assert _color_enabled("always", stream=pipe, environ={}) is True
    assert _color_enabled("never", stream=tty, environ={}) is False
    # NO_COLOR is honored, but an explicit --color=always is a stronger request.
    assert _color_enabled("auto", stream=tty, environ={"NO_COLOR": "1"}) is False
    assert _color_enabled("always", stream=tty, environ={"NO_COLOR": "1"}) is True
    assert _color_enabled("auto", stream=tty, environ={"TERM": "dumb"}) is False


def test_color_flag_is_accepted_by_status_and_report_only():
    assert parser().parse_args(["status"]).color == "auto"
    assert parser().parse_args(["report", "--color", "never"]).color == "never"
    with pytest.raises(SystemExit):
        parser().parse_args(["status", "--color", "sometimes"])


CONFIG_TEMPLATE = """\
[runtime]
repo = "."
state_db = "state.db"
log_dir = "logs"
dry_run = false
[github]
repo = "a/b"
ready_label = "go-agent"
[agents.codex]
command = "codex exec -"
"""


def test_json_output_is_never_painted_even_when_color_is_forced(tmp_path, monkeypatch, capsys):
    from issue_agent.cli import async_main

    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(CONFIG_TEMPLATE)
    config = load_config(config_file)
    monkeypatch.setattr("issue_agent.cli.load_config", lambda path: config)
    state = StateStore(config.state_db)
    state.claim(Issue(7, "Check CLI", "Body"), "codex")
    state.update(7, TaskStatus.CODING)

    def render(*, as_json: bool) -> str:
        args = Namespace(
            command="status", json=as_json, active=False, color="always",
            config=str(config_file), verbose=False,
        )
        assert asyncio.run(async_main(args)) == 0
        return capsys.readouterr().out

    machine = render(as_json=True)
    assert json.loads(machine)[0]["status"] == "coding"
    assert "\033[" not in machine

    # Same flags, human format: the color machinery must actually be reachable,
    # so the machine-readable assertion above cannot pass for the wrong reason.
    human = render(as_json=False)
    assert "coding" in human and "\033[" in human


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
    # No flag -> discovery, not a literal default: the file is looked up in the
    # current directory and the two above it (see test_config_discovery.py).
    assert command.parse_args(["status"]).config is None


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
[agents.codex]
command = "codex exec -"
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
