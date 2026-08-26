from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .config import load_config
from .orchestrator import Orchestrator


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="autocode")
    result.add_argument("--config", default="orchestrator.toml")
    result.add_argument("--verbose", action="store_true")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="poll GitHub continuously")
    sub.add_parser("once", help="poll GitHub once and wait for workers")
    sub.add_parser("status", help="show persisted task state")
    return result


async def async_main(args: argparse.Namespace) -> None:
    app = Orchestrator(load_config(args.config))
    if args.command != "status":
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
    else:
        print(json.dumps(app.state.rows(), ensure_ascii=False, indent=2))


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
