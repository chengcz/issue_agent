"""Standalone {prompt}/{session_id} placeholders must not become flag-like argv elements.

Issue bodies (untrusted) flow into prompts and agent stdout flows into session ids;
a value starting with "-" substituted as its own argv element could be parsed as a
flag by the target CLI. Embedded forms ("--prompt={prompt}") stay allowed: the
rendered element carries its own flag prefix and cannot be read as a bare flag.
"""

import asyncio

import pytest

from issue_agent.agents import CliAgent
from issue_agent.config import AgentConfig
from issue_agent.process import CommandError, Result


def recording_run(calls: list):
    async def fake_run(command, **kwargs):
        calls.append(command)
        return Result(0, "ok", "")

    return fake_run


def test_standalone_prompt_placeholder_rejects_dash_leading_value(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr("issue_agent.agents.run", recording_run(calls))
    agent = CliAgent("worker", AgentConfig(command=("my-agent", "{prompt}")))
    with pytest.raises(CommandError, match="standalone"):
        asyncio.run(agent.execute(tmp_path, "--dangerously-skip-permissions do it"))
    assert calls == []


def test_standalone_session_placeholder_rejects_dash_leading_value(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr("issue_agent.agents.run", recording_run(calls))
    agent = CliAgent(
        "worker",
        AgentConfig(command=("my-agent",), resume_command=("my-agent", "resume", "{session_id}")),
    )
    with pytest.raises(CommandError, match="standalone"):
        asyncio.run(agent.execute(tmp_path, "hi", session_id="--evil"))
    assert calls == []


def test_embedded_placeholder_allows_dash_leading_value(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr("issue_agent.agents.run", recording_run(calls))
    agent = CliAgent("worker", AgentConfig(command=("my-agent", "--prompt={prompt}")))
    asyncio.run(agent.execute(tmp_path, "-evil but harmless here"))
    assert calls[0][-1] == "--prompt=-evil but harmless here"


def test_standalone_placeholder_allows_normal_prompt(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr("issue_agent.agents.run", recording_run(calls))
    agent = CliAgent("worker", AgentConfig(command=("my-agent", "{prompt}")))
    asyncio.run(agent.execute(tmp_path, "You are the coding worker..."))
    assert calls[0][-1] == "You are the coding worker..."
