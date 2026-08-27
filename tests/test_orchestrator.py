import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from coding_agent_orchestrator.models import Issue, TaskStatus
from coding_agent_orchestrator.orchestrator import Orchestrator, review_verdict
from coding_agent_orchestrator.process import CommandError, Result


def result(stdout: str = "") -> Result:
    return Result(returncode=0, stdout=stdout, stderr="")


def make_orchestrator(tmp_path: Path, *, attempts: int = 2) -> Orchestrator:
    app = Orchestrator.__new__(Orchestrator)
    app.config = SimpleNamespace(
        max_attempts=attempts,
        checks=(),
        reviewer_agent="reviewer",
        base_branch="main",
        ready_label="agent-ready",
        dry_run=True,
    )
    app.state = SimpleNamespace(update=Mock())
    app.github = SimpleNamespace(
        labels=AsyncMock(),
        comment=AsyncMock(),
        create_pr=AsyncMock(return_value="dry-run://pr/4"),
    )
    app.workspaces = SimpleNamespace(
        create=AsyncMock(return_value=(tmp_path, "agent/4-task")),
        changed=AsyncMock(return_value=True),
        commit_push=AsyncMock(),
    )
    app.agents = {
        "worker": SimpleNamespace(execute=AsyncMock(return_value=result())),
        "reviewer": SimpleNamespace(execute=AsyncMock()),
    }
    return app


def test_review_verdict_must_be_the_final_line():
    assert review_verdict("Looks good.\nVERDICT: APPROVE\n") == "VERDICT: APPROVE"
    assert review_verdict("VERDICT: APPROVE\nBut this is not done") is None
    assert review_verdict("No verdict") is None


def test_review_changes_are_sent_to_the_next_coding_attempt(tmp_path: Path):
    app = make_orchestrator(tmp_path)
    app.agents["reviewer"].execute.side_effect = [
        result("Tenant filter is missing.\nVERDICT: REQUEST_CHANGES\n"),
        result("The filter is now present.\nVERDICT: APPROVE\n"),
    ]
    issue = Issue(4, "Task", "Body")

    asyncio.run(app.process(issue, "worker"))

    worker_calls = app.agents["worker"].execute.await_args_list
    assert len(worker_calls) == 2
    assert "Tenant filter is missing" in worker_calls[1].args[1]
    assert app.agents["reviewer"].execute.await_count == 2
    app.workspaces.commit_push.assert_awaited_once()
    statuses = [call.args[1] for call in app.state.update.call_args_list]
    assert statuses[-1] == TaskStatus.HUMAN_REVIEW


def test_exhausted_review_changes_are_failed_and_reclaimable(tmp_path: Path):
    app = make_orchestrator(tmp_path, attempts=1)
    app.agents["reviewer"].execute.return_value = result(
        "Tenant filter is missing.\nVERDICT: REQUEST_CHANGES\n"
    )

    asyncio.run(app.process(Issue(4, "Task", "Body"), "worker"))

    app.workspaces.commit_push.assert_not_awaited()
    final_update = app.state.update.call_args_list[-1]
    assert final_update.args[1] == TaskStatus.FAILED
    assert "review requested changes" in final_update.kwargs["last_error"]
    assert app.github.comment.await_args.args[1].startswith("Agent run failed.")


def test_unexpected_errors_remain_blocked(tmp_path: Path):
    app = make_orchestrator(tmp_path)
    app.workspaces.create.side_effect = RuntimeError("database unavailable")

    asyncio.run(app.process(Issue(4, "Task", "Body"), "worker"))

    final_update = app.state.update.call_args_list[-1]
    assert final_update.args[1] == TaskStatus.BLOCKED


def test_command_errors_from_coding_are_retried(tmp_path: Path):
    app = make_orchestrator(tmp_path)
    app.agents["worker"].execute.side_effect = [CommandError("first failure"), result()]
    app.agents["reviewer"].execute.return_value = result("Done.\nVERDICT: APPROVE\n")

    asyncio.run(app.process(Issue(4, "Task", "Body"), "worker"))

    assert app.agents["worker"].execute.await_count == 2
    second_prompt = app.agents["worker"].execute.await_args_list[1].args[1]
    assert "first failure" in second_prompt
