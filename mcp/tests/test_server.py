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

from crucible_mcp.operations import CrucibleOperations
from crucible_mcp.jobs import JobConflictError, JobNotFoundError
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
        server.operations = CrucibleOperations(crucible_home, InputPolicy([root / "inputs"]))
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
        self.assertIn("inputSchema", tools["get_run_logs"])
        for tool_name in (
            "list_tools", "list_local_runs", "get_local_run_summary", "get_local_run_metadata", "list_local_archives", "archive_local_run", "unarchive_local_run", "list_indexed_results", "get_indexed_result", "list_indexed_periods", "get_indexed_metric",
            "list_log_sessions", "get_log_info", "search_documentation",
            "get_log_session",
            "search_logs",
            "list_local_run_tags", "add_local_run_tags", "remove_local_run_tags",
            "delete_indexed_result",
        ):
            self.assertIn(tool_name, tools)
            self.assertIn("inputSchema", tools[tool_name])

    def test_documentation_resources_are_curated_and_readable(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 11, "method": "resources/list"})
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        resources = payload["result"]["resources"]
        resource = next(item for item in resources if item["name"] == "run-files")
        self.assertEqual(resource["uri"], "crucible://docs/run-files")
        self.assertEqual(resource["mimeType"], "text/markdown")

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
        self.assertIn("start_run", info["capabilities"])
        self.assertIn("get_run_logs", info["capabilities"])
        self.assertIn("get_run_summary", info["capabilities"])

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


if __name__ == "__main__":
    unittest.main()
