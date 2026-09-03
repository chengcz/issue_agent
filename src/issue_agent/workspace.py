from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from .models import Issue, PlanTask
from .process import CommandError, run


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:48] or "task"


class WorkspaceManager:
    def __init__(
        self,
        repo: Path,
        root: Path,
        base_branch: str,
        *,
        fetch_ttl_seconds: int = 30,
    ):
        self.repo = repo
        self.root = root
        self.base_branch = base_branch
        self.fetch_ttl_seconds = fetch_ttl_seconds
        self._fetch_lock = asyncio.Lock()
        self._last_fetch = 0.0

    async def fetch_base(self) -> None:
        """Fetch the base branch once for concurrent/recent workspace requests."""
        now = time.monotonic()
        if now - self._last_fetch < self.fetch_ttl_seconds:
            return
        async with self._fetch_lock:
            now = time.monotonic()
            if now - self._last_fetch < self.fetch_ttl_seconds:
                return
            await run(("git", "fetch", "origin", self.base_branch), cwd=self.repo)
            self._last_fetch = time.monotonic()

    async def create(self, issue: Issue) -> tuple[Path, str]:
        self.root.mkdir(parents=True, exist_ok=True)
        branch = f"agent/{issue.number}-{slugify(issue.title)}"
        path = self.root / str(issue.number)
        await self.fetch_base()
        if path.exists():
            current = await run(("git", "branch", "--show-current"), cwd=path)
            branch = current.stdout.strip()
            if not branch:
                raise CommandError(f"existing worktree is not on a branch: {path}")
        else:
            branch_exists = await run(
                ("git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
                cwd=self.repo,
                check=False,
            )
            command = (
                ("git", "worktree", "add", str(path), branch)
                if branch_exists.returncode == 0
                else (
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(path),
                    f"origin/{self.base_branch}",
                )
            )
            await run(command, cwd=self.repo)
        task_dir = path / ".agent"
        task_dir.mkdir(exist_ok=True)
        self.write_task_file(path, issue, PlanTask(issue.title, issue.body))
        return path, branch

    def write_plan_file(self, path: Path, plan: list[PlanTask]) -> None:
        (path / ".agent").mkdir(parents=True, exist_ok=True)
        content = "# Plan\n\n" + "\n".join(
            f"{i + 1}. **{task.title}**\n   {task.description}" for i, task in enumerate(plan)
        ) + "\n"
        (path / ".agent" / "plan.md").write_text(content, encoding="utf-8")

    def write_feedback_file(self, path: Path, feedback: str) -> None:
        """Persist the full failure report a retry prompt only excerpts."""
        (path / ".agent").mkdir(parents=True, exist_ok=True)
        (path / ".agent" / "feedback.md").write_text(feedback + "\n", encoding="utf-8")

    def write_task_file(self, path: Path, issue: Issue, task: PlanTask) -> None:
        (path / ".agent").mkdir(parents=True, exist_ok=True)
        content = (
            f"# GitHub Issue #{issue.number}\n\n## {issue.title}\n\n{issue.body}\n\n"
            f"## 当前任务\n\n### {task.title}\n\n{task.description}\n\n## Labels\n\n"
            + "\n".join(f"- {x}" for x in issue.labels)
            + "\n"
        )
        (path / ".agent" / "task.md").write_text(content, encoding="utf-8")

    async def changed(self, path: Path) -> bool:
        return bool(await self.status(path))

    async def status(self, path: Path) -> str:
        """Return tracked and non-ignored untracked repository changes."""
        result = await run(("git", "status", "--porcelain", "--untracked-files=all"), cwd=path)
        return result.stdout.strip()

    async def commit(self, path: Path, message: str) -> None:
        await run(("git", "add", "--all"), cwd=path)
        await run(("git", "commit", "-m", message), cwd=path)

    async def amend(self, path: Path) -> None:
        await run(("git", "add", "--all"), cwd=path)
        await run(("git", "commit", "--amend", "--no-edit"), cwd=path)

    async def push(self, path: Path, branch: str, *, dry_run: bool) -> None:
        if not dry_run:
            await run(("git", "push", "--set-upstream", "origin", branch), cwd=path)

    async def reset(self, path: Path, target: str) -> None:
        await run(("git", "reset", "--hard", target), cwd=path)

    async def clean(self, path: Path) -> None:
        """Remove untracked non-ignored files so a retry starts from a clean tree.

        ``-fd`` deletes untracked files and directories but leaves gitignored
        ones (e.g. .venv, build artifacts) alone.
        """
        await run(("git", "clean", "-fd"), cwd=path)

    async def head_commit(self, path: Path) -> str:
        result = await run(("git", "rev-parse", "--short", "HEAD"), cwd=path)
        return result.stdout.strip()
