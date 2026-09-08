"""Durable SQLite storage for MCP jobs."""

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import ACTIVE_JOB_STATES, Job, JobState, ResultStatus


class JobError(RuntimeError):
    """Base class for durable job-store errors."""


class JobConflictError(JobError):
    """Raised when an idempotency key is reused for another request."""


class JobNotFoundError(JobError):
    """Raised when a requested job does not exist."""


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


class JobStore:
    """Single-process job store with SQLite transaction boundaries.

    The MCP supervisor owns one store instance. SQLite's write transaction
    lock prevents concurrent writers, while the schema's unique idempotency
    key makes duplicate submissions safe even across service restarts.
    """

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
                        state TEXT NOT NULL,
                        result_status TEXT NOT NULL,
                        logger_session_id TEXT,
                        rickshaw_run_id TEXT,
                        cdm_run_id TEXT,
                        run_directory TEXT,
                        runner_pid INTEGER,
                        runner_container_id TEXT,
                        exit_code INTEGER,
                        error_category TEXT,
                        error_message TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
            elif version[0] != 1:
                raise JobError(f"unsupported MCP job database schema: {version[0]}")

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

    def create_or_get(self, idempotency_key: str, request: Any) -> tuple[Job, bool]:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        hashed_request = request_hash(request)
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
                    mcp_job_id, idempotency_key, request_hash, state, result_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    hashed_request,
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

    def list_active(self) -> list[Job]:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at",
                tuple(state.value for state in ACTIVE_JOB_STATES),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def transition(self, job_id: str, state: JobState, **updates: Any) -> Job:
        allowed_columns = {
            "result_status", "logger_session_id", "rickshaw_run_id", "cdm_run_id",
            "run_directory", "runner_pid", "runner_container_id", "exit_code",
            "error_category", "error_message",
        }
        unknown = set(updates) - allowed_columns
        if unknown:
            raise ValueError(f"unknown job fields: {', '.join(sorted(unknown))}")
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
        return Job(**values)
