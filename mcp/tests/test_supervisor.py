import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crucible_mcp.host import host_context_command
from crucible_mcp.supervisor import run_job


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.job_directory = Path("/job")
        (self.root / "job").mkdir(parents=True)

    def tearDown(self):
        self.directory.cleanup()

    def _map_job_path(self, path: Path) -> Path:
        self.assertEqual(path, Path("/job"))
        return self.root / "job"

    def test_run_job_persists_exit_code_and_host_context_output(self):
        result = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            patch("crucible_mcp.supervisor._job_directory_path", side_effect=self._map_job_path),
            patch("crucible_mcp.supervisor.subprocess.run", return_value=result) as run,
            patch.dict(
                "os.environ",
                {
                    "CRUCIBLE_MCP_SESSION_ID": "session-1",
                    "CRUCIBLE_MCP_EVENT_FILE": "/var/lib/crucible/mcp/runs/job-1/events.jsonl",
                },
                clear=True,
            ),
        ):
            exit_code = run_job(
                "run",
                self.job_directory,
                Path("/opt/crucible"),
                ["/opt/crucible/bin/crucible", "run", "/tmp/run-file.json"],
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            run.call_args.args[0],
            host_context_command(
                ["/opt/crucible/bin/crucible", "run", "/tmp/run-file.json"],
                "/opt/crucible",
            ),
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["CRUCIBLE_MCP_SESSION_ID"], "session-1"
        )
        outcome = json.loads(
            (self.root / "job" / "supervisor-outcome.json").read_text()
        )
        self.assertEqual(outcome, {"operation": "run", "exit_code": 0})
        self.assertFalse(
            (self.root / "job" / "processing-complete").exists()
        )

    def test_successful_maintenance_job_writes_completion_marker(self):
        with (
            patch(
                "crucible_mcp.supervisor._job_directory_path",
                side_effect=self._map_job_path,
            ),
            patch(
                "crucible_mcp.supervisor.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0),
            ),
        ):
            exit_code = run_job(
                "index",
                self.job_directory,
                Path("/opt/crucible"),
                ["/opt/crucible/bin/crucible", "index", "/var/lib/crucible/run/x"],
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            (self.root / "job" / "processing-complete").read_text(),
            "index\n",
        )

    def test_failed_maintenance_job_does_not_mark_completion(self):
        with (
            patch("crucible_mcp.supervisor._job_directory_path", side_effect=self._map_job_path),
            patch(
                "crucible_mcp.supervisor.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=7),
            ),
        ):
            exit_code = run_job(
                "postprocess",
                self.job_directory,
                Path("/opt/crucible"),
                ["/opt/crucible/bin/crucible", "postprocess", "/var/lib/crucible/run/x"],
            )

        self.assertEqual(exit_code, 7)
        self.assertFalse(
            (self.root / "job" / "processing-complete").exists()
        )
        outcome = json.loads(
            (self.root / "job" / "supervisor-outcome.json").read_text()
        )
        self.assertEqual(outcome["exit_code"], 7)

    def test_signaled_child_status_is_preserved_in_durable_outcome(self):
        with (
            patch(
                "crucible_mcp.supervisor._job_directory_path",
                side_effect=self._map_job_path,
            ),
            patch(
                "crucible_mcp.supervisor.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=-15),
            ),
        ):
            exit_code = run_job(
                "run",
                self.job_directory,
                Path("/opt/crucible"),
                ["/opt/crucible/bin/crucible", "run", "/tmp/run-file.json"],
            )

        self.assertEqual(exit_code, -15)
        outcome = json.loads(
            (self.root / "job" / "supervisor-outcome.json").read_text()
        )
        self.assertEqual(outcome, {"operation": "run", "exit_code": -15})


if __name__ == "__main__":
    unittest.main()
