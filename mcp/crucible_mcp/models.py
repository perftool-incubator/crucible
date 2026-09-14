"""Version-one MCP domain models.

These models deliberately keep MCP job identity separate from the identifiers
created by Crucible, Rickshaw, and CDM.  Those systems assign their IDs at
different points in the run lifecycle and they are not interchangeable.
"""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional


class JobState(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    POSTPROCESSING = "postprocessing"
    INDEXING = "indexing"
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    UNKNOWN_AFTER_CRASH = "unknown_after_crash"


class ResultStatus(str, Enum):
    NOT_AVAILABLE = "not_available"
    PENDING = "pending"
    PARTIAL = "partial"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


ACTIVE_JOB_STATES = frozenset(
    {
        JobState.QUEUED,
        JobState.STARTING,
        JobState.RUNNING,
        JobState.POSTPROCESSING,
        JobState.INDEXING,
        JobState.RECOVERY_REQUIRED,
        JobState.UNKNOWN_AFTER_CRASH,
    }
)


@dataclass(frozen=True)
class Job:
    """Durable job record exposed by the operation layer."""

    mcp_job_id: str
    idempotency_key: str
    request_hash: str
    state: JobState
    result_status: ResultStatus
    operation: str = "run"
    logger_session_id: Optional[str] = None
    rickshaw_run_id: Optional[str] = None
    cdm_run_id: Optional[str] = None
    run_directory: Optional[str] = None
    runner_pid: Optional[int] = None
    runner_container_id: Optional[str] = None
    exit_code: Optional[int] = None
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["state"] = self.state.value
        values["result_status"] = self.result_status.value
        return values
