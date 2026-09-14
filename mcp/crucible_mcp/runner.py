"""Asynchronous supervision for MCP-launched Crucible runs."""

import json
import lzma
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from .jobs import JobConflictError, JobStore, request_hash
from .models import Job, JobState, ResultStatus
from .operations import CrucibleOperations, OperationError
from .policy import PolicyError


_MAINTENANCE_OPERATIONS = frozenset(
    {
        "postprocess",
        "index",
        "delete_indexed_result",
        "archive_local_run",
        "unarchive_local_run",
    }
)


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
        cdm_readiness_timeout: int = 60,
    ):
        self.store = store
        self.operations = operations
        self.run_root = Path(run_root)
        self.crucible_command = tuple(crucible_command)
        self.max_inline_bytes = max_inline_bytes
        self.cdm_readiness_timeout = cdm_readiness_timeout
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
        run_file = input_directory / "run-file.json"
        try:
            input_directory.mkdir(mode=0o700, parents=True)
            run_file.write_text(
                json.dumps(canonical_document, indent=2) + "\n", encoding="utf-8"
            )
            os.chmod(run_file, 0o600)
        except OSError as exc:
            failed = self.store.transition(
                job.mcp_job_id,
                JobState.FAILED,
                error_category="infrastructure",
                error_message=f"could not stage run input: {exc}",
            )
            return failed, True
        self.store.transition(
            job.mcp_job_id,
            JobState.QUEUED,
            logger_session_id=session_id,
            run_directory=str(job_directory),
            supervision_directory=str(job_directory),
        )
        self._launch(job.mcp_job_id, session_id, run_file, job_directory)
        return self.store.get(job.mcp_job_id), True

    def reconcile(self) -> list[Job]:
        """Recover supervision for jobs surviving an MCP service restart."""

        changed: list[Job] = []
        for job in self.store.list_active():
            if job.state in {JobState.UNKNOWN_AFTER_CRASH, JobState.RECOVERY_REQUIRED}:
                changed.append(self._fail_recovery_job(job, "job requires recovery after a previous restart"))
                continue

            if (
                job.runner_pid is not None
                and self._process_exists(job.runner_pid)
                and self._runner_identity_matches(job)
            ):
                self._reattach(job)
                continue

            changed.append(self._resolve_or_fail(job))
        return changed

    def submit_processing(self, idempotency_key: str, operation: str, run_directory: Path) -> tuple[Job, bool]:
        if operation not in {"postprocess", "index"}:
            raise OperationError("user", "unsupported processing operation", "invalid_operation")
        try:
            canonical = self.operations.run_policy.canonical_directory(run_directory)
        except PolicyError as exc:
            raise OperationError("authorization", str(exc), "run_path_rejected") from exc
        request = {"operation": operation, "run_directory": str(canonical)}
        job, created = self.store.create_or_get(idempotency_key, request, operation)
        if not created:
            return job, False
        session_id = str(uuid.uuid4())
        job_directory = self.run_root / job.mcp_job_id
        try:
            job_directory.mkdir(mode=0o700, parents=True)
        except OSError as exc:
            failed = self.store.transition(
                job.mcp_job_id,
                JobState.FAILED,
                error_category="infrastructure",
                error_message=f"could not create processing supervision directory: {exc}",
            )
            return failed, True
        self.store.transition(job.mcp_job_id, JobState.QUEUED,
                              logger_session_id=session_id,
                              run_directory=str(canonical),
                              supervision_directory=str(job_directory))
        self._launch_command(job.mcp_job_id, session_id, job_directory,
                              [operation, str(canonical)])
        return self.store.get(job.mcp_job_id), True

    def submit_indexed_deletion(self, idempotency_key: str, run: str) -> tuple[Job, bool]:
        """Delete one indexed result through Crucible's existing CLI path."""

        if not isinstance(run, str) or not run:
            raise OperationError("user", "run is required", "missing_argument")
        if "\x00" in run:
            raise OperationError("user", "run contains an invalid character", "invalid_argument")
        request = {"operation": "delete_indexed_result", "run": run}
        job, created = self.store.create_or_get(
            idempotency_key, request, "delete_indexed_result"
        )
        if not created:
            return job, False
        session_id = str(uuid.uuid4())
        job_directory = self.run_root / job.mcp_job_id
        try:
            job_directory.mkdir(mode=0o700, parents=True)
        except OSError as exc:
            failed = self.store.transition(
                job.mcp_job_id,
                JobState.FAILED,
                error_category="infrastructure",
                error_message=f"could not create deletion supervision directory: {exc}",
            )
            return failed, True
        self.store.transition(
            job.mcp_job_id,
            JobState.QUEUED,
            logger_session_id=session_id,
            run_directory=run,
            supervision_directory=str(job_directory),
        )
        self._launch_command(
            job.mcp_job_id,
            session_id,
            job_directory,
            ["rm", "--run", run],
        )
        return self.store.get(job.mcp_job_id), True

    def submit_archive_operation(
        self, idempotency_key: str, operation: str, path: Path
    ) -> tuple[Job, bool]:
        """Run one local archive operation through the existing CLI."""

        if operation not in {"archive_local_run", "unarchive_local_run"}:
            raise OperationError("user", "unsupported archive operation", "invalid_operation")
        request = {"operation": operation, "path": str(path)}
        existing = self.store.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.request_hash != request_hash(request):
                raise JobConflictError(
                    "idempotency key was already used for a different request"
                )
            return existing, False
        if operation == "archive_local_run":
            try:
                path = self.operations.run_policy.canonical_child_directory(path)
            except PolicyError as exc:
                raise OperationError("authorization", str(exc), "run_path_rejected") from exc
        else:
            path = self.operations.canonical_local_archive(path)
        commands = {
            "archive_local_run": ["archive", str(path)],
            "unarchive_local_run": ["unarchive", str(path)],
        }
        job, created = self.store.create_or_get(idempotency_key, request, operation)
        if not created:
            return job, False
        session_id = str(uuid.uuid4())
        job_directory = self.run_root / job.mcp_job_id
        try:
            job_directory.mkdir(mode=0o700, parents=True)
        except OSError as exc:
            failed = self.store.transition(
                job.mcp_job_id,
                JobState.FAILED,
                error_category="infrastructure",
                error_message=f"could not create archive supervision directory: {exc}",
            )
            return failed, True
        self.store.transition(
            job.mcp_job_id,
            JobState.QUEUED,
            logger_session_id=session_id,
            run_directory=str(path),
            supervision_directory=str(job_directory),
        )
        self._launch_command(job.mcp_job_id, session_id, job_directory, commands[operation])
        return self.store.get(job.mcp_job_id), True

    def _fail_recovery_job(self, job: Job, message: str) -> Job:
        return self.store.transition(
            job.mcp_job_id,
            JobState.FAILED,
            result_status=ResultStatus.UNAVAILABLE.value,
            error_category="recovery",
            error_message=message,
        )

    def _resolve_or_fail(self, job: Job) -> Job:
        if job.operation in _MAINTENANCE_OPERATIONS:
            marker = self._processing_completion_marker(job)
            if marker.is_file():
                return self.store.transition(
                    job.mcp_job_id,
                    JobState.COMPLETED,
                    result_status=ResultStatus.PENDING.value,
                    exit_code=0,
                )
            return self._fail_recovery_job(
                job, "processing runner was lost before completion was recorded"
            )
        if job.run_directory:
            summary_path = Path(job.run_directory) / "run" / "result-summary.json"
            if summary_path.is_file() and job.state == JobState.RUNNING:
                return self.store.transition(
                    job.mcp_job_id,
                    JobState.COMPLETED,
                    result_status=ResultStatus.AVAILABLE.value,
                    exit_code=0,
                )
        return self._fail_recovery_job(
            job, "runner was not present or could not be verified during reconciliation"
        )

    def _processing_completion_marker(self, job: Job) -> Path:
        supervision_directory = job.supervision_directory or str(
            self.run_root / job.mcp_job_id
        )
        return Path(supervision_directory) / "processing-complete"

    def _runner_identity_matches(self, job: Job) -> bool:
        if job.runner_pid is None or not job.run_directory:
            return False
        identity_path = (
            Path(job.run_directory) / "input" / "run-file.json"
            if job.operation == "run"
            else Path(job.run_directory)
        )
        try:
            command_line = Path(f"/proc/{job.runner_pid}/cmdline").read_bytes()
        except OSError:
            return False
        return str(identity_path).encode("utf-8") in command_line.split(b"\0")

    def _reattach(self, job: Job) -> None:
        if job.mcp_job_id in self._threads:
            return
        supervision_directory = job.supervision_directory or str(self.run_root / job.mcp_job_id)
        event_path = Path(supervision_directory) / "events.jsonl"
        if event_path is None:
            self._fail_recovery_job(job, "runner has no persisted run directory")
            return
        thread = threading.Thread(
            target=self._wait_for_recovered_process,
            args=(job, event_path),
            daemon=True,
            name=f"mcp-recover-{job.mcp_job_id}",
        )
        self._threads[job.mcp_job_id] = thread
        thread.start()

    def _wait_for_recovered_process(self, job: Job, event_path: Path) -> None:
        position = 0
        while job.runner_pid is not None and self._process_exists(job.runner_pid):
            position = self._consume_events(job.mcp_job_id, event_path, position)
            time.sleep(0.1)
        position = self._consume_events(job.mcp_job_id, event_path, position)
        current = self.store.get(job.mcp_job_id)
        if current.state in {JobState.COMPLETED, JobState.FAILED}:
            self._threads.pop(job.mcp_job_id, None)
            return
        self._resolve_or_fail(current)
        self._threads.pop(job.mcp_job_id, None)

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
        deadline = time.monotonic() + self.cdm_readiness_timeout
        while not summary_path.is_file():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))
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
        self._launch_command(job_id, session_id, job_directory, ["run", str(run_file)])

    def _launch_command(self, job_id: str, session_id: str, job_directory: Path, command: list[str]) -> None:
        log_path = job_directory / "runner.log"
        log = log_path.open("ab")
        environment = os.environ.copy()
        event_path = job_directory / "events.jsonl"
        environment["CRUCIBLE_MCP_SESSION_ID"] = session_id
        environment["CRUCIBLE_MCP_EVENT_FILE"] = str(event_path)
        job = self.store.get(job_id)
        launch_command = [*self.crucible_command, *command]
        if job.operation in _MAINTENANCE_OPERATIONS:
            marker = self._processing_completion_marker(job)
            wrapper = (
                "import pathlib, subprocess, sys; "
                "marker = pathlib.Path(sys.argv[1]); "
                "result = subprocess.run(sys.argv[2:], check=False); "
                "marker.write_text('completed\\n', encoding='utf-8') if result.returncode == 0 else None; "
                "sys.exit(result.returncode)"
            )
            launch_command = [
                sys.executable,
                "-c",
                wrapper,
                str(marker),
                *launch_command,
            ]
        try:
            process = subprocess.Popen(
                launch_command,
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
            args=(job_id, process, log, event_path, f"crucible-rickshaw-run-{session_id}"),
            daemon=True,
            name=f"mcp-run-{job_id}",
        )
        self._threads[job_id] = thread
        thread.start()

    def _wait_for_completion(
        self,
        job_id: str,
        process: subprocess.Popen,
        log: Any,
        event_path: Path,
        container_name: str,
    ) -> None:
        job = self.store.get(job_id)
        lifecycle_state = {
            "postprocess": JobState.POSTPROCESSING,
            "index": JobState.INDEXING,
        }.get(job.operation, JobState.RUNNING)
        self.store.transition(job_id, lifecycle_state)
        event_position = 0
        while process.poll() is None:
            event_position = self._consume_events(job_id, event_path, event_position)
            self._capture_container_id(job_id, container_name)
            time.sleep(0.1)
        event_position = self._consume_events(job_id, event_path, event_position)
        self._backfill_identifiers(job_id)
        exit_code = process.returncode
        log.close()
        self._threads.pop(job_id, None)
        current = self.store.get(job_id)
        if exit_code == 0:
            if current.operation in _MAINTENANCE_OPERATIONS:
                try:
                    marker = self._processing_completion_marker(current)
                    marker.write_text(current.operation + "\n", encoding="utf-8")
                except OSError as exc:
                    self.store.transition(
                        job_id,
                        JobState.FAILED,
                        result_status=ResultStatus.UNAVAILABLE.value,
                        exit_code=exit_code,
                        error_category="infrastructure",
                        error_message=f"could not record processing completion: {exc}",
                    )
                    return
            result_status = current.result_status
            if result_status == ResultStatus.NOT_AVAILABLE:
                result_status = ResultStatus.PENDING
            self.store.transition(
                job_id,
                JobState.COMPLETED,
                result_status=result_status.value,
                exit_code=exit_code,
            )
        elif current.state == JobState.INDEXING and current.operation == "run":
            if self._wait_for_result_summary(current):
                self.store.transition(
                    job_id,
                    JobState.COMPLETED,
                    result_status=ResultStatus.AVAILABLE.value,
                    exit_code=exit_code,
                )
            else:
                self.store.transition(
                    job_id,
                    JobState.COMPLETED,
                    result_status=ResultStatus.UNAVAILABLE.value,
                    exit_code=exit_code,
                    error_category="cdm",
                    error_message=(
                        "CDM result summary was unavailable after the configured "
                        "readiness timeout"
                    ),
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

    def _wait_for_result_summary(self, job: Job) -> bool:
        if not job.run_directory:
            return False
        summary_path = Path(job.run_directory) / "run" / "result-summary.json"
        deadline = time.monotonic() + self.cdm_readiness_timeout
        while not summary_path.is_file():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))
        return True

    def _capture_container_id(self, job_id: str, container_name: str) -> None:
        try:
            result = subprocess.run(
                ["podman", "inspect", "--format", "{{.Id}}", container_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        container_id = result.stdout.strip()
        if result.returncode != 0 or not container_id:
            return
        job = self.store.get(job_id)
        if job.runner_container_id != container_id:
            self.store.transition(job_id, job.state, runner_container_id=container_id)

    def _backfill_identifiers(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job.run_directory:
            return
        updates: dict[str, str] = {}
        rickshaw_data = self._read_json_artifact(Path(job.run_directory) / "run" / "rickshaw-run.json")
        if rickshaw_data is None:
            rickshaw_data = self._read_json_artifact(
                Path(job.run_directory) / "run" / "rickshaw-run.json.xz"
            )
        if isinstance(rickshaw_data, dict):
            value = rickshaw_data.get("run-id") or rickshaw_data.get("id")
            if isinstance(value, str) and value:
                updates["rickshaw_run_id"] = value

        summary = self._read_json_artifact(Path(job.run_directory) / "run" / "result-summary.json")
        if isinstance(summary, dict):
            value = summary.get("cdm_run_id") or summary.get("cdm-run-id")
            if isinstance(value, str) and value:
                updates["cdm_run_id"] = value
        if updates:
            self.store.transition(job_id, job.state, **updates)

    @staticmethod
    def _read_json_artifact(path: Path) -> Any | None:
        try:
            if path.suffix == ".xz":
                return json.loads(lzma.open(path, "rt", encoding="utf-8").read())
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, lzma.LZMAError, json.JSONDecodeError):
            return None

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
                for field in ("rickshaw_run_id", "cdm_run_id", "runner_container_id"):
                    if isinstance(event.get(field), str) and event[field]:
                        updates[field] = event[field]
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
