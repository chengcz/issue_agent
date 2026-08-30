from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from .agents import (
    CliAgent,
    make_final_fix_prompt,
    make_final_review_prompt,
    make_plan_prompt,
    make_task_prompt,
    make_task_review_prompt,
)
from .config import Config
from .github import GitHub
from .issue_log import IssueLog
from .models import Issue, PlanTask, TaskStatus
from .process import CommandError, shell
from .state import StateStore
from .workspace import WorkspaceManager

log = logging.getLogger(__name__)
_REVIEW_ATTEMPTS = 2


class ReviewRejected(CommandError):
    """A review remains rejected after its single allowed fix cycle."""


class InvalidReviewVerdict(CommandError):
    """A reviewer response cannot be acted on safely and should be retried later."""


class ReadOnlyViolation(CommandError):
    """A planner or reviewer changed the repository during a read-only run."""


def review_verdict(stdout: str) -> str | None:
    """Return a review verdict only when it is the final non-empty output line."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    verdict = lines[-1]
    if verdict in {"VERDICT: APPROVE", "VERDICT: REQUEST_CHANGES"}:
        return verdict
    return None


_RE_FAILED_TEST = re.compile(r"^FAILED (\S+)", re.MULTILINE)


def failed_tests(output: str) -> set[str]:
    """Extract the failing test IDs pytest prints in its short summary.

    The ``-q`` short summary emits one ``FAILED <path>::<node_id>`` line per
    failure; those IDs are what we compare against the base-branch baseline to
    tell a pre-existing failure from a regression the agent introduced.
    """
    return set(_RE_FAILED_TEST.findall(output))


@dataclass(frozen=True)
class CheckBaseline:
    """One command's failure state captured before implementation starts."""

    returncode: int
    failed_tests: frozenset[str]
    output: str


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


