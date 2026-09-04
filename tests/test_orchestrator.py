import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from issue_agent.agents import (
    make_final_fix_prompt,
    make_final_review_prompt,
    make_plan_prompt,
    make_task_prompt,
    make_task_review_prompt,
)
from issue_agent.codegraph import CodegraphConfig
from issue_agent.models import Issue, PlanTask, TaskStatus
from issue_agent.orchestrator import Orchestrator, parse_plan, review_verdict
from issue_agent.process import CommandError, Result
from issue_agent.state import StateStore

APPROVE = "Looks good.\nVERDICT: APPROVE\n"


class _Log:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))

    def review(self, phase, output, **fields):
        self.events.append(("review", {"phase": phase, "output": output, **fields}))


def result(stdout: str = "") -> Result:
    return Result(returncode=0, stdout=stdout, stderr="")


def make_orchestrator(tmp_path: Path, *, attempts: int = 2, reviewer: str = "reviewer") -> Orchestrator:
    app = Orchestrator.__new__(Orchestrator)
    app.config = SimpleNamespace(
        max_attempts=attempts,
        max_task_attempts=attempts,
        checks=(),
        check_timeout_seconds=1800,
        checks_parallel=True,
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
        repo=tmp_path,
        codegraph=CodegraphConfig(),
        review_task_mode="full",
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
        write_feedback_file=Mock(),
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


def test_plan_prompt_appends_guidance_without_touching_base():
    base = make_plan_prompt(Issue(9, "T", "B"), 8)
    assert "codegraph" not in base
    guided = make_plan_prompt(Issue(9, "T", "B"), 8, guidance="GUIDANCE-BLOCK")
    assert guided.startswith(base)
    assert guided.endswith("GUIDANCE-BLOCK")


def test_task_prompt_inlines_titles_only_and_points_to_plan_file():
    plan = [PlanTask("One", "first long description"), PlanTask("Two", "second long description")]
    prompt = make_task_prompt(Issue(9, "T", "B"), plan[1], plan)
    assert "1. One" in prompt
    assert "2. Two" in prompt
    assert ".agent/plan.md" in prompt
    assert "first long description" not in prompt
    assert "second long description" not in prompt


def test_task_prompt_retry_uses_feedback_pointer_and_short_excerpt():
    plan = [PlanTask("One", "D")]
    prompt = make_task_prompt(Issue(9, "T", "B"), plan[0], plan, retry_error="E" * 5000)
    assert ".agent/feedback.md" in prompt
    assert "E" * 800 in prompt
    assert "E" * 801 not in prompt


def test_task_review_prompt_appends_guidance_without_touching_base():
    base = make_task_review_prompt(Issue(9, "T", "B"), PlanTask("One", "D"))
    assert "codegraph" not in base
    guided = make_task_review_prompt(Issue(9, "T", "B"), PlanTask("One", "D"), guidance="G")
    assert guided.startswith(base)
    assert guided.endswith("G")


def test_final_review_prompt_uses_plan_pointer_and_guidance():
    plan = [PlanTask("One", "first long description")]
    base = make_final_review_prompt(Issue(9, "T", "B"), plan, "main")
    assert ".agent/plan.md" in base
    assert "1. One" in base
    assert "first long description" not in base
    guided = make_final_review_prompt(Issue(9, "T", "B"), plan, "main", guidance="G")
    assert guided.startswith(base)
    assert guided.endswith("G")


def test_final_fix_prompt_uses_feedback_pointer_and_short_excerpt():
    prompt = make_final_fix_prompt(Issue(9, "T", "B"), "F" * 5000)
    assert ".agent/feedback.md" in prompt
    assert "F" * 800 in prompt
    assert "F" * 801 not in prompt


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
    app.github.labels.assert_awaited_once_with(4, add=("agent-planned",))
    app.workspaces.push.assert_not_awaited()
    app.github.create_pr.assert_not_awaited()

    app.workspaces.changed.side_effect = [True, True, False]
    assert app.state.claim(issue, "worker") is True
    asyncio.run(app.process(issue, "worker"))

    assert app.agents["planner"].execute.await_count == 1
    app.github.labels.assert_any_await(
        4, add=("agent-running",), remove=("agent-ready", "agent-planned")
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


def test_run_once_routes_issue_without_agent_workflow_label_to_plan_only(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(12, "Needs planning", "A vague request", ("bug", "agent:claude"))
    app.running = {}
    app.github.runnable_issues = AsyncMock(return_value=[])
    app.github.unassigned_issues = AsyncMock(return_value=[issue])
    app._guarded_plan_only = AsyncMock()

    async def run_scheduler() -> None:
        await app.run_once()
        await asyncio.gather(*tuple(app.running.values()))

    asyncio.run(run_scheduler())

    app.github.unassigned_issues.assert_awaited_once_with(20)
    app._guarded_plan_only.assert_awaited_once_with(issue, "planner")
    assert app.state.rows()[0]["status"] == str(TaskStatus.CLAIMED)


def test_run_once_reconciles_labels_for_persisted_human_review(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body", ("agent-running",))
    app.running = {}
    app.github.runnable_issues = AsyncMock(return_value=[issue])
    app.github.unassigned_issues = AsyncMock(return_value=[])
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


def test_resume_fails_when_completed_task_has_no_commit_anchor(tmp_path):
    app = make_orchestrator(tmp_path)
    app.state.claim(Issue(4, "Task", "Body"), "worker")
    app.state.save_plan(4, [PlanTask("One", "D"), PlanTask("Two", "D")])
    app.state.update_plan_task(4, 0, status=TaskStatus.DONE)

    with pytest.raises(CommandError, match="missing commit anchor"):
        asyncio.run(app._reset_to_anchor(tmp_path, 4, 1))

    app.workspaces.reset.assert_not_awaited()


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

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
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


def test_final_fix_agent_error_is_persisted(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),
        result("Need docs.\nVERDICT: REQUEST_CHANGES\n"),
    ]
    app.agents["worker"].execute.side_effect = [result(), CommandError("final agent unavailable")]
    app.workspaces.changed.side_effect = [True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert "final agent unavailable" in app.state.final_context(4)[1]


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


def test_prompts_include_codegraph_guidance_when_index_ready(tmp_path):
    app = make_orchestrator(tmp_path)
    (tmp_path / ".codegraph").mkdir()
    app.workspaces.changed.side_effect = [True, True, False]

    run_process(app, Issue(4, "Task", "Body"))

    planner_prompt = app.agents["planner"].execute.await_args_list[0].args[1]
    assert "codegraph" in planner_prompt
    worker_prompts = [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    assert all("codegraph" in prompt for prompt in worker_prompts)
    reviewer_prompts = [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    assert all("codegraph" in prompt for prompt in reviewer_prompts)


def test_prompts_omit_codegraph_block_without_index(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, True, False]

    run_process(app, Issue(4, "Task", "Body"))

    prompts = [app.agents["planner"].execute.await_args_list[0].args[1]]
    prompts += [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    prompts += [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    assert all("codegraph" not in prompt for prompt in prompts)


def test_task_retry_writes_feedback_file_and_prompt_points_to_it(tmp_path):
    app = make_orchestrator(tmp_path)
    app.agents["worker"].execute.side_effect = [CommandError("first failure"), result()]
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    app.workspaces.write_feedback_file.assert_called_once_with(tmp_path, "first failure")
    retry_prompt = app.agents["worker"].execute.await_args_list[1].args[1]
    assert ".agent/feedback.md" in retry_prompt
    assert "first failure" in retry_prompt


def test_final_fix_writes_feedback_file_and_prompt_points_to_it(tmp_path):
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

    feedback_texts = [call.args[1] for call in app.workspaces.write_feedback_file.call_args_list]
    assert any("Missing docs" in text for text in feedback_texts)
    fix_prompt = app.agents["worker"].execute.await_args_list[1].args[1]
    assert ".agent/feedback.md" in fix_prompt
    assert "Missing docs" in fix_prompt


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


# ---------------------------------------------------------------------------
# review_task_mode tests
# ---------------------------------------------------------------------------

def test_formal_review_mode_skips_llm_reviewer(tmp_path, monkeypatch):
    """When review_task_mode=formal, the LLM reviewer agent is never called for tasks."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "formal"
    monkeypatch.setattr(
        "issue_agent.orchestrator.formal_review",
        lambda ws: FormalReviewResult(approved=True, reason=""),
    )
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    # LLM reviewer not called for task review (only final review)
    reviewer_prompts = [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    task_review_prompts = [p for p in reviewer_prompts if "most recent commit" in p]
    assert len(task_review_prompts) == 0, \
        f"LLM task reviewer should not be called in formal mode, got: {task_review_prompts}"
    app.workspaces.push.assert_awaited_once()


def test_formal_review_mode_rejects_secret_in_diff(tmp_path, monkeypatch):
    """Formal review detects secrets and triggers a retry."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "formal"
    call_count = {"n": 0}

    def fake_formal_review(workspace):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FormalReviewResult(approved=False, reason="Potential AWS access key detected")
        return FormalReviewResult(approved=True, reason="")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", fake_formal_review)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert call_count["n"] == 2  # first rejected, second approved
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_awaited_once()


def test_review_off_mode_skips_all_task_review(tmp_path, monkeypatch):
    """When review_task_mode=off, no task review happens at all."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "off"
    monkeypatch.setattr(
        "issue_agent.orchestrator.formal_review",
        lambda ws: FormalReviewResult(approved=True, reason=""),
    )
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    # reviewer only called for final review, not task review
    reviewer_prompts = [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    task_review_prompts = [p for p in reviewer_prompts if "most recent commit" in p]
    assert len(task_review_prompts) == 0
    app.workspaces.push.assert_awaited_once()


def test_full_review_mode_uses_llm_reviewer(tmp_path, monkeypatch):
    """When review_task_mode=full, the existing LLM reviewer is used for tasks."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "full"
    monkeypatch.setattr(
        "issue_agent.orchestrator.formal_review",
        lambda ws: FormalReviewResult(approved=True, reason=""),
    )
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    reviewer_prompts = [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    task_review_prompts = [p for p in reviewer_prompts if "most recent commit" in p]
    assert len(task_review_prompts) == 1
    app.workspaces.push.assert_awaited_once()


# ---------------------------------------------------------------------------
# agent_call usage logging tests
# ---------------------------------------------------------------------------

def test_execute_agent_logs_usage_and_duration(tmp_path):
    """_execute_agent records an agent_call event with duration and token usage."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(
            returncode=0,
            stdout="done",
            stderr="",
            duration_ms=4200,
            usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
        )
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "prompt", issue_log=log))

    agent_calls = [e for e in log.events if e[0] == "agent_call"]
    assert len(agent_calls) == 1
    _, fields = agent_calls[0]
    assert fields["agent"] == "worker"
    assert fields["role"] == "worker"
    assert fields["duration_ms"] == 4200
    assert fields["input_tokens"] == 100
    assert fields["output_tokens"] == 50
    assert fields["cost_usd"] == 0.01


def test_execute_read_only_logs_usage_with_role(tmp_path):
    """_execute_read_only records agent_call with the caller-supplied role."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    app.agents["reviewer"].execute = AsyncMock(
        return_value=Result(
            returncode=0,
            stdout=APPROVE,
            stderr="",
            duration_ms=3100,
            usage={"input_tokens": 80, "output_tokens": 20, "cache_read_input_tokens": 500},
        )
    )

    asyncio.run(
        app._execute_read_only("reviewer", tmp_path, "p", role="task reviewer", issue_log=log)
    )

    agent_calls = [e for e in log.events if e[0] == "agent_call"]
    assert len(agent_calls) == 1
    _, fields = agent_calls[0]
    assert fields["role"] == "task reviewer"
    assert fields["duration_ms"] == 3100
    assert fields["cache_read_input_tokens"] == 500


def test_execute_agent_without_issue_log_does_not_fail(tmp_path):
    """Backward compat: omitting issue_log skips logging without raising."""
    app = make_orchestrator(tmp_path)
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(returncode=0, stdout="ok", stderr="", duration_ms=10)
    )

    res = asyncio.run(app._execute_agent("worker", tmp_path, "prompt"))
    assert res.stdout == "ok"


def test_execute_agent_logs_duration_when_usage_absent(tmp_path):
    """Plain-text CLI (no JSON envelope): duration still logged, tokens omitted."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(returncode=0, stdout="ok", stderr="", duration_ms=999, usage=None)
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "prompt", issue_log=log))

    agent_calls = [e for e in log.events if e[0] == "agent_call"]
    assert len(agent_calls) == 1
    _, fields = agent_calls[0]
    assert fields["duration_ms"] == 999
    assert "input_tokens" not in fields


def test_execute_agent_accumulates_usage_in_state_db(tmp_path):
    """Dual-write: agent_call usage also lands in the state DB totals."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(
            returncode=0,
            stdout="done",
            stderr="",
            duration_ms=2000,
            usage={"input_tokens": 100, "output_tokens": 40, "cost_usd": 0.008},
        )
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "prompt", issue_log=log, issue_number=4))

    row = next(r for r in app.state.rows() if r["issue_number"] == 4)
    assert row["total_input_tokens"] == 100
    assert row["total_output_tokens"] == 40
    assert row["total_cost_usd"] == 0.008
    assert row["total_duration_ms"] == 2000
    # JSONL log still written (dual-channel preserved)
    assert any(e[0] == "agent_call" for e in log.events)


def test_execute_read_only_accumulates_usage_in_state_db(tmp_path):
    """Read-only agents (planner/reviewer) also accumulate into state DB."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.agents["reviewer"].execute = AsyncMock(
        return_value=Result(
            returncode=0,
            stdout=APPROVE,
            stderr="",
            duration_ms=1500,
            usage={"input_tokens": 60, "output_tokens": 15, "cache_read_input_tokens": 300},
        )
    )

    asyncio.run(
        app._execute_read_only(
            "reviewer", tmp_path, "p", role="task reviewer", issue_log=log, issue_number=4
        )
    )

    row = next(r for r in app.state.rows() if r["issue_number"] == 4)
    assert row["total_input_tokens"] == 60
    assert row["total_cache_read_tokens"] == 300
    assert row["total_duration_ms"] == 1500


def test_execute_agent_without_issue_number_skips_state_write(tmp_path):
    """Backward compat: omitting issue_number logs to JSONL only, no state write."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(
            returncode=0, stdout="ok", stderr="", duration_ms=10,
            usage={"input_tokens": 5},
        )
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "prompt", issue_log=log))

    assert any(e[0] == "agent_call" for e in log.events)
    # no state rows touched
    assert app.state.rows() == []


# ---------------------------------------------------------------------------
# formal review without reviewer_agent (Fix #1)
# ---------------------------------------------------------------------------

def test_formal_review_runs_without_reviewer_agent(tmp_path, monkeypatch):
    """formal mode is deterministic — it must run even with no reviewer agent configured."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, reviewer="")
    app.config.review_task_mode = "formal"
    calls = {"n": 0}

    def fake_formal_review(workspace):
        calls["n"] += 1
        return FormalReviewResult(approved=True, reason="")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", fake_formal_review)
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert calls["n"] == 1, "formal review must run without reviewer_agent"
    app.workspaces.push.assert_awaited_once()
    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)


def test_formal_review_without_reviewer_agent_uses_review_attempt_budget(tmp_path, monkeypatch):
    """With formal review active, attempt_limit is _REVIEW_ATTEMPTS (2), not max_task_attempts (3)."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, attempts=3, reviewer="")
    app.config.review_task_mode = "formal"
    calls = {"n": 0}

    def rejecting_formal_review(workspace):
        calls["n"] += 1
        return FormalReviewResult(approved=False, reason="Forbidden file modified: .env")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", rejecting_formal_review)
    app.workspaces.changed.side_effect = [True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    # review budget is 2 attempts even though max_task_attempts is 3
    assert calls["n"] == 2
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_not_awaited()
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)
    assert "formal review rejected after the allowed fix cycle" in row["last_error"]


def test_check_failure_retries_honor_max_task_attempts_when_review_is_active(tmp_path):
    """With review active, raising max_task_attempts above 2 extends check/agent-failure
    retries; the review-rejection cap stays at two rejections (see the tests below)."""
    app = make_orchestrator(tmp_path, attempts=1)
    app.config.review_task_mode = "formal"
    app.config.reviewer_agent = ""
    app.config.max_task_attempts = 4
    app.agents["worker"].execute.side_effect = CommandError("agent unavailable")
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 4
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)


