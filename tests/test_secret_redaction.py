"""Secret redaction at the GitHub boundary: comments and PR titles never echo secrets."""

import asyncio

from issue_agent.formal_review import redact_secrets
from issue_agent.github import GitHub
from issue_agent.process import Result

AWS_KEY = "AKIAABCDEFGHIJKLMNOP"
GHP_TOKEN = "ghp_" + "a" * 36


def test_redact_secrets_replaces_matches_with_markers():
    text = f"failed with key {AWS_KEY} and token {GHP_TOKEN}"
    out = redact_secrets(text)
    assert AWS_KEY not in out
    assert GHP_TOKEN not in out
    assert "[REDACTED AWS access key ID]" in out
    assert "[REDACTED GitHub personal access token]" in out


def test_redact_secrets_leaves_clean_text_unchanged():
    assert redact_secrets("plain failure output\nline 2") == "plain failure output\nline 2"


def test_github_comment_redacts_secret_patterns(tmp_path, monkeypatch):
    calls: list = []

    async def fake_run(command, **kwargs):
        calls.append(command)
        return Result(0, "", "")

    monkeypatch.setattr("issue_agent.github.run", fake_run)
    github = GitHub("a/b", tmp_path)
    asyncio.run(github.comment(7, f"Agent run failed.\n\n```text\nkey={AWS_KEY}\n```"))
    body = calls[0][calls[0].index("--body") + 1]
    assert AWS_KEY not in body
    assert "[REDACTED AWS access key ID]" in body


def test_github_create_pr_redacts_title(tmp_path, monkeypatch):
    calls: list = []

    async def fake_run(command, **kwargs):
        calls.append(command)
        # first call: pr list (find_pr) -> empty; second: pr create
        return Result(0, "[]" if "list" in command else "https://pr", "")

    monkeypatch.setattr("issue_agent.github.run", fake_run)
    github = GitHub("a/b", tmp_path)
    url = asyncio.run(github.create_pr(7, "agent/7-x", "main", f"Fix {AWS_KEY} leak", ()))
    assert url == "https://pr"
    create_call = calls[-1]
    title = create_call[create_call.index("--title") + 1]
    assert AWS_KEY not in title
    assert "[REDACTED AWS access key ID]" in title
