"""SQLite-backed durable transport state and normalized event journal."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import UUID

from exocore_runtime.contracts import (
    GenerationSpec,
    RuntimeEvent,
    generation_identity as contract_generation_identity,
)
from exocore_runtime.errors import ConflictError, NotFoundError, RetiredError


TERMINAL_REQUEST_STATES = frozenset({"completed", "failed", "cancelled", "indeterminate"})


@dataclass(frozen=True)
class GenerationRecord:
    binding_id: str
    identity_hash: str
    runtime_kind: str
    status: str
    provider_session_id: str | None
    provider_model_id: str
    bootstrap_sent: bool


@dataclass(frozen=True)
class RequestRecord:
    binding_id: str
    request_id: str
    payload_hash: str
    status: str
    owner_id: str | None
    last_sequence: int
    terminal_code: str | None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_REQUEST_STATES


class RuntimeStateStore:
    """One SQLite file is the sole durable fact source for Milestone A."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._read_connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS generations (
                    binding_id TEXT PRIMARY KEY,
                    identity_hash TEXT NOT NULL,
                    runtime_kind TEXT NOT NULL,
                    provider_model_id TEXT NOT NULL,
                    bootstrap_fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    provider_session_id TEXT,
                    bootstrap_sent INTEGER NOT NULL DEFAULT 0 CHECK(bootstrap_sent IN (0, 1)),
                    status TEXT NOT NULL CHECK(status IN ('starting', 'active', 'retired', 'failed')),
                    retired_reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS requests (
                    binding_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'prepared', 'sent', 'completed', 'failed', 'cancelled', 'indeterminate'
                    )),
                    owner_id TEXT,
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    terminal_code TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(binding_id, request_id),
                    FOREIGN KEY(binding_id) REFERENCES generations(binding_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS events (
                    binding_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    is_terminal INTEGER NOT NULL CHECK(is_terminal IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(binding_id, request_id, sequence),
                    FOREIGN KEY(binding_id, request_id)
                        REFERENCES requests(binding_id, request_id) ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_terminal_per_request
                ON events(binding_id, request_id) WHERE is_terminal = 1;
                """
            )
            generation_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(generations)").fetchall()
            }
            if "bootstrap_sent" not in generation_columns:
                connection.execute(
                    "ALTER TABLE generations ADD COLUMN bootstrap_sent INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute("PRAGMA secure_delete = ON")
            if self._migrate_legacy_generation_identities(connection):
                connection.execute("VACUUM")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @staticmethod
    def _identity_payload(spec_data: dict[str, object]) -> dict[str, object]:
        instructions = spec_data.pop("system_instructions", None)
        spec_data["system_instructions_sha256"] = (
            hashlib.sha256(str(instructions).strip().encode("utf-8")).hexdigest()
            if instructions is not None
            else None
        )
        return spec_data

    @staticmethod
    def generation_identity(spec: GenerationSpec) -> str:
        return contract_generation_identity(spec)

    @classmethod
    def _migrate_legacy_generation_identities(cls, connection: sqlite3.Connection) -> bool:
        rows = connection.execute(
            "SELECT binding_id, identity_hash FROM generations"
        ).fetchall()
        migrated = False
        for row in rows:
            existing = row["identity_hash"]
            if len(existing) == 64 and all(character in "0123456789abcdef" for character in existing):
                continue
            try:
                legacy = json.loads(existing)
                if not isinstance(legacy, dict):
                    raise TypeError("legacy generation identity is not an object")
                canonical = json.dumps(
                    cls._identity_payload(legacy),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            except (TypeError, ValueError, json.JSONDecodeError):
                canonical = existing.encode("utf-8")
            connection.execute(
                "UPDATE generations SET identity_hash = ? WHERE binding_id = ?",
                (hashlib.sha256(canonical).hexdigest(), row["binding_id"]),
            )
            migrated = True
        return migrated

    def ensure_generation(
        self,
        binding_id: str,
        spec: GenerationSpec,
    ) -> tuple[GenerationRecord, bool]:
        identity_hash = self.generation_identity(spec)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is not None:
                record = self._generation_from_row(row)
                if record.identity_hash != identity_hash:
                    raise ConflictError("generation identity is immutable")
                return record, False
            connection.execute(
                """
                INSERT INTO generations(
                    binding_id, identity_hash, runtime_kind, provider_model_id,
                    bootstrap_fingerprint, config_fingerprint, provider_session_id,
                    bootstrap_sent, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'starting')
                """,
                (
                    binding_id,
                    identity_hash,
                    spec.runtime_kind,
                    spec.provider_model_id,
                    spec.bootstrap_fingerprint,
                    spec.config_fingerprint,
                    spec.provider_session_id,
                    0,
                ),
            )
            row = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            return self._generation_from_row(row), True

    def activate_generation(self, binding_id: str, provider_session_id: str) -> GenerationRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM generations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("generation not found")
            if row["status"] == "retired":
                raise RetiredError("generation is retired")
            if row["status"] != "starting":
                raise ConflictError("generation is no longer starting")
            connection.execute(
                """
                UPDATE generations
                SET status = 'active', provider_session_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND status = 'starting'
                """,
                (provider_session_id, binding_id),
            )
            updated = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            return self._generation_from_row(updated)

    def fail_generation(self, binding_id: str, *, include_active: bool = False) -> None:
        statuses = ("starting", "active") if include_active else ("starting",)
        placeholders = ",".join("?" for _ in statuses)
        with self._transaction() as connection:
            connection.execute(
                f"""
                UPDATE generations SET status = 'failed', updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND status IN ({placeholders})
                """,
                (binding_id, *statuses),
            )

    def get_generation(self, binding_id: str) -> GenerationRecord:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("generation not found")
        return self._generation_from_row(row)

    def retire_generation(self, binding_id: str, reason: str) -> tuple[GenerationRecord, bool]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("generation not found")
            changed = row["status"] != "retired"
            if changed:
                connection.execute(
                    """
                    UPDATE generations
                    SET status = 'retired', retired_reason = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE binding_id = ?
                    """,
                    (reason, binding_id),
                )
                self._terminalize_open_requests(
                    connection,
                    binding_id=binding_id,
                    prepared_code="retired_before_send",
                    sent_code="indeterminate_on_retire",
                )
                row = connection.execute(
                    "SELECT * FROM generations WHERE binding_id = ?",
                    (binding_id,),
                ).fetchone()
            return self._generation_from_row(row), changed

    def terminalize_open_requests_for_shutdown(self) -> int:
        with self._transaction() as connection:
            return self._terminalize_open_requests(
                connection,
                prepared_code="shutdown_before_send",
                sent_code="indeterminate_on_shutdown",
            )

    def fail_inherited_starting_generations(self) -> int:
        """Make ambiguous generation acquisition from an earlier lifecycle explicit."""
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE generations
                SET status = 'failed', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'starting'
                """
            )
            return cursor.rowcount

    def recover_after_restart(self) -> int:
        """Conservatively terminalize sent requests and release prepared claims."""
        recovered = 0
        with self._transaction() as connection:
            connection.execute(
                "UPDATE requests SET owner_id = NULL WHERE status = 'prepared'"
            )
            rows = connection.execute(
                "SELECT * FROM requests WHERE status = 'sent' ORDER BY binding_id, request_id"
            ).fetchall()
            for row in rows:
                sequence = int(row["last_sequence"]) + 1
                payload = json.dumps(
                    {"code": "indeterminate_after_restart"},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        binding_id, request_id, sequence, event_type, payload_json, is_terminal
                    ) VALUES (?, ?, ?, 'error', ?, 1)
                    """,
                    (row["binding_id"], row["request_id"], sequence, payload),
                )
                connection.execute(
                    """
                    UPDATE requests
                    SET status = 'indeterminate', terminal_code = 'indeterminate_after_restart',
                        last_sequence = ?, owner_id = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE binding_id = ? AND request_id = ?
                    """,
                    (sequence, row["binding_id"], row["request_id"]),
                )
                recovered += 1
        return recovered

    def claim_request(
        self,
        binding_id: str,
        request_id: str,
        payload_hash: str,
        owner_id: str,
    ) -> tuple[RequestRecord, str]:
        with self._transaction() as connection:
            generation = connection.execute(
                "SELECT status FROM generations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if generation is None:
                raise NotFoundError("generation not found")
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
            if row is not None:
                record = self._request_from_row(row)
                if record.payload_hash != payload_hash:
                    raise ConflictError("request identity is immutable")
                if record.terminal:
                    return record, "replay"
                if record.status == "prepared" and record.owner_id is None:
                    connection.execute(
                        """
                        UPDATE requests SET owner_id = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE binding_id = ? AND request_id = ? AND owner_id IS NULL
                        """,
                        (owner_id, binding_id, request_id),
                    )
                    row = connection.execute(
                        "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                        (binding_id, request_id),
                    ).fetchone()
                    return self._request_from_row(row), "owner"
                return record, "observer"
            if generation["status"] == "retired":
                raise RetiredError("generation is retired")
            if generation["status"] != "active":
                raise ConflictError("generation is not active")
            connection.execute(
                """
                INSERT INTO requests(binding_id, request_id, payload_hash, status, owner_id)
                VALUES (?, ?, ?, 'prepared', ?)
                """,
                (binding_id, request_id, payload_hash, owner_id),
            )
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
            return self._request_from_row(row), "owner"

    def get_request(self, binding_id: str, request_id: str) -> RequestRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
        return self._request_from_row(row) if row is not None else None

    def mark_sent(
        self,
        binding_id: str,
        request_id: str,
        owner_id: str,
        *,
        consume_bootstrap: bool = False,
    ) -> RequestRecord:
        with self._transaction() as connection:
            generation = connection.execute(
                "SELECT status FROM generations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if generation is None:
                raise NotFoundError("generation not found")
            if generation["status"] == "retired":
                raise RetiredError("generation is retired")
            if generation["status"] != "active":
                raise ConflictError("generation is not active")
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = 'sent', updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND request_id = ? AND status = 'prepared' AND owner_id = ?
                """,
                (binding_id, request_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("request send boundary is already owned")
            if consume_bootstrap:
                generation_cursor = connection.execute(
                    """
                    UPDATE generations
                    SET bootstrap_sent = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE binding_id = ? AND bootstrap_sent = 0
                    """,
                    (binding_id,),
                )
                if generation_cursor.rowcount != 1:
                    raise ConflictError("generation bootstrap was already consumed")
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
            return self._request_from_row(row)

    def append_event(
        self,
        binding_id: str,
        request_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> RuntimeEvent | None:
        payload_json = self._canonical_payload(payload)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("request not found")
            record = self._request_from_row(row)
            if record.terminal:
                return None
            if record.status != "sent":
                raise ConflictError("request has not crossed send boundary")
            sequence = record.last_sequence + 1
            connection.execute(
                """
                INSERT INTO events(
                    binding_id, request_id, sequence, event_type, payload_json, is_terminal
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (binding_id, request_id, sequence, event_type, payload_json),
            )
            connection.execute(
                """
                UPDATE requests SET last_sequence = ?, updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND request_id = ?
                """,
                (sequence, binding_id, request_id),
            )
        return self._runtime_event(
            binding_id,
            request_id,
            sequence,
            event_type,
            json.loads(payload_json),
            False,
        )

    def append_terminal(
        self,
        binding_id: str,
        request_id: str,
        event_type: str,
        payload: dict[str, object],
        status: str,
        terminal_code: str,
    ) -> tuple[RuntimeEvent, bool]:
        if status not in TERMINAL_REQUEST_STATES:
            raise ValueError("invalid terminal request status")
        payload_json = self._canonical_payload(payload)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("request not found")
            record = self._request_from_row(row)
            if record.terminal:
                event_row = connection.execute(
                    """
                    SELECT * FROM events
                    WHERE binding_id = ? AND request_id = ? AND is_terminal = 1
                    """,
                    (binding_id, request_id),
                ).fetchone()
                return self._event_from_row(event_row), False
            sequence = record.last_sequence + 1
            connection.execute(
                """
                INSERT INTO events(
                    binding_id, request_id, sequence, event_type, payload_json, is_terminal
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                (binding_id, request_id, sequence, event_type, payload_json),
            )
            connection.execute(
                """
                UPDATE requests
                SET status = ?, terminal_code = ?, last_sequence = ?, owner_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND request_id = ?
                """,
                (status, terminal_code, sequence, binding_id, request_id),
            )
        return self._runtime_event(
            binding_id,
            request_id,
            sequence,
            event_type,
            json.loads(payload_json),
            True,
        ), True

    def read_events(self, binding_id: str, request_id: str) -> list[RuntimeEvent]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE binding_id = ? AND request_id = ? ORDER BY sequence
                """,
                (binding_id, request_id),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def terminal_count(self, binding_id: str, request_id: str) -> int:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM events
                WHERE binding_id = ? AND request_id = ? AND is_terminal = 1
                """,
                (binding_id, request_id),
            ).fetchone()
        return int(row["count"])

    @classmethod
    def _terminalize_open_requests(
        cls,
        connection: sqlite3.Connection,
        *,
        prepared_code: str,
        sent_code: str,
        binding_id: str | None = None,
    ) -> int:
        where = "WHERE status IN ('prepared', 'sent')"
        parameters: tuple[object, ...] = ()
        if binding_id is not None:
            where += " AND binding_id = ?"
            parameters = (binding_id,)
        rows = connection.execute(
            f"SELECT * FROM requests {where} ORDER BY binding_id, request_id",
            parameters,
        ).fetchall()
        for row in rows:
            was_sent = row["status"] == "sent"
            code = sent_code if was_sent else prepared_code
            status = "indeterminate" if was_sent else "failed"
            sequence = int(row["last_sequence"]) + 1
            payload = cls._canonical_payload({"code": code})
            connection.execute(
                """
                INSERT INTO events(
                    binding_id, request_id, sequence, event_type, payload_json, is_terminal
                ) VALUES (?, ?, ?, 'error', ?, 1)
                """,
                (row["binding_id"], row["request_id"], sequence, payload),
            )
            connection.execute(
                """
                UPDATE requests
                SET status = ?, terminal_code = ?, last_sequence = ?, owner_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND request_id = ?
                """,
                (status, code, sequence, row["binding_id"], row["request_id"]),
            )
        return len(rows)

    @staticmethod
    def _canonical_payload(payload: dict[str, object]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _generation_from_row(row: sqlite3.Row) -> GenerationRecord:
        return GenerationRecord(
            binding_id=row["binding_id"],
            identity_hash=row["identity_hash"],
            runtime_kind=row["runtime_kind"],
            status=row["status"],
            provider_session_id=row["provider_session_id"],
            provider_model_id=row["provider_model_id"],
            bootstrap_sent=bool(row["bootstrap_sent"]),
        )

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> RequestRecord:
        return RequestRecord(
            binding_id=row["binding_id"],
            request_id=row["request_id"],
            payload_hash=row["payload_hash"],
            status=row["status"],
            owner_id=row["owner_id"],
            last_sequence=int(row["last_sequence"]),
            terminal_code=row["terminal_code"],
        )

    @staticmethod
    def _runtime_event(
        binding_id: str,
        request_id: str,
        sequence: int,
        event_type: str,
        payload: dict[str, object],
        terminal: bool,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            binding_id=UUID(binding_id),
            request_id=UUID(request_id),
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            terminal=terminal,
        )

    def _event_from_row(self, row: sqlite3.Row) -> RuntimeEvent:
        return self._runtime_event(
            row["binding_id"],
            row["request_id"],
            int(row["sequence"]),
            row["event_type"],
            json.loads(row["payload_json"]),
            bool(row["is_terminal"]),
        )
