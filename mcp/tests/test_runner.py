import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from crucible_mcp.jobs import JobStore
from crucible_mcp.models import JobState
from crucible_mcp.operations import CrucibleOperations, OperationError
from crucible_mcp.runner import RunManager


class TestRunManager(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        benchmark = self.root / "subprojects" / "benchmarks" / "example"
        benchmark.mkdir(parents=True)
        (benchmark / "rickshaw.json").write_text('{"benchmark":"example"}', encoding="utf-8")
        schema = self.root / "subprojects" / "core" / "rickshaw" / "schema"
        schema.mkdir(parents=True)
        (schema / "run-file.json").write_text(
            '{"type":"object","required":["benchmarks"]}', encoding="utf-8"
        )
        self.store = JobStore(self.root / "jobs.db")
        event_script = (
            "import os; "
            "open(os.environ['CRUCIBLE_MCP_EVENT_FILE'], 'a').write("
            "'{\\\"state\\\":\\\"postprocessing\\\"}\\n')"
        )
        self.manager = RunManager(
            self.store,
            CrucibleOperations(self.root),
            self.root / "runs",
            [sys.executable, "-c", event_script],
        )

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_submission_is_idempotent_and_completes_asynchronously(self):
        document = {"benchmarks": [{"name": "example"}]}
        job, created = self.manager.submit("key-1", document=document)
        self.assertTrue(created)
        self.assertTrue(job.runner_pid)
        duplicate, duplicate_created = self.manager.submit("key-1", document=document)
        self.assertFalse(duplicate_created)
        self.assertEqual(job.mcp_job_id, duplicate.mcp_job_id)
        thread = self.manager._threads[job.mcp_job_id]
        thread.join(timeout=5)
        self.assertEqual(self.store.get(job.mcp_job_id).state.value, "completed")
        summary_path = self.root / "runs" / job.mcp_job_id / "run" / "result-summary.json"
        summary_path.parent.mkdir()
        summary_path.write_text('{"run":"complete"}', encoding="utf-8")
        refreshed = self.manager.refresh_result_status(job.mcp_job_id)
        self.assertEqual(refreshed.result_status.value, "available")
        self.assertEqual(
            self.manager.get_summary(job.mcp_job_id)["summary"], {"run": "complete"}
        )
        logs = self.manager.get_logs(job.mcp_job_id)
        self.assertTrue(logs["complete"])
        self.assertEqual(logs["text"], "")
        self.assertTrue((self.root / "runs" / job.mcp_job_id / "input" / "run-file.json").is_file())

    def test_failed_runner_is_persisted(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "failed-runs",
            [sys.executable, "-c", "import sys; sys.exit(7)"],
        )
        job, _ = manager.submit("key-2", document={"benchmarks": [{"name": "example"}]})
        manager._threads[job.mcp_job_id].join(timeout=5)
        failed = self.store.get(job.mcp_job_id)
        self.assertEqual(failed.state.value, "failed")
        self.assertEqual(failed.exit_code, 7)

    def test_cdm_failure_after_indexing_is_not_benchmark_failure(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "cdm-failed-runs",
            [
                sys.executable,
                "-c",
                "import json, os, sys; "
                "open(os.environ['CRUCIBLE_MCP_EVENT_FILE'], 'a').write("
                "json.dumps({'state': 'postprocessing'}) + '\\n' + "
                "json.dumps({'state': 'indexing'}) + '\\n'); "
                "import time; time.sleep(0.2); sys.exit(1)",
            ],
            cdm_readiness_timeout=0.01,
        )
        job, _ = manager.submit(
            "key-cdm-failure", document={"benchmarks": [{"name": "example"}]}
        )
        manager._threads[job.mcp_job_id].join(timeout=5)
        completed = self.store.get(job.mcp_job_id)
        self.assertEqual(completed.state, JobState.COMPLETED)
        self.assertEqual(completed.result_status.value, "unavailable")
        self.assertEqual(completed.error_category, "cdm")

    def test_lifecycle_event_persists_correlation_identifiers(self):
        job, _ = self.store.create_or_get("key-identifiers", {"run": 1})
        event_path = self.root / "events.jsonl"
        event_path.write_text(
            '{"state":"starting","run_directory":"/runs/one",'
            '"rickshaw_run_id":"rickshaw-1","cdm_run_id":"cdm-1",'
            '"runner_container_id":"container-1"}\n',
            encoding="utf-8",
        )
        self.manager._consume_events(job.mcp_job_id, event_path, 0)
        updated = self.store.get(job.mcp_job_id)
        self.assertEqual(updated.rickshaw_run_id, "rickshaw-1")
        self.assertEqual(updated.cdm_run_id, "cdm-1")
        self.assertEqual(updated.runner_container_id, "container-1")

    def test_completed_run_backfills_rickshaw_and_cdm_identifiers(self):
        job, _ = self.store.create_or_get("key-backfill", {"run": 1})
        run_directory = self.root / "backfill-run"
        (run_directory / "run").mkdir(parents=True)
        (run_directory / "run" / "rickshaw-run.json").write_text(
            '{"run-id":"rickshaw-2"}', encoding="utf-8"
        )
        (run_directory / "run" / "result-summary.json").write_text(
            '{"cdm_run_id":"cdm-2"}', encoding="utf-8"
        )
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(run_directory),
        )
        self.manager._backfill_identifiers(job.mcp_job_id)
        updated = self.store.get(job.mcp_job_id)
        self.assertEqual(updated.rickshaw_run_id, "rickshaw-2")
        self.assertEqual(updated.cdm_run_id, "cdm-2")

    def test_staging_failure_is_persisted_as_infrastructure_failure(self):
        document = {"benchmarks": [{"name": "example"}]}
        with patch.object(Path, "write_text", side_effect=OSError("no space left on device")):
            job, created = self.manager.submit("key-staging-failure", document=document)

        self.assertTrue(created)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertEqual(job.error_category, "infrastructure")
        self.assertIn("could not stage run input", job.error_message)
        duplicate, duplicate_created = self.manager.submit(
            "key-staging-failure", document=document
        )
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.mcp_job_id, job.mcp_job_id)
        self.assertEqual(duplicate.state, JobState.FAILED)

    def test_processing_setup_failure_is_persisted_as_infrastructure_failure(self):
        target = self.root / "run" / "result"
        target.mkdir(parents=True)
        with patch.object(Path, "mkdir", side_effect=OSError("no space left on device")):
            job, created = self.manager.submit_processing(
                "key-processing-staging-failure", "index", target
            )

        self.assertTrue(created)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertEqual(job.error_category, "infrastructure")
        self.assertIn("could not create processing supervision directory", job.error_message)

    def test_processing_jobs_report_operation_lifecycle_state(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "processing-lifecycle",
            [sys.executable, "-c", "import time; time.sleep(0.3)"],
        )
        target = self.root / "run" / "lifecycle-result"
        target.mkdir(parents=True)
        job, _ = manager.submit_processing("key-processing-lifecycle", "index", target)
        time.sleep(0.05)
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.INDEXING)
        manager._threads[job.mcp_job_id].join(timeout=5)

    def test_input_path_outside_approved_root_returns_authorization_error(self):
        path = self.root / "outside.json"
        path.write_text('{"benchmarks":[{"name":"example"}]}', encoding="utf-8")
        with self.assertRaises(OperationError) as raised:
            self.manager.submit("key-outside", path=path)
        self.assertEqual(raised.exception.category, "authorization")
        self.assertEqual(raised.exception.code, "input_path_rejected")

    def test_reconcile_resolves_previous_recovery_states(self):
        job, _ = self.store.create_or_get("key-recovery", {"run": 1})
        self.store.transition(job.mcp_job_id, JobState.UNKNOWN_AFTER_CRASH)
        changed = self.manager.reconcile()
        self.assertEqual([item.mcp_job_id for item in changed], [job.mcp_job_id])
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.FAILED)

        job, _ = self.store.create_or_get("key-recovery-2", {"run": 2})
        self.store.transition(job.mcp_job_id, JobState.RECOVERY_REQUIRED)
        changed = self.manager.reconcile()
        self.assertEqual([item.mcp_job_id for item in changed], [job.mcp_job_id])
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.FAILED)

    def test_reconcile_reattaches_verified_live_runner(self):
        job, _ = self.store.create_or_get("key-live", {"run": 1})
        run_directory = self.root / "live-run"
        (run_directory / "input").mkdir(parents=True)
        (run_directory / "input" / "run-file.json").write_text("{}", encoding="utf-8")
        self.store.transition(
            job.mcp_job_id, JobState.STARTING, runner_pid=1234, run_directory=str(run_directory)
        )
        with (
            patch.object(self.manager, "_process_exists", return_value=True),
            patch.object(self.manager, "_runner_identity_matches", return_value=True),
            patch.object(self.manager, "_reattach") as reattach,
        ):
            changed = self.manager.reconcile()
        self.assertEqual(changed, [])
        reattach.assert_called_once_with(self.store.get(job.mcp_job_id))

    def test_recovery_uses_processing_supervision_directory(self):
        job, _ = self.store.create_or_get(
            "key-processing-recovery", {"operation": "index"}, "index"
        )
        target = self.root / "target-run"
        supervision = self.root / "mcp-job"
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(target),
            supervision_directory=str(supervision),
        )
        with patch("crucible_mcp.runner.threading.Thread") as thread:
            self.manager._reattach(self.store.get(job.mcp_job_id))
        self.assertEqual(
            thread.call_args.kwargs["args"][1], supervision / "events.jsonl"
        )

    def test_reconcile_marks_unverified_runner_failed(self):
        job, _ = self.store.create_or_get("key-unverified", {"run": 1})
        self.store.transition(job.mcp_job_id, JobState.STARTING, runner_pid=1234)
        with patch.object(self.manager, "_process_exists", return_value=True):
            changed = self.manager.reconcile()
        self.assertEqual([item.mcp_job_id for item in changed], [job.mcp_job_id])
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.FAILED)

    def test_summary_wait_uses_configured_cdm_readiness_timeout(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "summary-timeout-runs",
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            cdm_readiness_timeout=0.01,
        )
        job, _ = manager.submit("key-summary-timeout", document={"benchmarks": [{"name": "example"}]})
        manager._threads[job.mcp_job_id].join(timeout=5)
        started = time.monotonic()
        with self.assertRaises(OperationError) as raised:
            manager.get_summary(job.mcp_job_id)
        self.assertEqual(raised.exception.code, "result_unavailable")
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