def test_review_rejection_cap_survives_larger_max_task_attempts(tmp_path, monkeypatch):
    """A larger max_task_attempts must not extend the review fix cycle: the second
    rejection still terminates immediately."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, attempts=1)
    app.config.review_task_mode = "formal"
    app.config.reviewer_agent = ""
    app.config.max_task_attempts = 4
    calls = {"n": 0}

    def rejecting_formal_review(workspace):
        calls["n"] += 1
        return FormalReviewResult(approved=False, reason="Forbidden file modified: .env")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", rejecting_formal_review)
    app.workspaces.changed.side_effect = [True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert calls["n"] == 2
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_not_awaited()
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)
    assert "formal review rejected after the allowed fix cycle" in row["last_error"]


def test_second_review_rejection_terminates_regardless_of_attempt_index(tmp_path, monkeypatch):
    """The two-rejection cap counts rejections, not attempt indexes: after a check
    failure consumes attempt 1, rejections on attempts 2 and 3 still terminate on 3."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, attempts=1)
    app.config.review_task_mode = "formal"
    app.config.reviewer_agent = ""
    app.config.max_task_attempts = 4
    app.config.checks = ("pytest -q",)
    calls = {"shell": 0, "review": 0}

    async def fake_shell(command: str, **kwargs: object) -> Result:
        # call order: baseline capture, attempt-1 checks (fail), attempt-2/3 checks (pass)
        calls["shell"] += 1
        if calls["shell"] == 2:
            raise CommandError("pytest failed: 1 failed")
        return result()

    def rejecting_formal_review(workspace):
        calls["review"] += 1
        return FormalReviewResult(approved=False, reason="Forbidden file modified: .env")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    monkeypatch.setattr("issue_agent.orchestrator.formal_review", rejecting_formal_review)
    app.workspaces.changed.side_effect = [True, True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 3
    assert calls["review"] == 2
    app.workspaces.push.assert_not_awaited()
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)
    assert "formal review rejected after the allowed fix cycle" in row["last_error"]


