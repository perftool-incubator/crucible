import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crucible_mcp.host_tools import main
from crucible_mcp.jobs import JobStore


class TestControllerHostTools(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_active_jobs_does_not_create_a_missing_database(self):
        database = self.root / "missing" / "jobs.db"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["active-jobs", "--database", str(database)])

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertFalse(database.exists())

    def test_active_jobs_prints_persisted_jobs(self):
        database = self.root / "jobs.db"
        store = JobStore(database)
        job, _created = store.create_or_get("token", {"run": "test"})
        store.close()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["active-jobs", "--database", str(database)])

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), f"{job.mcp_job_id} queued\n")

    def test_token_operations_delegate_to_the_existing_policy(self):
        token_path = self.root / "token"
        with (
            patch("crucible_mcp.host_tools.validate_token_rotation_path") as validate,
            patch("crucible_mcp.host_tools.rotate_token") as rotate,
        ):
            self.assertEqual(main(["validate-token", "--path", str(token_path)]), 0)
            self.assertEqual(main(["rotate-token", "--path", str(token_path)]), 0)

        self.assertEqual(validate.call_count, 2)
        validate.assert_called_with(token_path)
        rotate.assert_called_once_with(token_path)


if __name__ == "__main__":
    unittest.main()
