import tempfile
import unittest
from pathlib import Path

from crucible_mcp.jobs import JobConflictError, JobStore
from crucible_mcp.models import JobState, ResultStatus


class TestJobStore(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.directory.name) / "mcp" / "jobs.db")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_idempotent_submission_returns_existing_job(self):
        request = {"inline_json": {"benchmarks": [{"name": "sleep"}]}}
        first, created = self.store.create_or_get("request-1", request)
        second, duplicate = self.store.create_or_get("request-1", request)

        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(first.mcp_job_id, second.mcp_job_id)
        self.assertEqual(first.state, JobState.QUEUED)

    def test_reusing_key_for_different_request_is_rejected(self):
        self.store.create_or_get("request-1", {"run": 1})
        with self.assertRaises(JobConflictError):
            self.store.create_or_get("request-1", {"run": 2})

    def test_transition_persists_identifiers_and_result_status(self):
        job, _ = self.store.create_or_get("request-1", {"run": 1})
        updated = self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            logger_session_id="logger-1",
            run_directory="/var/lib/crucible/run/job-1",
        )
        updated = self.store.transition(
            job.mcp_job_id,
            JobState.RUNNING,
            runner_pid=123,
        )
        updated = self.store.transition(
            job.mcp_job_id,
            JobState.POSTPROCESSING,
        )
        updated = self.store.transition(
            job.mcp_job_id,
            JobState.INDEXING,
            rickshaw_run_id="rickshaw-1",
            result_status=ResultStatus.PENDING.value,
        )
        updated = self.store.transition(
            job.mcp_job_id,
            JobState.COMPLETED,
            cdm_run_id="cdm-1",
            result_status=ResultStatus.AVAILABLE.value,
            exit_code=0,
        )

        self.assertEqual(updated.logger_session_id, "logger-1")
        self.assertEqual(updated.rickshaw_run_id, "rickshaw-1")
        self.assertEqual(updated.cdm_run_id, "cdm-1")
        self.assertEqual(updated.result_status, ResultStatus.AVAILABLE)

    def test_invalid_transition_is_rejected(self):
        job, _ = self.store.create_or_get("request-1", {"run": 1})
        with self.assertRaises(Exception):
            self.store.transition(job.mcp_job_id, JobState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
