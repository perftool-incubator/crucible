"""Durable SQLite storage for MCP jobs."""

import hashlib
import json
import fcntl
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import ACTIVE_JOB_STATES, IndexedQueryStatus, Job, JobState, ResultStatus


class JobError(RuntimeError):
    """Base class for durable job-store errors."""


class JobConflictError(JobError):
    """Raised when an idempotency key is reused for another request."""


class JobNotFoundError(JobError):
    """Raised when a requested job does not exist."""


class PlanHandleCapacityError(JobError):
    """Raised when the bounded live plan-handle set is full."""


class PlanHandleExpiredError(JobError):
    """Raised when a caller presents its own expired plan handle."""


@dataclass(frozen=True)
class PlanHandle:
    """Bounded metadata that binds an inspected plan to its submitted input."""

    handle: str
    caller_fingerprint: str
    input_digest: str
    planner_contract: str
    component_versions: dict[str, str]
    plan_fingerprint: str
    effective_limits: dict[str, int]
    created_at: str
    expires_at: str


_TRANSITIONS = {
    JobState.QUEUED: {
        JobState.STARTING,
        JobState.FAILED,
        JobState.RECOVERY_REQUIRED,
        JobState.UNKNOWN_AFTER_CRASH,
    },
    JobState.STARTING: {
        JobState.RUNNING,
        JobState.POSTPROCESSING,
        JobState.INDEXING,
        JobState.FAILED,
        JobState.RECOVERY_REQUIRED,
        JobState.UNKNOWN_AFTER_CRASH,
    },
    JobState.RUNNING: {
        JobState.POSTPROCESSING,
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.RECOVERY_REQUIRED,
        JobState.UNKNOWN_AFTER_CRASH,
    },
    JobState.POSTPROCESSING: {
        JobState.INDEXING,
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.RECOVERY_REQUIRED,
    },
    JobState.INDEXING: {JobState.COMPLETED, JobState.FAILED, JobState.RECOVERY_REQUIRED},
    JobState.RECOVERY_REQUIRED: {JobState.FAILED},
    JobState.UNKNOWN_AFTER_CRASH: {JobState.RECOVERY_REQUIRED, JobState.FAILED},
    JobState.COMPLETED: set(),
    JobState.FAILED: set(),
}


def request_hash(request: Any) -> str:
    """Hash canonical JSON so retries can be compared deterministically."""

    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


PLAN_HANDLE_TTL = timedelta(hours=1)
PLAN_HANDLE_EXPIRY_RETENTION = timedelta(hours=24)
MAX_LIVE_PLAN_HANDLES = 1000
MAX_PLAN_HANDLE_METADATA_BYTES = 65536


