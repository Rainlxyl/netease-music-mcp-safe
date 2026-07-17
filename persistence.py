"""Bounded SQLite persistence for operation previews, logs, and private notes."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in (
        "arguments_json",
        "sanitized_arguments_json",
        "target_json",
        "before_json",
        "expected_after_json",
        "after_json",
        "artifact_meta_json",
        "result_json",
    ):
        if key in result:
            raw = result.pop(key)
            result[key.removesuffix("_json")] = json.loads(raw) if raw else None
    if "reversible" in result:
        result["reversible"] = bool(result["reversible"])
    if "upstream_action_started" in result:
        result["upstream_action_started"] = bool(result["upstream_action_started"])
    return result


class PersistentStore:
    """Small per-process SQLite facade using short, independent transactions."""

    def __init__(
        self,
        path: str,
        *,
        retention_days: int = 90,
        max_operations: int = 1000,
        max_previews: int = 200,
    ) -> None:
        if not path:
            raise RuntimeError("MCP_STORAGE_PATH is required for previews, logs, and notes.")
        self.path = os.path.abspath(path)
        self.retention_days = retention_days
        self.max_operations = max_operations
        self.max_previews = max_previews
        self._init_lock = threading.Lock()
        self._initialized = False
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS previews (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        arguments_json TEXT NOT NULL,
                        sanitized_arguments_json TEXT NOT NULL,
                        target_json TEXT,
                        before_json TEXT,
                        expected_after_json TEXT,
                        state_hash TEXT NOT NULL,
                        risk_level TEXT NOT NULL,
                        reversible INTEGER NOT NULL,
                        artifact BLOB,
                        artifact_meta_json TEXT,
                        created_at TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        status TEXT NOT NULL,
                        operation_id TEXT,
                        result_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS previews_expiry_idx
                        ON previews(expires_at, status);

                    CREATE TABLE IF NOT EXISTS operations (
                        operation_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        sanitized_arguments_json TEXT NOT NULL,
                        target_json TEXT,
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        status TEXT NOT NULL,
                        before_json TEXT,
                        after_json TEXT,
                        reversible INTEGER NOT NULL,
                        undo_status TEXT NOT NULL DEFAULT 'not_requested',
                        undo_operation_id TEXT,
                        error_summary TEXT,
                        result_json TEXT,
                        parent_operation_id TEXT,
                        upstream_action_started INTEGER NOT NULL DEFAULT 0,
                        idempotency_key TEXT
                    );
                    CREATE INDEX IF NOT EXISTS operations_query_idx
                        ON operations(user_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS interaction_notes (
                        note_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        playlist_id INTEGER NOT NULL,
                        song_id INTEGER,
                        author TEXT NOT NULL,
                        content TEXT NOT NULL,
                        visibility TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        deleted_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS notes_query_idx
                        ON interaction_notes(user_id, playlist_id, song_id, author, created_at);
                    """
                )
                operation_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(operations)")
                }
                if "upstream_action_started" not in operation_columns:
                    connection.execute(
                        "ALTER TABLE operations ADD COLUMN upstream_action_started "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                    # Older schemas could not distinguish a crash before or after a
                    # mutating request. Treat their unfinished operations as having
                    # crossed the boundary so cleanup conservatively reports unknown.
                    connection.execute(
                        "UPDATE operations SET upstream_action_started=1 "
                        "WHERE status='started'"
                    )
                if "idempotency_key" not in operation_columns:
                    connection.execute(
                        "ALTER TABLE operations ADD COLUMN idempotency_key TEXT"
                    )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS operations_idempotency_idx "
                    "ON operations(user_id, operation, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
                )
            self._initialized = True

    @staticmethod
    def _json(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def cleanup(self) -> None:
        self.initialize()
        now = time.time()
        cutoff = datetime.fromtimestamp(
            now - self.retention_days * 86400, timezone.utc
        ).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE previews SET status='expired', artifact=NULL "
                "WHERE status='pending' AND expires_at < ?",
                (now,),
            )
            connection.execute(
                "UPDATE previews SET status='abandoned', artifact=NULL "
                "WHERE status='executing' AND expires_at < ?",
                (now,),
            )
            interrupted_cutoff = datetime.fromtimestamp(
                now - 3600, timezone.utc
            ).isoformat().replace("+00:00", "Z")
            connection.execute(
                "UPDATE operations SET status='unknown', completed_at=?, "
                "error_summary='Process stopped before the upstream result could be confirmed.' "
                "WHERE status='started' AND upstream_action_started=1 AND created_at < ?",
                (utc_now(), interrupted_cutoff),
            )
            connection.execute(
                "UPDATE operations SET status='failed_before_upstream', completed_at=?, "
                "error_summary='Process stopped before any upstream write was sent.' "
                "WHERE status='started' AND upstream_action_started=0 AND created_at < ?",
                (utc_now(), interrupted_cutoff),
            )
            connection.execute(
                "DELETE FROM previews WHERE expires_at < ?",
                (now - 86400,),
            )
            connection.execute(
                "DELETE FROM previews WHERE token_hash NOT IN "
                "(SELECT token_hash FROM previews ORDER BY created_at DESC LIMIT ?)",
                (self.max_previews,),
            )
            connection.execute("DELETE FROM operations WHERE created_at < ?", (cutoff,))
            connection.execute(
                "DELETE FROM operations WHERE operation_id NOT IN "
                "(SELECT operation_id FROM operations ORDER BY created_at DESC LIMIT ?)",
                (self.max_operations,),
            )
            connection.execute("COMMIT")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def save_preview(self, record: dict[str, Any]) -> None:
        self.cleanup()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO previews (
                    token_hash, user_id, operation, arguments_json,
                    sanitized_arguments_json, target_json, before_json,
                    expected_after_json, state_hash, risk_level, reversible,
                    artifact, artifact_meta_json, created_at, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    record["token_hash"],
                    str(record["user_id"]),
                    record["operation"],
                    self._json(record["arguments"]),
                    self._json(record["sanitized_arguments"]),
                    self._json(record.get("target")),
                    self._json(record.get("before_state")),
                    self._json(record.get("expected_after_state")),
                    record["state_hash"],
                    record["risk_level"],
                    int(bool(record["reversible"])),
                    record.get("artifact"),
                    self._json(record.get("artifact_meta")),
                    record["created_at"],
                    float(record["expires_at"]),
                ),
            )

    def claim_preview(
        self, token_hash: str, user_id: int, operation: str, arguments: Any
    ) -> tuple[str, dict[str, Any]]:
        self.initialize()
        arguments_json = self._json(arguments)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM previews WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if row is None or str(row["user_id"]) != str(user_id):
                connection.execute("ROLLBACK")
                raise ValueError("The preview token is invalid or belongs to another account.")
            if row["status"] == "consumed":
                connection.execute("COMMIT")
                decoded = _decode_row(row)
                assert decoded is not None
                return "consumed", decoded
            if row["status"] == "executing":
                connection.execute("ROLLBACK")
                raise ValueError("This preview is already being executed.")
            if row["status"] != "pending" or float(row["expires_at"]) < time.time():
                connection.execute(
                    "UPDATE previews SET status='expired', artifact=NULL WHERE token_hash=?",
                    (token_hash,),
                )
                connection.execute("COMMIT")
                raise ValueError("The preview token has expired; create a new preview.")
            if row["operation"] != operation or row["arguments_json"] != arguments_json:
                connection.execute("ROLLBACK")
                raise ValueError("The preview token does not match this operation and its arguments.")
            changed = connection.execute(
                "UPDATE previews SET status='executing' "
                "WHERE token_hash=? AND status='pending'",
                (token_hash,),
            ).rowcount
            if changed != 1:
                connection.execute("ROLLBACK")
                raise ValueError("This preview was claimed by another request.")
            connection.execute("COMMIT")
            decoded = _decode_row(row)
            assert decoded is not None
            decoded["status"] = "executing"
            return "claimed", decoded

    def finish_preview(
        self,
        token_hash: str,
        status: str,
        *,
        operation_id: str | None = None,
        result: Any = None,
    ) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "UPDATE previews SET status=?, operation_id=?, result_json=?, artifact=NULL "
                "WHERE token_hash=?",
                (status, operation_id, self._json(result), token_hash),
            )

    def release_preview(self, token_hash: str, operation_id: str, result: Any) -> str:
        """Return a claimed preview to pending when no mutating action began."""
        self.initialize()
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at, status FROM previews WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if row is None or row["status"] != "executing":
                return "unchanged"
            status = "pending" if float(row["expires_at"]) >= now else "expired"
            connection.execute(
                "UPDATE previews SET status=?, operation_id=?, result_json=?, "
                "artifact=CASE WHEN ?='pending' THEN artifact ELSE NULL END "
                "WHERE token_hash=? AND status='executing'",
                (status, operation_id, self._json(result), status, token_hash),
            )
            return status

    def start_operation(self, record: dict[str, Any]) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO operations (
                        operation_id, user_id, operation, sanitized_arguments_json,
                        target_json, created_at, status, before_json, reversible,
                        parent_operation_id, upstream_action_started, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, 0, ?)
                    """,
                    (
                        record["operation_id"],
                        str(record["user_id"]),
                        record["operation"],
                        self._json(record["sanitized_arguments"]),
                        self._json(record.get("target")),
                        record["created_at"],
                        self._json(record.get("before_state")),
                        int(bool(record["reversible"])),
                        record.get("parent_operation_id"),
                        record.get("idempotency_key"),
                    ),
                )
        except sqlite3.IntegrityError:
            if record.get("idempotency_key") is not None:
                return False
            raise
        return True

    def mark_upstream_action_started(self, operation_id: str) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "UPDATE operations SET upstream_action_started=1 WHERE operation_id=?",
                (operation_id,),
            )

    def get_operation_by_idempotency(
        self, user_id: int, operation: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE user_id=? AND operation=? "
                "AND idempotency_key=?",
                (str(user_id), operation, idempotency_key),
            ).fetchone()
        return _decode_row(row)

    def finish_operation(
        self,
        operation_id: str,
        *,
        status: str,
        after_state: Any,
        result: Any = None,
        error_summary: str | None = None,
    ) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE operations
                SET completed_at=?, status=?, after_json=?, result_json=?, error_summary=?
                WHERE operation_id=?
                """,
                (
                    utc_now(),
                    status,
                    self._json(after_state),
                    self._json(result),
                    error_summary,
                    operation_id,
                ),
            )
        self.cleanup()

    def get_operation(self, operation_id: str, user_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id=? AND user_id=?",
                (operation_id, str(user_id)),
            ).fetchone()
        return _decode_row(row)

    def set_undo_status(
        self, operation_id: str, status: str, undo_operation_id: str | None = None
    ) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "UPDATE operations SET undo_status=?, undo_operation_id=? WHERE operation_id=?",
                (status, undo_operation_id, operation_id),
            )

    def query_operations(
        self,
        user_id: int,
        *,
        limit: int,
        offset: int,
        operation: str | None = None,
        status: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> list[dict[str, Any]]:
        self.cleanup()
        clauses = ["user_id=?"]
        values: list[Any] = [str(user_id)]
        for column, value in (
            ("operation", operation),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        if created_after:
            clauses.append("created_at>=?")
            values.append(created_after)
        if created_before:
            clauses.append("created_at<=?")
            values.append(created_before)
        values.extend([limit, offset])
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operations WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [_decode_row(row) for row in rows if row is not None]

    def create_note(self, note: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO interaction_notes (
                    note_id, user_id, playlist_id, song_id, author, content,
                    visibility, created_at, updated_at, version, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
                """,
                (
                    note["note_id"],
                    str(note["user_id"]),
                    note["playlist_id"],
                    note.get("song_id"),
                    note["author"],
                    note["content"],
                    note["visibility"],
                    note["created_at"],
                    note["created_at"],
                ),
            )
        result = self.get_note(note["note_id"], int(note["user_id"]), include_deleted=True)
        assert result is not None
        return result

    def get_note(
        self, note_id: str, user_id: int, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        self.initialize()
        sql = "SELECT * FROM interaction_notes WHERE note_id=? AND user_id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(sql, (note_id, str(user_id))).fetchone()
        return dict(row) if row else None

    def update_note(
        self,
        note_id: str,
        user_id: int,
        version: int,
        *,
        author: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        assignments = ["updated_at=?", "version=version+1"]
        values: list[Any] = [utc_now()]
        if author is not None:
            assignments.append("author=?")
            values.append(author)
        if content is not None:
            assignments.append("content=?")
            values.append(content)
        values.extend([note_id, str(user_id), version])
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE interaction_notes SET "
                + ", ".join(assignments)
                + " WHERE note_id=? AND user_id=? AND version=? AND deleted_at IS NULL",
                values,
            ).rowcount
        if changed != 1:
            raise ValueError("The note changed concurrently, was deleted, or does not exist.")
        result = self.get_note(note_id, user_id, include_deleted=True)
        assert result is not None
        return result

    def soft_delete_note(self, note_id: str, user_id: int, version: int) -> dict[str, Any]:
        self.initialize()
        now = utc_now()
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE interaction_notes SET deleted_at=?, updated_at=?, version=version+1 "
                "WHERE note_id=? AND user_id=? AND version=? AND deleted_at IS NULL",
                (now, now, note_id, str(user_id), version),
            ).rowcount
        if changed != 1:
            raise ValueError("The note changed concurrently, was deleted, or does not exist.")
        result = self.get_note(note_id, user_id, include_deleted=True)
        assert result is not None
        return result

    def restore_note_snapshot(
        self, snapshot: dict[str, Any], user_id: int, expected_version: int
    ) -> dict[str, Any]:
        self.initialize()
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE interaction_notes
                SET author=?, content=?, visibility=?, deleted_at=?, updated_at=?, version=version+1
                WHERE note_id=? AND user_id=? AND version=?
                """,
                (
                    snapshot["author"],
                    snapshot["content"],
                    snapshot["visibility"],
                    snapshot.get("deleted_at"),
                    utc_now(),
                    snapshot["note_id"],
                    str(user_id),
                    expected_version,
                ),
            ).rowcount
        if changed != 1:
            raise ValueError("The note changed after the operation and cannot be restored safely.")
        result = self.get_note(snapshot["note_id"], user_id, include_deleted=True)
        assert result is not None
        return result

    def list_notes(
        self,
        user_id: int,
        playlist_id: int,
        *,
        song_id: int | None,
        author: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses = ["user_id=?", "playlist_id=?", "deleted_at IS NULL"]
        values: list[Any] = [str(user_id), playlist_id]
        if song_id is not None:
            clauses.append("song_id=?")
            values.append(song_id)
        if author is not None:
            clauses.append("author=?")
            values.append(author)
        values.extend([limit, offset])
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM interaction_notes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at ASC LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [dict(row) for row in rows]
