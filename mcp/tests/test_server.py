import json
import os
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock
from http.client import HTTPConnection
from pathlib import Path
from socketserver import TCPServer

from crucible_mcp.operations import CrucibleOperations, OperationError
from crucible_mcp.jobs import JobConflictError, JobNotFoundError
from crucible_mcp.audit import AuditLogger
from crucible_mcp.models import Job, JobState, ResultStatus
from crucible_mcp.policy import InputPolicy, rotate_token
from crucible_mcp.server import IPv6ThreadingHTTPServer, MCPHandler
from http.server import ThreadingHTTPServer


class TestServer(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.token_path = root / "token"
        self.token = rotate_token(self.token_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), MCPHandler)
        server.token_path = self.token_path
        from crucible_mcp.jobs import JobStore

        server.jobs = JobStore(root / "jobs.db")
        crucible_home = Path(os.environ.get("CRUCIBLE_HOME", Path.cwd()))
        server.operations = CrucibleOperations(
            crucible_home,
            InputPolicy([root / "inputs"]),
            run_root=root / "runs",
        )
        server.max_request_bytes = 1024 * 1024
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.jobs.close()
        self.server.server_close()
        self.directory.cleanup()

    def request(self, method, path, body=None, token=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def request_raw(self, method, path, body=None, token=None, extra_headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), response.headers

    def test_health_requires_authentication(self):
        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 401)
        status, payload = self.request("GET", "/health", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_initialized_notification_returns_accepted_without_body(self):
        body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        status, payload, headers = self.request_raw("POST", "/mcp", body, self.token)
        self.assertEqual(status, 202)
        self.assertEqual(payload, b"")
        self.assertEqual(headers["Content-Length"], "0")

    def test_mcp_get_returns_method_not_allowed_without_body(self):
        status, payload, headers = self.request_raw("GET", "/mcp", token=self.token)
        self.assertEqual(status, 405)
        self.assertEqual(payload, b"")
        self.assertEqual(headers["Allow"], "POST")

    def test_mcp_origin_validation_allows_local_and_rejects_remote_origins(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        for origin in (
            f"http://127.0.0.1:{self.server.server_port}",
            f"http://localhost:{self.server.server_port}",
        ):
            status, payload, _ = self.request_raw(
                "POST", "/mcp", body, self.token, {"Origin": origin}
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload, b'{"jsonrpc":"2.0","id":1,"result":{}}')

        status, payload, _ = self.request_raw(
            "POST",
            "/mcp",
            body,
            self.token,
            {"Origin": "http://evil.example:443"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)["error"], "origin_not_allowed")

        status, payload, _ = self.request_raw(
            "POST", "/mcp", body, self.token, {"Origin": "null"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)["error"], "origin_not_allowed")

    def test_ipv6_server_uses_ipv6_address_family(self):
        self.assertEqual(IPv6ThreadingHTTPServer.address_family, socket.AF_INET6)

    def test_initialize_and_tools_list(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["serverInfo"]["name"], "crucible-mcp")
        self.assertIn("resources", payload["result"]["capabilities"])

        body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        _, payload = self.request("POST", "/mcp", body, self.token)
        tools = {tool["name"]: tool for tool in payload["result"]["tools"]}
        self.assertIn("start_run", tools)
        self.assertIn("inputSchema", tools["start_run"])
        self.assertEqual(tools["start_run"]["inputSchema"]["required"], ["idempotency_key"])
        self.assertIn("plan_digest", tools["start_run"]["inputSchema"]["properties"])
        self.assertIn("inputSchema", tools["get_run_logs"])
        self.assertIn("offset", tools["list_local_runs"]["inputSchema"]["properties"])
        for tool_name in (
            "list_tools", "list_endpoints", "list_active_runs", "list_local_runs", "get_local_run_summary", "get_local_run_metadata",
            "list_run_artifacts", "get_run_artifact", "list_local_archives",
            "archive_local_run", "unarchive_local_run", "list_indexed_results", "get_indexed_result", "list_indexed_periods", "get_indexed_metric",
            "list_log_sessions", "get_log_info", "search_documentation",
            "prepare_run", "estimate_run",
            "get_log_session",
            "search_logs",
            "list_local_run_tags", "add_local_run_tags", "remove_local_run_tags",
            "delete_indexed_result",
        ):
            self.assertIn(tool_name, tools)
            self.assertIn("inputSchema", tools[tool_name])

        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_endpoints", "arguments": {}},
        })
        _, payload = self.request("POST", "/mcp", body, self.token)
        self.assertIn("endpoints", payload["result"]["structuredContent"])

    def test_run_planning_tools_dispatch_inline_documents(self):
        plan = {
            "contract_version": "1",
            "input_digest": "digest",
            "validation": {"valid": True, "errors": [], "warnings": []},
            "totals": {"global_iteration_count": 2},
            "runtime": {"confidence": "unavailable"},
            "limits": {"truncated": False},
        }
        document = {"benchmarks": [], "endpoints": []}

        for request_id, name in ((30, "prepare_run"), (31, "estimate_run")):
            operation = Mock(return_value=plan)
            setattr(self.server.operations, name, operation)
            body = json.dumps({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": {
                        "document": document,
                        "max_parameter_sets": 7,
                    },
                },
            })

            status, payload = self.request("POST", "/mcp", body, self.token)

            with self.subTest(name=name):
                self.assertEqual(status, 200)
                self.assertEqual(payload["result"]["structuredContent"], plan)
                operation.assert_called_once_with(
                    document,
                    max_parameter_sets=7,
                    max_response_bytes=1048576,
                    request_id=request_id,
                )

    def test_start_run_passes_plan_digest_and_returns_plan_summary(self):
        plan = {
            "contract_version": "1",
            "input_digest": "digest",
            "validation": {"valid": True, "errors": [], "warnings": []},
            "totals": {"global_iteration_count": 2},
            "runtime": {"confidence": "unavailable"},
            "limits": {"truncated": False},
        }
        document = {"benchmarks": []}
        job = Job(
            mcp_job_id="planned-job",
            idempotency_key="planned-key",
            request_hash="hash",
            state=JobState.QUEUED,
            result_status=ResultStatus.NOT_AVAILABLE,
            plan_digest="digest",
            plan_summary={
                "contract_version": "1",
                "input_digest": "digest",
                "totals": {"global_iteration_count": 2},
                "runtime": {"confidence": "unavailable"},
                "limits": {"truncated": False},
            },
        )
        self.server.run_manager = Mock()
        self.server.run_manager.submit.return_value = (job, True)
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {
                "name": "start_run",
                "arguments": {
                    "idempotency_key": "planned-key",
                    "document": document,
                    "plan_digest": "digest",
                },
            },
        })

        status, payload = self.request("POST", "/mcp", body, self.token)

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["structuredContent"]["job"]["plan"], {
            "contract_version": "1",
            "input_digest": "digest",
            "totals": {"global_iteration_count": 2},
            "runtime": {"confidence": "unavailable"},
            "limits": {"truncated": False},
        })
        self.server.run_manager.submit.assert_called_once_with(
            "planned-key",
            document=document,
            plan_digest="digest",
        )

    def test_start_run_rejects_stale_plan_digest(self):
        self.server.run_manager = Mock()
        self.server.run_manager.submit.side_effect = OperationError(
            "user", "plan digest does not match the submitted run", "stale_plan"
        )
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 33,
            "method": "tools/call",
            "params": {
                "name": "start_run",
                "arguments": {
                    "idempotency_key": "stale-key",
                    "document": {"benchmarks": []},
                    "plan_digest": "old",
                },
            },
        })

        status, payload = self.request("POST", "/mcp", body, self.token)

        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32000)
        self.assertIn("stale_plan", payload["error"]["message"])
        self.server.run_manager.submit.assert_called_once_with(
            "stale-key",
            document={"benchmarks": []},
            plan_digest="old",
        )

    def test_metadata_response_bound_includes_wire_request_id(self):
        run_directory = self.server.operations.local_run_root / "wire-metadata"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps({"entries": [{"password": "x"} for _ in range(20000)]}),
            encoding="utf-8",
        )
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "request-" + ("x" * 100000),
                "method": "tools/call",
                "params": {
                    "name": "get_local_run_metadata",
                    "arguments": {"run_path": str(run_directory)},
                },
            }
        )

        status, payload = self.request("POST", "/mcp", body, self.token)

        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32000)
        self.assertIn("result_too_large", payload["error"]["message"])

    def test_estimate_path_dispatches_projected_operation(self):
        estimate = {
            "contract_version": "1",
            "input_digest": "digest",
            "validation": {"valid": True, "errors": [], "warnings": []},
            "totals": {"global_iteration_count": 2},
            "runtime": {"confidence": "unavailable"},
            "limits": {"truncated": False},
        }
        operation = Mock(return_value=estimate)
        self.server.operations.estimate_run_file = operation
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {
                "name": "estimate_run",
                "arguments": {
                    "path": "/approved/run.json",
                    "max_response_bytes": 1500,
                },
            },
        })

        status, payload = self.request("POST", "/mcp", body, self.token)

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["structuredContent"], estimate)
        operation.assert_called_once_with(
            Path("/approved/run.json"),
            max_response_bytes=1500,
            request_id=32,
        )

    def test_list_active_runs_is_bounded_and_paginated(self):
        jobs = [
            self.server.jobs.create_or_get(f"active-{index}", {"run": index})[0]
            for index in range(3)
        ]

        def call(cursor=None):
            body = json.dumps({
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "list_active_runs",
                    "arguments": {
                        "cursor": cursor,
                        "limit": 2,
                    } if cursor else {"limit": 2},
                },
            })
            status, payload = self.request("POST", "/mcp", body, self.token)
            self.assertEqual(status, 200)
            return payload["result"]["structuredContent"]

        first_page = call()
        ordered_jobs = sorted(jobs, key=lambda job: (job.created_at, job.mcp_job_id))
        self.server.jobs.transition(ordered_jobs[0].mcp_job_id, JobState.STARTING)
        self.server.jobs.transition(ordered_jobs[0].mcp_job_id, JobState.RUNNING)
        self.server.jobs.transition(ordered_jobs[0].mcp_job_id, JobState.COMPLETED)
        second_page = call(first_page["next_cursor"])

        self.assertEqual(
            [job["mcp_job_id"] for job in first_page["jobs"]],
            [job.mcp_job_id for job in ordered_jobs[:2]],
        )
        self.assertEqual(first_page["count"], 2)
        self.assertFalse(first_page["complete"])
        self.assertIsInstance(first_page["next_cursor"], str)
        self.assertEqual([job["mcp_job_id"] for job in second_page["jobs"]], [ordered_jobs[2].mcp_job_id])
        self.assertEqual(second_page["count"], 1)
        self.assertTrue(second_page["complete"])

    def test_list_active_runs_rejects_malformed_cursor_as_invalid_params(self):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "list_active_runs",
                "arguments": {"cursor": "not-a-valid-cursor"},
            },
        })
        status, payload = self.request("POST", "/mcp", body, self.token)

        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], 21)
        self.assertEqual(payload["error"]["code"], -32602)

    def test_documentation_resources_are_curated_and_readable(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 11, "method": "resources/list"})
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        resources = payload["result"]["resources"]
        resource = next(item for item in resources if item["name"] == "run-files")
        self.assertEqual(resource["uri"], "crucible://docs/run-files")
        self.assertEqual(resource["mimeType"], "text/markdown")
        workflow = next(
            item for item in resources if item["name"] == "agentic-perf-workflow"
        )
        self.assertEqual(
            workflow["uri"], "crucible://docs/agentic-perf-workflow"
        )

        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 12,
            "method": "resources/read",
            "params": {"uri": resource["uri"]},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        contents = payload["result"]["contents"]
        self.assertEqual(contents[0]["uri"], resource["uri"])
        self.assertIn("run file", contents[0]["text"].lower())

    def test_artifact_tools_resolve_completed_mcp_jobs(self):
        job, _ = self.server.jobs.create_or_get(
            "artifact-source", {"operation": "run"}
        )
        self.server.jobs.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory="/approved/run",
        )
        self.server.jobs.transition(job.mcp_job_id, JobState.RUNNING)
        self.server.jobs.transition(job.mcp_job_id, JobState.COMPLETED)
        self.server.operations.list_run_artifacts = Mock(
            return_value={"run_path": "/approved/run", "artifacts": []}
        )

        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 16,
            "method": "tools/call",
            "params": {
                "name": "list_run_artifacts",
                "arguments": {"mcp_job_id": job.mcp_job_id},
            },
        })
        status, payload = self.request("POST", "/mcp", body, self.token)

        self.assertEqual(status, 200)
        self.assertNotIn("error", payload)
        self.server.operations.list_run_artifacts.assert_called_once_with(
            Path("/approved/run"), 0, 100
        )

    def test_artifact_tools_resolve_failed_mcp_jobs_with_run_directories(self):
        job, _ = self.server.jobs.create_or_get(
            "failed-artifact-source", {"operation": "run"}
        )
        self.server.jobs.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory="/approved/failed-run",
        )
        self.server.jobs.transition(job.mcp_job_id, JobState.RUNNING)
        self.server.jobs.transition(
            job.mcp_job_id,
            JobState.FAILED,
            error_category="runtime",
            error_message="benchmark failed",
            exit_code=1,
        )
        self.server.operations.list_run_artifacts = Mock(
            return_value={"run_path": "/approved/failed-run", "artifacts": []}
        )
        self.server.operations.get_run_artifact = Mock(
            return_value={
                "run_path": "/approved/failed-run",
                "artifact_path": "run/tool-data/failure.txt",
                "text": "failure details",
            }
        )

        for request_id, name, arguments in (
            (
                17,
                "list_run_artifacts",
                {"mcp_job_id": job.mcp_job_id},
            ),
            (
                18,
                "get_run_artifact",
                {
                    "mcp_job_id": job.mcp_job_id,
                    "artifact_path": "run/tool-data/failure.txt",
                },
            ),
        ):
            body = json.dumps({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            status, payload = self.request("POST", "/mcp", body, self.token)
            with self.subTest(name=name):
                self.assertEqual(status, 200)
                self.assertNotIn("error", payload)

        self.server.operations.list_run_artifacts.assert_called_once_with(
            Path("/approved/failed-run"), 0, 100
        )
        self.server.operations.get_run_artifact.assert_called_once_with(
            Path("/approved/failed-run"),
            "run/tool-data/failure.txt",
            0,
            131072,
            18,
        )

    def test_documentation_resource_rejects_arbitrary_paths(self):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 13,
            "method": "resources/read",
            "params": {"uri": "file:///etc/passwd"},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32000)

    def test_documentation_search_returns_resource_metadata(self):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "search_documentation",
                "arguments": {"query": "run-file format", "limit": 3},
            },
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        value = payload["result"]["structuredContent"]
        self.assertLessEqual(value["count"], 3)
        self.assertTrue(any(item["name"] == "run-files" for item in value["resources"]))

    def test_crucible_info_is_structured(self):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "crucible_info", "arguments": {}},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        info = payload["result"]["structuredContent"]
        self.assertTrue(info["execution_supported"])
        self.assertEqual(info["mcp_contract_version"], "2")
        self.assertIn("start_run", info["capabilities"])
        self.assertIn("get_run_logs", info["capabilities"])
        self.assertIn("get_run_summary", info["capabilities"])
        self.assertIn("list_endpoints", info["capabilities"])
        self.assertIn("archive_local_run", info["capabilities"])
        self.assertIn("unarchive_local_run", info["capabilities"])
        self.assertIn("list_run_artifacts", info["capabilities"])
        self.assertIn("get_run_artifact", info["capabilities"])

    def test_invalid_parameter_shapes_return_json_rpc_errors(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": []})
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32602)

        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": [], "arguments": {}},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32602)

        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "list_indexed_results", "arguments": {"limit": "1"}},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32602)

        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "get_indexed_metric",
                "arguments": {
                    "run": "run-1", "source": "fio", "type": "IOPS",
                    "period": "measurement", "breakout": "hostname",
                },
            },
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32602)

        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "crucible_info", "arguments": []},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32602)

    def test_job_store_errors_return_json_rpc_errors(self):
        self.server.run_manager = Mock()
        self.server.run_manager.get_logs.side_effect = JobNotFoundError("unknown MCP job: missing")
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "get_run_logs", "arguments": {"mcp_job_id": "missing"}},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32004)

        self.server.run_manager.refresh_result_status.side_effect = JobNotFoundError(
            "unknown MCP job: missing"
        )
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "get_run_status", "arguments": {"mcp_job_id": "missing"}},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32004)

        self.server.run_manager.submit.side_effect = JobConflictError("idempotency conflict")
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "start_run",
                "arguments": {"idempotency_key": "duplicate", "document": {}},
            },
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32009)

    def test_non_string_paths_return_json_rpc_errors(self):
        for tool_name in ("validate_run", "start_run"):
            arguments = {"path": None}
            if tool_name == "start_run":
                arguments["idempotency_key"] = "path-type"
            body = json.dumps({
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            status, payload = self.request("POST", "/mcp", body, self.token)
            self.assertEqual(status, 200)
            self.assertEqual(payload["error"]["code"], -32602)

    def test_missing_summary_job_id_returns_json_rpc_error(self):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "get_run_summary", "arguments": {}},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], -32602)

    def test_tool_audit_records_tool_name_and_generated_job_id(self):
        audit_path = Path(self.directory.name) / "audit.jsonl"
        self.server.audit = AuditLogger(audit_path)
        self.server.run_manager = Mock()
        job = Job(
            mcp_job_id="generated-job",
            idempotency_key="delete-key",
            request_hash="hash",
            state=JobState.QUEUED,
            result_status=ResultStatus.NOT_AVAILABLE,
            operation="delete_indexed_result",
        )
        self.server.run_manager.submit_indexed_deletion.return_value = (job, True)
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {
                "name": "delete_indexed_result",
                "arguments": {"idempotency_key": "delete-key", "run": "run-1"},
            },
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertNotIn("error", payload)
        record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["operation"], "delete_indexed_result")
        self.assertEqual(record["job_id"], "generated-job")


