import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from socketserver import TCPServer

from crucible_mcp.operations import CrucibleOperations
from crucible_mcp.policy import InputPolicy, rotate_token
from crucible_mcp.server import MCPHandler
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

    def test_health_requires_authentication(self):
        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 401)
        status, payload = self.request("GET", "/health", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_initialize_and_tools_list(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["serverInfo"]["name"], "crucible-mcp")

        body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        _, payload = self.request("POST", "/mcp", body, self.token)
        self.assertIn("start_run", [tool["name"] for tool in payload["result"]["tools"]])

    def test_crucible_info_is_structured(self):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "crucible_info", "arguments": {}},
        })
        status, payload = self.request("POST", "/mcp", body, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["structuredContent"]["mcp_contract_version"], "1")


if __name__ == "__main__":
    unittest.main()
