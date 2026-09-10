from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from pathlib import Path

from .formal_review import redact_secrets
from .models import Blocker, Comment, Issue
from .process import CommandError, run

log = logging.getLogger(__name__)

# Labels the orchestrator applies itself, with recommended colors and
# descriptions used to generate `gh label create` guidance at startup.
# Keep in sync with the init script in README「初始化 GitHub Labels」.
ORCHESTRATOR_LABELS: dict[str, tuple[str, str]] = {
    "agent-running": ("1d76db", "Implementation in progress"),
    "agent-planned": ("7057ff", "Plan published; awaiting human approval"),
    "agent-failed": ("d73a4a", "Agent run failed"),
    "agent-needs-info": ("d4c5f9", "Planner needs more information"),
    "human-review": ("fbca04", "Awaiting human review"),
}
_READY_LABEL_SPEC = ("0e8a16", "Ready for coding-agent implementation")
_AGENT_ROUTE_COLOR = "a219d8"
# Dry-run issue numbers come from a range GitHub can never hand out, so a
# recorded fake child can never collide with a real one.
_DRY_RUN_ISSUE_BASE = 1_000_000_000


def _issue_number(url: str) -> int:
    """The issue number in a GitHub issue URL, or 0 when the URL has none."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def required_label_specs(
    ready_label: str, agent_names: Iterable[str]
) -> dict[str, tuple[str, str]]:
    """Every label the orchestrator applies, mapped to a suggested (color, description).

    Covers the configured ready label, the orchestrator-maintained workflow
    labels, and one ``agent:<name>`` routing label per enabled agent. User-side
    hint labels (e.g. ``resource:database-schema``) are optional and not listed.
    """
    specs = {ready_label: _READY_LABEL_SPEC, **ORCHESTRATOR_LABELS}
    for name in agent_names:
        specs[f"agent:{name}"] = (
            _AGENT_ROUTE_COLOR,
            f"Implement with the '{name}' agent",
        )
    return specs


class GitHub:
    def __init__(self, repo: str, cwd: Path, *, dry_run: bool = False):
        self.repo = repo
        self.cwd = cwd
        self.dry_run = dry_run
        self._dry_run_issue_counter = 0

    async def _gh(self, *args: str, check: bool = True, repo: bool = True) -> str:
        command = ["gh", *args]
        if repo and self.repo and "--repo" not in args:
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
        args.extend(
            ("--limit", str(limit), "--json", "number,title,body,labels,url,blockedBy,parent")
        )
        output = await self._gh(*args)
        return [
            Issue(
                number=item["number"], title=item["title"], body=item.get("body") or "",
                labels=tuple(label["name"] for label in item.get("labels", [])), url=item.get("url", ""),
                blocked_by=tuple(
                    node["number"] for node in (item.get("blockedBy") or {}).get("nodes", [])
                ),
                parent=(item.get("parent") or {}).get("number"),
            )
            for item in json.loads(output)
        ]

    async def blocker_states(self, numbers: Iterable[int]) -> dict[int, Blocker]:
        """Map each blocker issue number to its title and whether it is closed.

        One ``gh issue view`` per blocker rather than a single GraphQL query:
        ``gh api`` takes no ``--repo`` flag, so batching would need its own repo
        plumbing, and blockers are rare enough that the extra calls only happen
        for the handful of gated candidates. Titles come along because the gate
        names its blockers in a comment. Callers must treat a missing entry as
        still open. A failing lookup stays missing (with a log line) instead of
        raising, so one deleted blocker cannot starve the whole scheduler queue.
        """
        wanted = sorted(set(numbers))
        outputs = await asyncio.gather(
            *(
                self._gh("issue", "view", str(number), "--json", "number,title,state")
                for number in wanted
            ),
            return_exceptions=True,
        )
        states: dict[int, Blocker] = {}
        for number, output in zip(wanted, outputs):
            if isinstance(output, BaseException):
                # A deleted/transferred blocker or a transient gh failure must
                # not raise through the scheduler: the documented contract is
                # that callers treat a missing entry as still open, so a failed
                # lookup simply stays missing.
                log.warning("blocker #%s lookup failed; treating it as still open: %s", number, output)
                continue
            item = json.loads(output)
            states[int(item["number"])] = Blocker(
                number=int(item["number"]),
                title=str(item.get("title") or ""),
                closed=item.get("state") == "CLOSED",
            )
        return states

    async def viewer_login(self) -> str:
        """This machine's GitHub login, or "" when it cannot be asked.

        ``gh api`` accepts no ``--repo``, hence the flag off. Dry-run returns ""
        without a request, matching ``labels`` and ``create_pr``; callers must
        read "" as "cannot tell my comments from a human's" and switch reply
        detection off rather than guess.
        """
        if self.dry_run:
            return ""
        return (await self._gh("api", "user", "--jq", ".login", repo=False)).strip()

    async def comments(self, number: int) -> list[Comment]:
        """Every comment on an issue, oldest first, for reply detection."""
        output = await self._gh("issue", "view", str(number), "--json", "comments")
        items = json.loads(output).get("comments") or []
        return [
            Comment(
                author=str((item.get("author") or {}).get("login") or ""),
                created_at=str(item.get("createdAt") or ""),
                body=str(item.get("body") or ""),
            )
            for item in items
        ]

    async def ready_issues(self, label: str, limit: int = 20) -> list[Issue]:
        return await self.open_issues(limit, label=label)

    async def unassigned_issues(self, limit: int = 20, *, ready_label: str = "") -> list[Issue]:
        """Return open Issues that are not already in the agent workflow.

        Product labels such as ``bug`` and ``enhancement`` must not prevent the
        plan-only phase.  An Issue becomes ineligible once an ``agent-*``
        workflow label is present (for example ``agent-ready`` or
        ``agent-running``) or when it carries the configured ready label — which
        need not be named ``agent-*`` at all.  ``agent:<name>`` remains a
        routing preference, so it intentionally does not suppress planning.
        """
        return [
            issue
            for issue in await self.open_issues(limit)
            if not any(
                label == ready_label or label.startswith("agent-") for label in issue.labels
            )
        ]

    async def runnable_issues(self, ready_label: str, limit: int = 20) -> list[Issue]:
        """Include interrupted jobs whose ready label was already removed.

        Merged and sorted by issue number: gh lists newest-first by default, so
        a backlog deeper than the limit would starve its oldest members without
        the local sort.
        """
        issues, interrupted = await asyncio.gather(
            self.ready_issues(ready_label, limit),
            self.ready_issues("agent-running", limit),
        )
        merged = {issue.number: issue for issue in (*issues, *interrupted)}
        return [merged[number] for number in sorted(merged)]

    async def label_names(self) -> set[str]:
        """All label names defined in the repository (single read-only call).

        ``--limit 500`` caps one page; repositories with more than 500 labels
        would need pagination, which is out of scope for the startup preflight.
        Malformed entries (no ``name`` key) raise ``ValueError`` so callers can
        treat them like any other verification failure.
        """
        output = await self._gh("label", "list", "--json", "name", "--limit", "500")
        items = json.loads(output)
        names = {item.get("name") for item in items}
        if not all(isinstance(name, str) for name in names):
            raise ValueError("unexpected 'gh label list' payload: label entries missing 'name'")
        return set(names)

    async def create_issue(
        self, title: str, body: str, *, labels: tuple[str, ...] = ()
    ) -> tuple[int, str]:
        """Create an issue and return ``(number, url)``.

        ``gh issue create`` prints only the URL, so the number is parsed out of
        its last path segment. Dry-run makes no request and returns distinct
        fake numbers from a range GitHub can never hand out, so a dry-run can
        exercise the whole split flow — recording the placeholder is safe
        because a dry-run never creates anything a later real run would skip.
        """
        title = redact_secrets(title)
        body = redact_secrets(body)
        if self.dry_run:
            self._dry_run_issue_counter += 1
            number = _DRY_RUN_ISSUE_BASE + self._dry_run_issue_counter
            return (number, f"dry-run://issue/{number}")
        args = ["issue", "create", "--title", title, "--body", body]
        for label in labels:
            args.extend(("--label", label))
        url = (await self._gh(*args)).strip()
        number = _issue_number(url)
        if not number:
            raise CommandError(f"gh issue create returned no issue number: {url!r}")
        return (number, url)

    async def link_parent(self, child: int, parent: int) -> None:
        """Make ``child`` a sub-issue of ``parent`` so GitHub tracks progress."""
        if self.dry_run:
            return
        await self._gh("issue", "edit", str(child), "--parent", str(parent))

    async def add_blocked_by(self, child: int, blocker: int) -> None:
        """Declare that ``child`` is blocked by ``blocker``, natively."""
        if self.dry_run:
            return
        await self._gh("issue", "edit", str(child), "--add-blocked-by", str(blocker))

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
        title = redact_secrets(title)
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
        body = redact_secrets(body)
        if not self.dry_run:
            await self._gh("issue", "comment", str(number), "--body", body)
