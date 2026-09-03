"""Deterministic formal review: secrets scan, forbidden files, empty-diff check.

No LLM calls. Raises nothing — returns a :class:`FormalReviewResult` so the
caller can decide how to react (log, retry, or hard-fail).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Secret patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS secret access key", re.compile(r"(?i)aws_secret_access_key\s*=\s*\S{20,}")),
    ("Generic API key assignment", re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token)\b\s*[=:]\s*['\"][^'\"]{8,}['\"]")),
    ("Private key block", re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----")),
    ("GitHub personal access token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("Slack bot token", re.compile(r"\bxoxb-[0-9]{10,}-[A-Za-z0-9]+\b")),
    ("Stripe secret key", re.compile(r"\bsk_live_[A-Za-z0-9]{24,}\b")),
    ("OpenAI API key", re.compile(r"\bsk-proj-[A-Za-z0-9\-_]{20,}\b")),
    ("Hardcoded password assignment", re.compile(r"(?i)\bpassword\b\s*[=:]\s*['\"][^'\"]{6,}['\"]")),
]

# ---------------------------------------------------------------------------
# Forbidden file patterns (paths that should never appear in a task commit)
# ---------------------------------------------------------------------------

_FORBIDDEN_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"(^|/)credentials(\.|$)", re.IGNORECASE),
    re.compile(r"(^|/)secrets?\.(json|ya?ml|toml|env)$", re.IGNORECASE),
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"(^|/)id_rsa(\.|$)"),
    re.compile(r"(^|/)id_ed25519(\.|$)"),
]


@dataclass(frozen=True)
class FormalReviewResult:
    """Outcome of a deterministic formal review."""

    approved: bool
    reason: str


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _changed_files(workspace: Path) -> list[str]:
    """Return the list of files changed in the last commit."""
    out = _git(workspace, "diff", "--name-only", "HEAD^", "HEAD")
    return [line.strip() for line in out.splitlines() if line.strip()]


def _last_commit_diff(workspace: Path) -> str:
    """Return the unified diff of the last commit."""
    return _git(workspace, "diff", "HEAD^", "HEAD")


def _scan_secrets(diff: str) -> str | None:
    """Return a human-readable reason if a secret pattern is found in added lines."""
    added_lines = [
        line[1:] for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    for line in added_lines:
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                return f"Potential {label} detected in diff"
    return None


def _scan_forbidden_files(files: list[str]) -> str | None:
    """Return a reason if any changed file matches a forbidden pattern."""
    for path in files:
        for pattern in _FORBIDDEN_FILE_PATTERNS:
            if pattern.search(path):
                return f"Forbidden file modified: {path}"
    return None


def formal_review(workspace: Path) -> FormalReviewResult:
    """Run all deterministic formal checks on the last commit in *workspace*.

    Returns a :class:`FormalReviewResult`. The caller decides whether to
    raise, retry, or log.
    """
    files = _changed_files(workspace)

    # 1. Empty diff check
    if not files:
        return FormalReviewResult(
            approved=False,
            reason="Task commit is empty — no files changed.",
        )

    diff = _last_commit_diff(workspace)

    # 2. Secrets scan
    secret_reason = _scan_secrets(diff)
    if secret_reason:
        return FormalReviewResult(approved=False, reason=secret_reason)

    # 3. Forbidden files
    forbidden_reason = _scan_forbidden_files(files)
    if forbidden_reason:
        return FormalReviewResult(approved=False, reason=forbidden_reason)

    return FormalReviewResult(approved=True, reason="")