class JobStore:
    """Single-process job store with SQLite transaction boundaries.

    The MCP supervisor owns one store instance. SQLite's write transaction
    lock prevents concurrent writers, while the schema's unique idempotency
    key makes duplicate submissions safe even across service restarts.
    """

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lifecycle_lock_path = Path(f"{self.database_path}.lifecycle.lock")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path, timeout=10, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 10000")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def lifecycle_lock(self, exclusive: bool = False) -> Iterator[None]:
        """Coordinate MCP requests with service-level maintenance operations.

        The service-control shell code takes the same exclusive lock before
        stopping MCP or rotating its credentials.  Tool requests hold a
        shared lock for their full duration, so a maintenance operation cannot
        pass its active-job check while a submission is still in flight.
        """

        self.lifecycle_lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lifecycle_lock_path.open("a+") as lock_file:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_file.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _migrate(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                )
                """
            )
            version = connection.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if version is None:
                connection.execute("INSERT INTO schema_version(version) VALUES (1)")
                connection.execute(
                    """
                    CREATE TABLE jobs (
                        mcp_job_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_hash TEXT NOT NULL,
                        operation TEXT NOT NULL DEFAULT 'run',
                        plan_digest TEXT,
                        plan_summary TEXT,
                        supervision_directory TEXT,
                        state TEXT NOT NULL,
                        result_status TEXT NOT NULL,
                        indexed_query_status TEXT NOT NULL DEFAULT 'not_checked',
                        indexed_query_checked_at TEXT,
                        logger_session_id TEXT,
                        rickshaw_run_id TEXT,
                        cdm_run_id TEXT,
                        run_directory TEXT,
                        runner_pid INTEGER,
                        runner_container_id TEXT,
                        supervisor_container_name TEXT,
                        supervisor_container_id TEXT,
                        exit_code INTEGER,
                        error_category TEXT,
                        error_message TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute("UPDATE schema_version SET version = 6")
            elif version[0] == 1:
                connection.execute("ALTER TABLE jobs ADD COLUMN operation TEXT NOT NULL DEFAULT 'run'")
                connection.execute("ALTER TABLE jobs ADD COLUMN supervision_directory TEXT")
                connection.execute("ALTER TABLE jobs ADD COLUMN plan_digest TEXT")
                connection.execute("ALTER TABLE jobs ADD COLUMN plan_summary TEXT")
                connection.execute("ALTER TABLE jobs ADD COLUMN indexed_query_status TEXT NOT NULL DEFAULT 'not_checked'")
                connection.execute("ALTER TABLE jobs ADD COLUMN indexed_query_checked_at TEXT")
                connection.execute("UPDATE schema_version SET version = 5")
            elif version[0] == 2:
                connection.execute("ALTER TABLE jobs ADD COLUMN supervision_directory TEXT")
                connection.execute("ALTER TABLE jobs ADD COLUMN plan_digest TEXT")
                connection.execute("ALTER TABLE jobs ADD COLUMN plan_summary TEXT")
                connection.execute("ALTER TABLE jobs ADD COLUMN indexed_query_status TEXT NOT NULL DEFAULT 'not_checked'")
                connection.execute("ALTER TABLE jobs ADD COLUMN indexed_query_checked_at TEXT")
                connection.execute("UPDATE schema_version SET version = 5")
            elif version[0] == 3:
                connection.execute("ALTER TABLE jobs ADD COLUMN plan_digest TEXT")
                connection.execute("ALTER TABLE jobs ADD COLUMN plan_summary TEXT")
                connection.execute("ALTER TABLE jobs ADD COLUMN indexed_query_status TEXT NOT NULL DEFAULT 'not_checked'")
                connection.execute("ALTER TABLE jobs ADD COLUMN indexed_query_checked_at TEXT")
                connection.execute("UPDATE schema_version SET version = 5")
            elif version[0] == 4:
                connection.execute("ALTER TABLE jobs ADD COLUMN indexed_query_status TEXT NOT NULL DEFAULT 'not_checked'")
                connection.execute("ALTER TABLE jobs ADD COLUMN indexed_query_checked_at TEXT")
                connection.execute("UPDATE schema_version SET version = 5")
            elif version[0] not in {5, 6, 7, 8}:
                raise JobError(f"unsupported MCP job database schema: {version[0]}")
            current_version = connection.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()[0]
            if current_version == 5:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN supervisor_container_name TEXT"
                )
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN supervisor_container_id TEXT"
                )
                connection.execute("UPDATE schema_version SET version = 6")
                current_version = 6
            elif current_version not in {6, 7, 8}:
                raise JobError(f"unsupported MCP job database schema: {current_version}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_cdm_run_id_idx ON jobs(cdm_run_id)"
            )
            if current_version == 6:
                connection.execute(
                    """
                    CREATE TABLE plan_handles (
                        handle TEXT PRIMARY KEY,
                        caller_fingerprint TEXT NOT NULL,
                        input_digest TEXT NOT NULL,
                        planner_contract TEXT NOT NULL,
                        component_versions TEXT NOT NULL,
                        plan_fingerprint TEXT NOT NULL,
                        effective_limits TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX plan_handles_reuse_idx ON plan_handles("
                    "caller_fingerprint, input_digest, planner_contract)"
                )
                connection.execute("UPDATE schema_version SET version = 7")
                current_version = 7
            if current_version == 7:
                connection.execute(
                    """
                    CREATE TABLE expired_plan_handles (
                        handle TEXT PRIMARY KEY,
                        caller_fingerprint TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        cleaned_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX expired_plan_handles_cleaned_at_idx "
                    "ON expired_plan_handles(cleaned_at)"
                )
                connection.execute("UPDATE schema_version SET version = 8")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def create_or_get(
        self,
        idempotency_key: str,
        request: Any,
        operation: str = "run",
        *,
        plan_digest: str | None = None,
        plan_summary: dict[str, Any] | None = None,
    ) -> tuple[Job, bool]:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        hashed_request = request_hash(request)
        encoded_plan_summary = (
            json.dumps(plan_summary, separators=(",", ":"), sort_keys=True)
            if plan_summary is not None
            else None
        )
        now = _now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != hashed_request:
                    raise JobConflictError(
                        "idempotency key was already used for a different request"
                    )
                return self._row_to_job(existing), False

            job_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO jobs(
                    mcp_job_id, idempotency_key, request_hash, operation,
                    plan_digest, plan_summary, state, result_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    hashed_request,
                    operation,
                    plan_digest,
                    encoded_plan_summary,
                    JobState.QUEUED.value,
                    ResultStatus.NOT_AVAILABLE.value,
                    now,
                    now,
                ),
            )
            return self.get(job_id), True

    def get(self, job_id: str) -> Job:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE mcp_job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(f"unknown MCP job: {job_id}")
        return self._row_to_job(row)

    def get_by_idempotency_key(self, idempotency_key: str) -> Job | None:
        """Return an existing job before validating a retry's external inputs."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def create_or_get_plan_handle(
        self,
        *,
        caller_fingerprint: str,
        input_digest: str,
        planner_contract: str,
        component_versions: dict[str, str],
        plan_fingerprint: str,
        effective_limits: dict[str, int],
    ) -> tuple[PlanHandle, bool]:
        """Persist only bounded fingerprints and reuse an identical live handle."""

        if (
            not isinstance(component_versions, dict)
            or len(component_versions) > 16
            or any(
                not isinstance(key, str)
                or not key
                or len(key) > 128
                or not isinstance(value, str)
                or not value
                or len(value) > 256
                for key, value in component_versions.items()
            )
            or not isinstance(effective_limits, dict)
            or len(effective_limits) > 16
            or any(
                not isinstance(key, str)
                or not key
                or len(key) > 128
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or value > 1_048_576
                for key, value in effective_limits.items()
            )
        ):
            raise JobError("plan handle metadata has an invalid shape")
        encoded_versions = json.dumps(
            component_versions, separators=(",", ":"), sort_keys=True
        )
        encoded_limits = json.dumps(
            effective_limits, separators=(",", ":"), sort_keys=True
        )
        if (
            len(caller_fingerprint) != 64
            or not input_digest
            or len(input_digest) > 256
            or not planner_contract
            or len(planner_contract) > 128
            or len(plan_fingerprint) != 64
            or len(encoded_versions.encode("utf-8")) > MAX_PLAN_HANDLE_METADATA_BYTES
            or len(encoded_limits.encode("utf-8")) > 1024
        ):
            raise JobError("plan handle metadata exceeds its storage bounds")

        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat(timespec="seconds")
        expires_at = (now_value + PLAN_HANDLE_TTL).isoformat(timespec="seconds")
        with self._transaction() as connection:
            self._cleanup_expired_plan_handles(connection, now_value)
            candidates = connection.execute(
                "SELECT * FROM plan_handles WHERE caller_fingerprint = ? "
                "AND input_digest = ? AND planner_contract = ? ORDER BY created_at",
                (caller_fingerprint, input_digest, planner_contract),
            ).fetchall()
            for row in candidates:
                if (
                    row["component_versions"] == encoded_versions
                    and row["plan_fingerprint"] == plan_fingerprint
                    and row["effective_limits"] == encoded_limits
                ):
                    return self._row_to_plan_handle(row), False

            live_count = connection.execute(
                "SELECT COUNT(*) FROM plan_handles WHERE expires_at > ?", (now,)
            ).fetchone()[0]
            if live_count >= MAX_LIVE_PLAN_HANDLES:
                raise PlanHandleCapacityError("live plan-handle capacity is full")

            handle = secrets.token_urlsafe(32)
            connection.execute(
                """
                INSERT INTO plan_handles(
                    handle, caller_fingerprint, input_digest, planner_contract,
                    component_versions, plan_fingerprint, effective_limits,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handle,
                    caller_fingerprint,
                    input_digest,
                    planner_contract,
                    encoded_versions,
                    plan_fingerprint,
                    encoded_limits,
                    now,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM plan_handles WHERE handle = ?", (handle,)
            ).fetchone()
            return self._row_to_plan_handle(row), True

    def get_plan_handle(
        self, handle: str, caller_fingerprint: str
    ) -> PlanHandle | None:
        """Return an owned live handle, hiding unknown and foreign handles."""

        now_value = datetime.now(timezone.utc)
        expired = False
        result: PlanHandle | None = None
        with self._transaction() as connection:
            self._cleanup_expired_plan_handles(connection, now_value)
            row = connection.execute(
                "SELECT * FROM plan_handles WHERE handle = ?", (handle,)
            ).fetchone()
            if row is not None and row["caller_fingerprint"] == caller_fingerprint:
                result = self._row_to_plan_handle(row)
            else:
                tombstone = connection.execute(
                    "SELECT caller_fingerprint FROM expired_plan_handles WHERE handle = ?",
                    (handle,),
                ).fetchone()
                expired = (
                    tombstone is not None
                    and tombstone["caller_fingerprint"] == caller_fingerprint
                )
        if expired:
            raise PlanHandleExpiredError("plan handle has expired")
        return result

    @staticmethod
    def _cleanup_expired_plan_handles(
        connection: sqlite3.Connection, now: datetime
    ) -> None:
        """Free live capacity while retaining a bounded expired-handle signal."""

        now_text = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        retention_cutoff = (now - PLAN_HANDLE_EXPIRY_RETENTION).astimezone(
            timezone.utc
        ).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT OR REPLACE INTO expired_plan_handles(
                handle, caller_fingerprint, expires_at, cleaned_at
            )
            SELECT handle, caller_fingerprint, expires_at, ?
            FROM plan_handles
            WHERE expires_at <= ?
            """,
            (now_text, now_text),
        )
        connection.execute("DELETE FROM plan_handles WHERE expires_at <= ?", (now_text,))
        connection.execute(
            "DELETE FROM expired_plan_handles WHERE cleaned_at <= ?",
            (retention_cutoff,),
        )

    @staticmethod
    def _row_to_plan_handle(row: sqlite3.Row) -> PlanHandle:
        try:
            component_versions = json.loads(row["component_versions"])
            effective_limits = json.loads(row["effective_limits"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise JobError("stored plan-handle metadata is invalid JSON") from exc
        if not isinstance(component_versions, dict) or not isinstance(effective_limits, dict):
            raise JobError("stored plan-handle metadata has an invalid shape")
        return PlanHandle(
            handle=row["handle"],
            caller_fingerprint=row["caller_fingerprint"],
            input_digest=row["input_digest"],
            planner_contract=row["planner_contract"],
            component_versions=component_versions,
            plan_fingerprint=row["plan_fingerprint"],
            effective_limits=effective_limits,
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def update_indexed_query_status(
        self, cdm_run_id: str, status: IndexedQueryStatus
    ) -> int:
        """Record the latest indexed-query readiness observation for a CDM run."""

        if not cdm_run_id:
            return 0
        status = IndexedQueryStatus(status)
        checked_at = _now()
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET indexed_query_status = ?, indexed_query_checked_at = ?, "
                "updated_at = ? WHERE cdm_run_id = ?",
                (status.value, checked_at, checked_at, cdm_run_id),
            )
            return cursor.rowcount

    def list_active(
        self,
        limit: int | None = None,
        after: tuple[str, str] | None = None,
    ) -> list[Job]:
        if limit is not None and limit < 1:
            raise ValueError("active job pagination limit must be positive")
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        conditions = [f"state IN ({placeholders})"]
        parameters: tuple[Any, ...] = tuple(state.value for state in ACTIVE_JOB_STATES)
        if after is not None:
            conditions.append("(created_at > ? OR (created_at = ? AND mcp_job_id > ?))")
            parameters += (after[0], after[0], after[1])
        query = (
            f"SELECT * FROM jobs WHERE {' AND '.join(conditions)} "
            "ORDER BY created_at, mcp_job_id"
        )
        if limit is not None:
            query += " LIMIT ?"
            parameters += (limit,)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._row_to_job(row) for row in rows]

    def transition(self, job_id: str, state: JobState, **updates: Any) -> Job:
        allowed_columns = {
            "result_status", "logger_session_id", "rickshaw_run_id", "cdm_run_id",
            "run_directory", "runner_pid", "runner_container_id", "exit_code",
            "supervisor_container_name", "supervisor_container_id",
            "error_category", "error_message", "supervision_directory",
            "plan_digest", "plan_summary",
        }
        unknown = set(updates) - allowed_columns
        if unknown:
            raise ValueError(f"unknown job fields: {', '.join(sorted(unknown))}")
        if "plan_summary" in updates:
            updates["plan_summary"] = json.dumps(
                updates["plan_summary"], separators=(",", ":"), sort_keys=True
            )
        updates["state"] = state.value
        updates["updated_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in updates)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE mcp_job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFoundError(f"unknown MCP job: {job_id}")
            current = self._row_to_job(row)
            if state != current.state and state not in _TRANSITIONS[current.state]:
                raise JobError(f"invalid job transition: {current.state.value} -> {state.value}")
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE mcp_job_id = ?",
                tuple(updates.values()) + (job_id,),
            )
        return self.get(job_id)

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        values = dict(row)
        values["state"] = JobState(values["state"])
        values["result_status"] = ResultStatus(values["result_status"])
        values["indexed_query_status"] = IndexedQueryStatus(
            values["indexed_query_status"]
        )
        if values.get("plan_summary") is not None:
            try:
                values["plan_summary"] = json.loads(values["plan_summary"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise JobError("stored plan summary is invalid JSON") from exc
        return Job(**values)
