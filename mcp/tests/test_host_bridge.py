import tempfile
import unittest
import uuid
from pathlib import Path

from crucible_mcp.host_bridge import BridgeRequestError, HostCommandPolicy


class TestHostCommandPolicy(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = self.root / "crucible"
        (self.home / "bin").mkdir(parents=True)
        (self.home / "bin" / "crucible").touch()
        self.job_root = self.root / "jobs"
        self.run_root = self.root / "runs"
        self.archive_root = self.root / "archives"
        for path in (self.job_root, self.run_root, self.archive_root):
            path.mkdir()
        self.run = self.run_root / "run-one"
        self.run.mkdir()
        self.policy = HostCommandPolicy(
            self.home,
            "controller:test",
            self.job_root,
            self.run_root,
            self.archive_root,
        )
        self.job_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.job_directory = self.job_root / self.job_id
        input_directory = self.job_directory / "input"
        input_directory.mkdir(parents=True)
        self.run_file = input_directory / "run-file.json"
        self.run_file.write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def _supervisor_command(self, operation="run", child=None):
        name = f"crucible-mcp-job-{self.job_id}"
        event_file = self.job_directory / "events.jsonl"
        if child is None:
            child = [str(self.home / "bin" / "crucible"), "run", str(self.run_file)]
        return [
            "podman", "run", "--detach", "--rm", "--pull=never", "--name", name,
            "--label", f"io.crucible.mcp.job-id={self.job_id}", "--label",
            "io.crucible.mcp.supervisor=true", "--privileged", "--pid=host", "--ipc=host",
            "--net=host", "--security-opt=label=disable",
            f"--mount=type=bind,source={self.job_directory.resolve()},destination=/job",
            f"--mount=type=bind,source={self.home.resolve()},destination={self.home.resolve()}",
            "--env", f"PYTHONPATH={self.home.resolve() / 'mcp'}", "--env",
            f"CRUCIBLE_MCP_SESSION_ID={self.session_id}", "--env",
            f"CRUCIBLE_MCP_EVENT_FILE={event_file.resolve()}", "controller:test",
            "python3", "-m", "crucible_mcp.supervisor", "--operation", operation,
            "--job-directory", "/job", "--working-directory", str(self.home.resolve()),
            "--", *child,
        ]

    def test_allows_only_expected_supervisor_container_shape(self):
        command = self._supervisor_command()
        validated, environment, timeout = self.policy.validate(
            {"command": command, "working_directory": "/", "environment": {}}
        )
        self.assertEqual(validated[-len(command):], command)
        self.assertNotIn("CONTAINER_HOST", environment)
        self.assertNotIn("CRUCIBLE_MCP_SESSION_ID", environment)
        self.assertEqual(timeout, 30.0)

    def test_rejects_arbitrary_host_commands_and_mount_changes(self):
        with self.assertRaises(BridgeRequestError):
            self.policy.validate(
                {
                    "command": ["/bin/cat", "/root/.ssh/id_rsa"],
                    "working_directory": "/",
                    "environment": {},
                }
            )

        command = self._supervisor_command()
        command[16] = "--mount=type=bind,source=/,destination=/hostfs"
        with self.assertRaises(BridgeRequestError):
            self.policy.validate(
                {"command": command, "working_directory": "/", "environment": {}}
            )

    def test_allows_only_fixed_result_service_start(self):
        home_alias = self.root / "crucible-alias"
        home_alias.symlink_to(self.home, target_is_directory=True)
        command = [str(home_alias / "bin" / "crucible"), "start", "opensearch"]
        validated, _environment, timeout = self.policy.validate(
            {"command": command, "working_directory": str(self.home), "environment": {}}
        )
        self.assertEqual(
            validated[-3:],
            [str(self.policy.home / "bin" / "crucible"), "start", "opensearch"],
        )
        self.assertEqual(timeout, 300.0)

        with self.assertRaises(BridgeRequestError):
            self.policy.validate(
                {
                    "command": [str(self.home / "bin" / "crucible"), "start", "mcp-server"],
                    "working_directory": str(self.home),
                    "environment": {},
                }
            )

    def test_accepts_symlinked_crucible_executable_for_supervisor(self):
        home_alias = self.root / "crucible-alias"
        home_alias.symlink_to(self.home, target_is_directory=True)
        child = [str(home_alias / "bin" / "crucible"), "run", str(self.run_file)]
        command = self._supervisor_command(child=child)

        validated, _environment, _timeout = self.policy.validate(
            {"command": command, "working_directory": "/", "environment": {}}
        )

        self.assertEqual(validated[-3:], child)

    def test_rejects_archive_root_and_nested_targets(self):
        nested = self.run / "nested"
        nested.mkdir()
        command = self._supervisor_command(
            "archive_local_run",
            [str(self.home / "bin" / "crucible"), "archive", str(nested)],
        )
        with self.assertRaises(BridgeRequestError):
            self.policy.validate(
                {"command": command, "working_directory": "/", "environment": {}}
            )

    def test_rejects_processing_roots(self):
        for operation in ("postprocess", "index"):
            for root in (self.run_root, self.job_root):
                command = [
                    str(self.home / "bin" / "crucible"),
                    operation,
                    str(root),
                ]
                with self.subTest(operation=operation, root=root), self.assertRaises(
                    BridgeRequestError
                ):
                    self.policy._validate_cli_operation(operation, command)

    def test_rejects_relative_operation_paths(self):
        with self.assertRaises(BridgeRequestError):
            self.policy._canonical_child("run-one", self.run_root, directory=True)


if __name__ == "__main__":
    unittest.main()
