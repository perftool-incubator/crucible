import sys
import tempfile
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

    def test_input_path_outside_approved_root_returns_authorization_error(self):
        path = self.root / "outside.json"
        path.write_text('{"benchmarks":[{"name":"example"}]}', encoding="utf-8")
        with self.assertRaises(OperationError) as raised:
            self.manager.submit("key-outside", path=path)
        self.assertEqual(raised.exception.category, "authorization")
        self.assertEqual(raised.exception.code, "input_path_rejected")

    def test_reconcile_is_idempotent_for_recovery_states(self):
        job, _ = self.store.create_or_get("key-recovery", {"run": 1})
        self.store.transition(job.mcp_job_id, JobState.UNKNOWN_AFTER_CRASH)
        self.assertEqual(self.manager.reconcile(), [])
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.UNKNOWN_AFTER_CRASH)

        self.store.transition(job.mcp_job_id, JobState.RECOVERY_REQUIRED)
        self.assertEqual(self.manager.reconcile(), [])
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.RECOVERY_REQUIRED)

    def test_reconcile_marks_live_runner_for_recovery(self):
        job, _ = self.store.create_or_get("key-live", {"run": 1})
        self.store.transition(job.mcp_job_id, JobState.STARTING, runner_pid=1234)
        with patch.object(self.manager, "_process_exists", return_value=True):
            changed = self.manager.reconcile()
        self.assertEqual([item.mcp_job_id for item in changed], [job.mcp_job_id])
        recovered = self.store.get(job.mcp_job_id)
        self.assertEqual(recovered.state, JobState.RECOVERY_REQUIRED)
        self.assertEqual(recovered.error_category, "recovery")


if __name__ == "__main__":
    unittest.main()
