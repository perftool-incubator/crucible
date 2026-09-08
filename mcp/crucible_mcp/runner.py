"""Asynchronous supervision for MCP-launched Crucible runs."""

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from .jobs import JobStore
from .models import Job, JobState, ResultStatus
from .operations import CrucibleOperations, OperationError
from .policy import PolicyError


class RunManager:
    """Submit and supervise Crucible runs without shell interpolation.

    The command is passed to ``subprocess.Popen`` as an argument sequence.
    This first runner delegates the complete synchronous Crucible workflow to
    ``crucible run``; later lifecycle events can refine the state between
    ``running`` and ``completed`` without changing the durable job contract.
    """

    def __init__(
        self,
        store: JobStore,
        operations: CrucibleOperations,
        run_root: Path,
        crucible_command: Sequence[str],
        max_inline_bytes: int = 1_048_576,
    ):
        self.store = store
        self.operations = operations
        self.run_root = Path(run_root)
        self.crucible_command = tuple(crucible_command)
        self.max_inline_bytes = max_inline_bytes
        self.run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._threads: dict[str, threading.Thread] = {}

    def submit(
        self,
        idempotency_key: str,
        *,
        document: Any | None = None,
        path: Path | None = None,
    ) -> tuple[Job, bool]:
        if not idempotency_key:
            raise OperationError("user", "idempotency_key is required", "missing_idempotency_key")
        if (document is None) == (path is None):
            raise OperationError(
                "user", "provide exactly one of document or path", "invalid_input"
            )
        if document is not None:
            encoded = json.dumps(document, separators=(",", ":"), ensure_ascii=True)
            if len(encoded.encode("utf-8")) > self.max_inline_bytes:
                raise OperationError("user", "inline document exceeds size limit", "too_large")
            validation = self.operations.validate_run(document)
            if not validation["valid"]:
                raise OperationError("user", json.dumps(validation), "invalid_run")
            canonical_document = document
        else:
            assert path is not None
            try:
                canonical_path = self.operations.input_policy.canonical_input(path)
                canonical_document = json.loads(canonical_path.read_text(encoding="utf-8"))
            except PolicyError as exc:
                raise OperationError("authorization", str(exc), "input_path_rejected") from exc
            except (OSError, json.JSONDecodeError) as exc:
                raise OperationError("user", "run-file is not valid JSON", "invalid_json") from exc
            validation = self.operations.validate_run(canonical_document)
            if not validation["valid"]:
                raise OperationError("user", json.dumps(validation), "invalid_run")

        job, created = self.store.create_or_get(
            idempotency_key,
            {"run_document": canonical_document},
        )
        if not created:
            return job, False

        session_id = str(uuid.uuid4())
        job_directory = self.run_root / job.mcp_job_id
        input_directory = job_directory / "input"
        input_directory.mkdir(mode=0o700, parents=True)
        run_file = input_directory / "run-file.json"
        run_file.write_text(json.dumps(canonical_document, indent=2) + "\n", encoding="utf-8")
        os.chmod(run_file, 0o600)
        self.store.transition(
            job.mcp_job_id,
            JobState.QUEUED,
            logger_session_id=session_id,
            run_directory=str(job_directory),
        )
        self._launch(job.mcp_job_id, session_id, run_file, job_directory)
        return self.store.get(job.mcp_job_id), True

    def reconcile(self) -> list[Job]:
        """Mark jobs whose recorded runner no longer exists as crash-unknown."""

        changed = []
        for job in self.store.list_active():
            if job.state in {JobState.UNKNOWN_AFTER_CRASH, JobState.RECOVERY_REQUIRED}:
                continue
            if job.runner_pid is not None and self._process_exists(job.runner_pid):
                changed.append(
                    self.store.transition(
                        job.mcp_job_id,
                        JobState.RECOVERY_REQUIRED,
                        error_category="recovery",
                        error_message=(
                            "runner was still active during service reconciliation; "
                            "manual recovery is required"
                        ),
                    )
                )
                continue
            if job.runner_pid is None or not self._process_exists(job.runner_pid):
                changed.append(
                    self.store.transition(
                        job.mcp_job_id,
                        JobState.UNKNOWN_AFTER_CRASH,
                        error_category="recovery",
                        error_message="runner was not present during service reconciliation",
                    )
                )
        return changed

    def get_logs(self, job_id: str, offset: int = 0, limit: int = 65_536) -> dict[str, Any]:
        if offset < 0 or limit <= 0 or limit > 1_048_576:
            raise OperationError("user", "invalid log bounds", "invalid_bounds")
        job = self.store.get(job_id)
        log_path = self.run_root / job_id / "runner.log"
        if not log_path.is_file():
            return {"job_id": job_id, "offset": offset, "next_offset": offset, "complete": False, "text": ""}
        with log_path.open("rb") as log:
            log.seek(offset)
            data = log.read(limit)
            next_offset = log.tell()
            complete = len(data) < limit
        return {
            "job_id": job_id,
            "offset": offset,
            "next_offset": next_offset,
            "complete": complete and job.state in {JobState.COMPLETED, JobState.FAILED},
            "text": data.decode("utf-8", errors="replace"),
        }

    def get_summary(self, job_id: str, max_bytes: int = 1_048_576) -> dict[str, Any]:
        job = self.refresh_result_status(job_id)
        if job.state not in {JobState.COMPLETED, JobState.FAILED}:
            raise OperationError("user", "run has not completed", "result_not_ready")
        if not job.run_directory:
            raise OperationError("framework", "run directory is unavailable", "result_unavailable")
        summary_path = Path(job.run_directory) / "run" / "result-summary.json"
        if not summary_path.is_file():
            raise OperationError("framework", "result summary is unavailable", "result_unavailable")
        if summary_path.stat().st_size > max_bytes:
            raise OperationError("framework", "result summary exceeds size limit", "result_too_large")
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationError("framework", "result summary is not valid JSON", "invalid_result") from exc
        if job.result_status != ResultStatus.AVAILABLE:
            self.store.transition(
                job_id,
                job.state,
                result_status=ResultStatus.AVAILABLE.value,
            )
        return {"job_id": job_id, "result_status": ResultStatus.AVAILABLE.value, "summary": summary}

    def refresh_result_status(self, job_id: str) -> Job:
        """Refresh local result readiness after the runner has completed."""

        job = self.store.get(job_id)
        if job.state != JobState.COMPLETED or not job.run_directory:
            return job
        summary_path = Path(job.run_directory) / "run" / "result-summary.json"
        if not summary_path.is_file() or job.result_status == ResultStatus.AVAILABLE:
            return job
        return self.store.transition(
            job_id,
            job.state,
            result_status=ResultStatus.AVAILABLE.value,
        )

    def _launch(self, job_id: str, session_id: str, run_file: Path, job_directory: Path) -> None:
        log_path = job_directory / "runner.log"
        log = log_path.open("ab")
        environment = os.environ.copy()
        event_path = job_directory / "events.jsonl"
        environment["CRUCIBLE_MCP_SESSION_ID"] = session_id
        environment["CRUCIBLE_MCP_EVENT_FILE"] = str(event_path)
        try:
            process = subprocess.Popen(
                [*self.crucible_command, "run", str(run_file)],
                cwd=str(self.operations.crucible_home),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
        except OSError as exc:
            log.write(f"runner launch failed: {exc}\n".encode("utf-8"))
            log.close()
            self.store.transition(
                job_id,
                JobState.FAILED,
                error_category="infrastructure",
                error_message=str(exc),
                exit_code=127,
            )
            return

        self.store.transition(job_id, JobState.STARTING, runner_pid=process.pid)
        thread = threading.Thread(
            target=self._wait_for_completion,
            args=(job_id, process, log, event_path),
            daemon=True,
            name=f"mcp-run-{job_id}",
        )
        self._threads[job_id] = thread
        thread.start()

    def _wait_for_completion(
        self, job_id: str, process: subprocess.Popen, log: Any, event_path: Path
    ) -> None:
        self.store.transition(job_id, JobState.RUNNING)
        event_position = 0
        while process.poll() is None:
            event_position = self._consume_events(job_id, event_path, event_position)
            time.sleep(0.1)
        event_position = self._consume_events(job_id, event_path, event_position)
        exit_code = process.returncode
        log.close()
        self._threads.pop(job_id, None)
        if exit_code == 0:
            self.store.transition(
                job_id,
                JobState.COMPLETED,
                result_status=ResultStatus.PENDING.value,
                exit_code=exit_code,
            )
        else:
            self.store.transition(
                job_id,
                JobState.FAILED,
                result_status=ResultStatus.UNAVAILABLE.value,
                exit_code=exit_code,
                error_category="framework",
                error_message=f"crucible run exited with status {exit_code}",
            )

    def _consume_events(self, job_id: str, event_path: Path, position: int) -> int:
        if not event_path.is_file():
            return position
        with event_path.open("r", encoding="utf-8") as events:
            events.seek(position)
            for line in events:
                try:
                    event = json.loads(line)
                    state = JobState(event["state"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                updates = {}
                if event.get("run_directory"):
                    updates["run_directory"] = event["run_directory"]
                if state in {
                    JobState.STARTING,
                    JobState.RUNNING,
                    JobState.POSTPROCESSING,
                    JobState.INDEXING,
                }:
                    try:
                        self.store.transition(job_id, state, **updates)
                    except RuntimeError:
                        # A terminal process result may race with its final event.
                        pass
            return events.tell()

    @staticmethod
    def _process_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
