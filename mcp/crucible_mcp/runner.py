"""Asynchronous supervision for MCP-launched Crucible runs."""

import json
import lzma
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from .host import host_context_command, host_context_environment
from .jobs import JobConflictError, JobStore, request_hash
from .models import Job, JobState, ResultStatus
from .operations import (
    MAX_LOG_REDACTION_LINES,
    MAX_METADATA_RESPONSE_BYTES,
    CrucibleOperations,
    OperationError,
)
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
MAX_LOG_REDACTION_CONTEXT_BYTES = 1_048_576
_PRIVATE_KEY_PAYLOAD_LINE = re.compile(rb"[A-Za-z0-9+/]{32,}={0,2}")
_LOG_EMPTY_RECORDS = re.compile(rb"[\r\n]+")


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded, non-sensitive planner data on the durable job."""

    return {
        "contract_version": plan.get("contract_version"),
        "input_digest": plan.get("input_digest"),
        "totals": plan.get("totals", {}),
        "runtime": plan.get("runtime", {}),
        "limits": plan.get("limits", {}),
    }


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
        host_execution: bool = True,
    ):
        self.store = store
        self.operations = operations
        self.run_root = Path(run_root)
        self.crucible_command = tuple(crucible_command)
        self.max_inline_bytes = max_inline_bytes
        self.cdm_readiness_timeout = cdm_readiness_timeout
        self.host_execution = host_execution
        self.run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._threads: dict[str, threading.Thread] = {}

    def submit(
        self,
        idempotency_key: str,
        *,
        document: Any | None = None,
        path: Path | None = None,
        plan_digest: str | None = None,
        verified_plan: dict[str, Any] | None = None,
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

        if plan_digest is not None and (not isinstance(plan_digest, str) or not plan_digest):
            raise OperationError("user", "plan_digest is required", "invalid_plan")

        request = {"run_document": canonical_document}
        if plan_digest is not None:
            request["plan_digest"] = plan_digest
        existing = self.store.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.request_hash != request_hash(request):
                raise JobConflictError(
                    "idempotency key was already used for a different request"
                )
            return existing, False

        if plan_digest is not None:
            if verified_plan is None:
                verified_plan = self.operations.prepare_run(canonical_document)
            if not isinstance(verified_plan, dict):
                raise OperationError("framework", "verified plan is invalid", "invalid_plan")
            if not verified_plan.get("validation", {}).get("valid", False):
                raise OperationError("user", json.dumps(verified_plan), "invalid_run")
            if verified_plan.get("input_digest") != plan_digest:
                raise OperationError(
                    "user",
                    "plan digest does not match the submitted run",
                    "stale_plan",
                )

        plan_summary = _plan_summary(verified_plan) if verified_plan else None
        job, created = self.store.create_or_get(
            idempotency_key,
            request,
            plan_digest=plan_digest,
            plan_summary=plan_summary,
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
            plan_digest=plan_digest,
            plan_summary=plan_summary,
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
                path = self.operations.canonical_archive_run(path)
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
                if job.state == JobState.STARTING:
                    lifecycle_state = {
                        "postprocess": JobState.POSTPROCESSING,
                        "index": JobState.INDEXING,
                    }.get(job.operation, JobState.RUNNING)
                    job = self.store.transition(job.mcp_job_id, lifecycle_state)
                if job.operation in {"postprocess", "index"}:
                    self._backfill_identifiers(job.mcp_job_id)
                    job = self.store.get(job.mcp_job_id)
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
            if summary_path.is_file() and job.state in {
                JobState.RUNNING,
                JobState.POSTPROCESSING,
                JobState.INDEXING,
            }:
                self._backfill_identifiers(job.mcp_job_id)
                job = self.store.get(job.mcp_job_id)
                return self.store.transition(
                    job.mcp_job_id,
                    JobState.COMPLETED,
                    result_status=ResultStatus.AVAILABLE.value,
                    exit_code=job.exit_code,
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
        if job.runner_pid is None:
            return False
        if job.operation == "delete_indexed_result":
            supervision_directory = job.supervision_directory or str(
                self.run_root / job.mcp_job_id
            )
            identity_path = Path(supervision_directory) / "processing-complete"
        elif job.run_directory:
            identity_path = (
                Path(job.run_directory) / "input" / "run-file.json"
                if job.operation == "run"
                else Path(job.run_directory)
            )
        else:
            return False
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
        # A restarted supervisor does not run the original waiter's normal
        # completion path, so recover identifiers from the retained artifacts
        # before resolving the terminal state.
        self._backfill_identifiers(job.mcp_job_id)
        current = self.store.get(job.mcp_job_id)
        self._resolve_or_fail(current)
        self._threads.pop(job.mcp_job_id, None)

    def get_logs(self, job_id: str, offset: int = 0, limit: int = 65_536) -> dict[str, Any]:
        if offset < 0 or limit <= 0 or limit > 1_048_576:
            raise OperationError("user", "invalid log bounds", "invalid_bounds")
        job = self.store.get(job_id)
        log_path = self.run_root / job_id / "runner.log"
        if not log_path.is_file():
            return {
                "job_id": job_id,
                "offset": offset,
                "next_offset": offset,
                "complete": False,
                "text": "",
                "redacted": False,
                "redacted_lines": 0,
            }
        with log_path.open("rb") as log:
            log.seek(offset)
            data = log.read(limit)
            next_offset = log.tell()
            complete = len(data) < limit
            if data:
                prefix, prefix_state_known = self._log_prefix_context(log, offset)
                file_size = log.seek(0, os.SEEK_END)
                suffix, suffix_complete = self._log_suffix_context(
                    log, next_offset, file_size
                )
                context_bytes = prefix + data + suffix
                text, redacted_lines = self._redact_log_slice(
                    context_bytes,
                    len(prefix),
                    len(prefix) + len(data),
                    prefix_state_known,
                    suffix_complete,
                )
            else:
                text = ""
                redacted_lines = 0
        return {
            "job_id": job_id,
            "offset": offset,
            "next_offset": next_offset,
            "complete": complete and job.state in {JobState.COMPLETED, JobState.FAILED},
            "text": text,
            "redacted": redacted_lines > 0,
            "redacted_lines": redacted_lines,
        }

    def _redact_log_slice(
        self,
        context: bytes,
        page_start: int,
        page_end: int,
        prefix_state_known: bool,
        suffix_complete: bool,
    ) -> tuple[str, int]:
        """Redact affected line fragments while retaining adjacent safe lines.

        Offsets still refer to the raw log. A line that intersects a secret is
        represented by a marker only for the portion in this page, so the
        caller can continue paging without receiving unrelated lines as a
        collateral consequence of redaction.
        """

        if not prefix_state_known:
            # No text from this slice is safe to classify without the lost
            # prefix state; mask just the requested bytes instead of replaying
            # the retained megabyte line by line.
            return self._redact_unknown_log_slice(context[page_start:page_end])

        output: list[str] = []
        redacted_lines = 0
        private_key_label: str | None = None
        pending_sensitive_indent: int | None = None
        pending_sensitive_yaml_indent: int | None = None
        sensitive_structure_depth = 0
        shell_continuation = not prefix_state_known
        shell_quote: str | None = None
        pending_sensitive_heredocs: tuple[tuple[str, bool], ...] | None = ()
        pending_sensitive_json_key: tuple[int, bool] | None = None
        pending_sensitive_log_value = False
        sensitive_shell_continuation_lines = (
            self.operations._log_sensitive_shell_continuation_lines(
                context.decode("utf-8", errors="replace")
            )
        )
        line_start = 0
        processed_lines = 0
        while line_start < len(context):
            if processed_lines >= MAX_LOG_REDACTION_LINES:
                remainder_start = max(line_start, page_start)
                if remainder_start < page_end:
                    remainder = context[remainder_start:page_end]
                    output.append("[redacted]")
                    line_breaks = (
                        remainder.count(b"\n")
                        + remainder.count(b"\r")
                        - remainder.count(b"\r\n")
                    )
                    redacted_lines += line_breaks + bool(
                        remainder and not remainder.endswith((b"\n", b"\r"))
                    )
                break
            lf = context.find(b"\n", line_start)
            cr = context.find(b"\r", line_start)
            endings = [index for index in (lf, cr) if index >= 0]
            if not endings:
                line_end = len(context)
            else:
                ending_start = min(endings)
                line_end = ending_start + 1
                if (
                    context[ending_start] == 0x0D
                    and context[line_end : line_end + 1] == b"\n"
                ):
                    line_end += 1
            line = context[line_start:line_end]
            if line.endswith(b"\r\n"):
                content = line[:-2]
            elif line.endswith((b"\n", b"\r")):
                content = line[:-1]
            else:
                content = line
            decoded = content.decode("utf-8", errors="replace")
            (
                _,
                next_private_key_label,
                next_pending_sensitive_indent,
                next_pending_sensitive_yaml_indent,
                next_sensitive_structure_depth,
                next_shell_continuation,
                next_shell_quote,
                next_sensitive_heredocs,
                line_redacted,
                next_pending_sensitive_json_key,
                next_pending_sensitive_log_value,
            ) = self.operations._redact_log_line_with_stats(
                decoded,
                private_key_label,
                pending_sensitive_indent,
                sensitive_structure_depth,
                pending_sensitive_yaml_indent,
                shell_continuation,
                line.endswith((b"\n", b"\r")),
                shell_quote,
                pending_sensitive_heredocs,
                pending_sensitive_json_key,
                pending_sensitive_log_value,
            )
            if _PRIVATE_KEY_PAYLOAD_LINE.fullmatch(content):
                # Preserve the legacy fail-closed heuristic for private-key
                # payloads emitted without PEM delimiters, masking only this
                # record so adjacent diagnostics stay useful.
                line_redacted = True
            if processed_lines in sensitive_shell_continuation_lines:
                line_redacted = True

            fragment_start = max(line_start, page_start)
            fragment_end = min(line_end, page_end)
            if fragment_start < fragment_end:
                # If the bounded history window is incomplete, the page may
                # start inside a multiline secret structure. Without its
                # opener, keep the entire page slice hidden rather than
                # exposing later members after masking only the first line.
                uncertain_prefix = not prefix_state_known
                uncertain_suffix = (
                    not suffix_complete
                    and line_start < page_end < line_end
                )
                fragment = context[fragment_start:fragment_end]
                if line_redacted or uncertain_prefix or uncertain_suffix:
                    ending = (
                        b"\r\n"
                        if fragment.endswith(b"\r\n")
                        else b"\n"
                        if fragment.endswith(b"\n")
                        else b"\r"
                        if fragment.endswith(b"\r")
                        else b""
                    )
                    output.append("[redacted]" + ending.decode("ascii"))
                    redacted_lines += 1
                else:
                    output.append(fragment.decode("utf-8", errors="replace"))

            private_key_label = next_private_key_label
            pending_sensitive_indent = next_pending_sensitive_indent
            pending_sensitive_yaml_indent = next_pending_sensitive_yaml_indent
            sensitive_structure_depth = next_sensitive_structure_depth
            shell_continuation = next_shell_continuation
            shell_quote = next_shell_quote
            pending_sensitive_heredocs = next_sensitive_heredocs
            pending_sensitive_json_key = next_pending_sensitive_json_key
            pending_sensitive_log_value = next_pending_sensitive_log_value
            line_start = line_end
            processed_lines += 1

        return "".join(output), redacted_lines

    @staticmethod
    def _redact_unknown_log_slice(page: bytes) -> tuple[str, int]:
        """Mask a page whose preceding redaction state could not be recovered."""

        output: list[str] = []
        redacted_lines = 0
        position = 0
        while position < len(page):
            if redacted_lines >= MAX_LOG_REDACTION_LINES:
                remaining = page[position:]
                output.append("[redacted]")
                line_breaks = (
                    remaining.count(b"\n")
                    + remaining.count(b"\r")
                    - remaining.count(b"\r\n")
                )
                redacted_lines += line_breaks + bool(
                    remaining and not remaining.endswith((b"\n", b"\r"))
                )
                break
            lf = page.find(b"\n", position)
            cr = page.find(b"\r", position)
            endings = [index for index in (lf, cr) if index >= 0]
            if not endings:
                end = len(page)
            else:
                ending_start = min(endings)
                end = ending_start + 1
                if page[ending_start] == 0x0D and page[end : end + 1] == b"\n":
                    end += 1
            fragment = page[position:end]
            ending = (
                b"\r\n"
                if fragment.endswith(b"\r\n")
                else b"\n"
                if fragment.endswith(b"\n")
                else b"\r"
                if fragment.endswith(b"\r")
                else b""
            )
            output.append("[redacted]" + ending.decode("ascii"))
            redacted_lines += 1
            position = end
        return "".join(output), redacted_lines

    @classmethod
    def _log_prefix_context(cls, log: Any, offset: int) -> tuple[bytes, bool]:
        """Read bounded history and flag structure state lost at its cutoff."""

        if offset <= 0:
            return b"", True
        start = max(0, offset - MAX_LOG_REDACTION_CONTEXT_BYTES)
        log.seek(start)
        prefix = log.read(offset - start)
        if start == 0:
            return prefix, True
        delimiters = [
            index
            for index in (prefix.find(b"\n"), prefix.find(b"\r"))
            if index >= 0
        ]
        if not delimiters:
            return b"", False
        # The first bytes may begin mid-line when the history cap is reached.
        # Discard the partial record at the start of the bounded window.
        first_delimiter = min(delimiters)
        next_record = first_delimiter + 1
        if (
            prefix[first_delimiter] == 0x0D
            and prefix[next_record : next_record + 1] == b"\n"
        ):
            next_record += 1
        context = prefix[next_record:]
        if not context:
            return context, False
        context_offset = start + next_record
        state_ambiguous = cls._log_prefix_redaction_state_is_ambiguous(
            log, context_offset
        )
        return context, not state_ambiguous

    @staticmethod
    def _log_prefix_redaction_state_is_ambiguous(log: Any, boundary: int) -> bool:
        """Replay bounded history to detect secret state at the context boundary."""

        if boundary > MAX_LOG_REDACTION_CONTEXT_BYTES:
            return True
        log.seek(0)
        prefix = log.read(boundary)
        if len(prefix) != boundary:
            return True

        private_key_label: str | None = None
        pending_sensitive_indent: int | None = None
        pending_sensitive_yaml_indent: int | None = None
        sensitive_structure_depth = 0
        shell_continuation = False
        shell_quote: str | None = None
        pending_sensitive_heredocs: tuple[tuple[str, bool], ...] | None = ()
        pending_sensitive_json_key: tuple[int, bool] | None = None
        pending_sensitive_log_value = False
        position = 0
        scanned_lines = 0
        while position < len(prefix):
            if scanned_lines >= MAX_LOG_REDACTION_LINES:
                return True
            empty_records = _LOG_EMPTY_RECORDS.match(prefix, position)
            if empty_records is not None:
                separators = prefix[position : empty_records.end()]
                record_count = (
                    separators.count(b"\n")
                    + separators.count(b"\r")
                    - separators.count(b"\r\n")
                )
                if scanned_lines + record_count > MAX_LOG_REDACTION_LINES:
                    return True
                # Blank records preserve all redaction states, so count them
                # in bulk instead of invoking the relatively expensive
                # credential scanner once for every empty line.
                scanned_lines += record_count
                position = empty_records.end()
                continue
            lf = prefix.find(b"\n", position)
            cr = prefix.find(b"\r", position)
            endings = [index for index in (lf, cr) if index >= 0]
            if not endings:
                end = len(prefix)
            else:
                ending_start = min(endings)
                end = ending_start + 1
                if prefix[ending_start] == 0x0D and prefix[end : end + 1] == b"\n":
                    end += 1
            line = prefix[position:end]
            if line.endswith(b"\r\n"):
                content, ending = line[:-2], b"\r\n"
            elif line.endswith((b"\n", b"\r")):
                content, ending = line[:-1], line[-1:]
            else:
                content, ending = line, b""
            (
                _,
                private_key_label,
                pending_sensitive_indent,
                pending_sensitive_yaml_indent,
                sensitive_structure_depth,
                shell_continuation,
                shell_quote,
                pending_sensitive_heredocs,
                _,
                pending_sensitive_json_key,
                pending_sensitive_log_value,
            ) = CrucibleOperations._redact_log_line_with_stats(
                content.decode("utf-8", errors="replace"),
                private_key_label,
                pending_sensitive_indent,
                sensitive_structure_depth,
                pending_sensitive_yaml_indent,
                shell_continuation,
                bool(ending),
                shell_quote,
                pending_sensitive_heredocs,
                pending_sensitive_json_key,
                pending_sensitive_log_value,
            )
            position = end
            scanned_lines += 1

        return (
            private_key_label is not None
            or pending_sensitive_indent is not None
            or pending_sensitive_yaml_indent is not None
            or sensitive_structure_depth != 0
            or shell_continuation
            or shell_quote is not None
            or pending_sensitive_heredocs is None
            or bool(pending_sensitive_heredocs)
            or pending_sensitive_json_key is not None
            or pending_sensitive_log_value
        )

    @staticmethod
    def _log_suffix_context(
        log: Any, offset: int, file_size: int
    ) -> tuple[bytes, bool]:
        """Read bounded following lines for multiline credential detection."""

        if offset >= file_size:
            return b"", True
        log.seek(offset)
        suffix = log.read(MAX_LOG_REDACTION_CONTEXT_BYTES)
        complete = (
            offset + len(suffix) >= file_size
            or b"\n" in suffix
            or b"\r" in suffix
        )
        return suffix, complete

    def get_summary(
        self,
        job_id: str,
        max_bytes: int = MAX_METADATA_RESPONSE_BYTES,
        *,
        request_id: Any = None,
    ) -> dict[str, Any]:
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
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperationError("framework", "result summary is not valid JSON", "invalid_result") from exc
        try:
            summary = self.operations._redact_summary(summary)
        except RecursionError as exc:
            raise OperationError(
                "framework", "result summary exceeds nesting limit", "result_too_large"
            ) from exc
        if job.result_status != ResultStatus.AVAILABLE:
            self.store.transition(
                job_id,
                job.state,
                result_status=ResultStatus.AVAILABLE.value,
            )
        result = {
            "job_id": job_id,
            "result_status": ResultStatus.AVAILABLE.value,
            "summary": summary,
        }
        if (
            self.operations._mcp_response_size(result, request_id)
            > MAX_METADATA_RESPONSE_BYTES
        ):
            raise OperationError(
                "framework", "run summary response exceeds size limit", "result_too_large"
            )
        return result

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

    def _execution_context_command(
        self, command: Sequence[str], working_directory: str = "/"
    ) -> list[str]:
        if not self.host_execution:
            return list(command)
        return host_context_command(command, working_directory)

    def _launch_command(self, job_id: str, session_id: str, job_directory: Path, command: list[str]) -> None:
        log_path = job_directory / "runner.log"
        log = log_path.open("ab")
        environment = os.environ.copy()
        if self.host_execution:
            environment = host_context_environment(
                environment, preserve_mcp_session=True
            )
        event_path = job_directory / "events.jsonl"
        environment["CRUCIBLE_MCP_SESSION_ID"] = session_id
        environment["CRUCIBLE_MCP_EVENT_FILE"] = str(event_path)
        job = self.store.get(job_id)
        launch_command = [*self.crucible_command, *command]
        # The input and supervision paths are shared with the host, but path
        # visibility alone does not give this process the host Podman store or
        # cgroup view. Run the Crucible CLI in host context.
        launch_command = self._execution_context_command(
            launch_command, str(self.operations.crucible_home)
        )
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
                self._execution_context_command(
                    ["podman", "inspect", "--format", "{{.Id}}", container_name]
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
                env=host_context_environment(os.environ)
                if self.host_execution
                else None,
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
            if not isinstance(value, str) or not value:
                runs = summary.get("runs")
                run_ids = set()
                if isinstance(runs, list):
                    run_ids = {
                        run["run-id"]
                        for run in runs
                        if isinstance(run, dict)
                        and isinstance(run.get("run-id"), str)
                        and run["run-id"]
                    }
                # The job status has one CDM ID field. Do not pick an
                # arbitrary result when a summary contains several runs.
                value = next(iter(run_ids)) if len(run_ids) == 1 else None
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
        except (OSError, UnicodeDecodeError, lzma.LZMAError, json.JSONDecodeError):
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
