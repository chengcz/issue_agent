"""Secret redaction at the GitHub boundary: comments and PR titles never echo secrets."""

import asyncio

from issue_agent.formal_review import formal_review, redact_secrets
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

def test_redaction_covers_unquoted_assignments():
    """Unquoted password/token assignments used to sail through: the old
    patterns required quotes, so `password = hunter2secret` reached GitHub
    verbatim. Accessor calls and type annotations stay quiet."""
    text = "password = hunter2secret\napi_key = ab12cd34ef56\ntoken: gfx9012345678\n"
    redacted = redact_secrets(text)
    assert "hunter2secret" not in redacted
    assert "ab12cd34ef56" not in redacted
    assert "gfx9012345678" not in redacted
    # Calls and annotations are not secrets.
    quiet = "password = getpass()\npassword: str = field()\n"
    assert redact_secrets(quiet) == quiet


def test_formal_review_flags_key_files_case_insensitively(tmp_path):
    """`.ENV` and `.Pem` are the same hazards as their lowercase names; the
    key-file patterns (id_rsa, .pem, .key) stay covered here too."""
    import subprocess as sp

    def commit(path: str, content: str) -> None:
        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        sp.run(["git", "add", path], cwd=tmp_path, check=True)
        sp.run(["git", "commit", "-qm", path], cwd=tmp_path, check=True)

    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    # formal_review diffs HEAD^..HEAD, so a base commit must exist first.
    (tmp_path / "base.txt").write_text("base\n")
    sp.run(["git", "add", "base.txt"], cwd=tmp_path, check=True)
    sp.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    commit(".ENV", "SECRET=1\n")
    result = formal_review(tmp_path)
    assert not result.approved and "Forbidden" in result.reason

    commit("server.pem", "-----BEGIN CERTIFICATE-----\n")
    result = formal_review(tmp_path)
    assert not result.approved and "Forbidden" in result.reason

    commit("id_rsa", "private\n")
    result = formal_review(tmp_path)
    assert not result.approved and "Forbidden" in result.reason

    commit("ok.txt", "nothing here\n")
    result = formal_review(tmp_path)
    assert result.approved
