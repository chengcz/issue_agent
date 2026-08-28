from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .config import load_config
from .orchestrator import Orchestrator
from .state import StateStore


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
    return result


def format_status(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No matching tasks."
    headings = ("ISSUE", "STATUS", "CURRENT TASK", "AGENT", "UPDATED")
    values = [
        (
            f"#{row['issue_number']}",
            str(row["status"]),
            str(row.get("current_task") or row.get("title") or "-"),
            str(row.get("agent") or "-"),
            str(row.get("updated_at") or "-").replace("T", " ")[:19],
        )
        for row in rows
    ]
    widths = [max(len(headings[i]), *(len(row[i]) for row in values)) for i in range(len(headings))]
    header = "  ".join(value.ljust(widths[i]) for i, value in enumerate(headings))
    separator = "  ".join("-" * width for width in widths)
    body = ["  ".join(value.ljust(widths[i]) for i, value in enumerate(row)) for row in values]
    return "\n".join((header, separator, *body))


async def async_main(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.command == "status":
        rows = StateStore(config.state_db).status_rows(active_only=args.active)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else format_status(rows))
        return

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
        await app.run_once()
        if app.running:
            await asyncio.gather(*app.running.values())


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
