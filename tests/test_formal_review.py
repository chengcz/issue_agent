"""Tests for the deterministic formal-review module."""

from pathlib import Path

import pytest

from issue_agent.formal_review import FormalReviewResult, formal_review


def _make_workspace(tmp_path: Path, diff: str) -> Path:
    """Create a minimal workspace with a git repo whose last-commit diff is `diff`."""
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "initial.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Now create a second commit whose diff is what we want to review
    (tmp_path / "initial.txt").write_text(diff)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "task"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    return tmp_path


class TestFormalReviewSecrets:
    def test_detects_aws_access_key(self, tmp_path):
        ws = _make_workspace(tmp_path, "aws_key = AKIAIOSFODNN7EXAMPLE\n")
        result = formal_review(ws)
        assert result.approved is False
        assert "AWS access key" in result.reason

    def test_detects_generic_api_key_assignment(self, tmp_path):
        ws = _make_workspace(tmp_path, 'API_KEY = "sk-proj-abc123def456"\n')
        result = formal_review(ws)
        assert result.approved is False

    def test_detects_private_key_block(self, tmp_path):
        ws = _make_workspace(tmp_path, "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n")
        result = formal_review(ws)
        assert result.approved is False

    def test_passes_clean_diff(self, tmp_path):
        ws = _make_workspace(tmp_path, "def add(a, b):\n    return a + b\n")
        result = formal_review(ws)
        assert result.approved is True
        assert result.reason == ""


class TestFormalReviewForbiddenFiles:
    def test_detects_env_file_change(self, tmp_path):
        import subprocess

        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        # second commit touches .env
        (tmp_path / ".env").write_text("SECRET=abc\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "task"], cwd=tmp_path, check=True, capture_output=True)

        result = formal_review(tmp_path)
        assert result.approved is False
        assert ".env" in result.reason

    def test_allows_normal_source_files(self, tmp_path):
        ws = _make_workspace(tmp_path, "# new feature\ndef foo(): pass\n")
        result = formal_review(ws)
        assert result.approved is True


class TestFormalReviewEmptyDiff:
    def test_empty_diff_is_rejected(self, tmp_path):
        import subprocess

        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "a.txt").write_text("same\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        # second commit with no actual change
        subprocess.run(["git", "commit", "--allow-empty", "-m", "task"], cwd=tmp_path, check=True, capture_output=True)

        result = formal_review(tmp_path)
        assert result.approved is False
        assert "empty" in result.reason.lower() or "no change" in result.reason.lower()


class TestFormalReviewResult:
    def test_approved_result(self):
        r = FormalReviewResult(approved=True, reason="")
        assert r.approved
        assert r.reason == ""

    def test_rejected_result(self):
        r = FormalReviewResult(approved=False, reason="found secret")
        assert not r.approved
        assert "secret" in r.reason


class TestGitErrorHandling:
    def test_git_failure_raises_runtime_error(self, tmp_path):
        """_git() must raise RuntimeError with stderr when git command fails."""
        from issue_agent.formal_review import _git

        # tmp_path is not a git repo — git diff will fail
        with pytest.raises(RuntimeError, match="git diff.*failed"):
            _git(tmp_path, "diff", "--name-only", "HEAD^", "HEAD")

    def test_formal_review_propagates_git_error(self, tmp_path):
        """formal_review() on a non-git directory raises RuntimeError, not silent empty."""
        with pytest.raises(RuntimeError, match="git.*failed"):
            formal_review(tmp_path)
