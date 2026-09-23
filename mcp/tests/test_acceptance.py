"""End-to-end acceptance coverage for the execution-capable MCP slice."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from crucible_mcp.jobs import JobStore
from crucible_mcp.operations import CrucibleOperations
from crucible_mcp.policy import InputPolicy, rotate_token
from crucible_mcp.runner import RunManager
from crucible_mcp.server import MCPHandler


class TestMCPExecutionAcceptance(unittest.TestCase):
    """Exercise the first execution workflow through the MCP transport."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        benchmark = self.root / "subprojects" / "benchmarks" / "example"
        benchmark.mkdir(parents=True)
        (benchmark / "rickshaw.json").write_text(
            '{"benchmark":"example"}', encoding="utf-8"
        )
        schema = self.root / "subprojects" / "core" / "rickshaw" / "schema"
        schema.mkdir(parents=True)
        (schema / "run-file.json").write_text(
            '{"type":"object","required":["benchmarks"]}', encoding="utf-8"
        )

        self.run_root = self.root / "runs"
        self.input_root = self.root / "inputs"
        self.job_database = self.root / "jobs.db"
        self.token_path = self.root / "token"
        self.token = rotate_token(self.token_path)
        self.launched = self.root / "run-launched"
        self.release = self.root / "release-runner"
        self.environment = patch.dict(
            os.environ,
            {
                "MCP_ACCEPTANCE_LAUNCHED": str(self.launched),
                "MCP_ACCEPTANCE_RELEASE": str(self.release),
            },
        )
        self.environment.start()

        self.fake_crucible = (
            "import json, os, pathlib, sys, time\n"
            "operation = sys.argv[1]\n"
            "target = pathlib.Path(sys.argv[2])\n"
            "event = pathlib.Path(os.environ['CRUCIBLE_MCP_EVENT_FILE'])\n"
            "if operation == 'run':\n"
            "    run_directory = target.parent.parent / 'run'\n"
            "    run_directory.mkdir(parents=True, exist_ok=True)\n"
            "    (run_directory / 'rickshaw-run.json').write_text("
            "json.dumps({'run-id': 'rickshaw-e2e'})\n"
            ")\n"
            "    event.write_text("
            "json.dumps({'state': 'running'}) + '\\n' + "
            "json.dumps({'state': 'postprocessing'}) + '\\n' + "
            "json.dumps({'state': 'indexing'}) + '\\n'"
            ")\n"
            "    pathlib.Path(os.environ['MCP_ACCEPTANCE_LAUNCHED']).touch()\n"
            "    while not pathlib.Path(os.environ['MCP_ACCEPTANCE_RELEASE']).exists():\n"
            "        time.sleep(0.01)\n"
            "    (run_directory / 'result-summary.json').write_text("
            "json.dumps({'runs': [{'run-id': 'cdm-e2e', 'status': 'pass'}]})\n"
            ")\n"
            "elif operation in ('postprocess', 'index'):\n"
            "    target.mkdir(parents=True, exist_ok=True)\n"
            "else:\n"
            "    raise SystemExit('unexpected operation: ' + operation)\n"
        )

        self.store = JobStore(self.job_database)
        self.operations = CrucibleOperations(
            self.root,
            InputPolicy([self.input_root]),
            run_root=self.run_root,
        )
        self.manager = RunManager(
            self.store,
            self.operations,
            self.run_root,
            [sys.executable, "-c", self.fake_crucible],
            cdm_readiness_timeout=1,
        )
        self.server = self._make_server(self.store, self.operations, self.manager)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()

    def tearDown(self):
        active_pid = getattr(self, "active_pid", None)
        detached_process = getattr(self, "detached_process", None)
        if active_pid is not None and detached_process is None:
            try:
                os.kill(active_pid, 15)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(active_pid, 0)
            except ChildProcessError:
                pass
        elif active_pid is not None and detached_process.poll() is None:
            detached_process.terminate()
            detached_process.wait(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.environment.stop()
        self.directory.cleanup()

    def _make_server(self, store, operations, manager):
        server = ThreadingHTTPServer(("127.0.0.1", 0), MCPHandler)
        server.token_path = self.token_path
        server.jobs = store
        server.operations = operations
        server.run_manager = manager
        server.max_request_bytes = 1024 * 1024
        server.tls_enabled = False
        server.bind_host = "127.0.0.1"
        server.allowed_origins = ()
        return server

    def _call(self, request_id, name, arguments):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(
            "POST",
            "/mcp",
            body=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        return json.loads(response.read())

    def _wait_for_state(self, job_id, expected_state, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self._call(
                f"status-{job_id}",
                "get_run_status",
                {"mcp_job_id": job_id},
            )
            status = response["result"]["structuredContent"]
            if status["state"] == expected_state:
                return status
            time.sleep(0.02)
        self.fail(f"job {job_id} did not reach {expected_state}")

    def test_validate_start_restart_reconcile_process_index_summary_and_retry(self):
        document = {"benchmarks": [{"name": "example"}]}

        validation = self._call(
            "validate",
            "validate_run",
            {"document": document},
        )
        self.assertTrue(validation["result"]["structuredContent"]["valid"])

        # Suppress only the first supervisor thread to model an MCP process
        # restart while the supervised child is still alive.
        def detach_supervisor(*arguments):
            self.detached_process = arguments[1]
            arguments[2].close()

        with patch.object(
            self.manager,
            "_wait_for_completion",
            side_effect=detach_supervisor,
        ):
            started = self._call(
                "start",
                "start_run",
                {"idempotency_key": "acceptance-run", "document": document},
            )
        started_job = started["result"]["structuredContent"]["job"]
        job_id = started_job["mcp_job_id"]
        self.active_pid = started_job["runner_pid"]
        self.assertTrue(started["result"]["structuredContent"]["created"])
        self.assertTrue(started_job["runner_pid"])

        deadline = time.monotonic() + 5
        while not self.launched.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.launched.is_file())

        old_store = self.store
        old_store.close()
        self.store = JobStore(self.job_database)
        self.operations = CrucibleOperations(
            self.root,
            InputPolicy([self.input_root]),
            run_root=self.run_root,
        )
        self.manager = RunManager(
            self.store,
            self.operations,
            self.run_root,
            [sys.executable, "-c", self.fake_crucible],
            cdm_readiness_timeout=1,
        )
        self.server.jobs = self.store
        self.server.operations = self.operations
        self.server.run_manager = self.manager

        summary_path = self.run_root / job_id / "run" / "result-summary.json"

        def recovered_process_exists(_pid):
            # The test process remains the original child reaper, unlike a
            # restarted MCP process.  Use the generated artifact as the
            # deterministic point at which the reattached runner disappears.
            return not summary_path.is_file()

        with patch.object(
            self.manager,
            "_process_exists",
            side_effect=recovered_process_exists,
        ):
            reconciled = self.manager.reconcile()
            self.assertEqual(reconciled, [])
            self.release.touch()
            completed = self._wait_for_state(job_id, "completed")
        try:
            self.detached_process.wait(timeout=5)
        except ChildProcessError:
            # A restarted supervisor cannot reap the child, so another
            # interpreter may have reaped it before Popen observed exit.
            self.detached_process.returncode = 0
        self.active_pid = None
        self.assertEqual(completed["rickshaw_run_id"], "rickshaw-e2e")
        self.assertEqual(completed["cdm_run_id"], "cdm-e2e")

        postprocessed = self._call(
            "postprocess",
            "postprocess_local_run",
            {
                "idempotency_key": "acceptance-postprocess",
                "run_path": completed["run_directory"],
            },
        )
        postprocess_job = postprocessed["result"]["structuredContent"]["job"]
        self._wait_for_state(postprocess_job["mcp_job_id"], "completed")

        indexed = self._call(
            "index",
            "index_local_run",
            {
                "idempotency_key": "acceptance-index",
                "run_path": completed["run_directory"],
            },
        )
        index_job = indexed["result"]["structuredContent"]["job"]
        self._wait_for_state(index_job["mcp_job_id"], "completed")

        summary = self._call(
            "summary",
            "get_run_summary",
            {"mcp_job_id": job_id},
        )
        summary_value = summary["result"]["structuredContent"]
        self.assertEqual(summary_value["result_status"], "available")
        self.assertEqual(summary_value["summary"]["runs"][0]["run-id"], "cdm-e2e")

        retry = self._call(
            "retry",
            "start_run",
            {"idempotency_key": "acceptance-run", "document": document},
        )
        retry_value = retry["result"]["structuredContent"]
        self.assertFalse(retry_value["created"])
        self.assertEqual(retry_value["job"]["mcp_job_id"], job_id)


if __name__ == "__main__":
    unittest.main()
