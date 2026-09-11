"""The dependency ids an issue is waiting on, shown by `status` and `report`.

The numbers come from the last blocker comment the orchestrator recorded in
``blocker_notices`` (its ``pending`` key). Reading that local record keeps
``status`` a fast offline command; the price is that a blocked-by link removed
on GitHub keeps showing until the orchestrator records the new set.
"""

import asyncio
import json
from argparse import Namespace

from issue_agent.cli import format_report, format_status
from issue_agent.config import load_config
from issue_agent.models import Issue, TaskStatus
from issue_agent.state import StateStore

CONFIG_TEMPLATE = """\
[runtime]
repo = "."
state_db = "state.db"
log_dir = "logs"
dry_run = false
[github]
repo = "a/b"
ready_label = "go-agent"
[agents.codex]
command = "codex exec -"
"""


def status_row(status: str, *, blocked_by: list[int] | None = None) -> dict[str, object]:
    row = {
        "issue_number": 7,
        "status": status,
        "title": "Check CLI",
        "agent": "codex",
        "updated_at": "2026-08-28T12:34:56+00:00",
    }
    if blocked_by is not None:
        row["blocked_by"] = blocked_by
    return row


def report_row(*, blocked_by: list[int] | None = None) -> dict[str, object]:
    row = {
        "issue_number": 7,
        "title": "Improve runner",
        "status": "failed",
        "total_input_tokens": 100,
        "total_output_tokens": 20,
        "total_duration_ms": 1000,
        "total_check_duration_ms": 2000,
        "total_wall_duration_ms": 4000,
        "total_cost_usd": 0.01,
        "tasks": [],
    }
    if blocked_by is not None:
        row["blocked_by"] = blocked_by
    return row


def test_status_shows_the_ids_an_issue_waits_on():
    lines = format_status([status_row("failed", blocked_by=[34])]).splitlines()

    assert lines[0].split() == ["ISSUE", "STATUS", "BLOCKED", "BY", "CURRENT", "TASK", "AGENT",
                                "TOKENS", "COST", "TIME", "UPDATED"]
    assert lines[2].split()[2] == "#34"


def test_status_lists_every_blocker_and_dashes_the_rest():
    rows = [
        status_row("failed", blocked_by=[34, 35]),
        status_row("coding", blocked_by=[]),
    ]
    body = format_status(rows).splitlines()[2:]

    assert "#34,#35" in body[0]
    assert body[1].split()[2] == "-"


def test_report_names_the_blockers_on_the_issue_line():
    rendered = format_report([report_row(blocked_by=[34])])

    assert "blocked_by=#34" in rendered.splitlines()[0]


def test_report_omits_the_field_when_nothing_blocks_the_issue():
    assert "blocked_by" not in format_report([report_row(blocked_by=[])])
    assert "blocked_by" not in format_report([report_row()])


def test_status_rows_carry_the_recorded_blockers(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(7, "Check CLI", "Body"), "codex")
    state.save_blocker_notices(7, {"pending": [35, 34], "declared": [1]})
    state.claim(Issue(8, "Unrelated", "Body"), "codex")

    rows = {int(row["issue_number"]): row["blocked_by"] for row in state.status_rows()}

    assert rows[7] == [34, 35]
    assert rows[8] == []


def test_report_rows_carry_the_recorded_blockers(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(7, "Check CLI", "Body"), "codex")
    state.save_blocker_notices(7, {"pending": [34]})

    assert state.report_rows()[0]["blocked_by"] == [34]


def test_recorded_blockers_survive_an_issue_that_was_never_claimed(tmp_path):
    """A fully blocked issue has a notice but no task row: the gate runs before
    the claim, so the record must be readable without one."""
    state = StateStore(tmp_path / "state.db")
    state.save_blocker_notices(30, {"pending": [27, 28, 29]})

    assert state.blockers_by_issue() == {30: [27, 28, 29]}


def test_blockers_read_as_none_when_the_record_is_empty_or_unreadable(tmp_path):
    state = StateStore(tmp_path / "state.db")
    for issue_number, payload in (
        (1, "{"),
        (2, "[27]"),
        (3, '{"pending": null}'),
        (4, '{"pending": ["27"]}'),
        (5, '{"pending": []}'),
        (6, '{"declared": [27]}'),
    ):
        state.save_blocker_notices(issue_number, {})
        with state.connect() as db:
            db.execute(
                "UPDATE blocker_notices SET blockers_notified=? WHERE issue_number=?",
                (payload, issue_number),
            )

    # A hand-edited or half-written record must read as "nothing blocks this
    # issue" rather than taking status/report down with it.
    assert state.blockers_by_issue() == {}


def test_status_json_carries_blocked_by_as_numbers(tmp_path, monkeypatch, capsys):
    from issue_agent.cli import async_main

    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(CONFIG_TEMPLATE)
    config = load_config(config_file)
    monkeypatch.setattr("issue_agent.cli.load_config", lambda path: config)
    state = StateStore(config.state_db)
    state.claim(Issue(36, "Retry the listener", "Body"), "codex")
    state.update(36, TaskStatus.FAILED)
    state.save_blocker_notices(36, {"pending": [34]})

    args = Namespace(
        command="status", json=True, active=False, color="never",
        config=str(config_file), verbose=False,
    )
    assert asyncio.run(async_main(args)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["blocked_by"] == [34]


def test_status_json_reports_no_blockers_as_an_empty_list(tmp_path, monkeypatch, capsys):
    from issue_agent.cli import async_main

    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(CONFIG_TEMPLATE)
    config = load_config(config_file)
    monkeypatch.setattr("issue_agent.cli.load_config", lambda path: config)
    state = StateStore(config.state_db)
    state.claim(Issue(12, "Unrelated", "Body"), "codex")

    args = Namespace(
        command="status", json=True, active=False, color="never",
        config=str(config_file), verbose=False,
    )
    assert asyncio.run(async_main(args)) == 0

    assert json.loads(capsys.readouterr().out)[0]["blocked_by"] == []


def test_the_blocker_column_survives_a_narrow_terminal():
    rows = [status_row("failed", blocked_by=[34, 35, 36, 37, 38])]

    rendered = format_status(rows, terminal_width=20)

    # The wide table cannot fit; the compact layout must still name every
    # blocker rather than dropping the column on the floor — and must break the
    # list between entries rather than through a number.
    for number in (34, 35, 36, 37, 38):
        assert f"#{number}" in rendered