def test_full_mode_without_reviewer_agent_skips_review(tmp_path, monkeypatch):
    """full mode requires an LLM reviewer; without one, review is skipped (backward compat)."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, reviewer="")
    app.config.review_task_mode = "full"
    calls = {"n": 0}

    def fake_formal_review(workspace):
        calls["n"] += 1
        return FormalReviewResult(approved=True, reason="")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", fake_formal_review)
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert calls["n"] == 0, "full mode must not fall back to formal review"
    app.workspaces.push.assert_awaited_once()


def test_formal_review_git_failure_retries_via_command_error(tmp_path, monkeypatch):
    """A transient git failure in formal review raises CommandError and the task retries."""
    from issue_agent.formal_review import FormalReviewResult
    from issue_agent.process import CommandError

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "formal"
    calls = {"n": 0}

    def flaky_formal_review(workspace):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CommandError("git diff --name-only HEAD^ HEAD failed (exit 128): lock busy")
        return FormalReviewResult(approved=True, reason="")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", flaky_formal_review)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    # first call raised (retryable), second approved — issue still completes
    assert calls["n"] == 2
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_awaited_once()
    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)


def test_process_accumulates_usage_from_all_agent_calls_in_state_db(tmp_path):
    """E2E: a full process() run sums worker + task-reviewer + final-reviewer usage into the DB."""
    app = make_orchestrator(tmp_path)
    # worker: one call with usage
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(
            returncode=0, stdout="", stderr="", duration_ms=1000,
            usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
        )
    )
    # reviewer: task review + final review, both APPROVE with usage
    app.agents["reviewer"].execute = AsyncMock(
        return_value=Result(
            returncode=0, stdout=APPROVE, stderr="", duration_ms=500,
            usage={"input_tokens": 30, "output_tokens": 10, "cost_usd": 0.003},
        )
    )
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)
    row = next(r for r in app.state.rows() if r["issue_number"] == 4)
    # worker(1) + task reviewer(1) + final reviewer(1)
    assert row["total_input_tokens"] == 100 + 30 + 30
    assert row["total_output_tokens"] == 50 + 10 + 10
    assert abs(row["total_cost_usd"] - (0.01 + 0.003 + 0.003)) < 1e-9
    assert row["total_duration_ms"] == 1000 + 500 + 500
    task = app.state.report_rows(4)[0]["tasks"][0]
    # Task metrics include its worker and task reviewer, but not the final reviewer.
    assert task["total_input_tokens"] == 100 + 30
    assert task["total_output_tokens"] == 50 + 10
    assert task["total_duration_ms"] == 1000 + 500


def test_failed_agent_call_is_attributed_to_task_and_counted(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("Implement", "Details")])
    app.agents["worker"].execute = AsyncMock(
        side_effect=CommandError(
            "agent failed",
            result=Result(
                1,
                "",
                "boom",
                duration_ms=700,
                usage={"input_tokens": 40, "output_tokens": 5},
            ),
        )
    )
    log = _Log()

    with pytest.raises(CommandError, match="agent failed"):
        asyncio.run(
            app._execute_agent(
                "worker",
                tmp_path,
                "prompt",
                issue_log=log,
                issue_number=4,
                seq=0,
                attempt=2,
            )
        )

    task = app.state.report_rows(4)[0]["tasks"][0]
    assert task["total_input_tokens"] == 40
    assert task["total_output_tokens"] == 5
    assert task["total_duration_ms"] == 700
    event = next(fields for name, fields in log.events if name == "agent_call")
    assert event["success"] is False
    assert event["sequence"] == 0
    assert event["attempt"] == 2


def test_timed_out_agent_call_duration_is_counted_without_usage(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("Implement", "Details")])
    app.agents["worker"].execute = AsyncMock(
        side_effect=CommandError("timed out", duration_ms=900)
    )

    with pytest.raises(CommandError, match="timed out"):
        asyncio.run(
            app._execute_agent(
                "worker", tmp_path, "prompt", issue_number=4, seq=0, attempt=1
            )
        )

    task = app.state.report_rows(4)[0]["tasks"][0]
    assert task["total_duration_ms"] == 900
    assert task["total_input_tokens"] == 0


def test_worker_session_is_reused_when_resume_command_is_configured(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    execute = AsyncMock(
        side_effect=[
            Result(0, "ok", "", usage={"session_id": "thread-4"}),
            Result(0, "ok", ""),
        ]
    )
    app.agents["worker"] = SimpleNamespace(
        config=SimpleNamespace(
            resume_command=("worker", "resume", "{session_id}"),
            review_resume_command=None,
        ),
        execute=execute,
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "first", issue_number=4))
    asyncio.run(app._execute_agent("worker", tmp_path, "second", issue_number=4))

    assert execute.await_args_list[1].kwargs["session_id"] == "thread-4"


def test_failed_resumed_call_clears_session_so_next_attempt_starts_fresh(tmp_path):
    """A dead/expired session must not poison every retry: after a resumed call
    fails, the stored session is dropped and the next call starts fresh."""
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    execute = AsyncMock(
        side_effect=[
            Result(0, "ok", "", usage={"session_id": "thread-4"}),
            CommandError("resume failed: session not found"),
            Result(0, "ok", ""),
        ]
    )
    app.agents["worker"] = SimpleNamespace(
        config=SimpleNamespace(
            resume_command=("worker", "resume", "{session_id}"),
            review_resume_command=None,
        ),
        execute=execute,
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "first", issue_number=4))
    with pytest.raises(CommandError):
        asyncio.run(app._execute_agent("worker", tmp_path, "second", issue_number=4))
    asyncio.run(app._execute_agent("worker", tmp_path, "third", issue_number=4))

    assert execute.await_args_list[1].kwargs["session_id"] == "thread-4"
    assert "session_id" not in execute.await_args_list[2].kwargs
    assert app.state.load_session(4, "worker", "worker") == ""


def test_failed_resumed_review_call_clears_reviewer_session(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_session(4, "reviewer", "reviewer", "thread-9")
    execute = AsyncMock(side_effect=[CommandError("resume failed"), result(APPROVE)])
    app.agents["reviewer"] = SimpleNamespace(
        config=SimpleNamespace(
            resume_command=None,
            review_resume_command=("reviewer", "resume", "{session_id}"),
        ),
        execute=execute,
    )

    with pytest.raises(CommandError):
        asyncio.run(
            app._execute_read_only(
                "reviewer", tmp_path, "review", role="task reviewer", issue_number=4
            )
        )
    asyncio.run(
        app._execute_read_only(
            "reviewer", tmp_path, "review", role="task reviewer", issue_number=4
        )
    )

    assert execute.await_args_list[0].kwargs["session_id"] == "thread-9"
    assert "session_id" not in execute.await_args_list[1].kwargs
    assert app.state.load_session(4, "reviewer", "reviewer") == ""


def test_failed_issue_requeues_with_configured_ready_label(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.ready_label = "automation-ready"
    issue = Issue(4, "Task", "Body")

    asyncio.run(app._park_or_requeue(issue, failures=1))

    app.github.labels.assert_awaited_once_with(
        4,
        add=("agent-failed", "automation-ready"),
        remove=("agent-running",),
    )


def test_tracked_worker_completion_wakes_scheduler(tmp_path):
    app = make_orchestrator(tmp_path)
    app.running = {}
    app._wake = asyncio.Event()

    async def run():
        app._track(4, asyncio.sleep(0))
        await asyncio.gather(*tuple(app.running.values()))
        await asyncio.sleep(0)
        return app._wake.is_set()

    assert asyncio.run(run()) is True


def test_notification_failure_after_pr_does_not_rerun_implementation(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, False]
    app.github.labels.side_effect = [None, CommandError("label unavailable")]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)
    assert app.agents["worker"].execute.await_count == 1
    log = (tmp_path / "logs" / "issue-4.jsonl").read_text(encoding="utf-8")
    assert "publication_notification_failed" in log
