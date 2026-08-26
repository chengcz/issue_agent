from __future__ import annotations

import re
from pathlib import Path

from .models import Issue
from .process import run


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:48] or "task"


class WorkspaceManager:
    def __init__(self, repo: Path, root: Path, base_branch: str):
        self.repo = repo
        self.root = root
        self.base_branch = base_branch

    async def create(self, issue: Issue) -> tuple[Path, str]:
        self.root.mkdir(parents=True, exist_ok=True)
        branch = f"agent/{issue.number}-{slugify(issue.title)}"
        path = self.root / str(issue.number)
        await run(("git", "fetch", "origin", self.base_branch), cwd=self.repo)
        if not path.exists():
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
        (task_dir / "task.md").write_text(
            f"# GitHub Issue #{issue.number}\n\n## {issue.title}\n\n{issue.body}\n\n## Labels\n\n" + "\n".join(f"- {x}" for x in issue.labels) + "\n",
            encoding="utf-8",
        )
        return path, branch

    async def changed(self, path: Path) -> bool:
        result = await run(("git", "status", "--porcelain"), cwd=path)
        return bool(result.stdout.strip())

    async def commit_push(self, path: Path, branch: str, issue: Issue, *, dry_run: bool) -> None:
        if dry_run:
            return
        await run(("git", "add", "--all"), cwd=path)
        await run(("git", "commit", "-m", f"feat: {issue.title} (#{issue.number})"), cwd=path)
        await run(("git", "push", "--set-upstream", "origin", branch), cwd=path)
