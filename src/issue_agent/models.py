from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PLANNING = "planning"
    PLANNED = "planned"
    CODING = "coding"
    TESTING = "testing"
    REVIEWING = "reviewing"
    PUSHING = "pushing"
    HUMAN_REVIEW = "human_review"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...] = ()
    url: str = ""


@dataclass
class Task:
    issue: Issue
    status: TaskStatus = TaskStatus.PENDING
    agent: str = ""
    branch: str = ""
    worktree: str = ""
    attempts: int = 0
    last_error: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanTask:
    """One item in a planner-produced plan for an issue."""

    title: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "description": self.description}

    @classmethod
    def from_dict(cls, item: dict[str, str]) -> PlanTask:
        return cls(title=item["title"], description=item.get("description", ""))

