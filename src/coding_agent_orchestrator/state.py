from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import Issue, TaskStatus


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                issue_number INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                agent TEXT, branch TEXT, worktree TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, pr_url TEXT, updated_at TEXT NOT NULL
            )""")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def claim(self, issue: Issue, agent: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            row = db.execute("SELECT status FROM tasks WHERE issue_number=?", (issue.number,)).fetchone()
            if row and row["status"] not in (TaskStatus.FAILED, TaskStatus.PENDING):
                return False
            db.execute(
                """INSERT INTO tasks(issue_number,title,status,agent,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(issue_number) DO UPDATE SET
                status=excluded.status,agent=excluded.agent,updated_at=excluded.updated_at""",
                (issue.number, issue.title, TaskStatus.CLAIMED, agent, now),
            )
        return True

    def update(self, issue_number: int, status: TaskStatus, **values: object) -> None:
        allowed = {"agent", "branch", "worktree", "attempts", "last_error", "pr_url"}
        fields = {key: value for key, value in values.items() if key in allowed}
        fields.update(status=str(status), updated_at=datetime.now(UTC).isoformat())
        sql = ",".join(f"{key}=?" for key in fields)
        with self.connect() as db:
            db.execute(f"UPDATE tasks SET {sql} WHERE issue_number=?", (*fields.values(), issue_number))

    def rows(self) -> list[dict[str, object]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY updated_at DESC")]

    def recover_interrupted(self) -> int:
        """Make work interrupted by a process restart claimable again."""
        active = tuple(
            str(status)
            for status in (
                TaskStatus.CLAIMED,
                TaskStatus.CODING,
                TaskStatus.TESTING,
                TaskStatus.REVIEWING,
                TaskStatus.PUSHING,
            )
        )
        placeholders = ",".join("?" for _ in active)
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE tasks SET status=?, last_error=?, updated_at=? "
                f"WHERE status IN ({placeholders})",
                (
                    str(TaskStatus.FAILED),
                    "orchestrator restarted while task was active",
                    datetime.now(UTC).isoformat(),
                    *active,
                ),
            )
            return cursor.rowcount
