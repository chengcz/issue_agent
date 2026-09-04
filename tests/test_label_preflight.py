"""Startup preflight: every label the orchestrator applies must exist on GitHub."""

import asyncio
import json
from argparse import Namespace
from pathlib import Path

from issue_agent.config import load_config
from issue_agent.github import GitHub, required_label_specs
from issue_agent.process import CommandError, Result

CONFIG_TEMPLATE = """\
[runtime]
repo = "."
state_db = "state.db"
log_dir = "logs"
dry_run = {dry_run}
[github]
repo = "a/b"
ready_label = "go-agent"
[agents.codex]
command = "codex exec -"
[agents.claude]
command = "claude -p"
enabled = false
"""

ORCHESTRATOR_LABELS = (
    "agent-running",
    "agent-planned",
    "agent-failed",
    "human-review",
)


def write_config(tmp_path: Path, *, dry_run: bool = False) -> Path:
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(CONFIG_TEMPLATE.format(dry_run=str(dry_run).lower()))
    return config_file


def fake_label_run(payload: str, calls: list):
    async def fake_run(command, **kwargs):
        calls.append(command)
        return Result(0, payload, "")

    return fake_run


def test_label_names_parses_gh_json_output(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "issue_agent.github.run",
        fake_label_run('[{"name":"bug"},{"name":"agent-ready"}]', calls),
    )
    github = GitHub("a/b", tmp_path)
    names = asyncio.run(github.label_names())
    assert names == {"bug", "agent-ready"}
    command = calls[0]
    assert command[:3] == ["gh", "label", "list"]
    assert "--json" in command and "name" in command
    assert "--repo" in command and "a/b" in command


def test_required_label_specs_cover_ready_orchestrator_and_agent_routes():
    specs = required_label_specs("go-agent", ["codex"])
    assert set(specs) == {"go-agent", *ORCHESTRATOR_LABELS, "agent:codex"}
    for name, (color, description) in specs.items():
        assert len(color) == 6 and all(c in "0123456789abcdef" for c in color), name
        assert description, name
    assert "codex" in specs["agent:codex"][1]


def test_ready_label_collision_keeps_orchestrator_spec():
    from issue_agent import github

    specs = github.required_label_specs("agent-running", [])
    assert len(specs) == 4
    assert specs["agent-running"] == github.ORCHESTRATOR_LABELS["agent-running"]


def test_preflight_labels_passes_when_every_required_label_exists(tmp_path, monkeypatch, capsys):
    from issue_agent.cli import preflight_labels

    config = load_config(write_config(tmp_path))
    existing = [{"name": name} for name in ("go-agent", *ORCHESTRATOR_LABELS, "agent:codex", "bug")]
    monkeypatch.setattr(
        "issue_agent.github.run",
        fake_label_run(json.dumps(existing), []),
    )
    assert asyncio.run(preflight_labels(config)) == 0
    assert capsys.readouterr().err == ""


def test_preflight_labels_prints_create_commands_for_missing_labels(tmp_path, monkeypatch, capsys):
    from issue_agent.cli import preflight_labels

    config = load_config(write_config(tmp_path))
    monkeypatch.setattr(
        "issue_agent.github.run",
        fake_label_run('[{"name":"bug"},{"name":"enhancement"}]', []),
    )
    assert asyncio.run(preflight_labels(config)) == 1
    err = capsys.readouterr().err
    for name in ("go-agent", *ORCHESTRATOR_LABELS, "agent:codex"):
        assert f'gh label create "{name}"' in err
        assert "--color" in err and "--description" in err


def test_preflight_labels_notices_repo_fallback_when_github_repo_unset(tmp_path, monkeypatch, capsys):
    from issue_agent.cli import preflight_labels

    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(CONFIG_TEMPLATE.format(dry_run="false").replace('repo = "a/b"', 'repo = ""'))
    config = load_config(config_file)
    existing = [{"name": name} for name in ("go-agent", *ORCHESTRATOR_LABELS, "agent:codex")]
    monkeypatch.setattr("issue_agent.github.run", fake_label_run(json.dumps(existing), []))
    assert asyncio.run(preflight_labels(config)) == 0
    err = capsys.readouterr().err
    assert "notice" in err.lower()
    assert "github.repo" in err


def test_preflight_labels_skips_github_in_dry_run(tmp_path, monkeypatch, capsys):
    from issue_agent.cli import preflight_labels

    config = load_config(write_config(tmp_path, dry_run=True))

    async def forbidden_run(command, **kwargs):
        raise AssertionError(f"dry_run must not call gh: {command}")

    monkeypatch.setattr("issue_agent.github.run", forbidden_run)
    assert asyncio.run(preflight_labels(config)) == 0
    assert capsys.readouterr().err == ""


def test_preflight_labels_warns_and_exits_when_gh_fails(tmp_path, monkeypatch, capsys):
    from issue_agent.cli import preflight_labels

    config = load_config(write_config(tmp_path))

    async def failing_run(command, **kwargs):
        raise CommandError("gh: HTTP 401: bad credentials")

    monkeypatch.setattr("issue_agent.github.run", failing_run)
    assert asyncio.run(preflight_labels(config)) == 1
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "401" in err


def test_preflight_labels_warns_when_label_payload_is_malformed(tmp_path, monkeypatch, capsys):
    from issue_agent.cli import preflight_labels

    config = load_config(write_config(tmp_path))
    monkeypatch.setattr("issue_agent.github.run", fake_label_run('[{"id":1}]', []))
    assert asyncio.run(preflight_labels(config)) == 1
    err = capsys.readouterr().err
    assert "warning" in err.lower()


def test_non_startup_commands_skip_label_preflight(tmp_path, monkeypatch):
    from issue_agent import cli

    config_file = write_config(tmp_path)
    monkeypatch.setattr("issue_agent.cli.load_config", lambda path: load_config(config_file))

    async def forbidden_preflight(config):
        raise AssertionError("status/report/reset must not run the label preflight")

    monkeypatch.setattr(cli, "preflight_labels", forbidden_preflight)

    base = {"config": str(config_file), "verbose": False}
    status_args = Namespace(command="status", active=False, json=False, **base)
    assert asyncio.run(cli.async_main(status_args)) == 0
    report_args = Namespace(command="report", issue=None, json=False, **base)
    assert asyncio.run(cli.async_main(report_args)) == 0
    # no task row for #99 -> reset exits 1, but only after skipping the preflight
    reset_args = Namespace(command="reset", issue=99, no_label=True, **base)
    assert asyncio.run(cli.async_main(reset_args)) == 1


def test_async_main_aborts_before_orchestrator_when_preflight_fails(tmp_path, monkeypatch):
    from issue_agent import cli

    config_file = write_config(tmp_path)
    monkeypatch.setattr("issue_agent.cli.load_config", lambda path: load_config(config_file))

    async def failing_preflight(config):
        return 1

    monkeypatch.setattr(cli, "preflight_labels", failing_preflight)

    def forbidden_orchestrator(config):
        raise AssertionError("orchestrator must not start when the label preflight fails")

    monkeypatch.setattr(cli, "Orchestrator", forbidden_orchestrator)
    args = Namespace(command="serve", config=str(config_file), verbose=False)
    assert asyncio.run(cli.async_main(args)) == 1
