"""SQLite persistence for push batches, items and webhook deliveries."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ITEM_PENDING = "pending"
ITEM_DISPATCHING = "dispatching"
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_SUCCEEDED = "succeeded"
ITEM_FAILED = "failed"
ITEM_CANCELLED = "cancelled"

ACTIVE_ITEM_STATUSES = (
    ITEM_PENDING,
    ITEM_DISPATCHING,
    ITEM_QUEUED,
    ITEM_RUNNING,
)
TERMINAL_ITEM_STATUSES = (
    ITEM_SUCCEEDED,
    ITEM_FAILED,
    ITEM_CANCELLED,
)

BATCH_PENDING = "pending"
BATCH_RUNNING = "running"
BATCH_COMPLETED = "completed"
BATCH_PARTIAL = "partial"
BATCH_FAILED = "failed"
BATCH_CANCELLED = "cancelled"

WEBHOOK_PENDING = "pending"
WEBHOOK_SENDING = "sending"
WEBHOOK_SENT = "sent"
WEBHOOK_FAILED = "failed"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


class PushStore:
    """Owns the push.db SQLite file."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = (
            Path(db_path)
            if db_path is not None
            else Path.home() / ".config" / "autoglm" / "push.db"
        )
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._ensure_ready()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _ensure_ready(self) -> None:
        if self._conn is not None:
            return
        with self._lock:
            if self._conn is not None:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._db_path), check_same_thread=False, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS push_batches (
                    id TEXT PRIMARY KEY,
                    external_batch_id TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    callback_url TEXT,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    raw_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS push_items (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    external_item_id TEXT NOT NULL,
                    device_serial TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    message TEXT NOT NULL,
                    mention_all INTEGER NOT NULL DEFAULT 1,
                    scheduled_at TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    task_id TEXT,
                    device_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(batch_id, external_item_id)
                );

                CREATE TABLE IF NOT EXISTS push_webhook_deliveries (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    item_id TEXT,
                    event TEXT NOT NULL,
                    url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_push_items_due
                    ON push_items(status, scheduled_at);
                CREATE INDEX IF NOT EXISTS idx_push_items_batch
                    ON push_items(batch_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_push_items_task
                    ON push_items(task_id);
                CREATE INDEX IF NOT EXISTS idx_push_batches_created
                    ON push_batches(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_push_webhooks_due
                    ON push_webhook_deliveries(status, next_attempt_at);
                """
            )
            conn.commit()
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # Batches
    # ------------------------------------------------------------------
    def create_batch(
        self,
        *,
        external_batch_id: str,
        payload_hash: str,
        callback_url: str | None,
        window_start: str,
        window_end: str,
        raw_payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        batch_id = str(uuid4())
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute(
                """
                INSERT INTO push_batches (
                    id, external_batch_id, payload_hash, callback_url,
                    window_start, window_end, status, raw_payload,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    external_batch_id,
                    payload_hash,
                    callback_url,
                    window_start,
                    window_end,
                    BATCH_PENDING,
                    json.dumps(raw_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM push_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return _row_to_dict(row) or {}

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM push_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return _row_to_dict(row)

    def get_batch_by_external_id(self, external_batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM push_batches WHERE external_batch_id = ?",
                (external_batch_id,),
            ).fetchone()
        return _row_to_dict(row)

    def list_batches(
        self,
        *,
        status: str | None = None,
        device_serial: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("b.status = ?")
            params.append(status)
        if device_serial:
            clauses.append(
                "EXISTS (SELECT 1 FROM push_items i "
                "WHERE i.batch_id = b.id AND i.device_serial = ?)"
            )
            params.append(device_serial)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            total_row = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM push_batches b {where}", params
            ).fetchone()
            rows = self._conn.execute(
                f"""
                SELECT b.*,
                       (SELECT COUNT(*) FROM push_items i WHERE i.batch_id = b.id)
                           AS item_count,
                       (SELECT COUNT(*) FROM push_items i
                        WHERE i.batch_id = b.id AND i.status = 'succeeded')
                           AS success_count,
                       (SELECT COUNT(*) FROM push_items i
                        WHERE i.batch_id = b.id AND i.status = 'failed')
                           AS failure_count
                FROM push_batches b
                {where}
                ORDER BY b.created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return [_row_to_dict(r) or {} for r in rows], int(
            total_row["c"] if total_row else 0
        )

    def update_batch_status(
        self, batch_id: str, status: str, *, finished_at: str | None = None
    ) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute(
                """
                UPDATE push_batches
                SET status = ?, updated_at = ?, finished_at = COALESCE(?, finished_at)
                WHERE id = ?
                """,
                (status, now, finished_at, batch_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM push_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return _row_to_dict(row)

    def batch_counts(self, batch_id: str) -> dict[str, int]:
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS c FROM push_items "
                "WHERE batch_id = ? GROUP BY status",
                (batch_id,),
            ).fetchall()
        return {str(row["status"]): int(row["c"]) for row in rows}

    def reopen_batch(self, batch_id: str) -> dict[str, Any] | None:
        """Mark a batch active again after a manual retrigger."""
        now = utc_now_iso()
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute(
                "UPDATE push_batches SET status = ?, finished_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (BATCH_RUNNING, now, batch_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM push_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return _row_to_dict(row)

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------
    def create_items(
        self, batch_id: str, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        now = utc_now_iso()
        created: list[dict[str, Any]] = []
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            for item in items:
                item_id = str(item.get("id") or uuid4())
                self._conn.execute(
                    """
                    INSERT INTO push_items (
                        id, batch_id, external_item_id, device_serial,
                        group_name, message, mention_all, scheduled_at,
                        window_end, status, attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        batch_id,
                        str(item["external_item_id"]),
                        str(item["device_serial"]),
                        str(item["group_name"]),
                        str(item["message"]),
                        1 if item.get("mention_all", True) else 0,
                        str(item["scheduled_at"]),
                        str(item["window_end"]),
                        ITEM_PENDING,
                        0,
                        now,
                        now,
                    ),
                )
                created.append(
                    {
                        "id": item_id,
                        "batch_id": batch_id,
                        "external_item_id": item["external_item_id"],
                        "device_serial": item["device_serial"],
                        "group_name": item["group_name"],
                        "message": item["message"],
                        "mention_all": bool(item.get("mention_all", True)),
                        "scheduled_at": item["scheduled_at"],
                        "window_end": item["window_end"],
                        "status": ITEM_PENDING,
                        "attempts": 0,
                        "task_id": None,
                        "device_id": None,
                        "last_error": None,
                        "created_at": now,
                        "updated_at": now,
                        "finished_at": None,
                    }
                )
            self._conn.commit()
        return created

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM push_items WHERE id = ?", (item_id,)
            ).fetchone()
        return _row_to_dict(row)

    def list_items(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM push_items WHERE batch_id = ? ORDER BY created_at, id",
                (batch_id,),
            ).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    def claim_due_items(self, now_iso: str, limit: int = 50) -> list[dict[str, Any]]:
        """Atomically move due pending items to dispatching and return them."""
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                """
                SELECT * FROM push_items
                WHERE status = ? AND scheduled_at <= ?
                ORDER BY scheduled_at ASC
                LIMIT ?
                """,
                (ITEM_PENDING, now_iso, limit),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                self._conn.executemany(
                    "UPDATE push_items SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    [
                        (ITEM_DISPATCHING, now_iso, item_id, ITEM_PENDING)
                        for item_id in ids
                    ],
                )
            self._conn.commit()
        return [_row_to_dict(row) or {} for row in rows]

    def update_item(self, item_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "scheduled_at",
            "window_end",
            "status",
            "attempts",
            "task_id",
            "device_id",
            "last_error",
            "finished_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_item(item_id)
        updates["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute(
                f"UPDATE push_items SET {assignments} WHERE id = ?",
                [*updates.values(), item_id],
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM push_items WHERE id = ?", (item_id,)
            ).fetchone()
        return _row_to_dict(row)

    def list_items_by_status(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            rows = self._conn.execute(
                f"SELECT * FROM push_items WHERE status IN ({placeholders})",
                list(statuses),
            ).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    def list_batches_by_status(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            rows = self._conn.execute(
                f"SELECT * FROM push_batches WHERE status IN ({placeholders})",
                list(statuses),
            ).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    def reset_in_flight_items(self) -> int:
        """Return dispatching items to pending after a crash/restart."""
        now = utc_now_iso()
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            cursor = self._conn.execute(
                "UPDATE push_items SET status = ?, updated_at = ? WHERE status = ?",
                (ITEM_PENDING, now, ITEM_DISPATCHING),
            )
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def reset_sending_webhooks(self) -> int:
        now = utc_now_iso()
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            cursor = self._conn.execute(
                "UPDATE push_webhook_deliveries SET status = ?, updated_at = ? "
                "WHERE status = ?",
                (WEBHOOK_PENDING, now, WEBHOOK_SENDING),
            )
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def reset_item_for_retrigger(
        self, item_id: str, scheduled_at: str, window_end: str
    ) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute(
                """
                UPDATE push_items
                SET status = ?, scheduled_at = ?, window_end = ?,
                    attempts = 0, task_id = NULL, device_id = NULL, last_error = NULL,
                    finished_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (ITEM_PENDING, scheduled_at, window_end, now, item_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM push_items WHERE id = ?", (item_id,)
            ).fetchone()
        return _row_to_dict(row)

    # ------------------------------------------------------------------
    # Webhook deliveries
    # ------------------------------------------------------------------
    def enqueue_webhook(
        self,
        *,
        batch_id: str,
        item_id: str | None,
        event: str,
        url: str,
        payload: dict[str, Any],
        next_attempt_at: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        delivery_id = str(uuid4())
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute(
                """
                INSERT INTO push_webhook_deliveries (
                    id, batch_id, item_id, event, url, payload_json,
                    status, attempts, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    batch_id,
                    item_id,
                    event,
                    url,
                    json.dumps(payload, ensure_ascii=False),
                    WEBHOOK_PENDING,
                    0,
                    next_attempt_at or now,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM push_webhook_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return _row_to_dict(row) or {}

    def claim_due_webhooks(self, now_iso: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                """
                SELECT * FROM push_webhook_deliveries
                WHERE status = ? AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC
                LIMIT ?
                """,
                (WEBHOOK_PENDING, now_iso, limit),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                self._conn.executemany(
                    "UPDATE push_webhook_deliveries SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    [(WEBHOOK_SENDING, now_iso, did, WEBHOOK_PENDING) for did in ids],
                )
            self._conn.commit()
        return [_row_to_dict(row) or {} for row in rows]

    def mark_webhook_sent(self, delivery_id: str) -> None:
        now = utc_now_iso()
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute(
                "UPDATE push_webhook_deliveries SET status = ?, updated_at = ?, "
                "last_error = NULL WHERE id = ?",
                (WEBHOOK_SENT, now, delivery_id),
            )
            self._conn.commit()

    def reschedule_webhook(
        self,
        delivery_id: str,
        *,
        next_attempt_at: str,
        error: str,
        attempts: int,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute(
                """
                UPDATE push_webhook_deliveries
                SET status = ?, next_attempt_at = ?, last_error = ?,
                    attempts = ?, updated_at = ?
                WHERE id = ?
                """,
                (WEBHOOK_PENDING, next_attempt_at, error, attempts, now, delivery_id),
            )
            self._conn.commit()

    def mark_webhook_failed(
        self, delivery_id: str, *, error: str, attempts: int
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            self._conn.execute(
                """
                UPDATE push_webhook_deliveries
                SET status = ?, last_error = ?, attempts = ?, updated_at = ?
                WHERE id = ?
                """,
                (WEBHOOK_FAILED, error, attempts, now, delivery_id),
            )
            self._conn.commit()

    def list_webhooks(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM push_webhook_deliveries WHERE batch_id = ? "
                "ORDER BY created_at",
                (batch_id,),
            ).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def prune(self, older_than_days: int = 30) -> int:
        """Delete finished batches (and their items/webhooks) older than N days."""
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        terminal = ", ".join("?" for _ in TERMINAL_ITEM_STATUSES)
        with self._lock:
            self._ensure_ready()
            assert self._conn is not None
            rows = self._conn.execute(
                f"""
                SELECT b.id FROM push_batches b
                WHERE b.finished_at IS NOT NULL AND b.finished_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM push_items i
                      WHERE i.batch_id = b.id
                        AND i.status NOT IN ({terminal})
                  )
                """,
                [cutoff_iso, *TERMINAL_ITEM_STATUSES],
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            for batch_id in ids:
                self._conn.execute(
                    "DELETE FROM push_items WHERE batch_id = ?", (batch_id,)
                )
                self._conn.execute(
                    "DELETE FROM push_webhook_deliveries WHERE batch_id = ?",
                    (batch_id,),
                )
                self._conn.execute("DELETE FROM push_batches WHERE id = ?", (batch_id,))
            self._conn.commit()
        return len(ids)


push_store = PushStore()
