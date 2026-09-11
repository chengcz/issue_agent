from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .models import Issue, PlanTask, RecordedChild, TaskStatus

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


def _pending_blockers(payload: object) -> list[int]:
    """The blocker numbers a stored notice lists, sorted and deduplicated.

    Anything unreadable — a hand-edited row, a shape an older version wrote —
    reads as "nothing blocks this issue" rather than raising: a display command
    must not be taken down by one bad row.
    """
    if not isinstance(payload, str):
        return []
    try:
        notices = json.loads(payload)
    except json.JSONDecodeError:
        return []
    pending = notices.get("pending") if isinstance(notices, dict) else None
    if not isinstance(pending, list):
        return []
    # ``bool`` is an ``int`` subclass, and issue #True would render as #1.
    return sorted(
        {
            number
            for number in pending
            if isinstance(number, int) and not isinstance(number, bool)
        }
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
                final_last_error TEXT, final_approved_commit TEXT,
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
                ("final_approved_commit", "TEXT"),
                ("clarify_rounds", "INTEGER NOT NULL DEFAULT 0"),
                ("clarify_marker", "TEXT"),
                ("split", "TEXT"),
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
            # A table rather than another ``tasks`` column: the dependency gate
            # runs before an issue is ever claimed, so there is usually no tasks
            # row to hang the record on yet.
            db.execute("""CREATE TABLE IF NOT EXISTS blocker_notices (
                issue_number INTEGER PRIMARY KEY,
                blockers_notified TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
                title=excluded.title,status=excluded.status,agent=excluded.agent,
                updated_at=excluded.updated_at""",
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
                    if status in (str(TaskStatus.FAILED), str(TaskStatus.BLOCKED)):
                        if int(row["failures"]) >= max_attempts:
                            return False
                    elif status in (str(TaskStatus.PENDING), str(TaskStatus.PLANNED)):
                        # A PENDING/PLANNED row that already carries a plan was
                        # crash-interrupted between save_plan and publishing it
                        # (or is stranded there after an older recovery). Reclaim
                        # so the already-persisted plan is republished without
                        # another LLM call.
                        pass
                    else:
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
        still re-claimable (``failures < max_attempts``) or parked. An unknown
        issue gets a fresh row (its title is not known here) instead of silently
        dropping the failure: the return value must always reflect what the
        database now holds.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            row = db.execute("SELECT failures FROM tasks WHERE issue_number=?", (issue_number,)).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO tasks(issue_number,title,status,failures,last_error,updated_at)
                    VALUES(?,'',?,1,?,?)""",
                    (issue_number, str(status), last_error, now),
                )
                return 1
            failures = int(row["failures"]) + 1
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
            # A shorter replacement plan must not leave ghost rows behind: they
            # would mis-index plan_task_statuses and haunt the CLI report.
            db.execute(
                "DELETE FROM plan_tasks WHERE issue_number=? AND seq >= ?",
                (issue_number, len(plan)),
            )
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
        try:
            items = json.loads(row["plan"])
            return [PlanTask.from_dict(item) for item in items]
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            # Same posture as load_split: a corrupted or hand-edited payload
            # falls back to "no plan" — fresh planning overwrites it — instead
            # of wedging the poll loop with an unparseable column.
            return None

    def plan_task_statuses(self, issue_number: int) -> list[TaskStatus]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT seq,status FROM plan_tasks WHERE issue_number=? ORDER BY seq", (issue_number,)
            ).fetchall()
        statuses: list[TaskStatus] = []
        for row in rows:
            try:
                statuses.append(TaskStatus(row["status"]))
            except ValueError:
                # An unknown status string in a hand-edited database reads as
                # "not done yet", keeping the list length aligned with the plan.
                statuses.append(TaskStatus.PENDING)
        return statuses

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

    def set_final_approved(self, issue_number: int, commit: str) -> None:
        """Record the commit whose whole-branch finalize passed, enabling reuse.

        A push/PR-failure retry that resets to this exact commit skips the final
        review and full checks; any other HEAD always re-runs finalize.
        """
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET final_approved_commit=? WHERE issue_number=?",
                (commit, issue_number),
            )

    def final_approved_commit(self, issue_number: int) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT final_approved_commit FROM tasks WHERE issue_number=?",
                (issue_number,),
            ).fetchone()
        return str(row["final_approved_commit"]) if row and row["final_approved_commit"] else ""

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
        self._annotate_blockers(issues)
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

    def clear_session(self, issue_number: int, agent: str, role: str) -> None:
        """Drop a stored session so the next call starts a fresh one.

        Used when a resumed call fails: the stored session may be expired or
        corrupt, and retrying against it would fail identically until a human
        resets the issue.
        """
        with self.connect() as db:
            db.execute(
                "DELETE FROM agent_sessions WHERE issue_number=? AND agent=? AND role=?",
                (issue_number, agent, role),
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
            rows = [dict(row) for row in db.execute(sql, parameters)]
        self._annotate_blockers(rows)
        return rows

    def recover_interrupted(self, max_attempts: int = 3) -> int:
        """Make work interrupted by a process restart claimable again.

        Each interruption consumes one unit of the whole-issue failure budget, so
        an issue that reproducibly crashes the orchestrator eventually parks as
        FAILED (claimable only via a human ``reset``) instead of burning tokens
        on every restart.
        """
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
                "UPDATE tasks SET failures=failures+1, updated_at=?, last_error=?,"
                " status=CASE WHEN failures+1>=? THEN ?"
                " WHEN plan IS NOT NULL AND plan != '' THEN ?"
                " ELSE ? END"
                " WHERE status=?",
                (
                    now, "orchestrator restarted during planning", max_attempts,
                    str(TaskStatus.FAILED), str(TaskStatus.PLANNED), str(TaskStatus.PENDING),
                    str(TaskStatus.PLANNING),
                ),
            )
            resumed = db.execute(
                f"UPDATE tasks SET failures=failures+1, updated_at=?,"
                f" status=CASE WHEN failures+1>=? THEN ? ELSE ? END, last_error=?"
                f" WHERE status IN ({placeholders}) AND plan IS NOT NULL AND plan != ''",
                (
                    now, max_attempts, str(TaskStatus.FAILED), str(TaskStatus.PLANNED),
                    "orchestrator restarted while task was active", *active,
                ),
            )
            failed = db.execute(
                f"UPDATE tasks SET failures=failures+1, updated_at=?, last_error=?,"
                f" status=CASE WHEN failures+1>=? THEN ? ELSE ? END"
                f" WHERE status IN ({placeholders}) AND (plan IS NULL OR plan = '')",
                (
                    now, "orchestrator restarted while task was active", max_attempts,
                    str(TaskStatus.FAILED), str(TaskStatus.PENDING), *active,
                ),
            )
            db.execute(
                f"UPDATE plan_tasks SET status=?, last_error=?, updated_at=? WHERE status IN ({placeholders})",
                (str(TaskStatus.PENDING), "orchestrator restarted", now, *active),
            )
            return planning.rowcount + resumed.rowcount + failed.rowcount

    def blockers_by_issue(self) -> dict[int, list[int]]:
        """The blocker numbers each issue was last recorded as waiting on.

        Read from the same local notice the dependency comment was written
        from, so ``status``/``report`` stay offline commands. Only non-empty
        sets are listed: an issue whose blockers all closed reads as unblocked,
        which is also what an issue with no notice row at all reads as.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT issue_number, blockers_notified FROM blocker_notices"
            ).fetchall()
        return {
            int(row["issue_number"]): pending
            for row in rows
            if (pending := _pending_blockers(row["blockers_notified"]))
        }

    def _annotate_blockers(self, issues: list[dict[str, object]]) -> None:
        """Add the recorded blocker numbers to each row, in place."""
        blockers = self.blockers_by_issue()
        for issue in issues:
            issue["blocked_by"] = blockers.get(int(issue["issue_number"]), [])

    def load_blocker_notices(self, issue_number: int) -> dict[str, object]:
        """Read back which dependency comments were already posted for an issue.

        Keys are ``pending`` (the blocker set the last comment listed, or
        ``null`` when none was posted yet), ``declared``/``native`` (the mismatch
        a warning already described) and ``self`` (a self-dependency warning was
        sent). Anything unreadable reads as "nothing sent yet" so a hand-edited
        database cannot wedge the poll loop.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT blockers_notified FROM blocker_notices WHERE issue_number=?",
                (issue_number,),
            ).fetchone()
        if not row:
            return {}
        try:
            notices = json.loads(row["blockers_notified"])
        except json.JSONDecodeError:
            return {}
        return notices if isinstance(notices, dict) else {}

    def save_blocker_notices(self, issue_number: int, notices: dict[str, object]) -> None:
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(notices, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute(
                """INSERT INTO blocker_notices(issue_number,blockers_notified,updated_at)
                VALUES(?,?,?) ON CONFLICT(issue_number) DO UPDATE SET
                blockers_notified=excluded.blockers_notified,updated_at=excluded.updated_at""",
                (issue_number, payload, now),
            )

    def save_split(self, issue_number: int, children: list[RecordedChild]) -> None:
        """Persist a split decision before any child issue is created.

        Writing the whole proposal first is what makes a crashed or failed
        attempt resumable: the orchestrator can re-read what it intended to
        create instead of asking the planner for a second, differently-worded
        proposal.
        """
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(
            {"children": [child.to_dict() for child in children]}, ensure_ascii=False
        )
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET split=?, updated_at=? WHERE issue_number=?",
                (payload, now, issue_number),
            )

    def load_split(self, issue_number: int) -> list[RecordedChild]:
        """Read back the recorded split, or ``[]`` when there is none.

        A missing row and an unreadable payload both read as "no split", so a
        hand-edited database falls back to planning rather than wedging.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT split FROM tasks WHERE issue_number=?", (issue_number,)
            ).fetchone()
        if not row or not row["split"]:
            return []
        try:
            payload = json.loads(row["split"])
        except json.JSONDecodeError:
            return []
        items = payload.get("children") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [RecordedChild.from_dict(item) for item in items if isinstance(item, dict)]

    def update_split_child(self, issue_number: int, index: int, **fields: object) -> None:
        """Merge ``fields`` into one recorded child, identified by position.

        Raises ``ValueError`` when the record or the index is gone: the caller
        has usually just created a real issue on GitHub, and silently dropping
        its number would make the next attempt create a duplicate.
        """
        children = self.load_split(issue_number)
        if not 0 <= index < len(children):
            raise ValueError(
                f"cannot record child index {index} for issue #{issue_number}: "
                f"the split record holds {len(children)} children"
            )
        child = replace(children[index], **fields)
        children[index] = child
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(
            {"children": [item.to_dict() for item in children]}, ensure_ascii=False
        )
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET split=?, updated_at=? WHERE issue_number=?",
                (payload, now, issue_number),
            )

    def clarify_state(self, issue_number: int) -> tuple[int, str]:
        """Return ``(rounds asked, marker of the outstanding question)``.

        An empty marker means no question is waiting, which is also what an
        issue that has never needed one reports.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT clarify_rounds, clarify_marker FROM tasks WHERE issue_number=?",
                (issue_number,),
            ).fetchone()
        if not row:
            return (0, "")
        return (int(row["clarify_rounds"]), str(row["clarify_marker"] or ""))

    def record_clarify_round(self, issue_number: int, marker: str) -> int:
        """Note that the planner asked for information, returning the new round count.

        The increment happens in SQL so the counter cannot drift from the marker
        it belongs with. Callers compare the result against
        ``max_clarify_rounds``; keeping the budget in this table is what makes it
        survive a restart. An unknown issue gets a fresh row (its title is not
        known here) instead of returning a count nothing backed.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE tasks SET clarify_rounds=clarify_rounds+1, clarify_marker=?, updated_at=? "
                "WHERE issue_number=?",
                (marker, now, issue_number),
            )
            if cursor.rowcount == 0:
                db.execute(
                    """INSERT INTO tasks(issue_number,title,status,clarify_rounds,clarify_marker,updated_at)
                    VALUES(?,'',?,1,?,?)""",
                    (issue_number, str(TaskStatus.PENDING), marker, now),
                )
                return 1
            row = db.execute(
                "SELECT clarify_rounds FROM tasks WHERE issue_number=?", (issue_number,)
            ).fetchone()
        return int(row["clarify_rounds"]) if row else 0

    def clear_clarify(self, issue_number: int) -> None:
        """Drop the outstanding question, leaving the round budget spent.

        The budget bounds how often the planner may ask over the issue's life,
        not how many answers it is allowed to receive, so consuming an answer
        must not hand back another round.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET clarify_marker=NULL, updated_at=? WHERE issue_number=?",
                (now, issue_number),
            )

    def reset(
        self,
        issue_number: int,
        *,
        allowed: tuple[str, ...] | frozenset[str] | None = None,
    ) -> str | None:
        """Reset a task row back to a claimable state, returning its old status.

        Clears the whole-issue retry budget (``failures``) and the in-cycle
        ``attempts`` marker and returns the row to PENDING so a parked
        FAILED/BLOCKED issue can be claimed again. Any existing plan is kept;
        DONE plan items stay DONE so execution resumes from the first unfinished
        task. The recorded dependency notices go too, so a human who resets an
        issue gets told again about whatever still blocks it. The ask budget
        (``clarify_rounds``) resets so the planner may ask again, but the
        outstanding-question marker is kept: the question-and-answer transcript
        is read from it, and a re-plan after a reset must still see the answers
        the human already gave. A recorded split is dropped: resetting a split
        parent is how a human asks for it to be planned again as one unit.

        When *allowed* is given, the status check and the reset run against the
        same connection back to back: a serve process claiming the issue between
        the caller's snapshot and this call makes the reset raise ``ValueError``
        instead of clobbering a live run's row. Returns None when no row exists.
        """
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            row = db.execute("SELECT status FROM tasks WHERE issue_number=?", (issue_number,)).fetchone()
            if not row:
                return None
            old_status = str(row["status"])
            if allowed is not None and old_status not in allowed:
                raise ValueError(
                    f"status {old_status} cannot be reset; resettable statuses: "
                    f"{', '.join(sorted(allowed))}"
                )
            db.execute(
                "UPDATE tasks SET status=?, failures=0, attempts=0, last_error='', "
                "current_seq=-1, clarify_rounds=0, split=NULL, updated_at=? "
                "WHERE issue_number=?",
                (str(TaskStatus.PENDING), now, issue_number),
            )
            db.execute(
                "UPDATE plan_tasks SET status=?, attempts=0, last_error='', updated_at=? "
                "WHERE issue_number=? AND status != ?",
                (str(TaskStatus.PENDING), now, issue_number, str(TaskStatus.DONE)),
            )
            db.execute("DELETE FROM agent_sessions WHERE issue_number=?", (issue_number,))
            db.execute("DELETE FROM blocker_notices WHERE issue_number=?", (issue_number,))
        return old_status
