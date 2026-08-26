from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import AgentConfig
from .process import Result, run


@dataclass(frozen=True)
class CliAgent:
    name: str
    config: AgentConfig

    async def execute(self, workspace: Path, prompt: str, *, review: bool = False) -> Result:
        command = self.config.review_command if review and self.config.review_command else self.config.command
        rendered = [part.replace("{prompt}", prompt) for part in command]
        has_placeholder = any("{prompt}" in part for part in command)
        return await run(
            rendered,
            cwd=workspace,
            timeout=self.config.timeout_seconds,
            stdin=None if has_placeholder else prompt,
        )


def make_prompt(issue_number: int, *, retry_error: str = "") -> str:
    retry = f"\nPrevious attempt failed. Fix this error:\n{retry_error[-4000:]}\n" if retry_error else ""
    return f"""You are the coding worker for GitHub Issue #{issue_number}.
Read AGENTS.md when present and .agent/task.md. Implement the task completely.
Stay inside this worktree. Do not commit, push, create a PR, merge, deploy, or edit secrets.
Add or update tests and documentation as required. Review your diff before finishing.
{retry}"""


def make_review_prompt(issue_number: int) -> str:
    return f"""Review the uncommitted implementation for GitHub Issue #{issue_number}.
Read AGENTS.md and .agent/task.md. Do not modify files.
Check correctness, security, compatibility, migrations, tests, and unrelated changes.
End with exactly VERDICT: APPROVE or VERDICT: REQUEST_CHANGES, followed by concrete reasons.
"""

