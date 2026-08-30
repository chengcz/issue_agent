import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from issue_agent.agents import make_plan_prompt
from issue_agent.models import Issue, PlanTask, TaskStatus
from issue_agent.orchestrator import Orchestrator, parse_plan, review_verdict
from issue_agent.process import CommandError, Result
from issue_agent.state import StateStore

APPROVE = "Looks good.\nVERDICT: APPROVE\n"


def result(stdout: str = "") -> Result:
    return Result(returncode=0, stdout=stdout, stderr="")


def make_orchestrator(tmp_path: Path, *, attempts: int = 2, reviewer: str = "reviewer") -> Orchestrator:
    app = Orchestrator.__new__(Orchestrator)
    app.config = SimpleNamespace(
        max_attempts=attempts,
        max_task_attempts=attempts,
        checks=(),
        check_timeout_seconds=1800,
        reviewer_agent=reviewer,
        planner_agent="planner",
        max_tasks=8,
        base_branch="main",
        ready_label="agent-ready",
        default_agent="worker",
        auto_plan_unlabeled=True,
        auto_plan_limit=20,
        log_dir=tmp_path / "logs",
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
        status=AsyncMock(return_value=""),
        changed=AsyncMock(return_value=True),
        commit=AsyncMock(),
        amend=AsyncMock(),
        push=AsyncMock(),
        reset=AsyncMock(),
        clean=AsyncMock(),
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
    app.agent_limits = {name: asyncio.Semaphore(1) for name in app.agents}
    return app


def run_process(app: Orchestrator, issue: Issue) -> None:
    app.state.claim(issue, "worker")
    asyncio.run(app.process(issue, "worker"))


def test_review_verdict_must_be_the_final_line():
    assert review_verdict("Looks good.\nVERDICT: APPROVE\n") == "VERDICT: APPROVE"
    assert review_verdict("VERDICT: APPROVE\nBut this is not done") is None
    assert review_verdict("No verdict") is None


def test_read_only_agent_honors_its_own_concurrency_limit(tmp_path):
    app = make_orchestrator(tmp_path)
    active = 0
    max_active = 0

    async def execute(workspace, prompt, *, review=False):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return result(APPROVE)

    app.agents["reviewer"].execute = execute

    async def run_reviews():
        await asyncio.gather(
            app._execute_read_only(
                "reviewer", tmp_path, "one", role="reviewer", acquire_agent_limit=True
            ),
            app._execute_read_only(
                "reviewer", tmp_path, "two", role="reviewer", acquire_agent_limit=True
            ),
        )

    asyncio.run(run_reviews())
    assert max_active == 1


def test_coding_agent_honors_limit_per_cli_invocation(tmp_path):
    app = make_orchestrator(tmp_path)
    active = 0
    max_active = 0

    async def execute(workspace, prompt, *, review=False):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return result()

    app.agents["worker"].execute = execute

    async def run_workers():
        await asyncio.gather(
            app._execute_agent("worker", tmp_path, "one"),
            app._execute_agent("worker", tmp_path, "two"),
        )

    asyncio.run(run_workers())
    assert max_active == 1


def test_plan_prompt_demands_detail_but_single_line():
    """The planner must be pushed for concrete specs + acceptance criteria while
    being told that the single-line JSON rule limits line breaks, not length."""
    prompt = make_plan_prompt(Issue(9, "T", "B"), 8)
    assert "Acceptance:" in prompt
    assert "files or modules" in prompt
    assert "no raw line breaks" in prompt
    assert "long single line" in prompt


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


def test_parse_plan_tolerates_trailing_comma():
    plan = parse_plan('```json\n[{"title": "A", "description": "B",},]\n```', 8)
    assert [t.title for t in plan] == ["A"]
    assert plan[0].description == "B"


def test_parse_plan_tolerates_newline_inside_string():
    plan = parse_plan('```json\n[{"title": "A", "description": "line1\nline2"}]\n```', 8)
    assert [t.title for t in plan] == ["A"]
    assert plan[0].description == "line1\nline2"


def test_parse_plan_tolerates_realistic_llm_output():
    """Regression: deepseek-pro-0813 output a plan whose descriptions had raw newlines
    and trailing commas; parse must not reject it."""
    raw = '''```json
[
  {
    "title": "Add body-map classification and counting service",
    "description": "Create src/services/body_map.py defining an anatomical body-map
data model and pure helpers.",
  },
  {
    "title": "Add body-map SVG silhouette asset",
    "description": "Add a self-authored human silhouette with a fixed viewBox.",
  }
]
```'''
    plan = parse_plan(raw, 8)
    assert [t.title for t in plan] == [
        "Add body-map classification and counting service",
        "Add body-map SVG silhouette asset",
    ]
    assert "data model and pure helpers." in plan[0].description


def test_parse_plan_tolerates_prose_quotes_inside_string():
    """Stray double quotes used as prose punctuation must not split the string."""
    plan = parse_plan('```json\n[{"title": "call "body" map", "description": "use the "x" filter" }]\n```', 8)
    assert plan[0].title == 'call "body" map'
    assert plan[0].description == 'use the "x" filter'


def test_parse_plan_error_includes_context_snippet():
    """An unrepairable plan should surface the offending text in the error message."""
    with pytest.raises(CommandError, match="near:"):
        parse_plan('```json\n[{"title": "A", "description: B"}]\n```', 8)


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
    assert app.state.load_plan(4) == [PlanTask("Task", "Body")]
    assert app.state.plan_task_statuses(4) == [TaskStatus.DONE]
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
    review_log = (tmp_path / "logs" / "issue-4.reviews.jsonl").read_text(encoding="utf-8")
    assert "REQUEST_CHANGES" in review_log
    assert "APPROVE" in review_log
    execution_log = (tmp_path / "logs" / "issue-4.jsonl").read_text(encoding="utf-8")
    assert "task_attempt_failed" in execution_log
    assert "implementation_complete" in execution_log


def test_planner_output_is_persisted_and_commented(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")

    run_process(app, issue)

    assert app.agents["planner"].execute.await_count == 1
    assert "## Agent Plan" in app.github.comment.await_args_list[0].args[1]
    assert app.state.plan_task_statuses(4) == [TaskStatus.DONE, TaskStatus.DONE]


def test_unlabeled_issue_plan_waits_for_ready_before_coding(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Vague task", "Improve this module")
    assert app.state.claim_for_planning(issue, "planner") is True

    asyncio.run(app.plan_only(issue))

    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.PLANNED)
    assert app.agents["planner"].execute.await_count == 1
    assert "agent-ready" in app.github.comment.await_args.args[1]
    app.github.labels.assert_not_awaited()
    app.workspaces.push.assert_not_awaited()
    app.github.create_pr.assert_not_awaited()

    app.workspaces.changed.side_effect = [True, True, False]
    assert app.state.claim(issue, "worker") is True
    asyncio.run(app.process(issue, "worker"))

    assert app.agents["planner"].execute.await_count == 1
    app.github.labels.assert_any_await(
        4, add=("agent-running",), remove=("agent-ready",)
    )
    app.workspaces.push.assert_awaited_once()
    app.github.create_pr.assert_awaited_once()


def test_plan_only_restores_workspace_when_planner_writes_files(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.status.side_effect = ["", " M src/app.py"]
    issue = Issue(4, "Vague task", "Improve this module")
    assert app.state.claim_for_planning(issue, "planner") is True

    asyncio.run(app.plan_only(issue))

    assert app.state.rows()[0]["status"] == str(TaskStatus.FAILED)
    app.workspaces.reset.assert_any_await(tmp_path, "HEAD")
    assert app.workspaces.clean.await_count >= 2
    assert "modified the workspace" in app.github.comment.await_args.args[1]


def test_run_once_routes_unlabeled_issue_to_plan_only(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(12, "Needs planning", "A vague request")
    app.running = {}
    app.github.runnable_issues = AsyncMock(return_value=[])
    app.github.unlabeled_issues = AsyncMock(return_value=[issue])
    app._guarded_plan_only = AsyncMock()

    async def run_scheduler() -> None:
        await app.run_once()
        await asyncio.gather(*tuple(app.running.values()))

    asyncio.run(run_scheduler())

    app.github.unlabeled_issues.assert_awaited_once_with(20)
    app._guarded_plan_only.assert_awaited_once_with(issue, "planner")
    assert app.state.rows()[0]["status"] == str(TaskStatus.CLAIMED)


def test_run_once_reconciles_labels_for_persisted_human_review(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body", ("agent-running",))
    app.running = {}
    app.github.runnable_issues = AsyncMock(return_value=[issue])
    app.github.unlabeled_issues = AsyncMock(return_value=[])
    app.state.claim(issue, "worker")
    app.state.update(4, TaskStatus.HUMAN_REVIEW, pr_url="https://example.test/pr/4")

    asyncio.run(app.run_once())

    app.github.labels.assert_awaited_once_with(
        4,
        add=("human-review",),
        remove=("agent-running", "agent-failed", "agent-ready"),
    )
    assert app.running == {}


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

    async def fake_shell(command: str, **kwargs: object) -> Result:
        # call order: baseline capture, task checks, final attempt-1 checks, final attempt-2 checks
        calls["n"] += 1
        if calls["n"] == 3:  # final checks of attempt 1 fail
            raise CommandError("pytest failed: 1 failed")
        return result()

    monkeypatch.setattr("issue_agent.orchestrator.shell", fake_shell)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    messages = [call.args[1] for call in app.workspaces.commit.await_args_list]
    assert messages == ["feat: One (#4)", "feat: final review fixes (#4)"]
    worker_prompts = [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    assert "pytest failed" in worker_prompts[1]
    assert calls["n"] == 4
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


def test_second_task_review_rejection_stops_without_auto_retry(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result("First feedback.\nVERDICT: REQUEST_CHANGES\n"),
        result("Latest feedback.\nVERDICT: REQUEST_CHANGES\n"),
    ]
    app.workspaces.changed.side_effect = [True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 2
    assert app.agents["reviewer"].execute.await_count == 2
    app.workspaces.push.assert_not_awaited()
    adds = app.github.labels.await_args_list[-1].kwargs["add"]
    removes = app.github.labels.await_args_list[-1].kwargs["remove"]
    assert adds == ("agent-failed",)
    assert "agent-ready" in removes
    assert app.state.plan_task_statuses(4) == [TaskStatus.PENDING]
    assert "Latest feedback" in app.state.plan_task_last_error(4, 0)


def test_invalid_task_review_verdict_requeues_within_failure_budget(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.return_value = result("Review completed without a verdict.")
    app.workspaces.changed.side_effect = [True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 1
    assert app.agents["reviewer"].execute.await_count == 1
    assert app.workspaces.push.await_count == 0
    labels = app.github.labels.await_args_list[-1].kwargs
    assert "agent-ready" in labels["add"]
    assert "agent-failed" in labels["add"]
    assert app.state.plan_task_statuses(4) == [TaskStatus.PENDING]


def test_task_reviewer_write_is_reverted_and_requeued(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.workspaces.status.side_effect = ["", " M src/app.py"]
    app.workspaces.changed.side_effect = [True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    app.workspaces.reset.assert_any_await(tmp_path, "HEAD")
    assert app.workspaces.clean.await_count >= 2
    labels = app.github.labels.await_args_list[-1].kwargs
    assert "agent-ready" in labels["add"]
    assert app.state.plan_task_statuses(4) == [TaskStatus.PENDING]


def test_second_final_review_rejection_stops_without_auto_retry(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),
        result("Need docs.\nVERDICT: REQUEST_CHANGES\n"),
        result("Still missing docs.\nVERDICT: REQUEST_CHANGES\n"),
    ]
    app.workspaces.changed.side_effect = [True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 2
    assert app.agents["reviewer"].execute.await_count == 3
    app.workspaces.push.assert_not_awaited()
    assert app.github.labels.await_args_list[-1].kwargs["add"] == ("agent-failed",)


def test_final_review_retry_resumes_from_final_fix_commit(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),
        result("Need docs.\nVERDICT: REQUEST_CHANGES\n"),
        result("Still missing docs.\nVERDICT: REQUEST_CHANGES\n"),
        result(APPROVE),
    ]
    app.workspaces.changed.side_effect = [True, True, False]
    app.workspaces.head_commit.side_effect = ["task111", "final222"]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    expected_error = (
        "final review requested changes after the allowed fix cycle:\n"
        "Still missing docs.\nVERDICT: REQUEST_CHANGES\n"
    )
    assert app.state.final_context(4) == ("final222", expected_error)
    app.workspaces.reset.reset_mock()
    assert app.state.claim(issue, "worker", max_attempts=3) is True
    asyncio.run(app.process(issue, "worker"))

    app.workspaces.reset.assert_awaited_once_with(tmp_path, "final222")
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_awaited_once()
    assert app.state.final_context(4) == ("final222", "")


def test_invalid_final_review_verdict_requeues_without_coding_fix(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),
        result("Final review omitted its verdict."),
    ]
    app.workspaces.changed.side_effect = [True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 1
    assert app.agents["reviewer"].execute.await_count == 2
    assert app.workspaces.push.await_count == 0
    labels = app.github.labels.await_args_list[-1].kwargs
    assert "agent-ready" in labels["add"]
    assert "agent-failed" in labels["add"]


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


def test_task_attempt_budget_is_independent_from_issue_retry_budget(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.config.max_task_attempts = 1
    app.config.reviewer_agent = ""
    app.agents["worker"].execute.side_effect = CommandError("agent unavailable")
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 1
    row = app.state.rows()[0]
    assert row["failures"] == 1
    assert row["status"] == str(TaskStatus.FAILED)
    assert "agent-ready" in app.github.labels.await_args_list[-1].kwargs["add"]


def test_unexpected_errors_remain_blocked(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.create.side_effect = RuntimeError("database unavailable")

    run_process(app, Issue(4, "Task", "Body"))

    assert app.state.rows()[0]["status"] == str(TaskStatus.BLOCKED)


def test_failure_restores_ready_until_attempt_budget_exhausted(tmp_path):
    app = make_orchestrator(tmp_path, attempts=2)
    app.agents["planner"].execute.side_effect = CommandError("plan boom")
    issue = Issue(4, "Task", "Body")

    assert app.state.claim(issue, "worker", max_attempts=2) is True
    asyncio.run(app.process(issue, "worker"))
    # first failure: attempts=1 < 2 -> kept runnable via agent-ready
    adds = app.github.labels.await_args_list[-1].kwargs["add"]
    removes = app.github.labels.await_args_list[-1].kwargs["remove"]
    assert "agent-ready" in adds
    assert "agent-failed" in adds
    assert "agent-running" in removes
    assert app.state.rows()[0]["failures"] == 1

    app.github.labels.reset_mock()
    app.github.comment.reset_mock()
    assert app.state.claim(issue, "worker", max_attempts=2) is True
    asyncio.run(app.process(issue, "worker"))
    # second failure: failures=2 >= budget -> parked, no agent-ready restored
    adds = app.github.labels.await_args_list[-1].kwargs["add"]
    assert "agent-ready" not in adds
    assert app.state.rows()[0]["failures"] == 2
    assert "agent-ready" in app.github.comment.await_args.args[1]


def test_blocked_restores_ready_until_attempt_budget_exhausted(tmp_path):
    app = make_orchestrator(tmp_path, attempts=2)
    app.workspaces.create.side_effect = RuntimeError("database unavailable")
    issue = Issue(4, "Task", "Body")

    run_process(app, issue)
    # BLOCKED under budget -> kept runnable
    assert "agent-ready" in app.github.labels.await_args_list[-1].kwargs["add"]
    assert app.state.rows()[0]["status"] == str(TaskStatus.BLOCKED)
    assert app.state.rows()[0]["failures"] == 1

    app.github.labels.reset_mock()
    app.github.comment.reset_mock()
    run_process(app, issue)
    # second BLOCKED: budget exhausted -> parked
    adds = app.github.labels.await_args_list[-1].kwargs["add"]
    assert "agent-ready" not in adds
    assert app.state.rows()[0]["failures"] == 2


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
