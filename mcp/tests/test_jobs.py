import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from crucible_mcp.jobs import (
    MAX_LIVE_PLAN_HANDLES,
    JobConflictError,
    JobStore,
    PlanHandleCapacityError,
    PlanHandleExpiredError,
)
from crucible_mcp.models import IndexedQueryStatus, JobState, ResultStatus


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

    def test_indexed_query_status_is_persisted_separately(self):
        job, _ = self.store.create_or_get("request-indexed", {"run": 1})
        self.assertEqual(job.indexed_query_status, IndexedQueryStatus.NOT_CHECKED)
        self.assertIsNone(job.indexed_query_checked_at)
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            cdm_run_id="cdm-1",
            result_status=ResultStatus.AVAILABLE.value,
        )

        updated_count = self.store.update_indexed_query_status(
            "cdm-1", IndexedQueryStatus.READY
        )
        database_path = self.store.database_path
        self.store.close()
        self.store = JobStore(database_path)
        updated = self.store.get(job.mcp_job_id)

        self.assertEqual(updated_count, 1)
        self.assertEqual(updated.result_status, ResultStatus.AVAILABLE)
        self.assertEqual(updated.indexed_query_status, IndexedQueryStatus.READY)
        self.assertIsNotNone(updated.indexed_query_checked_at)

    def test_v4_database_migrates_to_supervisor_job_fields(self):
        database_path = Path(self.directory.name) / "legacy-v4.db"
        connection = sqlite3.connect(database_path)
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version(version) VALUES (4)")
        connection.execute("CREATE TABLE jobs (cdm_run_id TEXT)")
        connection.commit()
        connection.close()

        migrated = JobStore(database_path)
        try:
            version = migrated._connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            columns = {
                row[1]
                for row in migrated._connection.execute("PRAGMA table_info(jobs)")
            }
        finally:
            migrated.close()

        self.assertEqual(version, 8)
        self.assertIn("indexed_query_status", columns)
        self.assertIn("indexed_query_checked_at", columns)
        self.assertIn("supervisor_container_name", columns)
        self.assertIn("supervisor_container_id", columns)

    def test_v5_database_migrates_to_supervisor_job_fields(self):
        database_path = Path(self.directory.name) / "legacy-v5.db"
        connection = sqlite3.connect(database_path)
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version(version) VALUES (5)")
        connection.execute("CREATE TABLE jobs (cdm_run_id TEXT)")
        connection.commit()
        connection.close()

        migrated = JobStore(database_path)
        try:
            version = migrated._connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            columns = {
                row[1]
                for row in migrated._connection.execute("PRAGMA table_info(jobs)")
            }
        finally:
            migrated.close()

        self.assertEqual(version, 8)
        self.assertIn("supervisor_container_name", columns)
        self.assertIn("supervisor_container_id", columns)

    def test_supervisor_identifiers_are_internal_job_metadata(self):
        job, _ = self.store.create_or_get("supervisor-internal", {"run": True})
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            supervisor_container_name="crucible-mcp-job-test",
            supervisor_container_id="container-id",
        )

        restored = self.store.get(job.mcp_job_id)
        public = restored.as_dict()
        self.assertEqual(restored.supervisor_container_name, "crucible-mcp-job-test")
        self.assertEqual(restored.supervisor_container_id, "container-id")
        self.assertNotIn("supervisor_container_name", public)
        self.assertNotIn("supervisor_container_id", public)

    def test_plan_summary_persists_across_store_reopen(self):
        summary = {
            "contract_version": "1",
            "input_digest": "digest",
            "totals": {"global_iteration_count": 2},
            "runtime": {"confidence": "unavailable"},
            "limits": {"truncated": False},
        }
        job, _ = self.store.create_or_get(
            "request-plan",
            {"run": 1},
            plan_digest="digest",
            plan_summary=summary,
        )
        database_path = self.store.database_path
        self.store.close()
        self.store = JobStore(database_path)

        restored = self.store.get(job.mcp_job_id)

        self.assertEqual(restored.plan_digest, "digest")
        self.assertEqual(restored.plan_summary, summary)
        self.assertEqual(restored.as_dict()["plan"], summary)

    def _create_plan_handle(self, **updates):
        values = {
            "caller_fingerprint": "a" * 64,
            "input_digest": "input-digest",
            "planner_contract": "planner-v1",
            "component_versions": {"rickshaw": "revision-1"},
            "plan_fingerprint": "b" * 64,
            "effective_limits": {
                "max_parameter_sets": 100,
                "max_engine_ids": 100,
                "max_tool_entries": 100,
                "max_response_bytes": 4096,
            },
        }
        values.update(updates)
        return self.store.create_or_get_plan_handle(**values)

    def test_plan_handles_reuse_only_identical_live_preparations(self):
        original, created = self._create_plan_handle()
        repeated, reused = self._create_plan_handle()

        self.assertTrue(created)
        self.assertFalse(reused)
        self.assertEqual(original.handle, repeated.handle)
        self.assertEqual(original.expires_at, repeated.expires_at)

        for field, value in (
            ("input_digest", "different-input"),
            ("caller_fingerprint", "c" * 64),
            ("planner_contract", "planner-v2"),
            ("component_versions", {"rickshaw": "revision-2"}),
            ("plan_fingerprint", "d" * 64),
            (
                "effective_limits",
                {
                    "max_parameter_sets": 101,
                    "max_engine_ids": 100,
                    "max_tool_entries": 100,
                    "max_response_bytes": 4096,
                },
            ),
        ):
            other, other_created = self._create_plan_handle(**{field: value})
            self.assertTrue(other_created, field)
            self.assertNotEqual(other.handle, original.handle, field)

    def test_plan_handle_survives_store_reopen_without_payload_data(self):
        handle, _ = self._create_plan_handle()
        database_path = self.store.database_path
        self.store.close()
        self.store = JobStore(database_path)

        restored = self.store.get_plan_handle(handle.handle, "a" * 64)

        self.assertEqual(restored, handle)
        columns = {
            row[1]
            for row in self.store._connection.execute("PRAGMA table_info(plan_handles)")
        }
        self.assertNotIn("document", columns)
        self.assertNotIn("plan", columns)
        stored_values = json.dumps(dict(self.store._connection.execute(
            "SELECT * FROM plan_handles WHERE handle = ?", (handle.handle,)
        ).fetchone()))
        self.assertNotIn("run_document", stored_values)
        self.assertNotIn('"benchmarks"', stored_values)
        self.assertNotIn('"totals"', stored_values)

    def test_plan_handle_owner_mismatch_is_hidden_and_expiry_is_distinct(self):
        handle, _ = self._create_plan_handle()
        self.assertIsNone(self.store.get_plan_handle(handle.handle, "z" * 64))

        self.store._connection.execute(
            "UPDATE plan_handles SET expires_at = ? WHERE handle = ?",
            ("2000-01-01T00:00:00+00:00", handle.handle),
        )
        self.store._connection.commit()
        with self.assertRaises(PlanHandleExpiredError):
            self.store.get_plan_handle(handle.handle, "a" * 64)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT COUNT(*) FROM plan_handles WHERE handle = ?", (handle.handle,)
            ).fetchone()[0],
            0,
        )

    def test_expired_handle_cleanup_frees_capacity_and_live_capacity_is_not_evicted(self):
        first, _ = self._create_plan_handle()
        self.store._connection.execute(
            "UPDATE plan_handles SET expires_at = ? WHERE handle = ?",
            ("2000-01-01T00:00:00+00:00", first.handle),
        )
        self.store._connection.commit()

        for index in range(MAX_LIVE_PLAN_HANDLES):
            self._create_plan_handle(input_digest=f"digest-{index}")
        with self.assertRaises(PlanHandleCapacityError):
            self._create_plan_handle(input_digest="over-capacity")

        live = self.store._connection.execute(
            "SELECT COUNT(*) FROM plan_handles"
        ).fetchone()[0]
        self.assertEqual(live, MAX_LIVE_PLAN_HANDLES)

    def test_invalid_transition_is_rejected(self):
        job, _ = self.store.create_or_get("request-1", {"run": 1})
        with self.assertRaises(Exception):
            self.store.transition(job.mcp_job_id, JobState.COMPLETED)

    def test_list_active_supports_bounded_pagination(self):
        jobs = [
            self.store.create_or_get(f"request-{index}", {"run": index})[0]
            for index in range(3)
        ]

        ordered_jobs = sorted(jobs, key=lambda job: (job.created_at, job.mcp_job_id))
        first_page = self.store.list_active(limit=2)
        second_page = self.store.list_active(
            limit=2,
            after=(ordered_jobs[1].created_at, ordered_jobs[1].mcp_job_id),
        )

        self.assertEqual([job.mcp_job_id for job in first_page], [job.mcp_job_id for job in ordered_jobs[:2]])
        self.assertEqual([job.mcp_job_id for job in second_page], [ordered_jobs[2].mcp_job_id])


if __name__ == "__main__":
    unittest.main()
