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
                current_seq INTEGER NOT NULL DEFAULT -1, final_commit_hash TEXT,
                final_last_error TEXT,
                total_input_tokens INTEGER NOT NULL DEFAULT 0,
                total_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                total_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                total_reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                total_cost_usd REAL NOT NULL DEFAULT 0,
                total_duration_ms INTEGER NOT NULL DEFAULT 0,
                total_check_duration_ms INTEGER NOT NULL DEFAULT 0,
                total_queue_duration_ms INTEGER NOT NULL DEFAULT 0,
                total_wall_duration_ms INTEGER NOT NULL DEFAULT 0,
                started_at TEXT, finished_at TEXT,
                updated_at TEXT NOT NULL
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "plan" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN plan TEXT")
            if "current_seq" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN current_seq INTEGER NOT NULL DEFAULT -1")
            if "failures" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN failures INTEGER NOT NULL DEFAULT 0")
            if "final_commit_hash" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN final_commit_hash TEXT")
            if "final_last_error" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN final_last_error TEXT")
            if "total_input_tokens" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN total_input_tokens INTEGER NOT NULL DEFAULT 0")
            if "total_output_tokens" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN total_output_tokens INTEGER NOT NULL DEFAULT 0")
            if "total_cache_read_tokens" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN total_cache_read_tokens INTEGER NOT NULL DEFAULT 0")
            if "total_cache_creation_tokens" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN total_cache_creation_tokens INTEGER NOT NULL DEFAULT 0")
            if "total_cost_usd" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN total_cost_usd REAL NOT NULL DEFAULT 0")
            if "total_duration_ms" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN total_duration_ms INTEGER NOT NULL DEFAULT 0")
            for name, definition in (
                ("total_reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("total_check_duration_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("total_queue_duration_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("total_wall_duration_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS plan_tasks (
                issue_number INTEGER NOT NULL, seq INTEGER NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, commit_hash TEXT,
                total_input_tokens INTEGER NOT NULL DEFAULT 0,
                total_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                total_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                total_reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                total_cost_usd REAL NOT NULL DEFAULT 0,
                total_duration_ms INTEGER NOT NULL DEFAULT 0,
                total_check_duration_ms INTEGER NOT NULL DEFAULT 0,
                total_wall_duration_ms INTEGER NOT NULL DEFAULT 0,
                started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY (issue_number, seq)
            )""")
            plan_columns = {row["name"] for row in db.execute("PRAGMA table_info(plan_tasks)")}
            for name, definition in (
                ("total_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("total_output_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("total_cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("total_cache_creation_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("total_reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("total_cost_usd", "REAL NOT NULL DEFAULT 0"),
                ("total_duration_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("total_check_duration_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("total_wall_duration_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
            ):
                if name not in plan_columns:
                    db.execute(f"ALTER TABLE plan_tasks ADD COLUMN {name} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS issue_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_number INTEGER NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, queued_at TEXT, started_at TEXT NOT NULL,
                finished_at TEXT, queue_duration_ms INTEGER NOT NULL DEFAULT 0,
                wall_duration_ms INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_number INTEGER NOT NULL, run_id INTEGER,
                seq INTEGER, attempt INTEGER, agent TEXT NOT NULL, role TEXT NOT NULL,
                success INTEGER NOT NULL, duration_ms INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                session_id TEXT, error TEXT, created_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_sessions (
                issue_number INTEGER NOT NULL, agent TEXT NOT NULL, role TEXT NOT NULL,
                session_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(issue_number,agent,role)
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS issue_runs_by_issue ON issue_runs(issue_number,id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS agent_calls_by_issue ON agent_calls(issue_number,id)"
            )
            run_columns = {row["name"] for row in db.execute("PRAGMA table_info(issue_runs)")}
            for name, definition in (
                ("queued_at", "TEXT"),
                ("queue_duration_ms", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in run_columns:
                    db.execute(f"ALTER TABLE issue_runs ADD COLUMN {name} {definition}")

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

    def claim_for_planning(self, issue: Issue, agent: str, max_attempts: int = 3) -> bool:
        """Claim an unassigned Issue for planning exactly once unless planning was interrupted."""
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            row = db.execute(
                "SELECT status, failures, plan FROM tasks WHERE issue_number=?", (issue.number,)
            ).fetchone()
            if row:
                status = row["status"]
                if row["plan"]:
                    if status not in (str(TaskStatus.FAILED), str(TaskStatus.BLOCKED)):
                        return False
                    if int(row["failures"]) >= max_attempts:
                        return False
                    # Reclaim so a failed GitHub comment/label transition can
                    # republish the already-persisted plan without another LLM call.
                elif status == str(TaskStatus.PENDING):
                    pass
                elif status in (str(TaskStatus.FAILED), str(TaskStatus.BLOCKED)):
                    if int(row["failures"]) >= max_attempts:
                        return False
                else:
                    return False
            db.execute(
                """INSERT INTO tasks(issue_number,title,status,agent,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(issue_number) DO UPDATE SET
                title=excluded.title,status=excluded.status,agent=excluded.agent,
                updated_at=excluded.updated_at""",
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

    def plan_task_last_error(self, issue_number: int, seq: int) -> str:
        """Return the last recorded error for a plan task, so a retry can start informed."""
        with self.connect() as db:
            row = db.execute(
                "SELECT last_error FROM plan_tasks WHERE issue_number=? AND seq=?", (issue_number, seq)
            ).fetchone()
        return (row["last_error"] if row else None) or ""

    def final_context(self, issue_number: int) -> tuple[str | None, str]:
        """Return the last verified final-fix commit and final-stage error."""
        with self.connect() as db:
            row = db.execute(
                "SELECT final_commit_hash,final_last_error FROM tasks WHERE issue_number=?",
                (issue_number,),
            ).fetchone()
        if not row:
            return None, ""
        return row["final_commit_hash"] or None, row["final_last_error"] or ""

    def update_final_context(
        self,
        issue_number: int,
        *,
        commit_hash: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """Persist final-stage recovery data without changing the task status."""
        fields: dict[str, object] = {"updated_at": datetime.now(UTC).isoformat()}
        if commit_hash is not None:
            fields["final_commit_hash"] = commit_hash
        if last_error is not None:
            fields["final_last_error"] = last_error
        sql = ",".join(f"{key}=?" for key in fields)
        with self.connect() as db:
            db.execute(
                f"UPDATE tasks SET {sql} WHERE issue_number=?",
                (*fields.values(), issue_number),
            )

    def accumulate_usage(
        self, issue_number: int, usage: dict[str, object] | None, *, duration_ms: int | None
    ) -> None:
        """Add one agent call's token/cost/duration to the issue's cumulative totals.

        Missing keys count as zero; unknown issues are silently ignored so a
        stray call never breaks the execution loop.
        """
        usage = usage or {}

        def _int(key: str) -> int:
            value = usage.get(key)
            return int(value) if isinstance(value, (int, float)) else 0

        def _float(key: str) -> float:
            value = usage.get(key)
            return float(value) if isinstance(value, (int, float)) else 0.0

        cost = _float("total_cost_usd") or _float("cost_usd")
        with self.connect() as db:
            db.execute(
                """UPDATE tasks SET
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read_tokens = total_cache_read_tokens + ?,
                    total_cache_creation_tokens = total_cache_creation_tokens + ?,
                    total_reasoning_tokens = total_reasoning_tokens + ?,
                    total_cost_usd = total_cost_usd + ?,
                    total_duration_ms = total_duration_ms + ?
                WHERE issue_number=?""",
                (
                    _int("input_tokens"),
                    _int("output_tokens"),
                    _int("cache_read_input_tokens"),
                    _int("cache_creation_input_tokens"),
                    _int("reasoning_output_tokens"),
                    cost,
                    int(duration_ms) if duration_ms else 0,
                    issue_number,
                ),
            )

    @staticmethod
    def _usage_values(usage: dict[str, object] | None) -> tuple[int, int, int, int, int, float]:
        usage = usage or {}

        def integer(key: str) -> int:
            value = usage.get(key)
            return int(value) if isinstance(value, (int, float)) else 0

        cost_value = usage.get("total_cost_usd") or usage.get("cost_usd") or 0
        cost = float(cost_value) if isinstance(cost_value, (int, float)) else 0.0
        return (
            integer("input_tokens"),
            integer("output_tokens"),
            integer("cache_read_input_tokens"),
            integer("cache_creation_input_tokens"),
            integer("reasoning_output_tokens"),
            cost,
        )

    def start_run(self, issue_number: int, kind: str) -> int:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self.connect() as db:
            task = db.execute(
                "SELECT updated_at FROM tasks WHERE issue_number=?", (issue_number,)
            ).fetchone()
            queued_at = task["updated_at"] if task else now
            queued_dt = datetime.fromisoformat(queued_at)
            queue_duration = max(0, int((now_dt - queued_dt).total_seconds() * 1000))
            cursor = db.execute(
                """INSERT INTO issue_runs(
                    issue_number,kind,status,queued_at,started_at,queue_duration_ms
                ) VALUES(?,?,?,?,?,?)""",
                (issue_number, kind, "running", queued_at, now, queue_duration),
            )
            db.execute(
                """UPDATE tasks SET started_at=COALESCE(started_at,?), finished_at=NULL,
                    total_queue_duration_ms=total_queue_duration_ms+? WHERE issue_number=?""",
                (now, queue_duration, issue_number),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self, run_id: int, issue_number: int, status: str, *, wall_duration_ms: int
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                "UPDATE issue_runs SET status=?,finished_at=?,wall_duration_ms=? WHERE id=?",
                (status, now, wall_duration_ms, run_id),
            )
            db.execute(
                "UPDATE tasks SET total_wall_duration_ms=total_wall_duration_ms+?,finished_at=? "
                "WHERE issue_number=?",
                (wall_duration_ms, now, issue_number),
            )

    def start_plan_task(self, issue_number: int, seq: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                "UPDATE plan_tasks SET started_at=COALESCE(started_at,?),finished_at=NULL "
                "WHERE issue_number=? AND seq=?",
                (now, issue_number, seq),
            )

    def finish_plan_task(self, issue_number: int, seq: int, *, wall_duration_ms: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                "UPDATE plan_tasks SET total_wall_duration_ms=total_wall_duration_ms+?,finished_at=? "
                "WHERE issue_number=? AND seq=?",
                (wall_duration_ms, now, issue_number, seq),
            )

    def record_check_duration(
        self, issue_number: int, *, duration_ms: int, seq: int | None = None
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET total_check_duration_ms=total_check_duration_ms+? "
                "WHERE issue_number=?",
                (duration_ms, issue_number),
            )
            if seq is not None:
                db.execute(
                    "UPDATE plan_tasks SET total_check_duration_ms=total_check_duration_ms+? "
                    "WHERE issue_number=? AND seq=?",
                    (duration_ms, issue_number, seq),
                )

    def record_agent_call(
        self,
        issue_number: int,
        *,
        run_id: int | None,
        seq: int | None,
        attempt: int | None,
        agent: str,
        role: str,
        success: bool,
        duration_ms: int | None,
        usage: dict[str, object] | None,
        error: str = "",
    ) -> None:
        values = self._usage_values(usage)
        duration = int(duration_ms or 0)
        session_id = str((usage or {}).get("session_id") or "") or None
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                """INSERT INTO agent_calls(
                    issue_number,run_id,seq,attempt,agent,role,success,duration_ms,
                    input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,
                    reasoning_tokens,cost_usd,session_id,error,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    issue_number, run_id, seq, attempt, agent, role, int(success), duration,
                    *values, session_id, error or None, now,
                ),
            )
            aggregate = (*values[:5], values[5], duration, issue_number)
            db.execute(
                """UPDATE tasks SET
                    total_input_tokens=total_input_tokens+?,
                    total_output_tokens=total_output_tokens+?,
                    total_cache_read_tokens=total_cache_read_tokens+?,
                    total_cache_creation_tokens=total_cache_creation_tokens+?,
                    total_reasoning_tokens=total_reasoning_tokens+?,
                    total_cost_usd=total_cost_usd+?,
                    total_duration_ms=total_duration_ms+?
                WHERE issue_number=?""",
                aggregate,
            )
            if seq is not None:
                db.execute(
                    """UPDATE plan_tasks SET
                        total_input_tokens=total_input_tokens+?,
                        total_output_tokens=total_output_tokens+?,
                        total_cache_read_tokens=total_cache_read_tokens+?,
                        total_cache_creation_tokens=total_cache_creation_tokens+?,
                        total_reasoning_tokens=total_reasoning_tokens+?,
                        total_cost_usd=total_cost_usd+?,
                        total_duration_ms=total_duration_ms+?
                    WHERE issue_number=? AND seq=?""",
                    (*aggregate, seq),
                )

    def report_rows(self, issue_number: int | None = None) -> list[dict[str, object]]:
        where = " WHERE issue_number=?" if issue_number is not None else ""
        parameters = (issue_number,) if issue_number is not None else ()
        with self.connect() as db:
            issues = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM tasks" + where + " ORDER BY updated_at DESC", parameters
                )
            ]
            for issue in issues:
                issue["tasks"] = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM plan_tasks WHERE issue_number=? ORDER BY seq",
                        (issue["issue_number"],),
                    )
                ]
                issue["runs"] = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM issue_runs WHERE issue_number=? ORDER BY id",
                        (issue["issue_number"],),
                    )
                ]
        return issues

    def load_session(self, issue_number: int, agent: str, role: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT session_id FROM agent_sessions WHERE issue_number=? AND agent=? AND role=?",
                (issue_number, agent, role),
            ).fetchone()
        return str(row["session_id"]) if row else ""

    def save_session(self, issue_number: int, agent: str, role: str, session_id: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO agent_sessions(issue_number,agent,role,session_id,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(issue_number,agent,role) DO UPDATE SET
                session_id=excluded.session_id,updated_at=excluded.updated_at""",
                (issue_number, agent, role, session_id, datetime.now(UTC).isoformat()),
            )

    def rows(self) -> list[dict[str, object]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY updated_at DESC")]

    def status_rows(self, *, active_only: bool = False) -> list[dict[str, object]]:
        """Return task status with the current plan item joined for CLI display."""
        sql = """SELECT tasks.issue_number, tasks.title, tasks.status, tasks.agent,
            tasks.branch, tasks.attempts, tasks.failures, tasks.current_seq,
            plan_tasks.title AS current_task, tasks.last_error, tasks.pr_url,
            tasks.total_input_tokens, tasks.total_output_tokens,
            tasks.total_cache_read_tokens, tasks.total_cache_creation_tokens,
            tasks.total_reasoning_tokens, tasks.total_cost_usd, tasks.total_duration_ms,
            tasks.total_check_duration_ms, tasks.total_wall_duration_ms,
            tasks.total_queue_duration_ms,
            tasks.started_at, tasks.finished_at,
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
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        placeholders = ",".join("?" for _ in _ACTIVE)
        active = [str(status) for status in _ACTIVE]
        with self.connect() as db:
            open_runs = db.execute(
                "SELECT id,issue_number,started_at FROM issue_runs WHERE finished_at IS NULL"
            ).fetchall()
            for run in open_runs:
                started = datetime.fromisoformat(run["started_at"])
                duration = max(0, int((now_dt - started).total_seconds() * 1000))
                db.execute(
                    "UPDATE issue_runs SET status='interrupted',finished_at=?,wall_duration_ms=? "
                    "WHERE id=?",
                    (now, duration, run["id"]),
                )
                db.execute(
                    "UPDATE tasks SET total_wall_duration_ms=total_wall_duration_ms+?,finished_at=? "
                    "WHERE issue_number=?",
                    (duration, now, run["issue_number"]),
                )
            open_tasks = db.execute(
                "SELECT issue_number,seq,started_at FROM plan_tasks "
                "WHERE started_at IS NOT NULL AND finished_at IS NULL"
            ).fetchall()
            for task in open_tasks:
                started = datetime.fromisoformat(task["started_at"])
                duration = max(0, int((now_dt - started).total_seconds() * 1000))
                db.execute(
                    "UPDATE plan_tasks SET total_wall_duration_ms=total_wall_duration_ms+?,"
                    "finished_at=? WHERE issue_number=? AND seq=?",
                    (duration, now, task["issue_number"], task["seq"]),
                )
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

    def reset(self, issue_number: int) -> str | None:
        """Reset a task row back to a claimable state, returning its old status.

        Clears the whole-issue retry budget (``failures``) and the in-cycle
        ``attempts`` marker and returns the row to PENDING so a parked
        FAILED/BLOCKED issue can be claimed again. Any existing plan is kept;
        DONE plan items stay DONE so execution resumes from the first unfinished
        task. Returns None when no row exists for the issue.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            row = db.execute("SELECT status FROM tasks WHERE issue_number=?", (issue_number,)).fetchone()
            if not row:
                return None
            old_status = str(row["status"])
            db.execute(
                "UPDATE tasks SET status=?, failures=0, attempts=0, last_error='', "
                "current_seq=-1, updated_at=? WHERE issue_number=?",
                (str(TaskStatus.PENDING), now, issue_number),
            )
            db.execute(
                "UPDATE plan_tasks SET status=?, attempts=0, last_error='', updated_at=? "
                "WHERE issue_number=? AND status != ?",
                (str(TaskStatus.PENDING), now, issue_number, str(TaskStatus.DONE)),
            )
            db.execute("DELETE FROM agent_sessions WHERE issue_number=?", (issue_number,))
        return old_status
