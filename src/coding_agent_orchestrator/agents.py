from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import AgentConfig
from .models import Issue, PlanTask
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


def make_plan_prompt(issue: Issue, max_tasks: int) -> str:
    return f"""You are the planner for GitHub Issue #{issue.number}.
Read AGENTS.md when present. Explore the codebase read-only. Do NOT modify files or commit.
Split the issue into a sequence of {max_tasks} or fewer concrete implementation tasks. Each task must:
- be independently committable and reviewable
- build on previous tasks (they execute in order on one branch)
- together fully satisfy the issue
Return ONLY a fenced JSON block with no prose outside it. Write every title and description as a
single line with no raw line breaks and no trailing commas inside the JSON:
```json
[{{"title": "short task title", "description": "what to implement and the acceptance intent"}}, ...]
```"""


def make_task_prompt(issue: Issue, task: PlanTask, plan: list[PlanTask], *, retry_error: str = "") -> str:
    plan_text = "\n".join(f"{i + 1}. {t.title}: {t.description}" for i, t in enumerate(plan))
    retry = f"\nPrevious attempt failed. Fix this error:\n{retry_error[-4000:]}\n" if retry_error else ""
    return f"""You are the coding worker for GitHub Issue #{issue.number}, implementing one task of the plan.
Read AGENTS.md when present and .agent/task.md. Implement ONLY the current task; earlier tasks are already
committed — do not redo or revert them. Stay inside this worktree. Do not commit, push, create a PR, merge,
deploy, or edit secrets. Add or update tests and documentation as required. Review your diff before finishing.
Full plan:
{plan_text}
{retry}"""


def make_task_review_prompt(issue: Issue, task: PlanTask) -> str:
    return f"""Review the most recent commit for GitHub Issue #{issue.number}, which implements the task:
{task.title}
Read AGENTS.md and .agent/task.md. Do not modify files. Inspect the last commit (e.g. `git diff HEAD^ HEAD`)
and review only this task's changes.
Check correctness, security, compatibility, migrations, tests, and unrelated changes.
Give concrete reasons before the verdict. End with exactly one of these lines:
VERDICT: APPROVE
VERDICT: REQUEST_CHANGES
"""


def make_final_review_prompt(issue: Issue, plan: list[PlanTask], base_branch: str) -> str:
    plan_text = "\n".join(f"{i + 1}. {t.title}: {t.description}" for i, t in enumerate(plan))
    return f"""Review the complete implementation for GitHub Issue #{issue.number} (all tasks on this branch).
Read AGENTS.md. Do not modify files. Review the full branch diff against the base
(e.g. `git diff origin/{base_branch} HEAD`) as one coherent change, and check it satisfies the plan:
{plan_text}
Check cross-task consistency, correctness, security, compatibility, migrations, tests, and unrelated changes.
Give concrete reasons before the verdict. End with exactly one of these lines:
VERDICT: APPROVE
VERDICT: REQUEST_CHANGES
"""


def make_final_fix_prompt(issue: Issue, error: str) -> str:
    return f"""You are the coding worker for GitHub Issue #{issue.number}. The final review or checks requested
changes. Fix the reported problems and leave no unrelated changes. Stay inside this worktree. Do not commit,
push, create a PR, merge, deploy, or edit secrets.
Reported problems:
{error[-4000:]}"""
