"""Crash interruptions count against the whole-issue failure budget.

A restart-recoverable issue must not loop forever: each interruption consumes one
budget unit, and once the budget is exhausted the row parks as FAILED (claimable
only via a human `reset`), stopping endless token burn on crash-inducing issues.
"""

from issue_agent.models import Issue, PlanTask, TaskStatus
from issue_agent.state import StateStore


def test_recover_increments_failures_and_keeps_resumable_status(tmp_path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(9, "Task", "Body")
    state.claim(issue, "worker")
    state.save_plan(9, [PlanTask("One", "D")])
    state.update(9, TaskStatus.CODING)

    assert state.recover_interrupted(3) == 1
    row = state.rows()[0]
    assert row["failures"] == 1
    assert row["status"] == str(TaskStatus.PLANNED)
    assert state.claim(issue, "worker", max_attempts=3) is True


def test_recover_counts_no_plan_interruption_too(tmp_path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(9, "Task", "Body")
    state.claim(issue, "worker")
    state.update(9, TaskStatus.TESTING)

    assert state.recover_interrupted(3) == 1
    row = state.rows()[0]
    assert row["failures"] == 1
    assert row["status"] == str(TaskStatus.FAILED)


def test_recover_parks_task_interruption_when_budget_exhausted(tmp_path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(9, "Task", "Body")
    state.claim(issue, "worker")
    state.save_plan(9, [PlanTask("One", "D")])
    state.record_failure(9, TaskStatus.FAILED, "earlier failure")
    assert state.claim(issue, "worker", max_attempts=2) is True
    state.update(9, TaskStatus.CODING)

    assert state.recover_interrupted(2) == 1
    row = state.rows()[0]
    assert row["failures"] == 2
    assert row["status"] == str(TaskStatus.FAILED)
    assert state.claim(issue, "worker", max_attempts=2) is False


def test_recover_parks_planning_interruption_when_budget_exhausted(tmp_path):
    state = StateStore(tmp_path / "state.db")
    issue = Issue(9, "Task", "Body")
    state.claim_for_planning(issue, "planner", 1)
    state.update(9, TaskStatus.PLANNING)

    assert state.recover_interrupted(1) == 1
    row = state.rows()[0]
    assert row["failures"] == 1
    assert row["status"] == str(TaskStatus.FAILED)
    assert state.claim_for_planning(issue, "planner", 1) is False
