"""Small, dependency-free MCP HTTP bootstrap.

This is the transport and health-check foundation for the service.  The
operation handlers are intentionally narrow until the typed operation layer is
implemented; unsupported tools return structured errors rather than invoking
shell commands or exposing arbitrary filesystem access.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .jobs import JobNotFoundError, JobStore
from .models import Job
from .policy import PolicyError, read_token, token_matches


TOOL_NAMES = (
    "crucible_info",
    "list_benchmarks",
    "describe_benchmark",
    "validate_run",
    "start_run",
    "get_run_status",
    "get_run_logs",
    "get_run_summary",
)


def _job_status(job: Job) -> dict[str, Any]:
    return job.as_dict()


class MCPHandler(BaseHTTPRequestHandler):
    server_version = "CrucibleMCP/0.1"

    def _json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return False
        try:
            expected = read_token(self.server.token_path)
        except PolicyError:
            return False
        return token_matches(authorization[7:], expected)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/health":
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        self._json(200, {"status": "ok", "service": "mcp-server"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/mcp":
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > self.server.max_request_bytes:
                raise ValueError("request size is outside configured bounds")
            request = json.loads(self.rfile.read(length))
            response = self._dispatch(request)
        except (ValueError, json.JSONDecodeError) as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": str(exc)}}
        self._json(200, response)

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "invalid JSON-RPC request"}}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "crucible-mcp", "version": "0.1.0"},
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": [{"name": name, "description": f"Crucible MCP operation: {name}"} for name in TOOL_NAMES]},
            }
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "crucible_info":
            value = {"name": "crucible", "mcp_contract_version": "1", "capabilities": list(TOOL_NAMES)}
        elif name == "get_run_status":
            try:
                value = _job_status(self.server.jobs.get(arguments["mcp_job_id"]))
            except (KeyError, JobNotFoundError) as exc:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}
        elif name in TOOL_NAMES:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": "operation not implemented"}}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "unknown tool"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}}

    def log_message(self, *_: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Crucible MCP service")
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--max-request-bytes", type=int, default=1_048_576)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.bind, args.port), MCPHandler)
    server.token_path = args.token_file
    server.jobs = JobStore(args.database)
    server.max_request_bytes = args.max_request_bytes
    try:
        server.serve_forever()
    finally:
        server.jobs.close()
        server.server_close()


if __name__ == "__main__":
    main()
