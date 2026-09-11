"""A commit with nothing to commit is a no-op, not a failure.

Issue #36: the final fixer deleted tracked ``.pyc`` artifacts, staging their
deletion, and the check run that followed regenerated them byte-identically.
``git status`` — which the orchestrator used to decide whether a commit was
needed — still showed the staged deletion, but ``git add --all`` put the index
straight back on HEAD. ``git commit`` then exited 1 with "nothing to commit,
working tree clean", and an issue whose checks had just passed was marked
failed at the very last step before its PR.
"""

import asyncio
import subprocess
from pathlib import Path

from issue_agent.workspace import WorkspaceManager


def git(tmp_path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_worktree(tmp_path: Path) -> WorkspaceManager:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "plot.pyc").write_text("X\n", encoding="utf-8")
    git(tmp_path, "add", "-A", ".")
    git(tmp_path, "commit", "-qm", "one")
    return WorkspaceManager(tmp_path, tmp_path / "worktrees", "main")


def test_commit_reports_nothing_to_do_when_a_check_regenerated_the_file(tmp_path):
    manager = make_worktree(tmp_path)
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "rm", "-q", "plot.pyc")  # the fixer deletes a tracked artifact
    (tmp_path / "plot.pyc").write_text("X\n", encoding="utf-8")  # a check regenerates it

    # The guard the orchestrator used says there is something to do ...
    assert asyncio.run(manager.changed(tmp_path)) is True

    # ... but the commit is the one that decides, and there is nothing to record.
    assert asyncio.run(manager.commit(tmp_path, "feat: final review fixes (#36)")) is False
    assert git(tmp_path, "rev-parse", "HEAD") == head


def test_commit_reports_nothing_to_do_on_a_clean_tree(tmp_path):
    manager = make_worktree(tmp_path)
    head = git(tmp_path, "rev-parse", "HEAD")

    assert asyncio.run(manager.commit(tmp_path, "feat: final review fixes (#36)")) is False

    assert git(tmp_path, "rev-parse", "HEAD") == head


def test_commit_reports_the_commit_it_created(tmp_path):
    manager = make_worktree(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")

    assert asyncio.run(manager.commit(tmp_path, "feat: add main (#36)")) is True

    assert git(tmp_path, "log", "-1", "--format=%s") == "feat: add main (#36)"
