"""Config discovery: the `-c` alias, the default name, and the upward search."""

import asyncio
from pathlib import Path

from issue_agent.cli import async_main, parser
from issue_agent.config import find_config

CONFIG_BODY = """\
[runtime]
repo = "."
state_db = "state.db"
log_dir = "logs"
dry_run = true
[github]
repo = "a/b"
[agents.codex]
command = "codex exec -"
"""


def write_config(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    config_file = directory / "issue-agent.toml"
    config_file.write_text(CONFIG_BODY)
    return config_file


def run_status(*argv: str) -> int:
    return asyncio.run(async_main(parser().parse_args([*argv, "status"])))


def test_find_config_prefers_the_nearest_file(tmp_path):
    nearest = write_config(tmp_path / "project")

    write_config(tmp_path)  # a farther one must not win

    assert find_config(tmp_path / "project") == nearest


def test_find_config_walks_up_two_levels(tmp_path):
    target = write_config(tmp_path)
    start = tmp_path / "a" / "b"
    start.mkdir(parents=True)

    assert find_config(start) == target


def test_find_config_stops_at_the_third_level(tmp_path):
    write_config(tmp_path)  # one level too far above `start` to be reached
    start = tmp_path / "a" / "b" / "c"
    start.mkdir(parents=True)

    assert find_config(start) is None


def test_config_flag_is_spelled_c_on_either_side_of_the_subcommand():
    assert parser().parse_args(["-c", "x.toml", "status"]).config == "x.toml"
    assert parser().parse_args(["status", "-c", "x.toml"]).config == "x.toml"


def test_config_defaults_to_discovery_rather_than_a_literal_path():
    assert parser().parse_args(["status"]).config is None


def test_startup_discovers_the_config_two_directories_up(tmp_path, monkeypatch):
    write_config(tmp_path)
    nested = tmp_path / "nested" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert run_status() == 0


def test_startup_names_every_directory_it_searched(tmp_path, monkeypatch, capsys):
    nested = tmp_path / "nested" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert run_status() == 2

    err = capsys.readouterr().err
    assert "issue-agent.toml" in err
    searched = Path.cwd()
    for directory in (searched, searched.parent, searched.parent.parent):
        assert str(directory) in err


def test_an_explicit_config_is_used_as_given_and_never_searched(tmp_path, monkeypatch, capsys):
    """A path the user typed is an answer, not a hint: a missing one must fail
    rather than silently run against some other issue-agent.toml found above."""
    write_config(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)

    assert run_status("-c", "nope.toml") == 2

    err = capsys.readouterr().err
    assert "nope.toml" in err
    assert "config file not found" in err
