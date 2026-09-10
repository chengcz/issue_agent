from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

from .agents import (
    CliAgent,
    make_final_fix_prompt,
    make_final_review_prompt,
    make_plan_prompt,
    make_task_prompt,
    make_task_review_prompt,
)
from .checks import CheckBaseline, capture_baseline, run_checks
from .codegraph import guidance_block
from .config import Config
from .formal_review import formal_review
from .github import GitHub
from .issue_log import IssueLog
from .models import (
    Blocker,
    Comment,
    Issue,
    PlanOutcome,
    PlanTask,
    RecordedChild,
    SplitChild,
    TaskStatus,
)
from .process import CommandError, Result
from .schedule import ScheduleConfig
from .state import RUNNING_STATUSES, StateStore
from .workspace import WorkspaceManager

log = logging.getLogger(__name__)
_REVIEW_ATTEMPTS = 2


_RUNNING_STATUSES = frozenset(str(status) for status in RUNNING_STATUSES)


class ReviewRejected(CommandError):
    """A review remains rejected after its single allowed fix cycle."""


class InvalidReviewVerdict(CommandError):
    """A reviewer response cannot be acted on safely and should be retried later."""


class ReadOnlyViolation(CommandError):
    """A planner or reviewer changed the repository during a read-only run."""


class ReviewChangesRequested(CommandError):
    """A task review asked for changes; retryable within the task loop.

    ``terminal`` carries the message for the ``ReviewRejected`` the task loop
    raises once the allowed fix cycle (two rejections, counted across attempts)
    is exhausted.
    """

    def __init__(self, message: str, *, terminal: str):
        super().__init__(message)
        self.terminal = terminal


def review_verdict(stdout: str) -> str | None:
    """Return a review verdict only when it is the final non-empty output line."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    verdict = lines[-1]
    if verdict in {"VERDICT: APPROVE", "VERDICT: REQUEST_CHANGES"}:
        return verdict
    return None


_STRUCTURAL = ':,]}'


def _repair_json(text: str) -> str:
    """Repair common LLM JSON defects before json.loads.

    Handles trailing commas, raw newlines/tabs inside strings, and stray double
    quotes used as prose punctuation inside strings (escaped via a lookahead:
    a quote only closes a string when followed by a structural character).
    """
    out: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                i += 1
            elif ch == "\\":
                out.append(ch)
                escaped = True
                i += 1
            elif ch == '"':
                j = i + 1
                while j < len(text) and text[j] in " \t\r\n":
                    j += 1
                if j < len(text) and text[j] in _STRUCTURAL:
                    out.append(ch)
                    in_string = False
                    i += 1
                else:
                    out.append('\\"')
                    i += 1
            elif ch == "\n":
                out.append("\\n")
                i += 1
            elif ch == "\r":
                out.append("\\r")
                i += 1
            elif ch == "\t":
                out.append("\\t")
                i += 1
            else:
                out.append(ch)
                i += 1
        elif ch == ",":
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "}]":
                i = j  # drop a trailing comma before a closing bracket
            else:
                out.append(ch)
                i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def has_detailed_plan(body: str) -> bool:
    """Recognize explicit plan sections with multiple concrete action items.

    Ignore examples and template comments; ambiguous prose still needs planning.
    Keep the original body intact when reusing it so context is never discarded.
    """
    text = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    heading = re.compile(
        r"^(?:implementation plan|implementation steps|execution plan|"
        r"proposed implementation|agent plan|plan|实施计划|实现计划|执行计划|"
        r"实施步骤|实现步骤|实现方案|技术方案|开发计划|计划)$", re.IGNORECASE
    )
    action = re.compile(
        r"\b(?:add|update|modify|create|implement|replace|remove|refactor|"
        r"extend|write|test|verify|run|change|move|rename|delete|ensure)\b|"
        r"新增|添加|修改|实现|替换|删除|重构|扩展|编写|测试|验证|运行|更新|调整",
        re.IGNORECASE,
    )
    # Require a concrete implementation reference in addition to action words.
    # A generic "implement the feature / add tests" checklist is not a plan.
    reference = re.compile(
        r"`[^`\n]+`|\b[\w-]+(?:/[\w.-]+)+|"
        r"\b[\w-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|sql|json|toml|yaml|yml|md)\b|"
        r"\b[A-Za-z]\w*\(\)|\b[A-Za-z]+_[A-Za-z_]\w*\b"
    )
    sections: list[list[str]] = []
    steps: list[str] | None = None
    plan_level = 0
    fence = ""
    for line in text.splitlines():
        stripped = line.strip()
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + ",}", stripped):
                fence = ""
            continue
        opening = re.match(r"^(`{3,}|~{3,})", stripped)
        if opening:
            fence = opening.group(1)
            continue
        atx = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
        bold = re.fullmatch(r"\*\*(.+?)\*\*[:：]?", stripped)
        title = atx.group(2) if atx else bold.group(1) if bold else stripped
        title = title.strip("* ").rstrip(":：").strip()
        if heading.fullmatch(title):
            # Nested plan headings belong to the same parent section.
            if steps is None or not atx or len(atx.group(1)) <= plan_level:
                steps = []
                sections.append(steps)
                plan_level = len(atx.group(1)) if atx else 6
            continue
        if bold or (atx and len(atx.group(1)) <= plan_level):
            steps = None
        if atx or bold:
            continue
        item = re.match(r"(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)、]\s*)(.+)", stripped)
        if steps is not None:
            if item:
                steps.append(item.group(1))
            elif steps and line.startswith((" ", "\t")) and stripped:
                steps[-1] += " " + stripped
    for section in sections:
        details = [step for step in section if len(step) >= 12 and action.search(step)]
        if len(details) >= 2 and any(reference.search(step) for step in details):
            return True
    return False


_BLOCKER_DECLARATION = re.compile(
    r"blocked\s+by|depends\s+on|dependenc(?:y|ies)|blockers?\s*[:：]|前置|依赖",
    re.IGNORECASE,
)
_ISSUE_REFERENCE = re.compile(r"#(\d+)")


def declared_blockers(body: str) -> tuple[int, ...]:
    """Issue numbers the body claims to depend on, sorted and deduplicated.

    Only a line that also names a dependency is a declaration: a bare ``#12`` in
    prose, or a ``## 依赖与风险`` heading with nothing under it, is not. Native
    ``blockedBy`` is the relation the gate acts on, so this reading is advisory
    and deliberately loose — a false positive costs one comment, while a miss
    would leave a human believing a body line gates the workflow when it does
    not. Fenced examples are not special-cased for the same reason.
    """
    text = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    found: set[int] = set()
    for line in text.splitlines():
        if _BLOCKER_DECLARATION.search(line):
            found.update(int(number) for number in _ISSUE_REFERENCE.findall(line))
    return tuple(sorted(found))


def _refs(numbers: Iterable[int]) -> str:
    """Render issue numbers as ``#1, #2`` for a comment body."""
    return ", ".join(f"#{number}" for number in numbers) or "none"


def _inheritable_labels(labels: Iterable[str]) -> tuple[str, ...]:
    """The labels a child issue may inherit from its parent, in parent order.

    Never the orchestrator's own: ``agent-*`` is workflow state, so copying
    ``agent-ready`` would start a child nobody released, and ``agent:<name>`` is
    the parent's routing preference, which the human re-applies when releasing
    the child. Product labels such as ``enhancement`` come along because the
    child is the same kind of request.
    """
    return tuple(label for label in labels if not label.startswith(("agent-", "agent:")))


def _child_lines(children: Iterable[RecordedChild]) -> str:
    """Bullet each created child as ``- #12 title``, for an issue comment."""
    return "\n".join(f"- #{child.number} {child.title}" for child in children)


def _split_comment(issue: Issue, children: list[RecordedChild], ready_label: str) -> str:
    """The parent's report: what was created and what happens next.

    Written for a human deciding what to do with the children, so it names the
    label that releases one and the reset that undoes the whole split.
    """
    return (
        "## Issue Agent Split\n\n"
        "This issue was too large for a single branch, so the planner split it into "
        f"{len(children)} child issues:\n\n{_child_lines(children)}\n\n"
        "## What happens next\n\n"
        "Each child is a separate issue that plans and implements on its own. They are "
        f"**not** released automatically: add the `{ready_label}` label to the ones that "
        "should start. Blocked-by links between the children keep them in order, so "
        "releasing the first is enough.\n\n"
        "This issue is parked for human review and is neither planned nor implemented while "
        f"it stays that way. To have it run as one unit instead, add the `{ready_label}` "
        f"label — or run `issue-agent reset {issue.number}` — and close the children you do "
        "not want."
    )


# Both the comment the orchestrator posts and the marker it later looks for, so
# the two cannot drift apart.
NEEDS_INFO_LABEL = "agent-needs-info"
CLARIFY_HEADING = "❓ **More information needed**"


