"""Check-command execution: baseline capture, regression verdicts, failure summaries.

Extracted from the orchestrator so check semantics live in one focused module.
Baseline-tolerance semantics are unchanged: pytest failures compare by node ID,
other commands by (returncode, output) fingerprints. Multiple check commands may
run concurrently (``[checks] parallel``); results are always processed in
configured order so errors and logs stay deterministic.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from .process import CommandError, Result, shell

_RE_FAILED_TEST = re.compile(r"^FAILED (\S+)", re.MULTILINE)
_ERROR_HINT_RE = re.compile(r"(?i)error|exception|traceback|failed|fatal|panic|assert")

_SUMMARY_MAX_CHARS = 2000
_CONTEXT_LINES = 3
_PER_TEST_MAX_CHARS = 300
FULL_OUTPUT_PATH = ".agent/check-output.txt"


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


def summarize_output(output: str, new_tests: set[str] | frozenset[str] = frozenset()) -> str:
    """Structured failure summary: error-bearing lines ± context, bounded size.

    With pytest node IDs, keep only the blocks around those IDs; otherwise scan
    for error-hint lines. Falls back to the tail when nothing matches or the
    summary overflows ``_SUMMARY_MAX_CHARS``. The full output is written to
    ``.agent/check-output.txt`` by :func:`run_checks` for deep inspection.
    """
    if not output.strip():
        return ""
    lines = output.splitlines()
    summary: str
    if new_tests:
        snippets: list[str] = []
        for test_id in sorted(new_tests):
            for index, line in enumerate(lines):
                if test_id in line:
                    block = lines[max(0, index - _CONTEXT_LINES): index + _CONTEXT_LINES + 1]
                    snippets.append("\n".join(block)[:_PER_TEST_MAX_CHARS])
                    break
        summary = "\n--\n".join(snippets)
    else:
        keep: set[int] = set()
        for index, line in enumerate(lines):
            if _ERROR_HINT_RE.search(line):
                keep.update(
                    range(max(0, index - _CONTEXT_LINES), min(len(lines), index + _CONTEXT_LINES + 1))
                )
        summary = "\n".join(lines[index] for index in sorted(keep))
    if not summary.strip() or len(summary) > _SUMMARY_MAX_CHARS:
        return output[-_SUMMARY_MAX_CHARS:]
    return summary


def _write_full_output(workspace: Path, output: str) -> None:
    agent_dir = workspace / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "check-output.txt").write_text(output + "\n", encoding="utf-8")


async def _execute(
    workspace: Path, checks: tuple[str, ...] | list[str], timeout: int, parallel: bool
) -> list[Result | BaseException]:
    """Run every check command; results (or exceptions) stay in configured order."""
    if parallel and len(checks) > 1:
        return list(
            await asyncio.gather(
                *(shell(command, cwd=workspace, timeout=timeout, check=False) for command in checks),
                return_exceptions=True,
            )
        )
    results: list[Result | BaseException] = []
    for command in checks:
        results.append(await shell(command, cwd=workspace, timeout=timeout, check=False))
    return results


def _combine(result: Result) -> str:
    return f"{result.stdout}\n{result.stderr}".strip()


async def capture_baseline(
    workspace: Path, checks: tuple[str, ...] | list[str], *, timeout: int, parallel: bool
) -> dict[str, CheckBaseline]:
    """Record which checks already fail on the current anchor commit."""
    results = await _execute(workspace, checks, timeout, parallel)
    baseline: dict[str, CheckBaseline] = {}
    for command, item in zip(checks, results):
        if isinstance(item, BaseException):
            raise item
        if item.returncode != 0:
            output = _combine(item)
            baseline[command] = CheckBaseline(
                returncode=item.returncode,
                failed_tests=frozenset(failed_tests(output)),
                output=output,
            )
    return baseline


async def run_checks(
    workspace: Path,
    issue_log,
    baseline: dict[str, CheckBaseline],
    *,
    checks: tuple[str, ...] | list[str],
    timeout: int,
    parallel: bool,
    seq: int | None = None,
    attempt: int | None = None,
    stage: str = "task",
) -> None:
    """Run the configured checks, tolerating failures already present on the base branch.

    Pytest failures are compared by node ID within the same command. Other
    failing commands are tolerated only while their return code and output
    remain unchanged. A regression raises ``CommandError`` carrying a structured
    summary; the raw output goes to ``.agent/check-output.txt``.
    """
    prefix = "final_" if stage == "final" else ""
    results = await _execute(workspace, checks, timeout, parallel)
    for command, item in zip(checks, results):
        if isinstance(item, BaseException):
            raise item
        if item.returncode == 0:
            issue_log.event(f"{prefix}check_passed", sequence=seq, attempt=attempt, command=command)
            continue
        output = _combine(item)
        current = failed_tests(output)
        previous = baseline.get(command)
        previous_tests = set(previous.failed_tests) if previous else set()
        new = current - previous_tests
        unchanged_generic_failure = (
            not current
            and previous is not None
            and not previous.failed_tests
            and item.returncode == previous.returncode
            and output == previous.output
        )
        if (current and not new) or unchanged_generic_failure:
            issue_log.event(
                f"{prefix}check_passed_pre_existing",
                sequence=seq,
                attempt=attempt,
                command=command,
                pre_existing=sorted(current) if current else ["unchanged command failure"],
            )
            continue
        _write_full_output(workspace, output)
        summary = summarize_output(output, new)
        if new:
            raise CommandError(
                f"check failed with {len(new)} new failure(s) not present on the base branch:\n"
                + "\n".join(sorted(new))
                + f"\n\n{summary}\n(full output: {FULL_OUTPUT_PATH})"
            )
        raise CommandError(
            f"command failed ({item.returncode}): {command}\n{summary}\n"
            f"(full output: {FULL_OUTPUT_PATH})"
        )
