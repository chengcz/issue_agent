import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from coding_agent_orchestrator.models import Issue, PlanTask, TaskStatus
from coding_agent_orchestrator.orchestrator import Orchestrator, parse_plan, review_verdict
from coding_agent_orchestrator.process import CommandError, Result
from coding_agent_orchestrator.state import StateStore

APPROVE = "Looks good.\nVERDICT: APPROVE\n"


def result(stdout: str = "") -> Result:
    return Result(returncode=0, stdout=stdout, stderr="")


def make_orchestrator(tmp_path: Path, *, attempts: int = 2, reviewer: str = "reviewer") -> Orchestrator:
    app = Orchestrator.__new__(Orchestrator)
    app.config = SimpleNamespace(
        max_attempts=attempts,
        checks=(),
        reviewer_agent=reviewer,
        planner_agent="planner",
        max_tasks=8,
        base_branch="main",
        ready_label="agent-ready",
        dry_run=True,
    )
    app.state = StateStore(tmp_path / "state.db")
    app.github = SimpleNamespace(
        labels=AsyncMock(),
        comment=AsyncMock(),
        create_pr=AsyncMock(return_value="dry-run://pr/4"),
    )
    app.workspaces = SimpleNamespace(
        create=AsyncMock(return_value=(tmp_path, "agent/4-task")),
        changed=AsyncMock(return_value=True),
        commit=AsyncMock(),
        amend=AsyncMock(),
        push=AsyncMock(),
        reset=AsyncMock(),
        head_commit=AsyncMock(return_value="abc1234"),
        write_plan_file=Mock(),
        write_task_file=Mock(),
    )
    app.agents = {
        "worker": SimpleNamespace(execute=AsyncMock(return_value=result())),
        "planner": SimpleNamespace(
            execute=AsyncMock(
                return_value=result(
                    '```json\n[{"title": "One", "description": "D"}, {"title": "Two", "description": "D"}]\n```\n'
                )
            )
        ),
        "reviewer": SimpleNamespace(execute=AsyncMock(return_value=result(APPROVE))),
    }
    return app


def run_process(app: Orchestrator, issue: Issue) -> None:
    app.state.claim(issue, "worker")
    asyncio.run(app.process(issue, "worker"))


def test_review_verdict_must_be_the_final_line():
    assert review_verdict("Looks good.\nVERDICT: APPROVE\n") == "VERDICT: APPROVE"
    assert review_verdict("VERDICT: APPROVE\nBut this is not done") is None
    assert review_verdict("No verdict") is None


def test_parse_plan_accepts_fenced_json_with_prose():
    plan = parse_plan(
        "Here is my plan:\n```json\n[{\"title\": \"A\", \"description\": \"B\"}]\n```\nDone.", 8
    )
    assert [t.title for t in plan] == ["A"]


def test_parse_plan_rejects_missing_fence():
    with pytest.raises(CommandError):
        parse_plan("no fence here", 8)


def test_parse_plan_rejects_invalid_json():
    with pytest.raises(CommandError):
        parse_plan("```json\nnot json\n```", 8)


def test_parse_plan_rejects_empty_list():
    with pytest.raises(CommandError):
        parse_plan("```json\n[]\n```", 8)


def test_parse_plan_rejects_too_many_tasks():
    with pytest.raises(CommandError):
        parse_plan('```json\n[{"title": "A"}, {"title": "B"}, {"title": "C"}]\n```', 2)


def test_single_task_fallback_without_planner(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.planner_agent = ""
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")

    run_process(app, issue)

    assert app.agents["planner"].execute.await_count == 0
    assert app.workspaces.commit.await_args_list[0].args[1] == "feat: Task (#4)"
    assert app.workspaces.amend.await_count == 0
    app.workspaces.push.assert_awaited_once()
    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)


