import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from issue_agent.checks import capture_baseline, run_checks, summarize_output
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
[agents.codex]
command = "codex exec -"
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


def test_concurrent_baseline_requests_share_one_execution(tmp_path, monkeypatch):
    app = _app(tmp_path, ["pytest"])
    app._baseline_cache = {}
    app._baseline_inflight = {}
    app.workspaces.head_commit = lambda workspace: asyncio.sleep(0, result="abc123")
    calls = 0

    async def fake_capture(workspace, checks, *, timeout, parallel):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {}

    monkeypatch.setattr("issue_agent.orchestrator.capture_baseline", fake_capture)

    async def run_both():
        return await asyncio.gather(
            app._capture_baseline(tmp_path),
            app._capture_baseline(tmp_path),
        )

    assert asyncio.run(run_both()) == [{}, {}]
    assert calls == 1


def test_baseline_cache_is_bounded(tmp_path, monkeypatch):
    app = _app(tmp_path, ["pytest"])
    app._baseline_cache = {}
    app._baseline_inflight = {}
    object.__setattr__(app.config, "baseline_cache_max_entries", 2)
    app.workspaces.head_commit = AsyncMock(side_effect=("one", "two", "three"))

    async def fake_capture(workspace, checks, *, timeout, parallel):
        return {}

    monkeypatch.setattr("issue_agent.orchestrator.capture_baseline", fake_capture)

    async def populate():
        await app._capture_baseline(tmp_path)
        await app._capture_baseline(tmp_path)
        await app._capture_baseline(tmp_path)

    asyncio.run(populate())

    assert len(app._baseline_cache) == 2
    assert ("one", ("pytest",)) not in app._baseline_cache


def test_empty_task_commands_skip_intermediate_checks_but_not_final(tmp_path, monkeypatch):
    app = _app(tmp_path, ["pytest"])
    app.config = type("ConfigView", (), {**vars(app.config), "task_checks": ()})()
    calls = []

    async def fake_run_checks(*args, **kwargs):
        calls.append(kwargs["stage"])

    monkeypatch.setattr("issue_agent.orchestrator.run_checks", fake_run_checks)
    log = _Log()

    asyncio.run(app._run_checks(tmp_path, log, {}, stage="task"))
    asyncio.run(app._run_checks(tmp_path, log, {}, stage="final"))

    assert calls == ["final"]
    assert ("task_checks_skipped", {"sequence": None, "attempt": None}) in log.events

def test_unchanged_failure_tolerates_shifted_line_numbers(tmp_path, monkeypatch):
    """A lint failure at a new line/column after legitimate edits is the same
    pre-existing violation, not a regression."""
    commands = ("ruff check .",)
    outputs = iter([
        (1, "src/app.py:12:1: E501 line too long (95 > 88)\nFound 1 error.\n"),   # baseline
        (1, "src/app.py:41:1: E501 line too long (95 > 88)\nFound 1 error.\n"),   # same violation, new line
    ])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        code, text = next(outputs)
        return Result(code, text, "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    baseline = asyncio.run(capture_baseline(tmp_path, commands, timeout=10, parallel=False))
    log = _Log()
    asyncio.run(run_checks(tmp_path, log, baseline, checks=commands, timeout=10, parallel=False))

    names = [name for name, _ in log.events]
    assert "check_passed_pre_existing" in names


def test_unchanged_failure_still_flags_a_genuinely_new_violation(tmp_path, monkeypatch):
    commands = ("ruff check .",)
    outputs = iter([
        (1, "src/app.py:12:1: E501 line too long (95 > 88)\n"),
        (1, "src/app.py:12:1: E501 line too long (95 > 88)\nsrc/other.py:3:1: F401 unused import\n"),
    ])

    async def fake_shell(command, *, cwd, timeout=3600, check=True):
        code, text = next(outputs)
        return Result(code, text, "")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    baseline = asyncio.run(capture_baseline(tmp_path, commands, timeout=10, parallel=False))

    with pytest.raises(CommandError, match="command failed"):
        asyncio.run(
            run_checks(tmp_path, _Log(), baseline, checks=commands, timeout=10, parallel=False)
        )