class TestOriginValidation(unittest.TestCase):
    @staticmethod
    def handler(origin="https://ui.example:443"):
        handler = object.__new__(MCPHandler)
        handler.headers = {
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        }
        handler.path = "/mcp"
        handler.server = Mock(
            tls_enabled=True,
            bind_host="0.0.0.0",
            server_port=8889,
            allowed_origins=("https://ui.example:443",),
        )
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        return handler

    def test_wildcard_bind_accepts_allowlisted_origin_on_another_port(self):
        handler = self.handler()

        self.assertTrue(MCPHandler._origin_allowed(handler))

    def test_https_origin_omitted_port_matches_listener_port_443(self):
        handler = self.handler("https://mcp.example")
        handler.server.bind_host = "mcp.example"
        handler.server.server_port = 443

        self.assertTrue(MCPHandler._origin_allowed(handler))

    def test_wildcard_origin_normalizes_explicit_default_port(self):
        handler = self.handler("https://ui.example")

        self.assertTrue(MCPHandler._origin_allowed(handler))

    def test_allowlisted_origin_receives_cors_preflight_headers(self):
        handler = self.handler()

        MCPHandler.do_OPTIONS(handler)

        handler.send_response.assert_called_once_with(204)
        headers = dict(call.args for call in handler.send_header.call_args_list)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://ui.example:443")
        self.assertEqual(headers["Access-Control-Allow-Methods"], "POST")
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])


if __name__ == "__main__":
    unittest.main()