def test_task_review_changes_amend_the_task_commit(tmp_path):
    app = make_orchestrator(tmp_path)
    app.agents["reviewer"].execute.side_effect = [
        result("Missing validation.\nVERDICT: REQUEST_CHANGES\n"),
        result("Fixed.\nVERDICT: APPROVE\n"),
        result(APPROVE),  # task two
        result(APPROVE),  # final review
    ]
    app.workspaces.changed.side_effect = [True, True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D"), PlanTask("Two", "D")])

    run_process(app, issue)

    # plan already persisted -> planner not re-invoked
    assert app.agents["planner"].execute.await_count == 0
    # one commit per task; the rejected task was amended once
    messages = [call.args[1] for call in app.workspaces.commit.await_args_list]
    assert messages == ["feat: One (#4)", "feat: Two (#4)"]
    assert app.workspaces.amend.await_count == 1
    app.workspaces.push.assert_awaited_once()
    # review feedback reached the next coding attempt
    worker_prompts = [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    assert "Missing validation" in worker_prompts[1]
    assert app.state.plan_task_statuses(4) == [TaskStatus.DONE, TaskStatus.DONE]
    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)


def test_planner_output_is_persisted_and_commented(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")

    run_process(app, issue)

    assert app.agents["planner"].execute.await_count == 1
    assert "## Agent Plan" in app.github.comment.await_args_list[0].args[1]
    assert app.state.plan_task_statuses(4) == [TaskStatus.DONE, TaskStatus.DONE]


def test_resume_reuses_plan_and_resets_to_last_done_commit(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D"), PlanTask("Two", "D")])
    app.state.update_plan_task(4, 0, status=TaskStatus.DONE, commit_hash="aaaa1111")

    run_process(app, issue)

    assert app.agents["planner"].execute.await_count == 0
    app.workspaces.reset.assert_awaited_once_with(tmp_path, "aaaa1111")
    # only the second task is implemented and committed
    assert [call.args[1] for call in app.workspaces.commit.await_args_list] == ["feat: Two (#4)"]
    assert app.agents["worker"].execute.await_count == 1
    app.workspaces.push.assert_awaited_once()


def test_final_review_changes_produce_a_fix_commit(tmp_path):
    app = make_orchestrator(tmp_path)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),  # task one
        result("Missing docs.\nVERDICT: REQUEST_CHANGES\n"),  # final review
        result("Docs added.\nVERDICT: APPROVE\n"),  # final re-review
    ]
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    messages = [call.args[1] for call in app.workspaces.commit.await_args_list]
    assert messages == ["feat: One (#4)", "feat: final review fixes (#4)"]
    worker_prompts = [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    assert "Missing docs" in worker_prompts[1]
    app.workspaces.push.assert_awaited_once()


def test_final_checks_failure_triggers_a_fix_commit(tmp_path, monkeypatch):
    app = make_orchestrator(tmp_path)
    app.config.checks = ("pytest -q",)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),  # task one
        result(APPROVE),  # final attempt 1 (checks fail)
        result(APPROVE),  # final attempt 2 after fix
    ]
    calls = {"n": 0}

    async def fake_shell(check: str, **kwargs: object) -> Result:
        calls["n"] += 1
        if calls["n"] == 2:  # final checks of attempt 1 fail
            raise CommandError("pytest failed: 1 failed")
        return result()

    monkeypatch.setattr("coding_agent_orchestrator.orchestrator.shell", fake_shell)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    messages = [call.args[1] for call in app.workspaces.commit.await_args_list]
    assert messages == ["feat: One (#4)", "feat: final review fixes (#4)"]
    worker_prompts = [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    assert "pytest failed" in worker_prompts[1]
    app.workspaces.push.assert_awaited_once()


def test_exhausted_task_review_fails_and_keeps_issue_reclaimable(tmp_path):
    app = make_orchestrator(tmp_path, attempts=1)
    app.agents["reviewer"].execute.return_value = result("Bad.\nVERDICT: REQUEST_CHANGES\n")
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    app.workspaces.push.assert_not_awaited()
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)
    assert "review requested changes" in row["last_error"]
    assert app.github.comment.await_args.args[1].startswith("Agent run failed.")
    # the failed task is not DONE, so a later re-claim resumes it
    assert app.state.plan_task_statuses(4) != [TaskStatus.DONE]


def test_command_errors_from_coding_are_retried(tmp_path):
    app = make_orchestrator(tmp_path)
    app.agents["worker"].execute.side_effect = [CommandError("first failure"), result()]
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert app.agents["worker"].execute.await_count == 2
    assert "first failure" in app.agents["worker"].execute.await_args_list[1].args[1]
    app.workspaces.push.assert_awaited_once()


def test_unexpected_errors_remain_blocked(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.create.side_effect = RuntimeError("database unavailable")

    run_process(app, Issue(4, "Task", "Body"))

    assert app.state.rows()[0]["status"] == str(TaskStatus.BLOCKED)


def test_recovery_replans_planning_and_resumes_planned(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(1, "A", ""), "codex")
    state.update(1, TaskStatus.PLANNING)
    state.claim(Issue(2, "B", ""), "codex")
    state.update(2, TaskStatus.CODING)
    state.save_plan(2, [PlanTask("T1", "d")])
    state.update_plan_task(2, 0, status=TaskStatus.CODING)

    assert state.recover_interrupted() == 2

    rows = {row["issue_number"]: row for row in state.rows()}
    assert rows[1]["status"] == str(TaskStatus.PENDING)
    assert rows[2]["status"] == str(TaskStatus.PLANNED)
    assert state.plan_task_statuses(2) == [TaskStatus.PENDING]


def test_recovery_marks_inflight_without_plan_failed(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(3, "C", ""), "codex")
    state.update(3, TaskStatus.TESTING)

    assert state.recover_interrupted() == 1
    assert state.rows()[0]["status"] == str(TaskStatus.FAILED)