def parse_plan(stdout: str, max_tasks: int) -> list[PlanTask]:
    """Parse the planner's fenced JSON block into PlanTasks, validating bounds."""
    match = re.search(r"```json\s*(.*?)\s*```", stdout, re.DOTALL)
    if not match:
        raise CommandError("plan output contained no fenced ```json block")
    raw = match.group(1)
    try:
        items = json.loads(_repair_json(raw), strict=False)
    except json.JSONDecodeError as exc:
        # Include a context snippet so a future planner regression is
        # diagnosable from the issue comment alone, without re-running the model.
        snippet = raw[max(0, exc.pos - 120): exc.pos + 120].replace("\n", "\\n")
        raise CommandError(f"plan JSON is invalid: {exc} near: {snippet}") from exc
    if not isinstance(items, list) or not items:
        raise CommandError("plan must be a non-empty list of tasks")
    if len(items) > max_tasks:
        raise CommandError(f"plan has {len(items)} tasks, exceeding max_tasks={max_tasks}")
    plan: list[PlanTask] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("title"):
            raise CommandError("each plan task needs a title and description")
        plan.append(PlanTask(title=str(item["title"]), description=str(item.get("description", ""))))
    return plan


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
        self.agent_limits = {
            name: asyncio.Semaphore(item.max_workers) for name, item in config.agents.items()
        }
        self.database_lock = asyncio.Lock()
        self.running: dict[int, asyncio.Task[None]] = {}
        self._baseline_cache: dict[
            tuple[str, tuple[str, ...]], dict[str, CheckBaseline]
        ] = {}

    def recover(self) -> int:
        return self.state.recover_interrupted()

    def select_agent(self, issue: Issue) -> str:
        requested = next((x.split(":", 1)[1] for x in issue.labels if x.startswith("agent:")), "")
        name = requested or self.config.default_agent
        if name not in self.agents:
            raise ValueError(f"unknown or disabled agent: {name}")
        return name

    async def run_once(self) -> None:
        runnable = await self.github.runnable_issues(self.config.ready_label)
        planning = (
            await self.github.unlabeled_issues(self.config.auto_plan_limit)
            if self.config.auto_plan_unlabeled
            else []
        )
        for issue in runnable:
            if issue.number in self.running:
                continue
            try:
                agent_name = self.select_agent(issue)
            except ValueError as exc:
                log.error("issue #%s: %s", issue.number, exc)
                continue
            if not self.state.claim(issue, agent_name, self.config.max_attempts):
                continue
            self._track(issue.number, self._guarded_process(issue, agent_name))

        planner_name = self.config.planner_agent
        if planner_name and planner_name not in self.agents:
            log.error("unknown or disabled planner agent: %s", planner_name)
            return
        for issue in planning:
            if issue.number in self.running:
                continue
            recorded_agent = planner_name or self.config.default_agent
            if not self.state.claim_for_planning(
                issue, recorded_agent, self.config.max_attempts
            ):
                continue
            self._track(issue.number, self._guarded_plan_only(issue, planner_name))

    def _track(self, issue_number: int, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.running[issue_number] = task
        task.add_done_callback(lambda _task, number=issue_number: self.running.pop(number, None))

    async def serve(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("scheduler iteration failed")
            await asyncio.sleep(self.config.poll_seconds)

    async def shutdown(self) -> None:
        if not self.running:
            return
        log.info("waiting for %s active worker(s) to finish", len(self.running))
        await asyncio.gather(*tuple(self.running.values()), return_exceptions=True)

    async def _guarded_process(self, issue: Issue, agent_name: str) -> None:
        async with self.global_limit, self.agent_limits[agent_name]:
            if "resource:database-schema" in issue.labels:
                async with self.database_lock:
                    await self.process(issue, agent_name)
            else:
                await self.process(issue, agent_name)

    async def _guarded_plan_only(self, issue: Issue, planner_name: str) -> None:
        if planner_name:
            async with self.global_limit, self.agent_limits[planner_name]:
                await self.plan_only(issue)
        else:
            async with self.global_limit:
                await self.plan_only(issue)

    async def plan_only(self, issue: Issue) -> None:
        """Create and publish a plan, then wait for a human to add agent-ready."""
        issue_log = IssueLog(self.config.log_dir, issue.number)
        issue_log.event("plan_only_started", title=issue.title, labels=issue.labels)
        try:
            workspace, branch = await self.workspaces.create(issue)
            issue_log.event("workspace_ready", workspace=workspace, branch=branch)
            self.state.update(
                issue.number, TaskStatus.PLANNING, branch=branch, worktree=str(workspace)
            )
            plan = self.state.load_plan(issue.number)
            if plan is None:
                plan = await self._plan(workspace, issue)
                issue_log.event("plan_generated", tasks=[task.to_dict() for task in plan])
            else:
                issue_log.event("plan_reused", tasks=[task.to_dict() for task in plan])
            self.workspaces.write_plan_file(workspace, plan)
            self.state.update(issue.number, TaskStatus.PLANNED, current_seq=0)
            issue_log.event("awaiting_human_approval", plan_tasks=len(plan))
            await self.github.comment(
                issue.number,
                "## Issue Agent Plan\n\n"
                + format_plan(plan)
                + "\n\n## Human approval required\n\n"
                "Review or update this Issue and the plan above. Add the `agent-ready` "
                "label when implementation may begin. Until then, Issue Agent will not "
                "modify code, push a branch, or create a pull request.",
            )
        except CommandError as exc:
            log.error("issue #%s planning failed: %s", issue.number, exc)
            failures = self.state.record_failure(issue.number, TaskStatus.FAILED, str(exc))
            issue_log.event("plan_failed", failures=failures, error=str(exc))
            await self._comment_planning_failure(issue, failures, str(exc))
        except Exception as exc:
            log.exception("issue #%s planning blocked", issue.number)
            failures = self.state.record_failure(issue.number, TaskStatus.BLOCKED, str(exc))
            issue_log.event("plan_blocked", failures=failures, error=str(exc))
            await self._comment_planning_failure(issue, failures, str(exc))

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
        issue_log = IssueLog(self.config.log_dir, issue.number)
        issue_log.event("implementation_started", title=issue.title, agent=agent_name, labels=issue.labels)
        try:
            workspace, branch = await self.workspaces.create(issue)
            issue_log.event("workspace_ready", workspace=workspace, branch=branch)
            self.state.update(issue.number, TaskStatus.PLANNING, branch=branch, worktree=str(workspace))
            await self.github.labels(issue.number, add=("agent-running",), remove=(self.config.ready_label,))

            plan = self.state.load_plan(issue.number)
            if plan is None:
                plan = await self._plan(
                    workspace,
                    issue,
                    acquire_agent_limit=self.config.planner_agent != agent_name,
                )
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
            baseline = await self._capture_baseline(workspace)
            self.workspaces.write_plan_file(workspace, plan)

            for seq in range(start_seq, len(plan)):
                await self._run_task(workspace, issue, plan, seq, agent_name, issue_log, baseline)

            await self._finalize(workspace, issue, plan, agent_name, issue_log, baseline)
            self.state.update(issue.number, TaskStatus.PUSHING, current_seq=-1)
            issue_log.event("push_started", branch=branch)
            await self.workspaces.push(workspace, branch, dry_run=self.config.dry_run)
            pr_url = await self.github.create_pr(
                issue.number, branch, self.config.base_branch, issue.title, self.config.checks
            )
            self.state.update(issue.number, TaskStatus.HUMAN_REVIEW, pr_url=pr_url)
            issue_log.event("implementation_complete", pr_url=pr_url)
            await self.github.labels(issue.number, add=("human-review",), remove=("agent-running", "agent-failed"))
            await self.github.comment(issue.number, f"Implementation ready for human review: {pr_url}")
        except CommandError as exc:
            log.error("issue #%s failed: %s", issue.number, exc)
            failures = self.state.record_failure(issue.number, TaskStatus.FAILED, str(exc))
            issue_log.event("implementation_failed", failures=failures, error=str(exc))
            if isinstance(exc, ReviewRejected):
                await self._park_after_review_failure(issue)
            else:
                await self._park_or_requeue(issue, failures)
            note = (
                "\n\nReview fix cycle exhausted; inspect the review log, then manually re-add "
                "the agent-ready label to retry."
                if isinstance(exc, ReviewRejected)
                else "\n\nRetry budget exhausted; reset the task row and re-add the agent-ready "
                "label to rerun."
                if failures >= self.config.max_attempts
                else ""
            )
            await self.github.comment(issue.number, f"Agent run failed.\n\n```text\n{str(exc)[-3000:]}\n```{note}")
        except Exception as exc:
            log.exception("issue #%s failed", issue.number)
            failures = self.state.record_failure(issue.number, TaskStatus.BLOCKED, str(exc))
            issue_log.event("implementation_blocked", failures=failures, error=str(exc))
            await self._park_or_requeue(issue, failures)
            note = (
                "\n\nRetry budget exhausted; reset the task row and re-add the agent-ready "
                "label to rerun."
                if failures >= self.config.max_attempts
                else ""
            )
            await self.github.comment(issue.number, f"Agent run blocked.\n\n```text\n{str(exc)[-3000:]}\n```{note}")

    async def _park_or_requeue(self, issue: Issue, failures: int) -> None:
        """Keep a failed issue in the runnable pool while its retry budget holds.

        Under budget the agent-ready label is restored so the next scheduler poll
        re-claims the issue; once the whole-issue failure budget is exhausted the
        issue is parked (no agent-ready) until a human resets the task row.
        """
        if failures < self.config.max_attempts:
            await self.github.labels(issue.number, add=("agent-failed", "agent-ready"), remove=("agent-running",))
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
    ) -> list[PlanTask]:
        if not self.config.planner_agent:
            plan = [PlanTask(title=issue.title, description=issue.body)]
        else:
            await self._reset_to_anchor(workspace, issue.number, 0)
            result = await self._execute_read_only(
                self.config.planner_agent,
                workspace,
                make_plan_prompt(issue, self.config.max_tasks),
                role="planner",
                acquire_agent_limit=acquire_agent_limit,
            )
            plan = parse_plan(result.stdout, self.config.max_tasks)
        self.state.save_plan(issue.number, plan)
        return plan

    async def _execute_read_only(
        self,
        agent_name,
        workspace,
        prompt: str,
        *,
        role: str,
        acquire_agent_limit: bool = False,
    ):
        """Run a planner/reviewer and restore any repository changes it makes."""
        if acquire_agent_limit:
            async with self.agent_limits[agent_name]:
                return await self._execute_read_only(
                    agent_name,
                    workspace,
                    prompt,
                    role=role,
                )
        if await self.workspaces.status(workspace):
            raise CommandError(f"cannot start read-only {role}: workspace is not clean")
        try:
            result = await self.agents[agent_name].execute(workspace, prompt, review=True)
        except Exception as exc:
            if await self.workspaces.status(workspace):
                await self.workspaces.reset(workspace, "HEAD")
                await self.workspaces.clean(workspace)
                raise ReadOnlyViolation(
                    f"read-only {role} modified the workspace before failing"
                ) from exc
            raise
        if await self.workspaces.status(workspace):
            await self.workspaces.reset(workspace, "HEAD")
            await self.workspaces.clean(workspace)
            raise ReadOnlyViolation(f"read-only {role} modified the workspace")
        return result

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
            if anchor:
                await self.workspaces.reset(workspace, anchor)
                await self.workspaces.clean(workspace)
                return
            return
        await self.workspaces.reset(workspace, f"origin/{self.config.base_branch}")
        await self.workspaces.clean(workspace)

    async def _capture_baseline(self, workspace) -> dict[str, CheckBaseline]:
        """Record which checks already fail on the anchor commit, before the agent works.

        The worktree sits at the anchor (origin/base or the last completed task
        commit) right after ``_reset_to_anchor``. Failing tests there are
        pre-existing on the base and are not the agent's responsibility; later
        checks only fail on failures *new* relative to this baseline.
        """
        cache = getattr(self, "_baseline_cache", None)
        cache_key: tuple[str, tuple[str, ...]] | None = None
        if cache is not None:
            cache_key = (await self.workspaces.head_commit(workspace), self.config.checks)
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        baseline: dict[str, CheckBaseline] = {}
        for check in self.config.checks:
            result = await shell(
                check,
                cwd=workspace,
                timeout=self.config.check_timeout_seconds,
                check=False,
            )
            if result.returncode != 0:
                output = f"{result.stdout}\n{result.stderr}".strip()
                baseline[check] = CheckBaseline(
                    returncode=result.returncode,
                    failed_tests=frozenset(failed_tests(output)),
                    output=output,
                )
        if cache_key is not None:
            cache[cache_key] = baseline
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
        """Run the configured checks, tolerating failures already present on the base branch.

        Pytest failures are compared by node ID within the same command. Other
        failing commands are tolerated only while their return code and output
        remain unchanged. A regression raises ``CommandError`` so the retry
        prompt tells the agent precisely what to fix.
        """
        prefix = "final_" if stage == "final" else ""
        for check in self.config.checks:
            result = await shell(
                check,
                cwd=workspace,
                timeout=self.config.check_timeout_seconds,
                check=False,
            )
            if result.returncode == 0:
                issue_log.event(f"{prefix}check_passed", sequence=seq, attempt=attempt, command=check)
                continue
            output = f"{result.stdout}\n{result.stderr}".strip()
            current = failed_tests(output)
            previous = baseline.get(check)
            previous_tests = set(previous.failed_tests) if previous else set()
            new = current - previous_tests
            unchanged_generic_failure = (
                not current
                and previous is not None
                and not previous.failed_tests
                and result.returncode == previous.returncode
                and output == previous.output
            )
            if (current and not new) or unchanged_generic_failure:
                issue_log.event(
                    f"{prefix}check_passed_pre_existing",
                    sequence=seq,
                    attempt=attempt,
                    command=check,
                    pre_existing=sorted(current) if current else ["unchanged command failure"],
                )
                continue
            tail = (result.stderr or result.stdout)[-4000:]
            if new:
                raise CommandError(
                    f"check failed with {len(new)} new failure(s) not present on the base branch:\n"
                    + "\n".join(sorted(new))
                    + f"\n\n{tail}"
                )
            raise CommandError(f"command failed ({result.returncode}): {check}\n{tail}")

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
        task = plan[seq]
        issue_log.event("task_started", sequence=seq, task=task.to_dict(), agent=agent_name)
        self.workspaces.write_task_file(workspace, issue, task)
        task_committed = False
        # A whole-issue retry resets the worktree to the anchor, so seed the
        # first attempt with the last recorded task failure.
        last_error = self.state.plan_task_last_error(issue.number, seq)
        attempt_limit = (
            _REVIEW_ATTEMPTS if self.config.reviewer_agent else self.config.max_task_attempts
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
                await self.agents[agent_name].execute(
                    workspace, make_task_prompt(issue, task, plan, retry_error=last_error)
                )
                issue_log.event("agent_implementation_finished", sequence=seq, attempt=attempt)
                self.state.update(issue.number, TaskStatus.TESTING)
                await self._run_checks(workspace, issue_log, baseline, seq=seq, attempt=attempt)

                if not await self.workspaces.changed(workspace):
                    raise CommandError("agent completed without changing files")
                if task_committed:
                    await self.workspaces.amend(workspace)
                    issue_log.event("task_commit_amended", sequence=seq, attempt=attempt)
                else:
                    await self.workspaces.commit(workspace, f"feat: {task.title} (#{issue.number})")
                    task_committed = True
                    issue_log.event("task_committed", sequence=seq, attempt=attempt)

                if self.config.reviewer_agent:
                    self.state.update(issue.number, TaskStatus.REVIEWING)
                    review = await self._execute_read_only(
                        self.config.reviewer_agent,
                        workspace,
                        make_task_review_prompt(issue, task),
                        role="task reviewer",
                        acquire_agent_limit=self.config.reviewer_agent != agent_name,
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
                        if attempt == _REVIEW_ATTEMPTS:
                            raise ReviewRejected(
                                "review requested changes after the allowed fix cycle:\n"
                                f"{review.stdout[-4000:]}"
                            )
                        raise CommandError(f"review requested changes:\n{review.stdout[-4000:]}")
                    if verdict != "VERDICT: APPROVE":
                        raise InvalidReviewVerdict(
                            "review returned no valid final verdict; expected "
                            "VERDICT: APPROVE or VERDICT: REQUEST_CHANGES\n"
                            f"{review.stdout[-4000:]}"
                        )

                self.state.update_plan_task(
                    issue.number, seq, status=TaskStatus.DONE,
                    commit_hash=await self.workspaces.head_commit(workspace),
                )
                issue_log.event("task_completed", sequence=seq, attempt=attempt)
                return
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
                        make_final_review_prompt(issue, plan, self.config.base_branch),
                        role="final reviewer",
                        acquire_agent_limit=self.config.reviewer_agent != agent_name,
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
            await self.agents[agent_name].execute(workspace, make_final_fix_prompt(issue, last_error))
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
