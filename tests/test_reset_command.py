"""`issue-agent reset`: the human escape hatch out of every parked status."""

import asyncio
from argparse import Namespace
from pathlib import Path

from issue_agent.config import load_config
from issue_agent.models import Issue, RecordedChild, TaskStatus
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


def write_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(CONFIG_TEMPLATE)
    return config_file


def reset(tmp_path: Path, number: int, monkeypatch, *, no_label: bool = False) -> int:
    from issue_agent import cli

    config_file = write_config(tmp_path)
    monkeypatch.setattr("issue_agent.cli.load_config", lambda path: load_config(config_file))
    calls: list[list[str]] = []

    async def fake_run(command, **kwargs):
        from issue_agent.process import Result

        calls.append(list(command))
        return Result(0, "", "")

    monkeypatch.setattr("issue_agent.github.run", fake_run)
    args = Namespace(
        command="reset", issue=number, no_label=no_label, config=str(config_file), verbose=False
    )
    code = asyncio.run(cli.async_main(args))
    return code, calls


def test_reset_releases_a_split_parent_for_replanning(tmp_path, monkeypatch, capsys):
    config = load_config(write_config(tmp_path))
    state = StateStore(config.state_db)
    issue = Issue(number=4, title="Too big", body="Do everything")
    state.claim_for_planning(issue, "planner")
    state.save_split(4, [RecordedChild(title="One of the children", body="Body")])
    state.update(4, TaskStatus.SPLIT)

    code, calls = reset(tmp_path, 4, monkeypatch)

    assert code == 0
    assert "split" in capsys.readouterr().out
    row = StateStore(config.state_db).rows()[0]
    assert row["status"] == str(TaskStatus.PENDING)
    # The split record goes too, so the next planning pass starts from scratch
    # rather than resuming the proposal the human just rejected.
    assert StateStore(config.state_db).load_split(4) == []
    edit = next(call for call in calls if call[1:3] == ["issue", "edit"])
    assert "--add-label" in edit and "go-agent" in edit
    # Leaving `human-review` behind would contradict the ready label just added.
    assert "--remove-label" in edit and "human-review" in edit


def test_reset_still_clears_the_labels_a_plain_failure_left(tmp_path, monkeypatch):
    config = load_config(write_config(tmp_path))
    state = StateStore(config.state_db)
    state.claim(Issue(number=5, title="Broken", body=""), "codex")
    state.record_failure(5, TaskStatus.FAILED, "boom")

    code, calls = reset(tmp_path, 5, monkeypatch)

    assert code == 0
    edit = next(call for call in calls if call[1:3] == ["issue", "edit"])
    for name in ("go-agent", "agent-running", "agent-failed", "agent-needs-info", "human-review"):
        assert name in edit


def test_reset_also_drops_a_stale_needs_info_label(tmp_path, monkeypatch):
    """A reset of an issue parked by the clarify flow must not leave
    agent-needs-info behind contradicting the ready label just added."""
    config = load_config(write_config(tmp_path))
    state = StateStore(config.state_db)
    state.claim_for_planning(Issue(number=8, title="Vague", body=""), "planner")
    state.record_clarify_round(8, "2026-09-10T01:00:00+00:00")
    state.record_failure(8, TaskStatus.FAILED, "planner gave up")

    code, calls = reset(tmp_path, 8, monkeypatch)

    assert code == 0
    edit = next(call for call in calls if call[1:3] == ["issue", "edit"])
    assert "agent-needs-info" in edit


def test_reset_reopens_a_human_review_issue(tmp_path, monkeypatch, capsys):
    """A reviewer closing the PR unmerged has no other way back: reset reopens
    the issue, and a re-run reuses the branch's existing PR instead of
    duplicating it."""
    config = load_config(write_config(tmp_path))
    state = StateStore(config.state_db)
    state.claim(Issue(number=6, title="PR up", body=""), "codex")
    state.update(6, TaskStatus.HUMAN_REVIEW)

    code, calls = reset(tmp_path, 6, monkeypatch)

    assert code == 0
    assert "human_review" in capsys.readouterr().out
    edit = next(call for call in calls if call[1:3] == ["issue", "edit"])
    assert "--remove-label" in edit and "human-review" in edit
    row = StateStore(config.state_db).rows()[0]
    assert row["status"] == str(TaskStatus.PENDING)


def test_reset_survives_a_failing_label_update(tmp_path, monkeypatch, capsys):
    """The DB reset is durable even when gh fails; the human gets the exact
    manual command instead of a traceback."""
    from issue_agent import cli
    from issue_agent.process import CommandError

    config_file = write_config(tmp_path)
    monkeypatch.setattr("issue_agent.cli.load_config", lambda path: load_config(config_file))

    async def failing_run(command, **kwargs):
        raise CommandError("gh: HTTP 502")

    monkeypatch.setattr("issue_agent.github.run", failing_run)
    state = StateStore(load_config(config_file).state_db)
    state.claim(Issue(number=7, title="Broken", body=""), "codex")
    state.record_failure(7, TaskStatus.FAILED, "boom")

    args = Namespace(command="reset", issue=7, no_label=False, config=str(config_file), verbose=False)
    code = asyncio.run(cli.async_main(args))

    assert code == 1
    err = capsys.readouterr().err
    assert "label update failed" in err and "gh issue edit 7" in err
    row = StateStore(load_config(config_file).state_db).rows()[0]
    assert row["status"] == str(TaskStatus.PENDING)
