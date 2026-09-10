from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import unicodedata
from functools import lru_cache

from .config import load_config
from .github import GitHub, required_label_specs
from .orchestrator import Orchestrator
from .process import CommandError
from .state import StateStore

# Task statuses that are safe to reset. Running statuses are excluded so an
# active worker is never disturbed mid-cycle; DONE/HUMAN_REVIEW are excluded
# because re-running would re-push the branch and create a duplicate PR. A SPLIT
# parent is included: resetting it is how a human overrules the split and asks
# for the issue to be planned again as a single unit.
_RESETTABLE = frozenset({"pending", "planned", "failed", "blocked", "split"})


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="issue-agent")
    result.add_argument("--config", default="issue-agent.toml")
    result.add_argument("--verbose", action="store_true")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="poll GitHub continuously")
    sub.add_parser("once", help="poll GitHub once and wait for workers")
    status = sub.add_parser("status", help="show current and persisted task state")
    status.add_argument("--active", action="store_true", help="show only running tasks")
    status.add_argument("--json", action="store_true", help="output machine-readable JSON")
    report = sub.add_parser("report", help="show per-Issue and per-task time and token usage")
    report.add_argument("--issue", type=int, help="limit the report to one GitHub Issue")
    report.add_argument("--json", action="store_true", help="output machine-readable JSON")
    reset = sub.add_parser("reset", help="reset a task so it can be claimed and run again")
    reset.add_argument("issue", type=int, help="GitHub issue number to reset")
    reset.add_argument(
        "--no-label", action="store_true", help="reset state only; do not re-add the ready label"
    )
    return result


def _format_tokens(row: dict[str, object]) -> str:
    """Combined input+output token count, compact notation for large values."""
    total = int(row.get("total_input_tokens") or 0) + int(row.get("total_output_tokens") or 0)
    if total == 0:
        return "-"
    if total >= 1_000_000:
        return f"{total / 1_000_000:.1f}M"
    if total >= 1_000:
        return f"{total / 1_000:.1f}k"
    return str(total)


def _format_cost(row: dict[str, object]) -> str:
    cost = float(row.get("total_cost_usd") or 0.0)
    if cost == 0.0:
        return "-"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def _format_duration(row: dict[str, object]) -> str:
    ms = int(row.get("total_duration_ms") or 0)
    if ms == 0:
        return "-"
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m"


