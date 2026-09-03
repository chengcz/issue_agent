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


def _with_guidance(prompt: str, guidance: str) -> str:
    """Append the codegraph guidance block; empty guidance keeps the prompt byte-identical."""
    return f"{prompt}\n\n{guidance}" if guidance else prompt


def _excerpt(error: str) -> str:
    """Head excerpt of a failure report; the full text lives in .agent/feedback.md."""
    return error[:800]


def make_plan_prompt(issue: Issue, max_tasks: int, guidance: str = "") -> str:
    prompt = f"""You are the planner for GitHub Issue #{issue.number}.
Read AGENTS.md when present. Explore the codebase read-only. Do NOT modify files or commit.
Split the issue into a sequence of {max_tasks} or fewer concrete implementation tasks. Each task must:
- be independently committable and reviewable (one logical change per task)
- build on previous tasks (they execute in order on one branch)
- together fully satisfy the issue

Write each task VERY specifically. For every task description name the files or modules to create or
modify, the key functions/components to add and how they connect to earlier tasks, and end with a
checkable "Acceptance:" criterion (what the reviewer runs or verifies). Never be vague: "implement
the feature" is not enough.

Return ONLY a fenced JSON block with no prose outside it. Write every title and description as a
single paragraph with no raw line breaks and no trailing commas inside the JSON. A long single line
is fine; only raw line breaks break the format:
```json
[{{"title": "short task title", "description": "which files, which functions, how it connects, and the Acceptance: check"}}]
```"""
    return _with_guidance(prompt, guidance)


def make_task_prompt(
    issue: Issue, task: PlanTask, plan: list[PlanTask], *, retry_error: str = "", guidance: str = ""
) -> str:
    overview = "\n".join(f"{i + 1}. {t.title}" for i, t in enumerate(plan))
    retry = (
        "\nPrevious attempt failed. Full report: .agent/feedback.md "
        "(raw check output: .agent/check-output.txt). Excerpt:\n"
        f"{_excerpt(retry_error)}\n"
        if retry_error
        else ""
    )
    prompt = f"""You are the coding worker for GitHub Issue #{issue.number}, implementing one task of the plan.
Read AGENTS.md when present, .agent/task.md (issue + current task), and .agent/plan.md (full plan with
per-task descriptions). Implement ONLY the current task; earlier tasks are already committed — do not
redo or revert them. Stay inside this worktree. Do not commit, push, create a PR, merge, deploy, or edit
secrets. Add or update tests and documentation as required. Review your diff before finishing.
Plan overview (full descriptions in .agent/plan.md):
{overview}
{retry}"""
    return _with_guidance(prompt, guidance)


def make_task_review_prompt(issue: Issue, task: PlanTask, guidance: str = "") -> str:
    prompt = f"""Review the most recent commit for GitHub Issue #{issue.number}, which implements the task:
{task.title}
Read AGENTS.md and .agent/task.md. Do not modify files. Inspect the last commit (e.g. `git diff HEAD^ HEAD`)
and review only this task's changes.
Check correctness, security, compatibility, migrations, tests, and unrelated changes.
Give concrete reasons before the verdict. End with exactly one of these lines:
VERDICT: APPROVE
VERDICT: REQUEST_CHANGES
"""
    return _with_guidance(prompt, guidance)


def make_final_review_prompt(
    issue: Issue, plan: list[PlanTask], base_branch: str, guidance: str = ""
) -> str:
    overview = "\n".join(f"{i + 1}. {t.title}" for i, t in enumerate(plan))
    prompt = f"""Review the complete implementation for GitHub Issue #{issue.number} (all tasks on this branch).
Read AGENTS.md and .agent/plan.md (full plan with per-task descriptions). Do not modify files. Review the
full branch diff against the base (e.g. `git diff origin/{base_branch} HEAD`) as one coherent change, and
check it satisfies the plan:
Plan overview (full descriptions in .agent/plan.md):
{overview}
Check cross-task consistency, correctness, security, compatibility, migrations, tests, and unrelated changes.
Give concrete reasons before the verdict. End with exactly one of these lines:
VERDICT: APPROVE
VERDICT: REQUEST_CHANGES
"""
    return _with_guidance(prompt, guidance)


def make_final_fix_prompt(issue: Issue, error: str) -> str:
    return f"""You are the coding worker for GitHub Issue #{issue.number}. The final review or checks requested
changes. Fix the reported problems and leave no unrelated changes. Stay inside this worktree. Do not commit,
push, create a PR, merge, deploy, or edit secrets.
Reported problems: full report in .agent/feedback.md (raw check output: .agent/check-output.txt). Excerpt:
{_excerpt(error)}"""
