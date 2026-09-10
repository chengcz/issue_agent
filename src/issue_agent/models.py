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
    # The issue was superseded by child issues the planner proposed; a human
    # resets the row to plan it again as a single unit.
    SPLIT = "split"


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...] = ()
    url: str = ""
    blocked_by: tuple[int, ...] = ()
    parent: int | None = None


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


@dataclass(frozen=True)
class Comment:
    """One issue comment, as far as reply detection needs it.

    ``created_at`` stays the raw GitHub timestamp: callers comparing it against a
    locally taken marker own that policy, and a string that cannot be parsed must
    be rejected where the comparison happens rather than silently here.
    """

    author: str
    created_at: str
    body: str


@dataclass(frozen=True)
class Blocker:
    """One native ``blockedBy`` entry, as far as the dependency gate needs it.

    ``title`` is carried so the one-shot "waiting on" comment can name each
    blocker instead of only numbering it. Defaults describe the pessimistic
    reading a caller must take when a lookup came back with nothing: open.
    """

    number: int
    title: str = ""
    closed: bool = False


@dataclass(frozen=True)
class SplitChild:
    """One child issue the planner proposes when the request is too large.

    ``depends_on`` holds indexes into the same proposal, expressing the order in
    which the children must be implemented, so ordering never has to be parsed
    out of prose.
    """

    title: str
    body: str
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class PlanOutcome:
    """Exactly one of the planner's three output shapes.

    A bare task list keeps the historical behavior. ``questions`` asks the human
    for the information planning is missing instead of guessing. ``split``
    proposes independent child issues for a request too large for one PR.
    """

    tasks: tuple[PlanTask, ...] = ()
    questions: tuple[str, ...] = ()
    split: tuple[SplitChild, ...] = ()