@lru_cache(maxsize=4096)
def _char_width(char: str) -> int:
    if unicodedata.category(char) in {"Mn", "Me", "Cf"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _display_width(text: str) -> int:
    """Terminal cells: CJK/fullwidth characters use two, combining marks zero."""
    return sum(map(_char_width, text))


def _wrap_display(text: str, width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    used = 0
    for char in text:
        size = _char_width(char)
        if used + size > width and line:
            # Prefer word boundaries for English; CJK and long identifiers can
            # still break at any character. Preserve the original whitespace.
            boundary = line.rfind(" ") + 1
            if 0 < boundary < len(line):
                lines.append(line[:boundary])
                line = line[boundary:]
                used = _display_width(line)
            else:
                lines.append(line)
                line, used = "", 0
        line += char
        used += size
    return [*lines, line]


def _compact_status(headings: tuple[str, ...], rows: list[tuple[str, ...]], width: int) -> str:
    """Keep all fields readable when the terminal cannot fit the table headers."""
    label_width = max(map(len, headings)) + 2
    lines: list[str] = []
    for row in rows:
        if lines:
            lines.append("")
        for heading, value in zip(headings, row):
            if width < label_width + 2:
                lines.extend(_wrap_display(heading + ":", width))
                lines.extend(_wrap_display(value, width))
                continue
            for index, part in enumerate(_wrap_display(value, width - label_width)):
                label = (heading + ":").ljust(label_width) if index == 0 else " " * label_width
                lines.append(label + part)
    return "\n".join(lines)


def format_status(rows: list[dict[str, object]], *, terminal_width: int | None = None) -> str:
    if not rows:
        return "No matching tasks."
    headings = ("ISSUE", "STATUS", "CURRENT TASK", "AGENT", "TOKENS", "COST", "TIME", "UPDATED")
    values = [
        (
            f"#{row['issue_number']}",
            str(row["status"]),
            str(row.get("current_task") or row.get("title") or "-"),
            str(row.get("agent") or "-"),
            _format_tokens(row),
            _format_cost(row),
            _format_duration(row),
            str(row.get("updated_at") or "-").replace("T", " ")[:19],
        )
        for row in rows
    ]
    # Embedded tabs/newlines must not move subsequent columns out of alignment.
    values = [tuple(" ".join(cell.split()) for cell in row) for row in values]
    widths = [
        max(_display_width(headings[i]), *(_display_width(row[i]) for row in values))
        for i in range(len(headings))
    ]
    if terminal_width is None:
        terminal_width = shutil.get_terminal_size(fallback=(120, 24)).columns
    terminal_width = max(2, terminal_width)
    minimums = list(map(_display_width, headings))
    gaps = 2 * (len(headings) - 1)
    if sum(minimums) + gaps > terminal_width:
        return _compact_status(headings, values, terminal_width)
    fixed_width = sum(widths) - widths[2] + 2 * (len(headings) - 1)
    widths[2] = min(widths[2], max(len(headings[2]), min(60, terminal_width - fixed_width)))
    # Long agent names/statuses must not force the terminal to wrap the whole row.
    while sum(widths) + gaps > terminal_width:
        column = max(range(len(widths)), key=lambda i: widths[i] - minimums[i])
        excess = sum(widths) + gaps - terminal_width
        widths[column] -= min(excess, widths[column] - minimums[column])

    def render(row: tuple[str, ...]) -> str:
        return "  ".join(value + " " * (widths[i] - _display_width(value))
                         for i, value in enumerate(row))

    header = render(headings)
    separator = "  ".join("-" * width for width in widths)
    body: list[str] = []
    for row in values:
        wrapped = [_wrap_display(cell, width) for cell, width in zip(row, widths)]
        for index in range(max(map(len, wrapped))):
            cells = [parts[index] if index < len(parts) else "" for parts in wrapped]
            body.append(render(tuple(cells)))
    return "\n".join((header, separator, *body))


def format_report(rows: list[dict[str, object]]) -> str:
    """Render cumulative wall/agent/check time and tokens for Issues and plan tasks."""
    if not rows:
        return "No matching tasks."
    sections: list[str] = []
    for row in rows:
        summary = (
            f"#{row['issue_number']} {row['title']} [{row['status']}]  "
            f"wall={_format_duration({'total_duration_ms': row.get('total_wall_duration_ms')})}  "
            f"queue={_format_duration({'total_duration_ms': row.get('total_queue_duration_ms')})}  "
            f"agent={_format_duration(row)}  "
            f"checks={_format_duration({'total_duration_ms': row.get('total_check_duration_ms')})}  "
            f"tokens={_format_tokens(row)}  cost={_format_cost(row)}"
        )
        tasks = row.get("tasks") or []
        if not tasks:
            sections.append(summary)
            continue
        headings = ("TASK", "STATUS", "ATTEMPTS", "TOKENS", "AGENT", "CHECKS", "WALL", "TITLE")
        values = [
            (
                str(int(task["seq"]) + 1),
                str(task["status"]),
                str(task["attempts"]),
                _format_tokens(task),
                _format_duration(task),
                _format_duration({"total_duration_ms": task.get("total_check_duration_ms")}),
                _format_duration({"total_duration_ms": task.get("total_wall_duration_ms")}),
                str(task["title"]),
            )
            for task in tasks
        ]
        widths = [
            max(len(headings[index]), *(len(value[index]) for value in values))
            for index in range(len(headings))
        ]
        table = [
            "  ".join(value.ljust(widths[index]) for index, value in enumerate(headings)),
            "  ".join("-" * width for width in widths),
            *("  ".join(value.ljust(widths[index]) for index, value in enumerate(item)) for item in values),
        ]
        sections.append(summary + "\n" + "\n".join(table))
    return "\n\n".join(sections)


async def reset_issue(config, issue_number: int, *, no_label: bool) -> int:
    """Reset one task row so it is claimable again, optionally requeueing it.

    Clears the whole-issue retry budget and returns the row to PENDING. Unless
    ``no_label`` is set, also re-adds the configured ready label (and removes the
    orchestrator-maintained ``agent-failed``/``agent-running`` labels) so the
    issue is picked up on the next scheduler poll. ``human-review`` goes with
    them, so a reset SPLIT parent does not keep advertising a review that is
    over. Prints a summary; returns a process exit code.
    """
    state = StateStore(config.state_db)
    row = next((r for r in state.rows() if int(r["issue_number"]) == issue_number), None)
    if row is None:
        print(f"issue #{issue_number} has no task row in state DB; nothing to reset", file=sys.stderr)
        return 1
    try:
        # The status check runs again on the reset connection itself: a serve
        # process claiming the issue after the snapshot above makes the reset
        # fail loudly instead of clobbering a live run's row.
        state.reset(issue_number, allowed=_RESETTABLE)
    except ValueError as exc:
        print(f"cannot reset issue #{issue_number}: {exc}", file=sys.stderr)
        return 1
    summary = f"reset issue #{issue_number}: {row['status']} (failures={row['failures']}) -> pending"
    if not no_label:
        github = GitHub(config.github_repo, config.repo, dry_run=config.dry_run)
        try:
            await github.labels(
                issue_number,
                add=(config.ready_label,),
                # agent-needs-info goes too: a reset must not leave the issue
                # advertising a question nobody is waiting to answer.
                remove=("agent-failed", "agent-running", "agent-needs-info", "human-review"),
            )
        except CommandError as exc:
            # The DB reset is durable; only the label handoff failed. Tell the
            # human exactly what is left to do instead of raising a traceback.
            print(
                f"warning: the task row was reset, but the GitHub label update failed: {exc}\n"
                f"re-add the {config.ready_label} label manually, e.g.:\n"
                f"  gh issue edit {issue_number} --add-label {config.ready_label}",
                file=sys.stderr,
            )
            return 1
        summary += f"; re-added {config.ready_label}, will be picked up on the next poll"
    else:
        summary += f"; add the {config.ready_label} label to rerun"
    print(summary)
    return 0


async def preflight_labels(config) -> int:
    """Fail fast when the GitHub repo lacks labels the orchestrator applies.

    Prints paste-ready ``gh label create`` guidance for every missing label and
    returns a non-zero exit code so no worker starts against a repo where claim,
    requeue, park, and hand-off transitions would fail mid-run. ``dry_run`` skips
    the check; a failing ``gh`` call (not logged in, unreachable repo) warns and
    also refuses to start.
    """
    if config.dry_run:
        return 0
    if not config.github_repo:
        print(
            f"notice: github.repo is not set; verifying labels against the git "
            f"remote of {config.repo}",
            file=sys.stderr,
        )
    github = GitHub(config.github_repo, config.repo)
    specs = required_label_specs(config.ready_label, config.agents)
    try:
        existing = await github.label_names()
    except (CommandError, ValueError) as exc:
        print(f"warning: cannot verify GitHub labels: {exc}", file=sys.stderr)
        print(
            "warning: refusing to start; check 'gh auth status' and the github.repo setting",
            file=sys.stderr,
        )
        return 1
    missing = [name for name in specs if name not in existing]
    if not missing:
        return 0
    print("error: GitHub repo is missing labels required by issue-agent:", file=sys.stderr)
    for name in missing:
        color, description = specs[name]
        print(
            f'  gh label create "{name}" --color {color} --description "{description}"',
            file=sys.stderr,
        )
    return 1


async def async_main(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.command == "status":
        rows = StateStore(config.state_db).status_rows(active_only=args.active)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else format_status(rows))
        return 0
    if args.command == "report":
        rows = StateStore(config.state_db).report_rows(args.issue)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else format_report(rows))
        return 0
    if args.command == "reset":
        return await reset_issue(config, args.issue, no_label=args.no_label)

    if args.command == "once" and not config.schedule.allows():
        logging.getLogger(__name__).info(
            "execution window closed; skipping new tasks (schedule)"
        )
        return 0

    exit_code = await preflight_labels(config)
    if exit_code:
        return exit_code

    app = Orchestrator(config)
    recovered = app.recover()
    if recovered:
        logging.getLogger(__name__).warning("recovered %s interrupted task(s)", recovered)
    if args.command == "serve":
        try:
            await app.serve()
        finally:
            await app.shutdown()
    elif args.command == "once":
        return await wait_for_once_workers(app)
    return 0


async def wait_for_once_workers(app: Orchestrator) -> int:
    """Wait for the workers one ``once`` poll started; 1 when any worker raised.

    Siblings must never be abandoned: a bare ``gather`` would surface the first
    exception and leave the remaining workers un-awaited with rows stuck on
    CLAIMED until the next restart's recovery charges them a failure unit.
    ``shutdown()`` runs even when the poll itself fails, mirroring ``serve``.
    """
    outcomes: list[BaseException | object] = []
    try:
        await app.run_once()
        if app.running:
            outcomes = list(
                await asyncio.gather(*app.running.values(), return_exceptions=True)
            )
    finally:
        await app.shutdown()
    exit_code = 0
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(
            outcome, asyncio.CancelledError
        ):
            logging.getLogger(__name__).error("once-mode worker failed: %r", outcome)
            exit_code = 1
    return exit_code


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
