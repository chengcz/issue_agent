import asyncio
import json
from pathlib import Path

import pytest

from issue_agent.checks import summarize_output
from issue_agent.config import load_config
from issue_agent.orchestrator import Orchestrator
from issue_agent.process import CommandError, Result


class _Log:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


def _app(tmp_path: Path, checks: list[str], *, parallel: bool = True) -> Orchestrator:
    config_file = tmp_path / "issue-agent.toml"
    config_file.write_text(
        f"""
[runtime]
repo = "."
state_db = "state.db"
log_dir = "logs"
dry_run = true
[github]
repo = "a/b"
[checks]
commands = {json.dumps(checks)}
parallel = {str(parallel).lower()}
"""
    )
    app = Orchestrator(load_config(config_file))
    app._baseline_cache = None
    return app


def test_summarize_output_keeps_new_pytest_failure_blocks():
    output = (
        "collected 100 items\n"
        "tests/test_a.py . [100%]\n"
        "=================================== FAILURES ===================================\n"
        "___________________________________ test_old ___________________________________\n"
        "assert 1 == 2\n"
        "FAILED tests/test_old.py::test_old - AssertionError\n"
        "FAILED tests/test_new.py::test_new - ValueError: boom\n"
        "2 failed in 3.0s\n"
    )
    summary = summarize_output(output, {"tests/test_new.py::test_new"})
    assert "test_new" in summary
    assert "ValueError: boom" in summary
    assert len(summary) <= 2000


def test_summarize_output_filters_error_lines_with_context():
    lines = [f"info line {i}" for i in range(200)]
    lines[100] = "ValueError: bad thing happened"
    summary = summarize_output("\n".join(lines), set())
    assert "ValueError: bad thing happened" in summary
    assert "info line 99" in summary
    assert "info line 103" in summary
    assert "info line 50" not in summary
    assert len(summary) <= 2000


def test_summarize_output_falls_back_to_tail_when_too_long():
    output = "\n".join(f"Error {i}: " + "x" * 100 for i in range(100))
    summary = summarize_output(output, set())
    assert summary == output[-2000:]


def test_summarize_output_handles_empty_output():
    assert summarize_output("", set()) == ""
    assert summarize_output("   \n", set()) == ""


def test_checks_run_in_parallel_by_default(tmp_path, monkeypatch):
    app = _app(tmp_path, ["sleep-a", "sleep-b"])
    active = 0
    max_active = 0

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return Result(0, "", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    baseline = asyncio.run(app._capture_baseline(tmp_path))
    assert baseline == {}
    assert max_active == 2


def test_checks_run_serially_when_parallel_disabled(tmp_path, monkeypatch):
    app = _app(tmp_path, ["sleep-a", "sleep-b"], parallel=False)
    active = 0
    max_active = 0

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return Result(0, "", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    baseline = asyncio.run(app._capture_baseline(tmp_path))
    assert baseline == {}
    assert max_active == 1


def test_parallel_failures_are_reported_in_configured_order(tmp_path, monkeypatch):
    app = _app(tmp_path, ["cmd-first", "cmd-second"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        # the first configured command finishes last; the error must still name it
        await asyncio.sleep(0.02 if command == "cmd-first" else 0)
        return Result(1, f"{command} crashed", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    with pytest.raises(CommandError, match="cmd-first"):
        asyncio.run(app._run_checks(tmp_path, _Log(), {}))


def test_parallel_timeout_propagates(tmp_path, monkeypatch):
    app = _app(tmp_path, ["a", "b"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        if command == "b":
            raise CommandError("command timed out after 1s: sh")
        return Result(0, "", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    with pytest.raises(CommandError, match="timed out"):
        asyncio.run(app._capture_baseline(tmp_path))


def test_failed_check_writes_full_output_file_and_points_to_it(tmp_path, monkeypatch):
    app = _app(tmp_path, ["pytest"])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        return Result(1, "FAILED tests/test_x.py::test_y - boom\n", "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    with pytest.raises(CommandError) as exc:
        asyncio.run(app._run_checks(tmp_path, _Log(), {}))
    full = (tmp_path / ".agent" / "check-output.txt").read_text(encoding="utf-8")
    assert "FAILED tests/test_x.py::test_y" in full
    assert ".agent/check-output.txt" in str(exc.value)