def clarification_notes(comments: list[Comment], login: str) -> str:
    """Render the questions asked and the answers given, oldest first.

    The transcript starts at this machine's first question rather than at the
    top of the thread, so ordinary discussion from before the issue needed
    clarifying is left out while every later round is kept — including the
    answers to the earlier questions, which keeping only the newest round would
    silently drop. Each line is labelled with its author, the only way the model
    can tell a question from an answer.
    """
    machine = login.lower()
    notes: list[str] = []
    for comment in comments:
        if not notes:
            if comment.author.lower() == machine and comment.body.strip().startswith(CLARIFY_HEADING):
                notes.append(comment)
            continue
        notes.append(comment)
    return "\n\n".join(
        f"@{comment.author}: {comment.body.strip()}" for comment in notes
    )


def _timestamp(value: str) -> datetime:
    """Parse the ISO timestamps GitHub and the state store produce.

    Anything unreadable becomes the epoch, so a comment that cannot be dated
    never counts as newer than a marker and reply detection stays conservative.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _planner_payload(stdout: str) -> object:
    """Extract and decode the planner's fenced JSON block."""
    match = re.search(r"```json\s*(.*?)\s*```", stdout, re.DOTALL)
    if not match:
        raise CommandError("plan output contained no fenced ```json block")
    raw = match.group(1)
    try:
        return json.loads(_repair_json(raw), strict=False)
    except json.JSONDecodeError as exc:
        # Include a context snippet so a future planner regression is
        # diagnosable from the issue comment alone, without re-running the model.
        snippet = raw[max(0, exc.pos - 120): exc.pos + 120].replace("\n", "\\n")
        raise CommandError(f"plan JSON is invalid: {exc} near: {snippet}") from exc


def _plan_tasks(items: list, max_tasks: int) -> list[PlanTask]:
    if len(items) > max_tasks:
        raise CommandError(f"plan has {len(items)} tasks, exceeding max_tasks={max_tasks}")
    plan: list[PlanTask] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("title"):
            raise CommandError("each plan task needs a title and description")
        plan.append(PlanTask(title=str(item["title"]), description=str(item.get("description", ""))))
    return plan


def _split_children(items: list, max_children: int) -> list[SplitChild]:
    if len(items) > max_children:
        raise CommandError(
            f"split has {len(items)} children, exceeding max_split_children={max_children}"
        )
    children: list[SplitChild] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("title") or not item.get("body"):
            raise CommandError("each split child needs a title and a body")
        depends_on = item.get("depends_on") or []
        if not isinstance(depends_on, list) or not all(
            isinstance(index, int) and not isinstance(index, bool) for index in depends_on
        ):
            raise CommandError("split child 'depends_on' must be a list of sibling indexes")
        children.append(
            SplitChild(
                title=str(item["title"]), body=str(item["body"]), depends_on=tuple(depends_on)
            )
        )
    _validate_split_order(children)
    return children


def _validate_split_order(children: list[SplitChild]) -> None:
    """Reject sibling dependencies the orchestrator could never create."""
    for index, child in enumerate(children):
        for dependency in child.depends_on:
            if dependency == index:
                raise CommandError(f"split child {index + 1} depends on itself")
            if not 0 <= dependency < len(children):
                raise CommandError(
                    f"split child {index + 1} depends on index {dependency}, "
                    "which is outside the proposed batch"
                )
    remaining = set(range(len(children)))
    while remaining:
        # Peeling off every child whose remaining dependencies are already
        # placed leaves nothing behind only when the graph has a cycle.
        ready = {index for index in remaining if not (set(children[index].depends_on) & remaining)}
        if not ready:
            raise CommandError("split children have a circular depends_on relationship")
        remaining -= ready


def parse_plan(stdout: str, max_tasks: int) -> list[PlanTask]:
    """Parse the planner's fenced JSON block into PlanTasks, validating bounds."""
    payload = _planner_payload(stdout)
    if not isinstance(payload, list) or not payload:
        raise CommandError("plan must be a non-empty list of tasks")
    return _plan_tasks(payload, max_tasks)


def parse_plan_output(
    stdout: str, max_tasks: int, max_children: int, *, allow_split: bool = True
) -> PlanOutcome:
    """Parse whichever of the planner's three output shapes came back.

    A bare array keeps the historical task list. An object carries either the
    questions that block planning or a proposal to split the issue instead.
    ``allow_split`` stays a caller-supplied gate rather than a prompt change:
    the planner may propose a split regardless, and a proposal the configuration
    forbids has to fail loudly instead of being read as an empty plan.
    """
    payload = _planner_payload(stdout)
    if isinstance(payload, list):
        if not payload:
            raise CommandError("plan must be a non-empty list of tasks")
        return PlanOutcome(tasks=tuple(_plan_tasks(payload, max_tasks)))
    if not isinstance(payload, dict):
        raise CommandError("plan output must be a JSON task array or a JSON object")
    shapes = [key for key in ("questions", "split") if key in payload]
    if len(shapes) != 1:
        raise CommandError("plan object must contain exactly one of 'questions' or 'split'")
    if shapes[0] == "questions":
        questions = payload["questions"]
        if not isinstance(questions, list) or not questions:
            raise CommandError("'questions' must be a non-empty list of questions")
        if not all(isinstance(question, str) and question.strip() for question in questions):
            raise CommandError("every planner question must be a non-empty string")
        return PlanOutcome(questions=tuple(question.strip() for question in questions))
    children = payload["split"]
    if not isinstance(children, list) or not children:
        raise CommandError("'split' must be a non-empty list of child issues")
    if not allow_split:
        raise CommandError(
            "the planner proposed splitting this issue but splitting is not enabled; "
            "set allow_split = true in issue-agent.toml to let it create the child issues"
        )
    return PlanOutcome(split=tuple(_split_children(children, max_children)))


def require_tasks(outcome: PlanOutcome) -> list[PlanTask]:
    """Return the planned tasks, refusing an outcome the caller cannot act on."""
    if outcome.questions:
        raise CommandError("planner asked for more information: " + " | ".join(outcome.questions))
    if outcome.split:
        raise CommandError(
            "planner proposed splitting this issue into child issues: "
            + " | ".join(child.title for child in outcome.split)
        )
    return list(outcome.tasks)


def format_plan(plan: list[PlanTask]) -> str:
    return "\n".join(f"{i + 1}. **{task.title}**\n   {task.description}" for i, task in enumerate(plan))


