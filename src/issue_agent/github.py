from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .models import Issue
from .process import CommandError, run


class GitHub:
    def __init__(self, repo: str, cwd: Path, *, dry_run: bool = False):
        self.repo = repo
        self.cwd = cwd
        self.dry_run = dry_run

    async def _gh(self, *args: str, check: bool = True) -> str:
        command = ["gh", *args]
        if self.repo and "--repo" not in args:
            command.extend(("--repo", self.repo))
        result = await run(command, cwd=self.cwd, check=check)
        return result.stdout

    async def open_issues(
        self, limit: int = 20, *, label: str = "", search: str = ""
    ) -> list[Issue]:
        args = ["issue", "list", "--state", "open"]
        if label:
            args.extend(("--label", label))
        if search:
            args.extend(("--search", search))
        args.extend(("--limit", str(limit), "--json", "number,title,body,labels,url"))
        output = await self._gh(*args)
        return [
            Issue(
                number=item["number"], title=item["title"], body=item.get("body") or "",
                labels=tuple(label["name"] for label in item.get("labels", [])), url=item.get("url", ""),
            )
            for item in json.loads(output)
        ]

    async def ready_issues(self, label: str, limit: int = 20) -> list[Issue]:
        return await self.open_issues(limit, label=label)

    async def unassigned_issues(self, limit: int = 20) -> list[Issue]:
        """Return open Issues that are not already in the agent workflow.

        Product labels such as ``bug`` and ``enhancement`` must not prevent the
        plan-only phase.  An Issue becomes ineligible only once an ``agent-*``
        workflow label is present (for example ``agent-ready`` or
        ``agent-running``).  ``agent:<name>`` remains a routing preference, so
        it intentionally does not suppress planning.
        """
        return [
            issue
            for issue in await self.open_issues(limit)
            if not any(label.startswith("agent-") for label in issue.labels)
        ]

    async def runnable_issues(self, ready_label: str, limit: int = 20) -> list[Issue]:
        """Include interrupted jobs whose ready label was already removed."""
        issues, interrupted = await asyncio.gather(
            self.ready_issues(ready_label, limit),
            self.ready_issues("agent-running", limit),
        )
        return list({issue.number: issue for issue in (*issues, *interrupted)}.values())

    async def labels(self, number: int, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> None:
        if self.dry_run:
            return
        args = ["issue", "edit", str(number)]
        for label in add:
            args.extend(("--add-label", label))
        for label in remove:
            args.extend(("--remove-label", label))
        await self._gh(*args)

    async def create_pr(self, number: int, branch: str, base: str, title: str, checks: tuple[str, ...]) -> str:
        if self.dry_run:
            return f"dry-run://pr/{number}"
        existing = await self.find_pr(branch)
        if existing:
            return existing
        body = "\n".join((f"Closes #{number}", "", "Automated checks:", *(f"- `{c}`" for c in checks), "", "Human review required."))
        try:
            return (await self._gh("pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body)).strip()
        except CommandError:
            existing = await self.find_pr(branch)
            if existing:
                return existing
            raise

    async def find_pr(self, branch: str) -> str:
        """Return an existing open PR URL for a branch, if any."""
        output = await self._gh(
            "pr", "list", "--state", "open", "--head", branch, "--limit", "1", "--json", "url"
        )
        items = json.loads(output)
        return str(items[0]["url"]) if items else ""

    async def comment(self, number: int, body: str) -> None:
        if not self.dry_run:
            await self._gh("issue", "comment", str(number), "--body", body)
