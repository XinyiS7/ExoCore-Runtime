"""Fresh v2 SQLite transport state and durable event journal."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import UUID

from exocore_runtime.contracts import (
    EffectiveResolution,
    GenerationSpec,
    ProcessExecutionOptions,
    RuntimeEvent,
    generation_identity as contract_generation_identity,
    system_instructions_sha256,
)
from exocore_runtime.errors import (
    ConflictError,
    NotFoundError,
    RetiredError,
    StateResetRequiredError,
)


SCHEMA_VERSION = 2
TERMINAL_REQUEST_STATES = frozenset({"completed", "failed", "cancelled", "indeterminate"})


@dataclass(frozen=True)
class GenerationRecord:
    binding_id: str
    identity_hash: str
    runtime_kind: str
    bootstrap_fingerprint: str
    system_instructions_sha256: str
    status: str
    provider_session_id: str | None
    activation_request_id: str | None
    bootstrap_sent: bool


@dataclass(frozen=True)
class RequestRecord:
    binding_id: str
    request_id: str
    payload_hash: str
    requested_model_id: str
    requested_thinking_level: str
    status: str
    owner_id: str | None
    resolution_status: str
    effective_provider_model_slug: str | None
    effective_effort: str | None
    resolver_policy_revision: str | None
    process_options: ProcessExecutionOptions | None
    last_sequence: int
    terminal_code: str | None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_REQUEST_STATES


class RuntimeStateStore:
    """One fresh v2 SQLite file is the sole durable transport fact source."""

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
        connection = self._connect()
        try:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if existing:
                if "runtime_meta" not in existing:
                    raise StateResetRequiredError("active state is not a v2 store")
                row = connection.execute(
                    "SELECT value FROM runtime_meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is None or row[0] != str(SCHEMA_VERSION):
                    raise StateResetRequiredError("active state schema is not v2")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generations (
                    binding_id TEXT PRIMARY KEY,
                    identity_hash TEXT NOT NULL,
                    runtime_kind TEXT NOT NULL,
                    bootstrap_fingerprint TEXT NOT NULL,
                    system_instructions_sha256 TEXT NOT NULL,
                    provider_session_id TEXT,
                    activation_request_id TEXT,
                    bootstrap_sent INTEGER NOT NULL DEFAULT 0 CHECK(bootstrap_sent IN (0, 1)),
                    status TEXT NOT NULL CHECK(status IN ('starting', 'active', 'retired', 'failed')),
                    retired_reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_provider_session_owner
                ON generations(runtime_kind, provider_session_id)
                WHERE provider_session_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS requests (
                    binding_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    requested_model_id TEXT NOT NULL,
                    requested_thinking_level TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'prepared', 'sent', 'completed', 'failed', 'cancelled', 'indeterminate'
                    )),
                    owner_id TEXT,
                    resolution_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(resolution_status IN ('pending', 'resolved', 'unsupported')),
                    effective_provider_model_slug TEXT,
                    effective_effort TEXT,
                    resolver_policy_revision TEXT,
                    process_options_json TEXT,
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
                    is_control INTEGER NOT NULL CHECK(is_control IN (0, 1)),
                    is_terminal INTEGER NOT NULL CHECK(is_terminal IN (0, 1)),
                    terminal_status TEXT CHECK(terminal_status IN (
                        'completed', 'failed', 'cancelled', 'indeterminate'
                    )),
                    bootstrap_consumed INTEGER CHECK(bootstrap_consumed IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(binding_id, request_id, sequence),
                    FOREIGN KEY(binding_id, request_id)
                        REFERENCES requests(binding_id, request_id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_terminal_per_request
                ON events(binding_id, request_id) WHERE is_terminal = 1;
                CREATE UNIQUE INDEX IF NOT EXISTS one_control_type_per_request
                ON events(binding_id, request_id, event_type) WHERE is_control = 1;
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO runtime_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute("PRAGMA user_version = 2")
            connection.execute("PRAGMA secure_delete = ON")
        finally:
            connection.close()

    @staticmethod
    def generation_identity(spec: GenerationSpec) -> str:
        return contract_generation_identity(spec)

    def ensure_generation(self, binding_id: str, spec: GenerationSpec) -> tuple[GenerationRecord, bool]:
        identity_hash = self.generation_identity(spec)
        instructions_hash = system_instructions_sha256(spec.system_instructions)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if row is not None:
                record = self._generation_from_row(row)
                if record.identity_hash != identity_hash:
                    raise ConflictError("generation identity is immutable")
                return record, False
            connection.execute(
                """
                INSERT INTO generations(
                    binding_id, identity_hash, runtime_kind, bootstrap_fingerprint,
                    system_instructions_sha256, status
                ) VALUES (?, ?, ?, ?, ?, 'starting')
                """,
                (
                    binding_id,
                    identity_hash,
                    spec.runtime_kind,
                    spec.bootstrap_fingerprint,
                    instructions_hash,
                ),
            )
            row = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            return self._generation_from_row(row), True

    def activate_generation(
        self,
        binding_id: str,
        provider_session_id: str,
        request_id: str,
    ) -> GenerationRecord:
        if not provider_session_id:
            raise ValueError("provider session id is required")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("generation not found")
            if row["status"] == "retired":
                raise RetiredError("generation is retired")
            if row["status"] == "active":
                if row["provider_session_id"] != provider_session_id:
                    raise ConflictError("active generation session conflicts")
                return self._generation_from_row(row)
            if row["status"] != "starting":
                raise ConflictError("generation is no longer starting")
            try:
                connection.execute(
                    """
                    UPDATE generations
                    SET status = 'active', provider_session_id = ?, activation_request_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE binding_id = ? AND status = 'starting'
                    """,
                    (provider_session_id, request_id, binding_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("provider session alias conflict") from exc
            updated = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?", (binding_id,)
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
                "SELECT * FROM generations WHERE binding_id = ?", (binding_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("generation not found")
        return self._generation_from_row(row)

    def retire_generation(self, binding_id: str, reason: str) -> tuple[GenerationRecord, bool]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM generations WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("generation not found")
            changed = row["status"] != "retired"
            if changed:
                connection.execute(
                    """
                    UPDATE generations SET status = 'retired', retired_reason = ?,
                        updated_at = CURRENT_TIMESTAMP WHERE binding_id = ?
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
                    "SELECT * FROM generations WHERE binding_id = ?", (binding_id,)
                ).fetchone()
            return self._generation_from_row(row), changed

    def terminalize_open_requests_for_shutdown(self) -> int:
        with self._transaction() as connection:
            return self._terminalize_open_requests(
                connection,
                prepared_code="shutdown_before_send",
                sent_code="indeterminate_on_shutdown",
            )

    def recover_after_restart(self) -> int:
        recovered = 0
        with self._transaction() as connection:
            connection.execute("UPDATE requests SET owner_id = NULL WHERE status = 'prepared'")
            rows = connection.execute(
                "SELECT * FROM requests WHERE status = 'sent' ORDER BY binding_id, request_id"
            ).fetchall()
            for row in rows:
                sequence = int(row["last_sequence"]) + 1
                self._insert_terminal_event(
                    connection,
                    binding_id=row["binding_id"],
                    request_id=row["request_id"],
                    sequence=sequence,
                    event_type="error",
                    payload_json=self._canonical_payload({"code": "indeterminate_after_restart"}),
                    terminal_status="indeterminate",
                )
                connection.execute(
                    """
                    UPDATE requests SET status = 'indeterminate',
                        terminal_code = 'indeterminate_after_restart', last_sequence = ?,
                        owner_id = NULL, updated_at = CURRENT_TIMESTAMP
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
        requested_model_id: str,
        requested_thinking_level: str,
        owner_id: str,
    ) -> tuple[RequestRecord, str]:
        with self._transaction() as connection:
            generation = connection.execute(
                "SELECT status FROM generations WHERE binding_id = ?", (binding_id,)
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
            if generation["status"] not in {"starting", "active"}:
                raise ConflictError("generation is not executable")
            connection.execute(
                """
                INSERT INTO requests(
                    binding_id, request_id, payload_hash, requested_model_id,
                    requested_thinking_level, status, owner_id
                ) VALUES (?, ?, ?, ?, ?, 'prepared', ?)
                """,
                (
                    binding_id,
                    request_id,
                    payload_hash,
                    requested_model_id,
                    requested_thinking_level,
                    owner_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
            return self._request_from_row(row), "owner"

    def freeze_resolution(
        self,
        binding_id: str,
        request_id: str,
        owner_id: str,
        resolution: EffectiveResolution,
    ) -> RequestRecord:
        options_json = resolution.process_options.model_dump_json()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("request not found")
            record = self._request_from_row(row)
            if record.resolution_status == "resolved":
                expected = (
                    resolution.provider_model_slug,
                    resolution.effort,
                    resolution.resolver_policy_revision,
                    resolution.process_options,
                )
                observed = (
                    record.effective_provider_model_slug,
                    record.effective_effort,
                    record.resolver_policy_revision,
                    record.process_options,
                )
                if observed != expected:
                    raise ConflictError("effective execution is immutable")
                return record
            if record.resolution_status != "pending" or record.owner_id != owner_id:
                raise ConflictError("request resolution is not owned")
            connection.execute(
                """
                UPDATE requests SET resolution_status = 'resolved',
                    effective_provider_model_slug = ?, effective_effort = ?,
                    resolver_policy_revision = ?, process_options_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND request_id = ? AND resolution_status = 'pending'
                    AND owner_id = ?
                """,
                (
                    resolution.provider_model_slug,
                    resolution.effort,
                    resolution.resolver_policy_revision,
                    options_json,
                    binding_id,
                    request_id,
                    owner_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
            return self._request_from_row(row)

    def mark_resolution_unsupported(
        self,
        binding_id: str,
        request_id: str,
        owner_id: str,
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE requests SET resolution_status = 'unsupported',
                    updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND request_id = ? AND resolution_status = 'pending'
                    AND owner_id = ?
                """,
                (binding_id, request_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("request resolution is not owned")

    def get_request(self, binding_id: str, request_id: str) -> RequestRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
        return self._request_from_row(row) if row is not None else None

    def append_control_event(
        self,
        binding_id: str,
        request_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> RuntimeEvent:
        if event_type not in {"generation_activated", "execution_resolved"}:
            raise ValueError("invalid runtime control event")
        payload_json = self._canonical_payload(payload)
        with self._transaction() as connection:
            request = connection.execute(
                "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
                (binding_id, request_id),
            ).fetchone()
            if request is None:
                raise NotFoundError("request not found")
            existing = connection.execute(
                """
                SELECT * FROM events WHERE binding_id = ? AND request_id = ?
                    AND event_type = ? AND is_control = 1
                """,
                (binding_id, request_id, event_type),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload_json:
                    raise ConflictError("runtime control truth conflicts")
                return self._event_from_row(existing)
            if request["status"] != "prepared":
                raise ConflictError("control truth must precede send boundary")
            sequence = int(request["last_sequence"]) + 1
            connection.execute(
                """
                INSERT INTO events(
                    binding_id, request_id, sequence, event_type, payload_json,
                    is_control, is_terminal
                ) VALUES (?, ?, ?, ?, ?, 1, 0)
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
            row = connection.execute(
                "SELECT * FROM events WHERE binding_id = ? AND request_id = ? AND sequence = ?",
                (binding_id, request_id, sequence),
            ).fetchone()
            return self._event_from_row(row)

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
                "SELECT status FROM generations WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if generation is None:
                raise NotFoundError("generation not found")
            if generation["status"] == "retired":
                raise RetiredError("generation is retired")
            if generation["status"] != "active":
                raise ConflictError("generation is not active")
            cursor = connection.execute(
                """
                UPDATE requests SET status = 'sent', updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND request_id = ? AND status = 'prepared'
                    AND owner_id = ? AND resolution_status = 'resolved'
                """,
                (binding_id, request_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("request send boundary is not ready")
            if consume_bootstrap:
                cursor = connection.execute(
                    """
                    UPDATE generations SET bootstrap_sent = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE binding_id = ? AND bootstrap_sent = 0
                    """,
                    (binding_id,),
                )
                if cursor.rowcount != 1:
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
                    binding_id, request_id, sequence, event_type, payload_json,
                    is_control, is_terminal
                ) VALUES (?, ?, ?, ?, ?, 0, 0)
                """,
                (binding_id, request_id, sequence, event_type, payload_json),
            )
            connection.execute(
                "UPDATE requests SET last_sequence = ? WHERE binding_id = ? AND request_id = ?",
                (sequence, binding_id, request_id),
            )
        return self._runtime_event(
            binding_id, request_id, sequence, event_type, json.loads(payload_json), False, None, None
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
            changed = not record.terminal
            if changed:
                sequence = record.last_sequence + 1
                self._insert_terminal_event(
                    connection,
                    binding_id=binding_id,
                    request_id=request_id,
                    sequence=sequence,
                    event_type=event_type,
                    payload_json=payload_json,
                    terminal_status=status,
                )
                connection.execute(
                    """
                    UPDATE requests SET status = ?, terminal_code = ?, last_sequence = ?,
                        owner_id = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE binding_id = ? AND request_id = ?
                    """,
                    (status, terminal_code, sequence, binding_id, request_id),
                )
            event_row = connection.execute(
                """
                SELECT * FROM events WHERE binding_id = ? AND request_id = ? AND is_terminal = 1
                """,
                (binding_id, request_id),
            ).fetchone()
            if event_row is None:
                raise ConflictError("terminal request is missing its durable event")
            return self._event_from_row(event_row), changed

    def read_events(self, binding_id: str, request_id: str) -> list[RuntimeEvent]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events WHERE binding_id = ? AND request_id = ? ORDER BY sequence
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
            f"SELECT * FROM requests {where} ORDER BY binding_id, request_id", parameters
        ).fetchall()
        for row in rows:
            was_sent = row["status"] == "sent"
            code = sent_code if was_sent else prepared_code
            status = "indeterminate" if was_sent else "failed"
            sequence = int(row["last_sequence"]) + 1
            cls._insert_terminal_event(
                connection,
                binding_id=row["binding_id"],
                request_id=row["request_id"],
                sequence=sequence,
                event_type="error",
                payload_json=cls._canonical_payload({"code": code}),
                terminal_status=status,
            )
            connection.execute(
                """
                UPDATE requests SET status = ?, terminal_code = ?, last_sequence = ?,
                    owner_id = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE binding_id = ? AND request_id = ?
                """,
                (status, code, sequence, row["binding_id"], row["request_id"]),
            )
        return len(rows)

    @staticmethod
    def _insert_terminal_event(
        connection: sqlite3.Connection,
        *,
        binding_id: str,
        request_id: str,
        sequence: int,
        event_type: str,
        payload_json: str,
        terminal_status: str,
    ) -> None:
        generation = connection.execute(
            "SELECT bootstrap_sent FROM generations WHERE binding_id = ?", (binding_id,)
        ).fetchone()
        if generation is None:
            raise NotFoundError("generation not found")
        connection.execute(
            """
            INSERT INTO events(
                binding_id, request_id, sequence, event_type, payload_json,
                is_control, is_terminal, terminal_status, bootstrap_consumed
            ) VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?)
            """,
            (
                binding_id,
                request_id,
                sequence,
                event_type,
                payload_json,
                terminal_status,
                int(bool(generation["bootstrap_sent"])),
            ),
        )

    @staticmethod
    def _canonical_payload(payload: dict[str, object]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _generation_from_row(row: sqlite3.Row) -> GenerationRecord:
        return GenerationRecord(
            binding_id=row["binding_id"],
            identity_hash=row["identity_hash"],
            runtime_kind=row["runtime_kind"],
            bootstrap_fingerprint=row["bootstrap_fingerprint"],
            system_instructions_sha256=row["system_instructions_sha256"],
            status=row["status"],
            provider_session_id=row["provider_session_id"],
            activation_request_id=row["activation_request_id"],
            bootstrap_sent=bool(row["bootstrap_sent"]),
        )

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> RequestRecord:
        options_json = row["process_options_json"]
        return RequestRecord(
            binding_id=row["binding_id"],
            request_id=row["request_id"],
            payload_hash=row["payload_hash"],
            requested_model_id=row["requested_model_id"],
            requested_thinking_level=row["requested_thinking_level"],
            status=row["status"],
            owner_id=row["owner_id"],
            resolution_status=row["resolution_status"],
            effective_provider_model_slug=row["effective_provider_model_slug"],
            effective_effort=row["effective_effort"],
            resolver_policy_revision=row["resolver_policy_revision"],
            process_options=(
                ProcessExecutionOptions.model_validate_json(options_json)
                if options_json is not None
                else None
            ),
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
        terminal_status: str | None,
        bootstrap_consumed: bool | None,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            binding_id=UUID(binding_id),
            request_id=UUID(request_id),
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            terminal=terminal,
            terminal_status=terminal_status,
            bootstrap_consumed=bootstrap_consumed,
        )

    def _event_from_row(self, row: sqlite3.Row) -> RuntimeEvent:
        terminal = bool(row["is_terminal"])
        bootstrap_value = row["bootstrap_consumed"]
        return self._runtime_event(
            row["binding_id"],
            row["request_id"],
            int(row["sequence"]),
            row["event_type"],
            json.loads(row["payload_json"]),
            terminal,
            row["terminal_status"],
            bool(bootstrap_value) if bootstrap_value is not None else None,
        )
