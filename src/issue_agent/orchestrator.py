from __future__ import annotations

import asyncio
import json
import logging
import re

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
from .models import Issue, PlanTask, TaskStatus
from .process import CommandError, shell
from .state import StateStore
from .workspace import WorkspaceManager

log = logging.getLogger(__name__)


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
        self.workspaces = WorkspaceManager(config.repo, config.worktrees, config.base_branch)
        self.agents = {name: CliAgent(name, item) for name, item in config.agents.items()}
        self.global_limit = asyncio.Semaphore(config.max_workers)
        self.agent_limits = {
            name: asyncio.Semaphore(item.max_workers) for name, item in config.agents.items()
        }
        self.database_lock = asyncio.Lock()
        self.running: dict[int, asyncio.Task[None]] = {}

    def recover(self) -> int:
        return self.state.recover_interrupted()

    def select_agent(self, issue: Issue) -> str:
        requested = next((x.split(":", 1)[1] for x in issue.labels if x.startswith("agent:")), "")
        name = requested or self.config.default_agent
        if name not in self.agents:
            raise ValueError(f"unknown or disabled agent: {name}")
        return name

    async def run_once(self) -> None:
        issues = await self.github.runnable_issues(self.config.ready_label)
        for issue in issues:
            if issue.number in self.running:
                continue
            try:
                agent_name = self.select_agent(issue)
            except ValueError as exc:
                log.error("issue #%s: %s", issue.number, exc)
                continue
            if not self.state.claim(issue, agent_name, self.config.max_attempts):
                continue
            task = asyncio.create_task(self._guarded_process(issue, agent_name))
            self.running[issue.number] = task
            task.add_done_callback(lambda _task, number=issue.number: self.running.pop(number, None))

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

    async def process(self, issue: Issue, agent_name: str) -> None:
        try:
            workspace, branch = await self.workspaces.create(issue)
            self.state.update(issue.number, TaskStatus.PLANNING, branch=branch, worktree=str(workspace))
            await self.github.labels(issue.number, add=("agent-running",), remove=(self.config.ready_label,))

            plan = self.state.load_plan(issue.number)
            if plan is None:
                plan = await self._plan(workspace, issue)
                await self.github.comment(issue.number, "## Agent Plan\n\n" + format_plan(plan))
            self.workspaces.write_plan_file(workspace, plan)

            start_seq = self._resume_seq(issue.number, plan)
            await self._reset_to_anchor(workspace, issue.number, start_seq)
            self.state.update(issue.number, TaskStatus.PLANNED, current_seq=start_seq)

            for seq in range(start_seq, len(plan)):
                await self._run_task(workspace, issue, plan, seq, agent_name)

            await self._finalize(workspace, issue, plan, agent_name)
            self.state.update(issue.number, TaskStatus.PUSHING, current_seq=-1)
            await self.workspaces.push(workspace, branch, dry_run=self.config.dry_run)
            pr_url = await self.github.create_pr(
                issue.number, branch, self.config.base_branch, issue.title, self.config.checks
            )
            self.state.update(issue.number, TaskStatus.HUMAN_REVIEW, pr_url=pr_url)
            await self.github.labels(issue.number, add=("human-review",), remove=("agent-running", "agent-failed"))
            await self.github.comment(issue.number, f"Implementation ready for human review: {pr_url}")
        except CommandError as exc:
            log.error("issue #%s failed: %s", issue.number, exc)
            failures = self.state.record_failure(issue.number, TaskStatus.FAILED, str(exc))
            await self._park_or_requeue(issue, failures)
            note = (
                "\n\nRetry budget exhausted; reset the task row and re-add the agent-ready "
                "label to rerun."
                if failures >= self.config.max_attempts
                else ""
            )
            await self.github.comment(issue.number, f"Agent run failed.\n\n```text\n{str(exc)[-3000:]}\n```{note}")
        except Exception as exc:
            log.exception("issue #%s failed", issue.number)
            failures = self.state.record_failure(issue.number, TaskStatus.BLOCKED, str(exc))
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

    async def _plan(self, workspace, issue: Issue) -> list[PlanTask]:
        if not self.config.planner_agent:
            plan = [PlanTask(title=issue.title, description=issue.body)]
        else:
            result = await self.agents[self.config.planner_agent].execute(
                workspace, make_plan_prompt(issue, self.config.max_tasks), review=True
            )
            plan = parse_plan(result.stdout, self.config.max_tasks)
        self.state.save_plan(issue.number, plan)
        return plan

    def _resume_seq(self, issue_number: int, plan: list[PlanTask]) -> int:
        """First plan index that is not DONE; len(plan) when all tasks are done (final phase)."""
        statuses = self.state.plan_task_statuses(issue_number)
        if len(statuses) != len(plan):
            return 0
        for seq, status in enumerate(statuses):
            if status != TaskStatus.DONE:
                return seq
        return len(plan)

    async def _reset_to_anchor(self, workspace, issue_number: int, start_seq: int) -> None:
        """Drop any half-finished commits past the last completed task, so a task can be retried cleanly."""
        if start_seq > 0:
            anchor = self.state.plan_task_commit(issue_number, start_seq - 1)
            if anchor:
                await self.workspaces.reset(workspace, anchor)
                return
            return
        await self.workspaces.reset(workspace, f"origin/{self.config.base_branch}")

    async def _run_task(self, workspace, issue: Issue, plan: list[PlanTask], seq: int, agent_name: str) -> None:
        task = plan[seq]
        self.workspaces.write_task_file(workspace, issue, task)
        task_committed = False
        last_error = ""
        for attempt in range(1, self.config.max_attempts + 1):
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
                self.state.update(issue.number, TaskStatus.TESTING)
                for check in self.config.checks:
                    await shell(check, cwd=workspace)

                if not await self.workspaces.changed(workspace):
                    raise CommandError("agent completed without changing files")
                if task_committed:
                    await self.workspaces.amend(workspace)
                else:
                    await self.workspaces.commit(workspace, f"feat: {task.title} (#{issue.number})")
                    task_committed = True

                if self.config.reviewer_agent:
                    self.state.update(issue.number, TaskStatus.REVIEWING)
                    review = await self.agents[self.config.reviewer_agent].execute(
                        workspace, make_task_review_prompt(issue, task), review=True
                    )
                    verdict = review_verdict(review.stdout)
                    if verdict == "VERDICT: REQUEST_CHANGES":
                        raise CommandError(f"review requested changes:\n{review.stdout[-4000:]}")
                    if verdict != "VERDICT: APPROVE":
                        raise CommandError(
                            "review returned no valid final verdict; expected "
                            "VERDICT: APPROVE or VERDICT: REQUEST_CHANGES\n"
                            f"{review.stdout[-4000:]}"
                        )

                self.state.update_plan_task(
                    issue.number, seq, status=TaskStatus.DONE,
                    commit_hash=await self.workspaces.head_commit(workspace),
                )
                return
            except CommandError as exc:
                last_error = str(exc)
        raise CommandError(last_error or "maximum attempts exceeded")

    async def _finalize(self, workspace, issue: Issue, plan: list[PlanTask], agent_name: str) -> None:
        """Whole-branch review, fixes, and the final checks before push."""
        last_error = ""
        for attempt in range(1, self.config.max_attempts + 1):
            self.state.update(
                issue.number, TaskStatus.REVIEWING, current_seq=-1, attempts=attempt, last_error=last_error
            )
            try:
                if self.config.reviewer_agent:
                    review = await self.agents[self.config.reviewer_agent].execute(
                        workspace, make_final_review_prompt(issue, plan, self.config.base_branch), review=True
                    )
                    verdict = review_verdict(review.stdout)
                    if verdict == "VERDICT: REQUEST_CHANGES":
                        raise CommandError(f"final review requested changes:\n{review.stdout[-4000:]}")
                    if verdict != "VERDICT: APPROVE":
                        raise CommandError(
                            "final review returned no valid final verdict; expected "
                            "VERDICT: APPROVE or VERDICT: REQUEST_CHANGES\n"
                            f"{review.stdout[-4000:]}"
                        )
                self.state.update(issue.number, TaskStatus.TESTING, current_seq=-1)
                for check in self.config.checks:
                    await shell(check, cwd=workspace)
                if await self.workspaces.changed(workspace):
                    await self.workspaces.commit(workspace, f"feat: final review fixes (#{issue.number})")
                return
            except CommandError as exc:
                last_error = str(exc)
            self.state.update(issue.number, TaskStatus.CODING, current_seq=-1)
            await self.agents[agent_name].execute(workspace, make_final_fix_prompt(issue, last_error))
            for check in self.config.checks:
                await shell(check, cwd=workspace)
            if await self.workspaces.changed(workspace):
                await self.workspaces.commit(workspace, f"feat: final review fixes (#{issue.number})")
        raise CommandError(last_error or "maximum attempts exceeded")
