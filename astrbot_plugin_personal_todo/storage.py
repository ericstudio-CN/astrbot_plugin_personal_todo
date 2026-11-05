"""Durable, small SQLite store for personal tasks and reminder delivery.

All timestamps are integer Unix seconds. This module deliberately has no
AstrBot dependency so restart and delivery behaviour can be tested separately.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class TaskStore:
    """One connection, intended to be used by the plugin's single event loop."""

    def __init__(self, path: str | Path):
        filename = str(path)
        if filename != ":memory:":
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(filename, timeout=1.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                owner TEXT NOT NULL,
                source_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (owner, source_key)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                origin TEXT NOT NULL,
                title TEXT NOT NULL,
                due_at INTEGER,
                due_precision TEXT NOT NULL
                    CHECK (due_precision IN ('date', 'datetime', 'none')),
                importance TEXT NOT NULL
                    CHECK (importance IN ('high', 'normal', 'low')),
                importance_reason TEXT NOT NULL,
                source_excerpt TEXT NOT NULL,
                source_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'done', 'cancelled')),
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tasks_owner_status
                ON tasks(owner, status, due_at);
            CREATE INDEX IF NOT EXISTS tasks_submission
                ON tasks(owner, source_key);
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                scheduled_at INTEGER NOT NULL,
                available_at INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN (
                        'pending', 'processing', 'sent', 'failed',
                        'cancelled', 'superseded'
                    )),
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_until INTEGER,
                last_error TEXT,
                sent_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(task_id, scheduled_at)
            );
            CREATE INDEX IF NOT EXISTS reminders_due
                ON reminders(state, available_at, scheduled_at);
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _normalise_item(item: dict[str, Any]) -> dict[str, Any]:
        title = str(item.get("title", "")).strip()
        if not title:
            raise ValueError("Task title cannot be empty")
        precision = item.get("due_precision", "none")
        if precision not in {"date", "datetime", "none"}:
            raise ValueError("Invalid deadline precision")
        importance = item.get("importance", "normal")
        if importance not in {"high", "normal", "low"}:
            raise ValueError("Invalid importance")
        due_at = item.get("due_at")
        if due_at is not None:
            due_at = int(due_at)
        if (due_at is None) != (precision == "none"):
            raise ValueError("Deadline and precision do not agree")
        return {
            "title": title,
            "due_at": due_at,
            "due_precision": precision,
            "importance": importance,
            "importance_reason": str(item.get("importance_reason", "")),
            "source_excerpt": str(item.get("source_excerpt", "")),
            "reminder_times": sorted({int(value) for value in item.get("reminder_times", [])}),
        }

    def add_tasks(
        self,
        owner: str,
        origin: str,
        source_key: str,
        items: list[dict],
        now: int,
    ) -> tuple[list[dict], bool]:
        """Save an entire submission atomically, or return its existing tasks."""
        if not owner or not origin or not source_key:
            raise ValueError("Owner, origin and submission key are required")
        if not items:
            raise ValueError("At least one task is required")
        prepared = [self._normalise_item(item) for item in items]
        task_ids = []
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO submissions(owner,source_key,created_at) VALUES(?,?,?)",
                (owner, source_key, now),
            ).rowcount
            if not inserted:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE owner=? AND source_key=? ORDER BY id",
                    (owner, source_key),
                ).fetchall()
                return [dict(row) for row in rows], False
            for item in prepared:
                cursor = self._conn.execute(
                    """INSERT INTO tasks(
                        owner,origin,title,due_at,due_precision,importance,
                        importance_reason,source_excerpt,source_key,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        owner,
                        origin,
                        item["title"],
                        item["due_at"],
                        item["due_precision"],
                        item["importance"],
                        item["importance_reason"],
                        item["source_excerpt"],
                        source_key,
                        now,
                        now,
                    ),
                )
                task_id = cursor.lastrowid
                task_ids.append(task_id)
                self._insert_reminders(task_id, item["reminder_times"], now)
            tasks = [self.get_task(task_id, owner) for task_id in task_ids]
        return tasks, True

    def list_tasks(self, owner: str, status: str = "pending", limit: int = 30) -> list[dict]:
        if status not in {"pending", "done", "cancelled", "all"}:
            raise ValueError("Invalid task status")
        status_filter = "" if status == "all" else " AND status=?"
        params = [owner] if status == "all" else [owner, status]
        params.append(max(0, int(limit)))
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE owner=?"
            + status_filter
            + " ORDER BY due_at IS NULL, due_at, id LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_task(self, task_id: int, owner: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, owner)
        ).fetchone()
        return dict(row) if row else None

    def list_reminders(self, task_id: int, owner: str) -> list[dict]:
        """Return outstanding and exhausted delivery jobs, scoped to the owner."""
        rows = self._conn.execute(
            """SELECT r.* FROM reminders r JOIN tasks t ON t.id=r.task_id
            WHERE t.id=? AND t.owner=? AND r.state IN ('pending','processing','failed')
            ORDER BY r.scheduled_at,r.id""",
            (task_id, owner),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_importance(self, task_id: int, owner: str, importance: str, now: int) -> dict | None:
        if importance not in {"high", "normal", "low"}:
            raise ValueError("Invalid importance")
        with self._conn:
            changed = self._conn.execute(
                """UPDATE tasks SET importance=?,importance_reason=?,updated_at=?
                WHERE id=? AND owner=? AND status='pending'""",
                (importance, "用户手动设置", now, task_id, owner),
            ).rowcount
        return self.get_task(task_id, owner) if changed else None

    def _cancel_outstanding(self, task_id: int, now: int) -> None:
        self._conn.execute(
            """UPDATE reminders SET state='cancelled',lease_until=NULL,updated_at=?
            WHERE task_id=? AND state IN ('pending','processing','failed')""",
            (now, task_id),
        )

    def _insert_reminders(self, task_id: int, reminder_times: list[int], now: int) -> None:
        self._conn.executemany(
            """INSERT INTO reminders(
                task_id,scheduled_at,available_at,created_at,updated_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(task_id,scheduled_at) DO UPDATE SET
                available_at=excluded.available_at,state='pending',attempts=0,
                lease_until=NULL,last_error=NULL,sent_at=NULL,updated_at=excluded.updated_at
            """,
            [
                (task_id, scheduled_at, scheduled_at, now, now)
                for scheduled_at in sorted({int(value) for value in reminder_times})
            ],
        )

    def finish_task(self, task_id: int, owner: str, status: str, now: int) -> dict | None:
        if status not in {"done", "cancelled"}:
            raise ValueError("Finished task status must be done or cancelled")
        with self._conn:
            task = self.get_task(task_id, owner)
            if task is None or task["status"] not in {"pending", status}:
                return None
            self._conn.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE id=? AND owner=?",
                (status, now, task_id, owner),
            )
            self._cancel_outstanding(task_id, now)
        return self.get_task(task_id, owner)

    def reschedule_task(
        self,
        task_id: int,
        owner: str,
        due_at: int | None,
        due_precision: str,
        reminder_times: list[int],
        now: int,
    ) -> dict | None:
        checked = self._normalise_item(
            {
                "title": "validation",
                "due_at": due_at,
                "due_precision": due_precision,
                "reminder_times": reminder_times,
            }
        )
        with self._conn:
            task = self.get_task(task_id, owner)
            if task is None or task["status"] != "pending":
                return None
            self._conn.execute(
                "UPDATE tasks SET due_at=?,due_precision=?,updated_at=? "
                "WHERE id=? AND owner=? AND status='pending'",
                (checked["due_at"], due_precision, now, task_id, owner),
            )
            self._cancel_outstanding(task_id, now)
            self._insert_reminders(task_id, checked["reminder_times"], now)
        return self.get_task(task_id, owner)

    def replace_reminders(
        self, task_id: int, owner: str, reminder_times: list[int], now: int
    ) -> dict | None:
        times = sorted({int(value) for value in reminder_times})
        with self._conn:
            task = self.get_task(task_id, owner)
            if task is None or task["status"] != "pending":
                return None
            self._cancel_outstanding(task_id, now)
            self._insert_reminders(task_id, times, now)
            self._conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now, task_id))
        return self.get_task(task_id, owner)

    def claim_due(self, now: int, limit: int = 20, lease_seconds: int = 120) -> list[dict]:
        """Lease delivery jobs, recovering abandoned leases and merging old points.

        A newer elapsed reminder point supersedes older unsent points for the
        same task. A retry's available_at is never moved forward by polling.
        """
        if limit <= 0:
            return []
        if lease_seconds <= 0:
            raise ValueError("Reminder lease must be positive")
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                """UPDATE reminders SET state='cancelled',lease_until=NULL,updated_at=?
                WHERE state IN ('pending','processing') AND task_id IN (
                    SELECT id FROM tasks WHERE status!='pending'
                )""",
                (now,),
            )
            self._conn.execute(
                """UPDATE reminders SET state='pending',lease_until=NULL,updated_at=?
                WHERE state='processing' AND lease_until<=?""",
                (now, now),
            )
            self._conn.execute(
                """UPDATE reminders AS old
                SET state='superseded',lease_until=NULL,updated_at=?
                WHERE old.state='pending' AND old.scheduled_at<=?
                  AND NOT EXISTS (
                    SELECT 1 FROM reminders active
                    WHERE active.task_id=old.task_id AND active.state='processing'
                  ) AND EXISTS (
                    SELECT 1 FROM reminders newer
                    WHERE newer.task_id=old.task_id AND newer.state='pending'
                      AND newer.scheduled_at<=?
                      AND newer.scheduled_at>old.scheduled_at
                  )""",
                (now, now, now),
            )
            rows = self._conn.execute(
                """SELECT r.id FROM reminders r
                JOIN tasks t ON t.id=r.task_id
                WHERE r.state='pending' AND r.scheduled_at<=? AND r.available_at<=?
                  AND t.status='pending' AND NOT EXISTS (
                    SELECT 1 FROM reminders active
                    WHERE active.task_id=r.task_id AND active.state='processing'
                  )
                ORDER BY r.available_at,r.id LIMIT ?""",
                (now, now, int(limit)),
            ).fetchall()
            claimed = []
            for row in rows:
                reminder_id = row["id"]
                self._conn.execute(
                    """UPDATE reminders SET state='processing',attempts=attempts+1,
                    lease_until=?,updated_at=? WHERE id=? AND state='pending'""",
                    (now + lease_seconds, now, reminder_id),
                )
                job = self._conn.execute(
                    """SELECT t.*, t.id AS task_id,r.id AS reminder_id,
                    r.scheduled_at,r.available_at,r.attempts,r.lease_until
                    FROM reminders r JOIN tasks t ON t.id=r.task_id WHERE r.id=?""",
                    (reminder_id,),
                ).fetchone()
                claimed.append(dict(job))
        return claimed

    def acknowledge(self, reminder_id: int, now: int) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE reminders SET state='sent',sent_at=?,lease_until=NULL,
                last_error=NULL,updated_at=? WHERE id=? AND state='processing'
                  AND task_id IN (SELECT id FROM tasks WHERE status='pending')""",
                (now, now, reminder_id),
            )

    def fail_reminder(
        self,
        reminder_id: int,
        now: int,
        error: str,
        max_attempts: int = 5,
    ) -> None:
        with self._conn:
            row = self._conn.execute(
                """SELECT attempts FROM reminders WHERE id=? AND state='processing'
                AND task_id IN (SELECT id FROM tasks WHERE status='pending')""",
                (reminder_id,),
            ).fetchone()
            if row is None:
                return
            attempts = row["attempts"]
            exhausted = attempts >= max(1, int(max_attempts))
            delay = min(3600, 30 * (2 ** min(max(0, attempts - 1), 7)))
            self._conn.execute(
                """UPDATE reminders SET state=?,available_at=?,lease_until=NULL,
                last_error=?,updated_at=? WHERE id=?""",
                (
                    "failed" if exhausted else "pending",
                    now + delay,
                    str(error)[:500],
                    now,
                    reminder_id,
                ),
            )

    def is_reminder_active(self, reminder_id: int) -> bool:
        row = self._conn.execute(
            """SELECT 1 FROM reminders r JOIN tasks t ON t.id=r.task_id
            WHERE r.id=? AND r.state='processing' AND t.status='pending'""",
            (reminder_id,),
        ).fetchone()
        return row is not None

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._conn:
            self._conn.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )
