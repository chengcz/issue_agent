from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import Issue, PlanTask, TaskStatus

_ACTIVE = (
    TaskStatus.CLAIMED,
    TaskStatus.CODING,
    TaskStatus.TESTING,
    TaskStatus.REVIEWING,
    TaskStatus.PUSHING,
)

RUNNING_STATUSES = (
    TaskStatus.CLAIMED,
    TaskStatus.PLANNING,
    TaskStatus.PLANNED,
    TaskStatus.CODING,
    TaskStatus.TESTING,
    TaskStatus.REVIEWING,
    TaskStatus.PUSHING,
)


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                issue_number INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                agent TEXT, branch TEXT, worktree TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, pr_url TEXT, plan TEXT,
                current_seq INTEGER NOT NULL DEFAULT -1, updated_at TEXT NOT NULL
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "plan" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN plan TEXT")
            if "current_seq" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN current_seq INTEGER NOT NULL DEFAULT -1")
            if "failures" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN failures INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE IF NOT EXISTS plan_tasks (
                issue_number INTEGER NOT NULL, seq INTEGER NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, commit_hash TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY (issue_number, seq)
            )""")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def claim(self, issue: Issue, agent: str, max_attempts: int = 3) -> bool:
        """Transition an issue to CLAIMED when it can be worked.

        Fresh issues and issues in PENDING/PLANNED are always re-claimable.
        FAILED and BLOCKED issues are re-claimed only while their failure budget
        lasts (``failures < max_attempts``); past that they are parked and need
        a human reset. Resources renamed: ``failures`` is the whole-issue retry
        counter; ``attempts`` stays the in-cycle attempt marker written by the
        task loop, so the two counters stay independent.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            row = db.execute(
                "SELECT status, failures FROM tasks WHERE issue_number=?", (issue.number,)
            ).fetchone()
            if row:
                status = row["status"]
                if status in (str(TaskStatus.PENDING), str(TaskStatus.PLANNED)):
                    pass
                elif status in (str(TaskStatus.FAILED), str(TaskStatus.BLOCKED)):
                    if int(row["failures"]) >= max_attempts:
                        return False
                else:
                    return False
            db.execute(
                """INSERT INTO tasks(issue_number,title,status,agent,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(issue_number) DO UPDATE SET
                status=excluded.status,agent=excluded.agent,updated_at=excluded.updated_at""",
                (issue.number, issue.title, TaskStatus.CLAIMED, agent, now),
            )
        return True

    def record_failure(self, issue_number: int, status: TaskStatus, last_error: str) -> int:
        """Record a whole-issue failure, incrementing the retry-budget counter.

        Returns the new failure count so callers can decide whether the issue is
        still re-claimable (``failures < max_attempts``) or parked.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            row = db.execute("SELECT failures FROM tasks WHERE issue_number=?", (issue_number,)).fetchone()
            failures = int(row["failures"]) + 1 if row else 1
            db.execute(
                "UPDATE tasks SET status=?, failures=?, last_error=?, updated_at=? WHERE issue_number=?",
                (str(status), failures, last_error, now, issue_number),
            )
        return failures

    def update(self, issue_number: int, status: TaskStatus, **values: object) -> None:
        allowed = {"agent", "branch", "worktree", "attempts", "last_error", "pr_url", "current_seq"}
        fields = {key: value for key, value in values.items() if key in allowed}
        fields.update(status=str(status), updated_at=datetime.now(UTC).isoformat())
        sql = ",".join(f"{key}=?" for key in fields)
        with self.connect() as db:
            db.execute(f"UPDATE tasks SET {sql} WHERE issue_number=?", (*fields.values(), issue_number))

    def save_plan(self, issue_number: int, plan: list[PlanTask]) -> None:
        now = datetime.now(UTC).isoformat()
        payload = json.dumps([task.to_dict() for task in plan], ensure_ascii=False)
        with self.connect() as db:
            db.execute("UPDATE tasks SET plan=?, updated_at=? WHERE issue_number=?", (payload, now, issue_number))
            db.executemany(
                """INSERT INTO plan_tasks(issue_number,seq,title,description,status,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(issue_number,seq) DO UPDATE SET
                title=excluded.title,description=excluded.description,updated_at=excluded.updated_at""",
                [(issue_number, i, task.title, task.description, str(TaskStatus.PENDING), now)
                 for i, task in enumerate(plan)],
            )

    def load_plan(self, issue_number: int) -> list[PlanTask] | None:
        with self.connect() as db:
            row = db.execute("SELECT plan FROM tasks WHERE issue_number=?", (issue_number,)).fetchone()
        if not row or not row["plan"]:
            return None
        items = json.loads(row["plan"])
        return [PlanTask.from_dict(item) for item in items]

    def plan_task_statuses(self, issue_number: int) -> list[TaskStatus]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT seq,status FROM plan_tasks WHERE issue_number=? ORDER BY seq", (issue_number,)
            ).fetchall()
        return [TaskStatus(row["status"]) for row in rows]

    def update_plan_task(self, issue_number: int, seq: int, **values: object) -> None:
        fields = {
            key: value for key, value in values.items() if key in {"status", "attempts", "last_error", "commit_hash"}
        }
        if "status" in fields:
            fields["status"] = str(fields["status"])
        fields["updated_at"] = datetime.now(UTC).isoformat()
        sql = ",".join(f"{key}=?" for key in fields)
        with self.connect() as db:
            db.execute(
                f"UPDATE plan_tasks SET {sql} WHERE issue_number=? AND seq=?",
                (*fields.values(), issue_number, seq),
            )

    def plan_task_commit(self, issue_number: int, seq: int) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT commit_hash FROM plan_tasks WHERE issue_number=? AND seq=?", (issue_number, seq)
            ).fetchone()
        return row["commit_hash"] if row else None

    def rows(self) -> list[dict[str, object]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY updated_at DESC")]

    def status_rows(self, *, active_only: bool = False) -> list[dict[str, object]]:
        """Return task status with the current plan item joined for CLI display."""
        sql = """SELECT tasks.issue_number, tasks.title, tasks.status, tasks.agent,
            tasks.branch, tasks.attempts, tasks.failures, tasks.current_seq,
            plan_tasks.title AS current_task, tasks.last_error, tasks.pr_url,
            tasks.updated_at
            FROM tasks
            LEFT JOIN plan_tasks ON plan_tasks.issue_number = tasks.issue_number
                AND plan_tasks.seq = tasks.current_seq"""
        parameters: tuple[str, ...] = ()
        if active_only:
            placeholders = ",".join("?" for _ in RUNNING_STATUSES)
            sql += f" WHERE tasks.status IN ({placeholders})"
            parameters = tuple(str(status) for status in RUNNING_STATUSES)
        sql += " ORDER BY tasks.updated_at DESC"
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, parameters)]

    def recover_interrupted(self) -> int:
        """Make work interrupted by a process restart claimable again."""
        now = datetime.now(UTC).isoformat()
        placeholders = ",".join("?" for _ in _ACTIVE)
        active = [str(status) for status in _ACTIVE]
        with self.connect() as db:
            planning = db.execute(
                "UPDATE tasks SET status=?, last_error=?, updated_at=? WHERE status=?",
                (str(TaskStatus.PENDING), "orchestrator restarted during planning", now, str(TaskStatus.PLANNING)),
            )
            resumed = db.execute(
                f"UPDATE tasks SET status=?, last_error=?, updated_at=? "
                f"WHERE status IN ({placeholders}) AND plan IS NOT NULL AND plan != ''",
                (str(TaskStatus.PLANNED), "orchestrator restarted while task was active", now, *active),
            )
            failed = db.execute(
                f"UPDATE tasks SET status=?, last_error=?, updated_at=? "
                f"WHERE status IN ({placeholders}) AND (plan IS NULL OR plan = '')",
                (str(TaskStatus.FAILED), "orchestrator restarted while task was active", now, *active),
            )
            db.execute(
                f"UPDATE plan_tasks SET status=?, last_error=?, updated_at=? WHERE status IN ({placeholders})",
                (str(TaskStatus.PENDING), "orchestrator restarted", now, *active),
            )
            return planning.rowcount + resumed.rowcount + failed.rowcount