class Orchestrator:
    def __init__(self, config: Config):
        self.config = config
        self.state = StateStore(config.state_db)
        self.github = GitHub(config.github_repo, config.repo, dry_run=config.dry_run)
        self.workspaces = WorkspaceManager(
            config.repo,
            config.worktrees,
            config.base_branch,
            fetch_ttl_seconds=config.fetch_ttl_seconds,
        )
        self.agents = {name: CliAgent(name, item) for name, item in config.agents.items()}
        self.global_limit = asyncio.Semaphore(config.max_workers)
        self.check_limit = asyncio.Semaphore(config.max_check_workers)
        self.agent_limits = {
            name: asyncio.Semaphore(item.max_workers) for name, item in config.agents.items()
        }
        self.database_lock = asyncio.Lock()
        self.running: dict[int, asyncio.Task[None]] = {}
        self._baseline_cache: OrderedDict[
            tuple[str, tuple[str, ...]], tuple[float, dict[str, CheckBaseline]]
        ] = OrderedDict()
        self._baseline_inflight: dict[
            tuple[str, tuple[str, ...]], asyncio.Task[dict[str, CheckBaseline]]
        ] = {}
        self._run_ids: dict[int, int] = {}
        self._viewer_login: str | None = None
        self._wake = asyncio.Event()
        if guidance_block(config.repo, config.codegraph):
            log.info(
                "codegraph index detected at %s; run 'codegraph install' to give agents MCP access",
                config.repo,
            )

    def _codegraph_guidance(self) -> str:
        """Prompt guidance for agents; empty (prompts unchanged) without a ready index."""
        return guidance_block(self.config.repo, self.config.codegraph)

    @staticmethod
    def _session_role(role: str) -> str:
        if "reviewer" in role:
            return "reviewer"
        if role == "planner":
            return "planner"
        return "worker"

    def _resume_session(self, issue_number: int | None, agent_name: str, role: str) -> str:
        if issue_number is None:
            return ""
        agent = self.agents[agent_name]
        config = getattr(agent, "config", None)
        if config is None:
            return ""
        resume = (
            config.review_resume_command
            if role in {"planner", "task reviewer", "final reviewer"}
            else config.resume_command
        )
        if not resume:
            return ""
        return self.state.load_session(issue_number, agent_name, self._session_role(role))

    def recover(self) -> int:
        return self.state.recover_interrupted(self.config.max_attempts)

    def select_agent(self, issue: Issue) -> str:
        requested = next((x.split(":", 1)[1] for x in issue.labels if x.startswith("agent:")), "")
        name = requested or self.config.default_agent
        if name not in self.agents:
            raise ValueError(f"unknown or disabled agent: {name}")
        return name

    def _schedule_allows(self) -> bool:
        allowed = getattr(self.config, "schedule", ScheduleConfig()).allows()
        previous = getattr(self, "_schedule_open", None)
        if allowed != previous:
            if not allowed:
                log.info("execution window closed; skipping new tasks (schedule)")
            elif previous is False:
                log.info("execution window open; accepting new tasks")
            self._schedule_open = allowed
        return allowed

    async def _dependency_gate(self, issue: Issue, row, *, cache: dict[int, Blocker]) -> bool:
        """True when an issue must wait for its native blockers to close.

        Only first admission is gated. An issue already under way is exempt: a
        crashed run keeps its ``agent-running`` label precisely so it can be
        resumed, and a blocker reopening must not strand that recovery. Exempt
        issues are left out of the comments below too, so recovery does not
        reopen a conversation about a relation that no longer gates anything.
        The cache spans one poll so several candidates sharing a blocker cost
        one lookup.
        """
        if "agent-running" in issue.labels:
            return False
        if row is not None and row["status"] in _RUNNING_STATUSES:
            return False
        declared = declared_blockers(issue.body)
        await self._warn_dependency_mismatch(issue, declared)
        if issue.number in issue.blocked_by or issue.number in declared:
            # A self-link can never close; hold the issue rather than spin on a
            # dependency that will never resolve, and leave the comment as the
            # only signal a human gets.
            await self._warn_self_dependency(issue)
            self._audit(issue).event("dependency_blocked", blockers=[issue.number])
            return True
        if not issue.blocked_by:
            return False
        missing = [number for number in issue.blocked_by if number not in cache]
        if missing:
            cache.update(await self.github.blocker_states(missing))
        # An unresolved blocker is treated as still open: blocking is the safe
        # direction, and the next poll resolves it.
        open_blockers = [
            blocker
            for blocker in (cache.get(number) or Blocker(number) for number in issue.blocked_by)
            if not blocker.closed
        ]
        await self._announce_blockers(issue, open_blockers)
        if open_blockers:
            self._audit(issue).event(
                "dependency_blocked", blockers=[blocker.number for blocker in open_blockers]
            )
        return bool(open_blockers)

    def _audit(self, issue: Issue) -> IssueLog:
        """An issue log for a check that runs before any run is in flight.

        The gate, its warnings, and reply detection all fire during a poll
        rather than inside a planning or coding attempt, so there is no caller
        with a log to pass down.
        """
        return IssueLog(self.config.log_dir, issue.number)

    async def _announce_blockers(self, issue: Issue, open_blockers: list[Blocker]) -> None:
        """Post the one-shot comment saying what still blocks this issue.

        What was already announced lives in SQLite, so a poll that changes
        nothing stays silent: a comment is sent only when the blocker set is new,
        has grown, or has just emptied.
        """
        notices = self.state.load_blocker_notices(issue.number)
        previous = notices.get("pending")
        current = sorted(blocker.number for blocker in open_blockers)
        # ``previous is None`` means nothing was ever announced, which is also
        # the silent case when there is nothing to announce.
        if current == previous or (previous is None and not current):
            return
        if current:
            listing = "\n".join(
                f"- #{blocker.number} {blocker.title}".rstrip() for blocker in open_blockers
            )
            body = (
                "⏳ **Waiting on dependencies**\n\n"
                "This issue is blocked by:\n\n"
                f"{listing}\n\n"
                "Planning and coding begin once every blocker is closed. If the "
                "relation is wrong, edit this issue's native blocked-by links."
            )
        else:
            body = (
                "✅ **Dependencies closed**\n\n"
                "Every blocker is now closed; this issue resumes on the next poll."
            )
            # Only the poll that empties a previously announced set logs this,
            # so the log records the transition rather than every later poll.
            self._audit(issue).event("dependency_cleared", blockers=previous or [])
        await self.github.comment(issue.number, body)
        notices["pending"] = current
        self.state.save_blocker_notices(issue.number, notices)

    async def _warn_dependency_mismatch(self, issue: Issue, declared: tuple[int, ...]) -> None:
        """Flag blockers the body claims that the native links do not have.

        The gate acts only on native ``blockedBy``, so a body line naming an
        issue the links omit silently does nothing. This runs for every
        candidate — it is pure string work with no API cost — and comments once
        per distinct (declared, native) pair. A body naming nothing is normal and
        stays quiet; the native links may simply not have been written down.
        """
        claimed = tuple(number for number in declared if number != issue.number)
        if not claimed:
            return
        native = tuple(sorted(issue.blocked_by))
        if claimed == native:
            return
        notices = self.state.load_blocker_notices(issue.number)
        if notices.get("declared") == list(claimed) and notices.get("native") == list(native):
            return
        await self.github.comment(
            issue.number,
            "⚠️ **Dependency mismatch**\n\n"
            "The issue body names blockers that the native blocked-by links do "
            "not have. Only the native links gate the workflow, so the body line "
            "is currently ignored.\n\n"
            f"- Body names: {_refs(claimed)}\n"
            f"- Native blocked-by: {_refs(native)}\n\n"
            "Update the links (or the body) so the two agree.",
        )
        self._audit(issue).event(
            "dependency_body_mismatch", declared=list(claimed), native=list(native)
        )
        notices["declared"] = list(claimed)
        notices["native"] = list(native)
        self.state.save_blocker_notices(issue.number, notices)

    async def _warn_self_dependency(self, issue: Issue) -> None:
        """Warn once that an issue lists itself as a blocker."""
        notices = self.state.load_blocker_notices(issue.number)
        if notices.get("self"):
            return
        await self.github.comment(
            issue.number,
            "⚠️ **Self-dependency**\n\n"
            f"This issue is listed as blocked by itself (#{issue.number}), a "
            "relation that can never close. Remove the self-link to let the "
            "workflow proceed.",
        )
        notices["self"] = True
        self.state.save_blocker_notices(issue.number, notices)

    def _eligible(self, row, *, planning: bool = False) -> bool:
        # Read-only prefilter prevents completed/parked candidates from repeatedly
        # waking serve. The transactional claim remains authoritative at admission.
        if row is None:
            return True
        status = row["status"]
        if status in (str(TaskStatus.FAILED), str(TaskStatus.BLOCKED)):
            return int(row["failures"]) < self.config.max_attempts
        if planning:
            # Fresh planning (PENDING, no plan) or republishing a plan that was
            # persisted before a crash cut publication short (PENDING/PLANNED
            # with a plan) — the latter skips the LLM entirely.
            return status == str(TaskStatus.PENDING) or (
                status == str(TaskStatus.PLANNED) and row["plan"]
            )
        return status in (str(TaskStatus.PENDING), str(TaskStatus.PLANNED))

    async def run_once(self) -> None:
        if not self._schedule_allows():
            return
        runnable_call = self.github.runnable_issues(self.config.ready_label)
        if self.config.auto_plan_unlabeled:
            runnable, planning = await asyncio.gather(
                runnable_call, self.github.unassigned_issues(self.config.auto_plan_limit)
            )
        else:
            runnable, planning = await runnable_call, []
        persisted = {int(row["issue_number"]): row for row in self.state.rows()}
        blocker_cache: dict[int, Blocker] = {}
        for issue in runnable:
            if issue.number in self.running:
                continue
            row = persisted.get(issue.number)
            try:
                if row and row["status"] == str(TaskStatus.HUMAN_REVIEW):
                    await self.github.labels(
                        issue.number,
                        add=("human-review",),
                        remove=("agent-running", "agent-failed", self.config.ready_label),
                    )
                    continue
                agent_name = self.select_agent(issue)
                if not self._eligible(row):
                    continue
                if await self._dependency_gate(issue, row, cache=blocker_cache):
                    continue
            except ValueError as exc:
                log.error("issue #%s: %s", issue.number, exc)
                continue
            except CommandError as exc:
                # One candidate's GitHub failure (deleted blocker, label churn,
                # rate limit) must not starve every candidate behind it.
                log.error("issue #%s: admission failed; skipping candidate this poll: %s", issue.number, exc)
                continue
            self._track(issue.number, self._guarded_process(issue, agent_name))

        planner_name = self.config.planner_agent
        if planner_name and planner_name not in self.agents:
            log.error("unknown or disabled planner agent: %s", planner_name)
            return
        for issue in planning:
            if issue.number in self.running:
                continue
            try:
                if not self._eligible(persisted.get(issue.number), planning=True):
                    continue
                if await self._dependency_gate(
                    issue, persisted.get(issue.number), cache=blocker_cache
                ):
                    continue
            except CommandError as exc:
                log.error("issue #%s: admission failed; skipping candidate this poll: %s", issue.number, exc)
                continue
            self._track(issue.number, self._guarded_plan_only(issue, planner_name))

        await self._release_answered_clarifications(persisted)

    def _track(self, issue_number: int, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.running[issue_number] = task

        def done(_task, number=issue_number):
            self.running.pop(number, None)
            wake = getattr(self, "_wake", None)
            if wake is not None:
                wake.set()

        task.add_done_callback(done)

    async def serve(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("scheduler iteration failed")
            wake = getattr(self, "_wake", None)
            if wake is None:
                await asyncio.sleep(self.config.poll_seconds)
                continue
            try:
                await asyncio.wait_for(wake.wait(), timeout=self.config.poll_seconds)
            except TimeoutError:
                pass
            wake.clear()

    async def shutdown(self) -> None:
        if not self.running:
            return
        log.info("waiting for %s active worker(s) to finish", len(self.running))
        await asyncio.gather(*tuple(self.running.values()), return_exceptions=True)

    async def _guarded_process(self, issue: Issue, agent_name: str) -> None:
        if "resource:database-schema" in issue.labels:
            async with self.database_lock, self.global_limit:
                await self._admit_process(issue, agent_name)
        else:
            async with self.global_limit:
                await self._admit_process(issue, agent_name)

    async def _admit_process(self, issue: Issue, agent_name: str) -> None:
        if self._schedule_allows() and self.state.claim(
            issue, agent_name, self.config.max_attempts
        ):
            await self.process(issue, agent_name)

    async def _guarded_plan_only(self, issue: Issue, planner_name: str) -> None:
        async with self.global_limit:
            if self._schedule_allows() and self.state.claim_for_planning(
                issue, planner_name or self.config.default_agent, self.config.max_attempts
            ):
                await self.plan_only(issue)

    async def _clarification(self, issue_number: int) -> str:
        """The question-and-answer transcript to re-plan from, or "".

        Gated on the clarify marker rather than the round counter: a reset
        restores the ask budget (rounds back to zero) on purpose, but the
        conversation that led there is exactly the context a re-plan after that
        reset needs, so it must keep flowing. Issues never asked anything have
        an empty marker and pay no extra API call.
        """
        if not self.state.clarify_state(issue_number)[1]:
            return ""
        return clarification_notes(
            await self.github.comments(issue_number), await self._my_login()
        )

    async def _request_clarification(
        self, issue: Issue, questions: tuple[str, ...], *, marker: str, issue_log: IssueLog
    ) -> None:
        """Publish the planner's questions and hold the issue for a human answer.

        The round budget bounds how many times the planner may ask over the
        issue's life. Once it is spent the label stays on and the comment says
        what a human has to do instead, because the orchestrator has stopped
        watching for a reply.
        """
        rounds = self.state.record_clarify_round(issue.number, marker)
        issue_log.event("clarify_requested", rounds=rounds, questions=list(questions))
        listing = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
        if rounds > self.config.max_clarify_rounds:
            issue_log.event("clarify_exhausted", rounds=rounds)
            body = (
                f"{CLARIFY_HEADING}\n\n{listing}\n\n"
                f"Planning has already asked {rounds - 1} time(s), the configured limit of "
                f"{self.config.max_clarify_rounds}. Answer in a comment and then run "
                "`issue-agent reset` for this Issue to plan again."
            )
        else:
            body = (
                f"{CLARIFY_HEADING}\n\n"
                "Planning could not name concrete tasks from this Issue as it stands. "
                f"Please answer in a comment:\n\n{listing}\n\n"
                "A later poll picks the answer up and plans again; no label needs removing."
            )
        await self.github.comment(issue.number, body)
        await self.github.labels(issue.number, add=(NEEDS_INFO_LABEL,))

    async def _release_answered_clarifications(self, persisted: dict[int, dict]) -> None:
        """Drop ``agent-needs-info`` from issues a human has since answered.

        Driven by SQLite rather than GitHub: the rows carrying a marker are
        exactly the issues waiting on an answer, so finding them costs no extra
        query, and rows whose round budget is spent fall out of the scan on their
        own — their reply must not re-queue them. Removing the label is the whole
        handoff, because that label is what keeps the issue out of the planning
        pool. The marker stays until a plan is produced: it is also the offset the
        clarification transcript is read from.
        """
        login = (await self._my_login()).lower()
        if not login:
            return
        ignored = {author.lower() for author in self.config.clarify_ignore_authors} | {login}
        for row in persisted.values():
            number = int(row["issue_number"])
            marker = str(row.get("clarify_marker") or "")
            if not marker or int(row.get("clarify_rounds") or 0) > self.config.max_clarify_rounds:
                continue
            if number in self.running:
                continue
            try:
                comments = await self.github.comments(number)
            except CommandError as exc:
                log.warning("issue #%s: cannot read comments: %s", number, exc)
                continue
            answered = any(
                comment.author.lower() not in ignored
                and _timestamp(comment.created_at) > _timestamp(marker)
                for comment in comments
            )
            if answered:
                await self.github.labels(number, remove=(NEEDS_INFO_LABEL,))
                IssueLog(self.config.log_dir, number).event("clarify_answered")
                log.info("issue #%s: clarification answered; back in the planning queue", number)

    async def _my_login(self) -> str:
        """This machine's GitHub login, looked up once per process.

        "" means unknown, and every caller must read it as "I cannot tell my own
        comments apart" and skip reply detection. An orchestrator that guessed
        here would count its own question as the human's answer and talk to
        itself, which is worse than leaving the label for a human to remove.
        """
        if self._viewer_login is None:
            try:
                self._viewer_login = await self.github.viewer_login()
            except CommandError as exc:
                log.warning(
                    "cannot determine the GitHub login; clarification replies stay manual: %s", exc
                )
                self._viewer_login = ""
        return self._viewer_login

    def _auto_ready_applies(self, issue: Issue) -> bool:
        """True when an issue may skip human approval of its own plan.

        Every condition matters. Without a configured ``planner_agent`` the
        planner returns "the body is the plan" for *every* issue, so auto-ready
        would release the whole queue — that guard is what keeps this feature
        from deleting the approval step. ``has_detailed_plan`` restricts the
        shortcut to plans a human wrote out in full, so an LLM-authored plan
        still waits for review, and ``parent`` excludes the child issues the
        orchestrator itself created from a split.
        """
        return (
            self.config.auto_ready_with_plan
            and bool(self.config.planner_agent)
            and has_detailed_plan(issue.body)
            and issue.parent is None
        )

    async def plan_only(self, issue: Issue) -> None:
        """Create and publish a plan, then wait for the configured ready label."""
        started = time.monotonic()
        run_id = self.state.start_run(issue.number, "planning")
        run_ids = getattr(self, "_run_ids", None)
        if run_ids is None:
            self._run_ids = run_ids = {}
        run_ids[issue.number] = run_id
        outcome = str(TaskStatus.BLOCKED)
        issue_log = IssueLog(self.config.log_dir, issue.number)
        issue_log.event("plan_only_started", title=issue.title, labels=issue.labels)
        try:
            workspace, branch = await self.workspaces.create(issue)
            issue_log.event("workspace_ready", workspace=workspace, branch=branch)
            self.state.update(
                issue.number, TaskStatus.PLANNING, branch=branch, worktree=str(workspace)
            )
            recorded_split = self.state.load_split(issue.number)
            if recorded_split:
                issue_log.event("split_reused", children=len(recorded_split))
                await self._complete_split(issue, recorded_split, issue_log=issue_log)
                outcome = str(TaskStatus.SPLIT)
                return
            plan = self.state.load_plan(issue.number)
            if plan is None:
                planned = await self._plan(
                    workspace,
                    issue,
                    acquire_agent_limit=bool(self.config.planner_agent),
                    issue_log=issue_log,
                    clarification=await self._clarification(issue.number),
                )
                if planned.split:
                    # The decision lands before the first creation, so a crash
                    # mid-split resumes from the record rather than asking the
                    # planner for a second, differently-worded proposal.
                    recorded = [RecordedChild.from_child(child) for child in planned.split]
                    self.state.save_split(issue.number, recorded)
                    issue_log.event(
                        "split_proposed", children=[child.title for child in recorded]
                    )
                    await self._complete_split(issue, recorded, issue_log=issue_log)
                    outcome = str(TaskStatus.SPLIT)
                    return
                if planned.questions:
                    # The marker has to predate the comment, so that any reply
                    # that follows is necessarily newer than it.
                    await self._request_clarification(
                        issue,
                        planned.questions,
                        marker=datetime.now(UTC).isoformat(),
                        issue_log=issue_log,
                    )
                    # PENDING with no plan is what makes the issue claimable
                    # again; waiting for a human is not a failure, so it must
                    # not burn the retry budget either.
                    self.state.update(issue.number, TaskStatus.PENDING)
                    outcome = str(TaskStatus.PENDING)
                    return
                plan = require_tasks(planned)
                issue_log.event("plan_generated", tasks=[task.to_dict() for task in plan])
            else:
                issue_log.event("plan_reused", tasks=[task.to_dict() for task in plan])
            # A plan that exists means the outstanding question has been dealt
            # with, so stop looking for a reply to it.
            self.state.clear_clarify(issue.number)
            self.workspaces.write_plan_file(workspace, plan)
            self.state.update(issue.number, TaskStatus.PLANNED, current_seq=0)
            outcome = str(TaskStatus.PLANNED)
            if self._auto_ready_applies(issue):
                issue_log.event("auto_ready_applied", plan_tasks=len(plan))
                await self.github.comment(
                    issue.number,
                    "## Issue Agent Plan\n\n"
                    + format_plan(plan)
                    + "\n\n## Auto-approved\n\n"
                    "The issue body already carried a complete implementation plan, so "
                    "`auto_ready_with_plan` published it as-is and added the "
                    f"`{self.config.ready_label}` label. Implementation starts on the "
                    "next poll without a second planning pass.",
                )
                await self.github.labels(issue.number, add=(self.config.ready_label,))
            else:
                issue_log.event("awaiting_human_approval", plan_tasks=len(plan))
                approval_label = f"`{self.config.ready_label}`"
                await self.github.comment(
                    issue.number,
                    "## Issue Agent Plan\n\n"
                    + format_plan(plan)
                    + "\n\n## Human approval required\n\n"
                    + f"Review or update this Issue and the plan above. Add the {approval_label} "
                    "label when implementation may begin. Until then, Issue Agent will not "
                    "modify code, push a branch, or create a pull request.",
                )
                await self.github.labels(issue.number, add=("agent-planned",))
        except CommandError as exc:
            log.error("issue #%s planning failed: %s", issue.number, exc)
            failures = self.state.record_failure(issue.number, TaskStatus.FAILED, str(exc))
            outcome = str(TaskStatus.FAILED)
            issue_log.event("plan_failed", failures=failures, error=str(exc))
            await self._comment_planning_failure(issue, failures, str(exc))
        except Exception as exc:
            log.exception("issue #%s planning blocked", issue.number)
            failures = self.state.record_failure(issue.number, TaskStatus.BLOCKED, str(exc))
            outcome = str(TaskStatus.BLOCKED)
            issue_log.event("plan_blocked", failures=failures, error=str(exc))
            await self._comment_planning_failure(issue, failures, str(exc))
        finally:
            self.state.finish_run(
                run_id,
                issue.number,
                outcome,
                wall_duration_ms=int((time.monotonic() - started) * 1000),
            )
            run_ids.pop(issue.number, None)

    async def _complete_split(
        self, issue: Issue, children: list[RecordedChild], *, issue_log: IssueLog
    ) -> None:
        """Create and link the child issues, then park the parent for review.

        Creation is idempotent by issue number, never by title: the proposal
        comes from an LLM whose wording changes between runs, so only the number
        an earlier attempt wrote back identifies a child that already exists.
        Each creation is persisted the moment it succeeds, and linking waits
        until every child exists — a sibling link needs both ends to be real.
        """
        created = list(children)
        try:
            for index, child in enumerate(created):
                if child.number:
                    continue
                number, url = await self.github.create_issue(
                    child.title, child.body, labels=_inheritable_labels(issue.labels)
                )
                if not number:
                    # gh reported success without an issue number, which is also
                    # what dry-run reports. Recording a placeholder would make a
                    # later attempt skip a child that was never created.
                    raise CommandError(
                        f"created child issue {index + 1} but gh reported no issue number"
                    )
                created[index] = replace(child, number=number, url=url)
                self.state.update_split_child(issue.number, index, number=number, url=url)
            for index, child in enumerate(created):
                if not child.number or child.linked:
                    continue
                await self.github.link_parent(child.number, issue.number)
                for dependency in child.depends_on:
                    await self.github.add_blocked_by(child.number, created[dependency].number)
                created[index] = replace(child, linked=True)
                self.state.update_split_child(issue.number, index, linked=True)
        except CommandError as exc:
            done = [child for child in created if child.number]
            issue_log.event(
                "split_partial",
                created=[child.number for child in done],
                error=str(exc),
            )
            if done:
                # Partially created: the parent stays on the retry path rather
                # than going to review, but the human still gets to see which
                # children already exist.
                raise CommandError(
                    f"{exc}\n\nChild issues created so far:\n{_child_lines(done)}"
                ) from exc
            raise
        issue_log.event(
            "split_created",
            children=[{"number": child.number, "title": child.title} for child in created],
        )
        self.state.update(issue.number, TaskStatus.SPLIT)
        await self.github.labels(
            issue.number,
            add=("human-review",),
            remove=("agent-running", "agent-planned", self.config.ready_label),
        )
        await self.github.comment(
            issue.number, _split_comment(issue, created, self.config.ready_label)
        )

    async def _comment_planning_failure(self, issue: Issue, failures: int, error: str) -> None:
        retry = (
            "Planning will be retried on the next poll."
            if failures < self.config.max_attempts
            else "Planning retry budget is exhausted; manual state reset is required."
        )
        await self.github.comment(
            issue.number,
            f"Issue Agent could not prepare a plan. {retry}\n\n```text\n{error[-3000:]}\n```",
        )

    async def process(self, issue: Issue, agent_name: str) -> None:
        started = time.monotonic()
        run_id = self.state.start_run(issue.number, "implementation")
        run_ids = getattr(self, "_run_ids", None)
        if run_ids is None:
            self._run_ids = run_ids = {}
        run_ids[issue.number] = run_id
        outcome = str(TaskStatus.BLOCKED)
        issue_log = IssueLog(self.config.log_dir, issue.number)
        issue_log.event("implementation_started", title=issue.title, agent=agent_name, labels=issue.labels)
        try:
            workspace, branch = await self.workspaces.create(issue)
            issue_log.event("workspace_ready", workspace=workspace, branch=branch)
            self.state.update(issue.number, TaskStatus.PLANNING, branch=branch, worktree=str(workspace))
            await self.github.labels(
                issue.number,
                add=("agent-running",),
                remove=(self.config.ready_label, "agent-planned"),
            )

            plan = self.state.load_plan(issue.number)
            if plan is None:
                planned = await self._plan(
                    workspace,
                    issue,
                    acquire_agent_limit=bool(self.config.planner_agent),
                    issue_log=issue_log,
                    clarification=await self._clarification(issue.number),
                )
                if planned.questions:
                    # Design-spec §11 D3: a planner that cannot name concrete
                    # tasks on the coding path re-routes through the plan-only
                    # clarification flow instead of burning the whole-issue
                    # failure budget on repeated planner runs.
                    issue_log.event("planner_needs_info", questions=list(planned.questions))
                    await self._request_clarification(
                        issue,
                        planned.questions,
                        marker=datetime.now(UTC).isoformat(),
                        issue_log=issue_log,
                    )
                    # The ready label went with admission; nothing is running
                    # while the issue waits for an answer, so drop the running
                    # label too — the needs-info label is what keeps the issue
                    # out of the scheduling pools.
                    await self.github.labels(issue.number, remove=("agent-running",))
                    self.state.update(issue.number, TaskStatus.PENDING)
                    outcome = str(TaskStatus.PENDING)
                    return
                if planned.split:
                    # The decision lands before the first creation, so a crash
                    # mid-split resumes from the record rather than asking the
                    # planner for a second, differently-worded proposal.
                    recorded = [RecordedChild.from_child(child) for child in planned.split]
                    self.state.save_split(issue.number, recorded)
                    issue_log.event(
                        "split_proposed", children=[child.title for child in recorded]
                    )
                    await self._complete_split(issue, recorded, issue_log=issue_log)
                    outcome = str(TaskStatus.SPLIT)
                    return
                plan = require_tasks(planned)
                await self.github.comment(issue.number, "## Agent Plan\n\n" + format_plan(plan))
                issue_log.event("plan_generated", tasks=[task.to_dict() for task in plan])
            else:
                issue_log.event("plan_reused", tasks=[task.to_dict() for task in plan])
            start_seq = self._resume_seq(issue.number, plan)
            final_commit, _ = self.state.final_context(issue.number)
            await self._reset_to_anchor(
                workspace,
                issue.number,
                start_seq,
                final_commit=final_commit if start_seq == len(plan) else None,
            )
            self.state.update(issue.number, TaskStatus.PLANNED, current_seq=start_seq)
            # A push/PR-failure retry resets to the very commit finalize already
            # approved: reuse that result instead of re-reviewing identical work.
            # Without an approved record the head probe is skipped entirely, so
            # first runs keep the historical git call sequence.
            approved = self.state.final_approved_commit(issue.number)
            head = await self.workspaces.head_commit(workspace) if approved else ""
            reuse_final = bool(approved) and approved == head and start_seq == len(plan)
            baseline: dict[str, CheckBaseline] = (
                {}
                if reuse_final
                else await self._capture_baseline(workspace, issue_number=issue.number)
            )
            self.workspaces.write_plan_file(workspace, plan)

            for seq in range(start_seq, len(plan)):
                await self._run_task(workspace, issue, plan, seq, agent_name, issue_log, baseline)

            if reuse_final:
                issue_log.event("final_review_reused", commit=head)
            else:
                await self._finalize(workspace, issue, plan, agent_name, issue_log, baseline)
                self.state.set_final_approved(
                    issue.number, await self.workspaces.head_commit(workspace)
                )
            self.state.update(issue.number, TaskStatus.PUSHING, current_seq=-1)
            issue_log.event("push_started", branch=branch)
            await self.workspaces.push(workspace, branch, dry_run=self.config.dry_run)
            pr_url = await self.github.create_pr(
                issue.number, branch, self.config.base_branch, issue.title, self.config.checks
            )
            self.state.update(issue.number, TaskStatus.HUMAN_REVIEW, pr_url=pr_url)
            outcome = str(TaskStatus.HUMAN_REVIEW)
            issue_log.event("implementation_complete", pr_url=pr_url)
            try:
                await self.github.labels(
                    issue.number,
                    add=("human-review",),
                    remove=("agent-running", "agent-failed"),
                )
                await self.github.comment(
                    issue.number, f"Implementation ready for human review: {pr_url}"
                )
            except CommandError as exc:
                # The PR and HUMAN_REVIEW state are already durable. Treat a
                # notification failure as reconcilable instead of rerunning all work.
                log.warning("issue #%s publication notification failed: %s", issue.number, exc)
                issue_log.event("publication_notification_failed", error=str(exc))
        except CommandError as exc:
            log.error("issue #%s failed: %s", issue.number, exc)
            failures = self.state.record_failure(issue.number, TaskStatus.FAILED, str(exc))
            outcome = str(TaskStatus.FAILED)
            issue_log.event("implementation_failed", failures=failures, error=str(exc))
            if isinstance(exc, ReviewRejected):
                await self._park_after_review_failure(issue)
            else:
                await self._park_or_requeue(issue, failures)
            ready_label = self.config.ready_label
            note = (
                "\n\nReview fix cycle exhausted; inspect the review log, then manually re-add "
                f"the {ready_label} label to retry."
                if isinstance(exc, ReviewRejected)
                else f"\n\nRetry budget exhausted; reset the task row and re-add the "
                f"{ready_label} label to rerun."
                if failures >= self.config.max_attempts
                else ""
            )
            await self.github.comment(issue.number, f"Agent run failed.\n\n```text\n{str(exc)[-3000:]}\n```{note}")
        except Exception as exc:
            log.exception("issue #%s failed", issue.number)
            failures = self.state.record_failure(issue.number, TaskStatus.BLOCKED, str(exc))
            outcome = str(TaskStatus.BLOCKED)
            issue_log.event("implementation_blocked", failures=failures, error=str(exc))
            await self._park_or_requeue(issue, failures)
            note = (
                "\n\nRetry budget exhausted; reset the task row and re-add the "
                f"{self.config.ready_label} label to rerun."
                if failures >= self.config.max_attempts
                else ""
            )
            await self.github.comment(issue.number, f"Agent run blocked.\n\n```text\n{str(exc)[-3000:]}\n```{note}")
        finally:
            self.state.finish_run(
                run_id,
                issue.number,
                outcome,
                wall_duration_ms=int((time.monotonic() - started) * 1000),
            )
            run_ids.pop(issue.number, None)

    async def _park_or_requeue(self, issue: Issue, failures: int) -> None:
        """Keep a failed issue in the runnable pool while its retry budget holds.

        Under budget the configured ready label is restored so the next scheduler poll
        re-claims the issue; once the whole-issue failure budget is exhausted the
        issue is parked until a human resets the task row.
        """
        if failures < self.config.max_attempts:
            await self.github.labels(
                issue.number,
                add=("agent-failed", self.config.ready_label),
                remove=("agent-running",),
            )
        else:
            await self.github.labels(issue.number, add=("agent-failed",), remove=("agent-running",))

    async def _park_after_review_failure(self, issue: Issue) -> None:
        """Stop automatic retries after the coding-review-fix-review cycle is rejected."""
        await self.github.labels(
            issue.number,
            add=("agent-failed",),
            remove=("agent-running", self.config.ready_label),
        )

    async def _plan(
        self,
        workspace,
        issue: Issue,
        *,
        acquire_agent_limit: bool = False,
        issue_log: IssueLog | None = None,
        clarification: str = "",
    ) -> PlanOutcome:
        existing_plan = bool(self.config.planner_agent) and has_detailed_plan(issue.body)
        if not self.config.planner_agent or existing_plan:
            outcome = PlanOutcome(tasks=(PlanTask(title=issue.title, description=issue.body),))
            if existing_plan and issue_log is not None:
                issue_log.event("planner_skipped", reason="issue_contains_detailed_plan")
        else:
            await self._reset_to_anchor(workspace, issue.number, 0)
            result = await self._execute_read_only(
                self.config.planner_agent,
                workspace,
                make_plan_prompt(
                    issue,
                    self.config.max_tasks,
                    guidance=self._codegraph_guidance(),
                    clarification=clarification,
                ),
                role="planner",
                acquire_agent_limit=acquire_agent_limit,
                issue_log=issue_log,
                issue_number=issue.number,
            )
            outcome = parse_plan_output(
                result.stdout,
                self.config.max_tasks,
                self.config.max_split_children,
                allow_split=self.config.allow_split,
            )
        if outcome.tasks:
            self.state.save_plan(issue.number, list(outcome.tasks))
        return outcome

    async def _execute_read_only(
        self,
        agent_name,
        workspace,
        prompt: str,
        *,
        role: str,
        acquire_agent_limit: bool = False,
        issue_log: IssueLog | None = None,
        issue_number: int | None = None,
        seq: int | None = None,
        attempt: int | None = None,
    ):
        """Run a planner/reviewer and restore any repository changes it makes."""
        if acquire_agent_limit:
            async with self.agent_limits[agent_name]:
                return await self._execute_read_only(
                    agent_name,
                    workspace,
                    prompt,
                    role=role,
                    issue_log=issue_log,
                    issue_number=issue_number,
                    seq=seq,
                    attempt=attempt,
                )
        if await self.workspaces.status(workspace):
            raise CommandError(f"cannot start read-only {role}: workspace is not clean")
        session_id = ""
        try:
            session_id = self._resume_session(issue_number, agent_name, role)
            if session_id:
                result = await self.agents[agent_name].execute(
                    workspace, prompt, review=True, session_id=session_id
                )
            else:
                result = await self.agents[agent_name].execute(workspace, prompt, review=True)
        except Exception as exc:
            if session_id and issue_number is not None and isinstance(exc, CommandError):
                # A failed resumed call must not poison the next attempt: drop
                # the stored session so the retry starts a fresh one.
                self.state.clear_session(issue_number, agent_name, self._session_role(role))
            if issue_number is not None and isinstance(exc, CommandError):
                failed_result = exc.result or Result(
                    1, "", "", duration_ms=exc.duration_ms
                )
                self._log_agent_call(
                    issue_log,
                    agent_name,
                    role,
                    failed_result,
                    issue_number=issue_number,
                    seq=seq,
                    attempt=attempt,
                    success=False,
                    error=str(exc),
                )
            if await self.workspaces.status(workspace):
                await self.workspaces.reset(workspace, "HEAD")
                await self.workspaces.clean(workspace)
                raise ReadOnlyViolation(
                    f"read-only {role} modified the workspace before failing"
                ) from exc
            raise
        if await self.workspaces.status(workspace):
            self._log_agent_call(
                issue_log,
                agent_name,
                role,
                result,
                issue_number=issue_number,
                seq=seq,
                attempt=attempt,
                success=False,
                error=f"read-only {role} modified the workspace",
            )
            await self.workspaces.reset(workspace, "HEAD")
            await self.workspaces.clean(workspace)
            raise ReadOnlyViolation(f"read-only {role} modified the workspace")
        self._log_agent_call(
            issue_log,
            agent_name,
            role,
            result,
            issue_number=issue_number,
            seq=seq,
            attempt=attempt,
        )
        return result

    async def _execute_agent(
        self,
        agent_name: str,
        workspace,
        prompt: str,
        *,
        issue_log: IssueLog | None = None,
        issue_number: int | None = None,
        seq: int | None = None,
        attempt: int | None = None,
        role: str = "worker",
    ):
        """Run one coding-agent invocation under that agent's own limit."""
        session_id = ""
        try:
            async with self.agent_limits[agent_name]:
                session_id = self._resume_session(issue_number, agent_name, role)
                if session_id:
                    result = await self.agents[agent_name].execute(
                        workspace, prompt, session_id=session_id
                    )
                else:
                    result = await self.agents[agent_name].execute(workspace, prompt)
        except CommandError as exc:
            if session_id and issue_number is not None:
                # A failed resumed call must not poison the next attempt: drop
                # the stored session so the retry starts a fresh one.
                self.state.clear_session(issue_number, agent_name, self._session_role(role))
            failed_result = exc.result or Result(1, "", "", duration_ms=exc.duration_ms)
            self._log_agent_call(
                issue_log,
                agent_name,
                role,
                failed_result,
                issue_number=issue_number,
                seq=seq,
                attempt=attempt,
                success=False,
                error=str(exc),
            )
            raise
        self._log_agent_call(
            issue_log,
            agent_name,
            role,
            result,
            issue_number=issue_number,
            seq=seq,
            attempt=attempt,
        )
        return result

    def _log_agent_call(
        self,
        issue_log: IssueLog | None,
        agent_name: str,
        role: str,
        result: Result,
        *,
        issue_number: int | None = None,
        seq: int | None = None,
        attempt: int | None = None,
        success: bool = True,
        error: str = "",
    ) -> None:
        """Record token usage and wall-clock duration for one agent invocation.

        Dual-write: the JSONL log always receives the event; the state DB totals
        are updated only when *issue_number* is supplied.
        """
        if issue_log is not None:
            fields: dict[str, object] = {
                "agent": agent_name,
                "role": role,
                "success": success,
            }
            if seq is not None:
                fields["sequence"] = seq
            if attempt is not None:
                fields["attempt"] = attempt
            if error:
                fields["error"] = error
            if result.duration_ms is not None:
                fields["duration_ms"] = result.duration_ms
            if result.usage:
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "cost_usd",
                    "total_cost_usd",
                    "num_turns",
                    "reasoning_output_tokens",
                    "session_id",
                ):
                    if key in result.usage:
                        fields[key] = result.usage[key]
            issue_log.event("agent_call", **fields)
        if issue_number is not None:
            self.state.record_agent_call(
                issue_number,
                run_id=getattr(self, "_run_ids", {}).get(issue_number),
                seq=seq,
                attempt=attempt,
                agent=agent_name,
                role=role,
                success=success,
                duration_ms=result.duration_ms,
                usage=result.usage,
                error=error,
            )
            session_id = str((result.usage or {}).get("session_id") or "")
            # A failed call can still carry a session id (Codex emits
            # thread.started before a turn fails; Claude result envelopes always
            # include one). Saving it would immediately re-insert the session
            # the CommandError handler just cleared, so every retry would resume
            # the same poisoned session until the attempt budget ran out.
            if session_id and success:
                self.state.save_session(
                    issue_number,
                    agent_name,
                    self._session_role(role),
                    session_id,
                )

    def _resume_seq(self, issue_number: int, plan: list[PlanTask]) -> int:
        """First plan index that is not DONE; len(plan) when all tasks are done (final phase)."""
        statuses = self.state.plan_task_statuses(issue_number)
        if len(statuses) != len(plan):
            return 0
        for seq, status in enumerate(statuses):
            if status != TaskStatus.DONE:
                return seq
        return len(plan)

    async def _reset_to_anchor(
        self,
        workspace,
        issue_number: int,
        start_seq: int,
        *,
        final_commit: str | None = None,
    ) -> None:
        """Drop half-finished commits and stray files past the last completed task.

        Resetting and cleaning gives a retry a clean starting point: leftover
        untracked files from a failed attempt cannot leak into the next one and
        shift which checks fail.
        """
        if final_commit:
            await self.workspaces.reset(workspace, final_commit)
            await self.workspaces.clean(workspace)
            return
        if start_seq > 0:
            anchor = self.state.plan_task_commit(issue_number, start_seq - 1)
            if not anchor:
                raise CommandError(
                    f"missing commit anchor for issue #{issue_number} task {start_seq - 1}"
                )
            await self.workspaces.reset(workspace, anchor)
            await self.workspaces.clean(workspace)
            return
        await self.workspaces.reset(workspace, f"origin/{self.config.base_branch}")
        await self.workspaces.clean(workspace)

    async def _capture_baseline(
        self, workspace, *, issue_number: int | None = None
    ) -> dict[str, CheckBaseline]:
        """Record which checks already fail on the anchor commit, before the agent works.

        The worktree sits at the anchor (origin/base or the last completed task
        commit) right after ``_reset_to_anchor``. Failing tests there are
        pre-existing on the base and are not the agent's responsibility; later
        checks only fail on failures *new* relative to this baseline.
        """
        task_checks = getattr(self.config, "task_checks", None)
        baseline_checks = tuple(
            dict.fromkeys((*self.config.checks, *(task_checks or ())))
        )
        cache = getattr(self, "_baseline_cache", None)
        cache_key: tuple[str, tuple[str, ...]] | None = None
        now = time.monotonic()
        if cache is not None and self.config.baseline_cache_ttl_seconds > 0:
            cache_key = (await self.workspaces.head_commit(workspace), baseline_checks)
            for key, item in list(cache.items()):
                if now - item[0] >= self.config.baseline_cache_ttl_seconds:
                    cache.pop(key, None)
            cached = cache.get(cache_key)
            if cached is not None:
                if hasattr(cache, "move_to_end"):
                    cache.move_to_end(cache_key)
                return cached[1]

        async def capture() -> dict[str, CheckBaseline]:
            limit = getattr(self, "check_limit", None)
            if limit is not None:
                async with limit:
                    return await capture_baseline(
                        workspace,
                        baseline_checks,
                        timeout=self.config.check_timeout_seconds,
                        parallel=self.config.checks_parallel,
                    )
            return await capture_baseline(
                workspace,
                baseline_checks,
                timeout=self.config.check_timeout_seconds,
                parallel=self.config.checks_parallel,
            )

        started = time.monotonic()
        inflight = getattr(self, "_baseline_inflight", None)
        if inflight is None:
            self._baseline_inflight = inflight = {}
        owner = cache_key is None or cache_key not in inflight
        operation = asyncio.create_task(capture()) if owner else inflight[cache_key]
        if cache_key is not None and owner:
            inflight[cache_key] = operation
        try:
            baseline = await operation
        finally:
            if cache_key is not None and owner:
                inflight.pop(cache_key, None)
            if issue_number is not None:
                self.state.record_check_duration(
                    issue_number,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
        if cache_key is not None:
            cache[cache_key] = (time.monotonic(), baseline)
            if hasattr(cache, "move_to_end"):
                cache.move_to_end(cache_key)
            maximum = getattr(self.config, "baseline_cache_max_entries", 32)
            while len(cache) > maximum:
                if hasattr(cache, "move_to_end"):
                    cache.popitem(last=False)
                else:
                    cache.pop(next(iter(cache)))
        return baseline

    async def _run_checks(
        self,
        workspace,
        issue_log,
        baseline: dict[str, CheckBaseline],
        *,
        seq: int | None = None,
        attempt: int | None = None,
        stage: str = "task",
    ) -> None:
        """Run the configured checks; see :mod:`issue_agent.checks` for tolerance semantics."""
        configured_task_checks = getattr(self.config, "task_checks", None)
        checks = (
            configured_task_checks
            if stage == "task" and configured_task_checks is not None
            else self.config.checks
        )
        if not checks:
            issue_log.event("task_checks_skipped", sequence=seq, attempt=attempt)
            return
        started = time.monotonic()
        try:
            limit = getattr(self, "check_limit", None)
            if limit is not None:
                async with limit:
                    await run_checks(
                        workspace,
                        issue_log,
                        baseline,
                        checks=checks,
                        timeout=self.config.check_timeout_seconds,
                        parallel=self.config.checks_parallel,
                        seq=seq,
                        attempt=attempt,
                        stage=stage,
                    )
            else:
                await run_checks(
                    workspace,
                    issue_log,
                    baseline,
                    checks=checks,
                    timeout=self.config.check_timeout_seconds,
                    parallel=self.config.checks_parallel,
                    seq=seq,
                    attempt=attempt,
                    stage=stage,
                )
        finally:
            issue_number = getattr(issue_log, "issue_number", None)
            if issue_number is not None:
                self.state.record_check_duration(
                    issue_number,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    seq=seq,
                )

    def _task_review_active(self) -> bool:
        """Whether per-task review runs: formal always; full only with a reviewer agent."""
        mode = self.config.review_task_mode
        if mode == "formal":
            return True
        return mode == "full" and bool(self.config.reviewer_agent)

    async def _run_task_review(
        self,
        workspace,
        issue: Issue,
        task: PlanTask,
        seq: int,
        attempt: int,
        issue_log: IssueLog,
    ) -> None:
        """Execute the per-task review according to config.review_task_mode.

        Raises CommandError (retryable) or, on rejection, ReviewChangesRequested;
        the task loop turns the second rejection into a terminal ReviewRejected.
        """
        if not self._task_review_active():
            return
        self.state.update(issue.number, TaskStatus.REVIEWING)
        mode = self.config.review_task_mode
        if mode == "full":
            review = await self._execute_read_only(
                self.config.reviewer_agent,
                workspace,
                make_task_review_prompt(issue, task, guidance=self._codegraph_guidance()),
                role="task reviewer",
                acquire_agent_limit=True,
                issue_log=issue_log,
                issue_number=issue.number,
                seq=seq,
                attempt=attempt,
            )
            verdict = review_verdict(review.stdout)
            issue_log.review(
                "task",
                review.stdout,
                sequence=seq,
                attempt=attempt,
                task=task.title,
                verdict=verdict or "invalid",
            )
            if verdict == "VERDICT: REQUEST_CHANGES":
                raise ReviewChangesRequested(
                    f"review requested changes:\n{review.stdout[-4000:]}",
                    terminal="review requested changes after the allowed fix cycle:\n"
                    f"{review.stdout[-4000:]}",
                )
            if verdict != "VERDICT: APPROVE":
                raise InvalidReviewVerdict(
                    "review returned no valid final verdict; expected "
                    "VERDICT: APPROVE or VERDICT: REQUEST_CHANGES\n"
                    f"{review.stdout[-4000:]}"
                )
        elif mode == "formal":
            fr_result = await asyncio.get_running_loop().run_in_executor(
                None, formal_review, workspace
            )
            issue_log.review(
                "task",
                f"formal review: {'APPROVE' if fr_result.approved else 'REQUEST_CHANGES'}\n{fr_result.reason}",
                sequence=seq,
                attempt=attempt,
                task=task.title,
                verdict="VERDICT: APPROVE" if fr_result.approved else "VERDICT: REQUEST_CHANGES",
            )
            if not fr_result.approved:
                raise ReviewChangesRequested(
                    f"formal review requested changes:\n{fr_result.reason}",
                    terminal="formal review rejected after the allowed fix cycle:\n"
                    f"{fr_result.reason}",
                )
        # mode == "off": skip review entirely

    async def _run_task(
        self,
        workspace,
        issue: Issue,
        plan: list[PlanTask],
        seq: int,
        agent_name: str,
        issue_log: IssueLog,
        baseline: dict[str, CheckBaseline],
    ) -> None:
        started = time.monotonic()
        self.state.start_plan_task(issue.number, seq)
        try:
            await self._run_task_inner(
                workspace, issue, plan, seq, agent_name, issue_log, baseline
            )
        finally:
            self.state.finish_plan_task(
                issue.number,
                seq,
                wall_duration_ms=int((time.monotonic() - started) * 1000),
            )

    async def _run_task_inner(
        self,
        workspace,
        issue: Issue,
        plan: list[PlanTask],
        seq: int,
        agent_name: str,
        issue_log: IssueLog,
        baseline: dict[str, CheckBaseline],
    ) -> None:
        task = plan[seq]
        issue_log.event("task_started", sequence=seq, task=task.to_dict(), agent=agent_name)
        self.workspaces.write_task_file(workspace, issue, task)
        task_committed = False
        # A whole-issue retry resets the worktree to the anchor, so seed the
        # first attempt with the last recorded task failure.
        last_error = self.state.plan_task_last_error(issue.number, seq)
        review_rejections = 0
        # Review-active modes guarantee the one fix cycle (2 attempts) the review
        # cap needs; a larger max_task_attempts extends check/agent-failure retries
        # without ever extending the review fix cycle (see ReviewChangesRequested).
        attempt_limit = max(
            self.config.max_task_attempts,
            _REVIEW_ATTEMPTS if self._task_review_active() else 0,
        )
        for attempt in range(1, attempt_limit + 1):
            issue_log.event("task_attempt_started", sequence=seq, attempt=attempt)
            self.state.update(
                issue.number, TaskStatus.CODING, current_seq=seq, attempts=attempt, last_error=last_error
            )
            self.state.update_plan_task(
                issue.number, seq, status=TaskStatus.CODING, attempts=attempt, last_error=last_error
            )
            try:
                if last_error:
                    self.workspaces.write_feedback_file(workspace, last_error)
                await self._execute_agent(
                    agent_name,
                    workspace,
                    make_task_prompt(
                        issue,
                        task,
                        plan,
                        retry_error=last_error,
                        guidance=self._codegraph_guidance(),
                    ),
                    issue_log=issue_log,
                    issue_number=issue.number,
                    seq=seq,
                    attempt=attempt,
                )
                issue_log.event("agent_implementation_finished", sequence=seq, attempt=attempt)
                if not await self.workspaces.changed(workspace):
                    raise CommandError("agent completed without changing files")
                self.state.update(issue.number, TaskStatus.TESTING)
                await self._run_checks(workspace, issue_log, baseline, seq=seq, attempt=attempt)

                if task_committed:
                    await self.workspaces.amend(workspace)
                    issue_log.event("task_commit_amended", sequence=seq, attempt=attempt)
                else:
                    await self.workspaces.commit(workspace, f"feat: {task.title} (#{issue.number})")
                    task_committed = True
                    issue_log.event("task_committed", sequence=seq, attempt=attempt)

                await self._run_task_review(workspace, issue, task, seq, attempt, issue_log)

                self.state.update_plan_task(
                    issue.number, seq, status=TaskStatus.DONE,
                    commit_hash=await self.workspaces.head_commit(workspace),
                )
                issue_log.event("task_completed", sequence=seq, attempt=attempt)
                return
            except ReviewChangesRequested as exc:
                review_rejections += 1
                last_error = str(exc)
                issue_log.event("task_attempt_failed", sequence=seq, attempt=attempt, error=last_error)
                if review_rejections >= _REVIEW_ATTEMPTS:
                    last_error = exc.terminal
                    self.state.update_plan_task(
                        issue.number, seq, status=TaskStatus.PENDING, last_error=last_error
                    )
                    self.state.update(
                        issue.number, TaskStatus.FAILED, current_seq=-1, last_error=last_error
                    )
                    raise ReviewRejected(exc.terminal) from exc
            except CommandError as exc:
                last_error = str(exc)
                issue_log.event("task_attempt_failed", sequence=seq, attempt=attempt, error=last_error)
                if isinstance(exc, (InvalidReviewVerdict, ReadOnlyViolation, ReviewRejected)):
                    self.state.update_plan_task(
                        issue.number, seq, status=TaskStatus.PENDING, last_error=last_error
                    )
                    self.state.update(
                        issue.number, TaskStatus.FAILED, current_seq=-1, last_error=last_error
                    )
                    raise
        # Leave the DB in a retryable state: the task is no longer being worked,
        # so its plan row and the whole-issue cursor must not stay stuck on CODING.
        self.state.update_plan_task(issue.number, seq, status=TaskStatus.PENDING, last_error=last_error)
        self.state.update(issue.number, TaskStatus.FAILED, current_seq=-1, last_error=last_error)
        raise CommandError(last_error or "maximum attempts exceeded")

    async def _finalize(
        self,
        workspace,
        issue: Issue,
        plan: list[PlanTask],
        agent_name: str,
        issue_log: IssueLog,
        baseline: dict[str, CheckBaseline],
    ) -> None:
        """Whole-branch review, fixes, and the final checks before push."""
        _, last_error = self.state.final_context(issue.number)
        checks_current = False
        attempt_limit = (
            _REVIEW_ATTEMPTS if self.config.reviewer_agent else self.config.max_task_attempts
        )
        for attempt in range(1, attempt_limit + 1):
            issue_log.event("final_review_attempt_started", attempt=attempt)
            self.state.update(
                issue.number, TaskStatus.REVIEWING, current_seq=-1, attempts=attempt, last_error=last_error
            )
            try:
                if self.config.reviewer_agent:
                    review = await self._execute_read_only(
                        self.config.reviewer_agent,
                        workspace,
                        make_final_review_prompt(
                            issue,
                            plan,
                            self.config.base_branch,
                            guidance=self._codegraph_guidance(),
                        ),
                        role="final reviewer",
                        acquire_agent_limit=True,
                        issue_log=issue_log,
                        issue_number=issue.number,
                        attempt=attempt,
                    )
                    verdict = review_verdict(review.stdout)
                    issue_log.review("final", review.stdout, attempt=attempt, verdict=verdict or "invalid")
                    if verdict == "VERDICT: REQUEST_CHANGES":
                        if attempt == _REVIEW_ATTEMPTS:
                            raise ReviewRejected(
                                "final review requested changes after the allowed fix cycle:\n"
                                f"{review.stdout[-4000:]}"
                            )
                        raise CommandError(f"final review requested changes:\n{review.stdout[-4000:]}")
                    if verdict != "VERDICT: APPROVE":
                        raise InvalidReviewVerdict(
                            "final review returned no valid final verdict; expected "
                            "VERDICT: APPROVE or VERDICT: REQUEST_CHANGES\n"
                            f"{review.stdout[-4000:]}"
                        )
                self.state.update(issue.number, TaskStatus.TESTING, current_seq=-1)
                if not checks_current:
                    await self._run_checks(
                        workspace, issue_log, baseline, attempt=attempt, stage="final"
                    )
                    checks_current = True
                else:
                    issue_log.event("final_check_reused", attempt=attempt)
                if await self.workspaces.changed(workspace):
                    await self.workspaces.commit(workspace, f"feat: final review fixes (#{issue.number})")
                    self.state.update_final_context(
                        issue.number,
                        commit_hash=await self.workspaces.head_commit(workspace),
                    )
                    issue_log.event("final_fix_committed", attempt=attempt)
                self.state.update_final_context(issue.number, last_error="")
                issue_log.event("final_review_completed", attempt=attempt)
                return
            except CommandError as exc:
                last_error = str(exc)
                checks_current = False
                self.state.update_final_context(issue.number, last_error=last_error)
                issue_log.event("final_review_failed", attempt=attempt, error=last_error)
                if isinstance(exc, (InvalidReviewVerdict, ReadOnlyViolation, ReviewRejected)):
                    raise
            self.state.update(issue.number, TaskStatus.CODING, current_seq=-1)
            issue_log.event("final_fix_started", attempt=attempt)
            try:
                self.workspaces.write_feedback_file(workspace, last_error)
                await self._execute_agent(
                    agent_name, workspace, make_final_fix_prompt(issue, last_error),
                    issue_log=issue_log,
                    issue_number=issue.number,
                    attempt=attempt,
                    role="final fixer",
                )
            except CommandError as exc:
                self.state.update_final_context(issue.number, last_error=str(exc))
                raise
            try:
                await self._run_checks(workspace, issue_log, baseline, attempt=attempt, stage="final")
                checks_current = True
            except CommandError as exc:
                self.state.update_final_context(issue.number, last_error=str(exc))
                raise
            if await self.workspaces.changed(workspace):
                await self.workspaces.commit(workspace, f"feat: final review fixes (#{issue.number})")
                self.state.update_final_context(
                    issue.number,
                    commit_hash=await self.workspaces.head_commit(workspace),
                )
                issue_log.event("final_fix_committed", attempt=attempt)
        raise CommandError(last_error or "maximum attempts exceeded")
