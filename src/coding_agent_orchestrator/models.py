from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    CODING = "coding"
    TESTING = "testing"
    REVIEWING = "reviewing"
    PUSHING = "pushing"
    HUMAN_REVIEW = "human_review"
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

