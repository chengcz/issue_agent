from __future__ import annotations

import asyncio
import logging

from .agents import CliAgent, make_prompt, make_review_prompt
from .config import Config
from .github import GitHub
from .models import Issue, TaskStatus
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
            if not self.state.claim(issue, agent_name):
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
            self.state.update(issue.number, TaskStatus.CODING, branch=branch, worktree=str(workspace))
            await self.github.labels(issue.number, add=("agent-running",), remove=(self.config.ready_label,))
            last_error = ""
            for attempt in range(1, self.config.max_attempts + 1):
                self.state.update(issue.number, TaskStatus.CODING, attempts=attempt, last_error=last_error)
                try:
                    await self.agents[agent_name].execute(workspace, make_prompt(issue.number, retry_error=last_error))
                    self.state.update(issue.number, TaskStatus.TESTING)
                    for check in self.config.checks:
                        await shell(check, cwd=workspace)

                    if not await self.workspaces.changed(workspace):
                        raise CommandError("agent completed without changing files")

                    if self.config.reviewer_agent:
                        self.state.update(issue.number, TaskStatus.REVIEWING)
                        review = await self.agents[self.config.reviewer_agent].execute(
                            workspace, make_review_prompt(issue.number), review=True
                        )
                        verdict = review_verdict(review.stdout)
                        if verdict == "VERDICT: REQUEST_CHANGES":
                            raise CommandError(
                                f"review requested changes:\n{review.stdout[-4000:]}"
                            )
                        if verdict != "VERDICT: APPROVE":
                            raise CommandError(
                                "review returned no valid final verdict; expected "
                                "VERDICT: APPROVE or VERDICT: REQUEST_CHANGES\n"
                                f"{review.stdout[-4000:]}"
                            )

                    break
                except CommandError as exc:
                    last_error = str(exc)
            else:
                raise CommandError(last_error or "maximum attempts exceeded")

            self.state.update(issue.number, TaskStatus.PUSHING)
            await self.workspaces.commit_push(workspace, branch, issue, dry_run=self.config.dry_run)
            pr_url = await self.github.create_pr(issue.number, branch, self.config.base_branch, issue.title, self.config.checks)
            self.state.update(issue.number, TaskStatus.HUMAN_REVIEW, pr_url=pr_url)
            await self.github.labels(issue.number, add=("human-review",), remove=("agent-running", "agent-failed"))
            await self.github.comment(issue.number, f"Implementation ready for human review: {pr_url}")
        except CommandError as exc:
            log.error("issue #%s failed: %s", issue.number, exc)
            self.state.update(issue.number, TaskStatus.FAILED, last_error=str(exc))
            await self.github.labels(issue.number, add=("agent-failed",), remove=("agent-running",))
            await self.github.comment(issue.number, f"Agent run failed.\n\n```text\n{str(exc)[-3000:]}\n```")
        except Exception as exc:
            log.exception("issue #%s failed", issue.number)
            self.state.update(issue.number, TaskStatus.BLOCKED, last_error=str(exc))
            await self.github.labels(issue.number, add=("agent-failed",), remove=("agent-running",))
            await self.github.comment(issue.number, f"Agent run blocked.\n\n```text\n{str(exc)[-3000:]}\n```")
