"""Small, dependency-free MCP HTTP bootstrap.

This is the transport and health-check foundation for the service.  The
operation handlers are intentionally narrow until the typed operation layer is
implemented; unsupported tools return structured errors rather than invoking
shell commands or exposing arbitrary filesystem access.
"""

import argparse
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft201909Validator

from .jobs import JobConflictError, JobNotFoundError, JobStore
from .models import Job
from .operations import CrucibleOperations, OperationError
from .policy import InputPolicy, PolicyError, read_token, token_matches
from .runner import RunManager
from .audit import AuditLogger


TOOL_NAMES = (
    "crucible_info",
    "list_tools",
    "list_benchmarks",
    "list_results",
    "get_result",
    "list_run_periods",
    "get_metric",
    "list_log_sessions",
    "get_log_info",
    "describe_benchmark",
    "validate_run",
    "start_run",
    "get_run_status",
    "get_run_logs",
    "get_run_summary",
)

_EMPTY_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}
TOOL_DEFINITIONS = (
    {"name": "crucible_info", "description": "Describe Crucible MCP capabilities.", "inputSchema": _EMPTY_INPUT},
    {
        "name": "list_tools",
        "description": "List installed Crucible tools and their metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        },
    },
    {"name": "list_benchmarks", "description": "List installed Crucible benchmarks.", "inputSchema": _EMPTY_INPUT},
    {
        "name": "list_results",
        "description": "List historical run IDs from the configured CDM service.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "email": {"type": "string", "minLength": 1},
                "harness": {"type": "string", "minLength": 1},
                "benchmark": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_result",
        "description": "Get structured metadata for a historical CDM run.",
        "inputSchema": {"type": "object", "properties": {"run": {"type": "string", "minLength": 1}}, "required": ["run"], "additionalProperties": False},
    },
    {
        "name": "list_run_periods",
        "description": "List the primary periods and samples associated with a historical run.",
        "inputSchema": {"type": "object", "properties": {"run": {"type": "string", "minLength": 1}}, "required": ["run"], "additionalProperties": False},
    },
    {
        "name": "get_metric",
        "description": "Query metric data for a historical CDM run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string", "minLength": 1}, "period": {"type": "string", "minLength": 1},
                "source": {"type": "string", "minLength": 1}, "type": {"type": "string", "minLength": 1},
                "begin": {"type": "integer", "minimum": 0}, "end": {"type": "integer", "minimum": 0},
                "resolution": {"type": "integer", "minimum": 1, "maximum": 100000},
                "breakout": {"type": "array", "items": {"type": "string"}}, "filter": {"type": "string"},
                "aggregation": {"type": "string", "enum": ["sum", "avg", "max", "min"]},
                "distribution_stats": {"type": "string"}, "allow_incompatible_aggregation": {"type": "boolean"}
            },
            "required": ["run", "source", "type"], "additionalProperties": False
        },
    },
    {
        "name": "list_log_sessions",
        "description": "List recent Crucible logger sessions.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, "additionalProperties": False},
    },
    {"name": "get_log_info", "description": "Return aggregate Crucible logger database counts.", "inputSchema": _EMPTY_INPUT},
    {
        "name": "describe_benchmark",
        "description": "Describe an installed benchmark.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "validate_run",
        "description": "Validate an inline run document or approved run-file path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document": {"type": "object"},
                "path": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
            "oneOf": [{"required": ["document"]}, {"required": ["path"]}],
        },
    },
    {
        "name": "start_run",
        "description": "Start an idempotent asynchronous Crucible run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string", "minLength": 1},
                "document": {"type": "object"},
                "path": {"type": "string", "minLength": 1},
            },
            "required": ["idempotency_key"],
            "additionalProperties": False,
            "oneOf": [{"required": ["document"]}, {"required": ["path"]}],
        },
    },
    {
        "name": "get_run_status",
        "description": "Get lifecycle and result readiness for an MCP run.",
        "inputSchema": {
            "type": "object",
            "properties": {"mcp_job_id": {"type": "string", "minLength": 1}},
            "required": ["mcp_job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_run_logs",
        "description": "Retrieve bounded runner logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mcp_job_id": {"type": "string", "minLength": 1},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1048576},
            },
            "required": ["mcp_job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_run_summary",
        "description": "Retrieve a completed run summary when results are ready.",
        "inputSchema": {
            "type": "object",
            "properties": {"mcp_job_id": {"type": "string", "minLength": 1}},
            "required": ["mcp_job_id"],
            "additionalProperties": False,
        },
    },
)
_TOOL_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOL_DEFINITIONS}


class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server variant for IPv6 loopback bindings."""

    address_family = socket.AF_INET6


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

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return False
        try:
            expected = read_token(self.server.token_path)
        except PolicyError:
            return False
        matched = token_matches(authorization[7:], expected)
        if matched:
            self._authenticated_token = expected
        return matched

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/mcp":
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path != "/health":
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._audit("health", "denied")
            self._json(401, {"error": "unauthorized"})
            return
        self._audit("health", "success")
        self._json(200, {"status": "ok", "service": "mcp-server"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/mcp":
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._audit("mcp", "denied")
            self._json(401, {"error": "unauthorized"})
            return
        if not self._origin_allowed():
            self._audit("mcp", "denied")
            self._json(403, {"error": "origin_not_allowed"})
            return
        request: Any = {}
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > self.server.max_request_bytes:
                raise ValueError("request size is outside configured bounds")
            request = json.loads(self.rfile.read(length))
            if self._is_notification(request):
                operation = request["method"]
                self._audit(operation, "success")
                self._empty(202)
                return
            response = self._dispatch(request)
        except (ValueError, json.JSONDecodeError) as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": str(exc)}}
        operation = request.get("method", "invalid") if isinstance(request, dict) else "invalid"
        params = request.get("params", {}) if isinstance(request, dict) else {}
        arguments = params.get("arguments") if isinstance(params, dict) else None
        self._audit(
            operation,
            "error" if "error" in response else "success",
            job_id=arguments.get("mcp_job_id") if isinstance(arguments, dict) else None,
        )
        self._json(200, response)

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme != "http" or parsed.username or parsed.password:
            return False
        if parsed.path or parsed.query or parsed.fragment or port != self.server.server_port:
            return False
        allowed_hosts = {"localhost", "127.0.0.1"}
        if isinstance(self.server, IPv6ThreadingHTTPServer):
            allowed_hosts.add("::1")
        return parsed.hostname in allowed_hosts

    @staticmethod
    def _is_notification(request: Any) -> bool:
        return (
            isinstance(request, dict)
            and request.get("jsonrpc") == "2.0"
            and isinstance(request.get("method"), str)
            and "id" not in request
        )

    def _audit(self, operation: str, outcome: str, job_id: str | None = None) -> None:
        audit = getattr(self.server, "audit", None)
        if audit is not None:
            audit.record(
                operation=operation,
                outcome=outcome,
                source_address=self.client_address[0],
                job_id=job_id,
                token=getattr(self, "_authenticated_token", None),
            )

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid JSON-RPC request"}}
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "params must be an object")
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
                "result": {"tools": list(TOOL_DEFINITIONS)},
            }
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "arguments must be an object")
        if not isinstance(name, str):
            return self._error(request_id, -32602, "tool name must be a string")
        schema = _TOOL_SCHEMAS.get(name)
        if schema is None:
            return self._error(request_id, -32602, "unknown tool")
        validation_error = next(iter(Draft201909Validator(schema).iter_errors(arguments)), None)
        if validation_error is not None:
            return self._error(request_id, -32602, f"invalid arguments: {validation_error.message}")
        try:
            if name == "crucible_info":
                value = self.server.operations.crucible_info()
            elif name == "list_tools":
                value = {"tools": self.server.operations.list_tools(arguments.get("name"))}
            elif name == "list_benchmarks":
                value = {"benchmarks": self.server.operations.list_benchmarks()}
            elif name == "list_results":
                value = self.server.operations.list_results(
                    **{key: arguments[key] for key in ("run", "name", "email", "harness", "benchmark", "limit") if key in arguments}
                )
            elif name == "get_result":
                value = self.server.operations.get_result(arguments.get("run", ""))
            elif name == "list_run_periods":
                value = self.server.operations.list_run_periods(arguments.get("run", ""))
            elif name == "get_metric":
                value = self.server.operations.get_metric(
                    run=arguments.get("run", ""), source=arguments.get("source", ""),
                    metric_type=arguments.get("type", ""), period=arguments.get("period"),
                    begin=arguments.get("begin"), end=arguments.get("end"),
                    resolution=arguments.get("resolution", 1), breakout=arguments.get("breakout"),
                    filter=arguments.get("filter"), aggregation=arguments.get("aggregation"),
                    distribution_stats=arguments.get("distribution_stats"),
                    allow_incompatible_aggregation=arguments.get("allow_incompatible_aggregation", False),
                )
            elif name == "list_log_sessions":
                value = self.server.operations.list_log_sessions(arguments.get("limit", 100))
            elif name == "get_log_info":
                value = self.server.operations.get_log_info()
            elif name == "describe_benchmark":
                value = self.server.operations.describe_benchmark(arguments.get("name", ""))
            elif name == "validate_run":
                if "document" in arguments and "path" in arguments:
                    return self._error(request_id, -32602, "provide exactly one of document or path")
                if "path" in arguments and not isinstance(arguments["path"], str):
                    return self._error(request_id, -32602, "path must be a string")
                if "document" in arguments:
                    value = self.server.operations.validate_run(arguments["document"])
                elif "path" in arguments:
                    value = self.server.operations.validate_run_file(Path(arguments["path"]))
                else:
                    return self._error(request_id, -32602, "validate_run requires document or path")
            elif name == "start_run":
                if "document" in arguments and "path" in arguments:
                    return self._error(request_id, -32602, "provide exactly one of document or path")
                if "path" in arguments and not isinstance(arguments["path"], str):
                    return self._error(request_id, -32602, "path must be a string")
                if "document" in arguments:
                    job, created = self.server.run_manager.submit(
                        arguments.get("idempotency_key", ""),
                        document=arguments["document"],
                    )
                elif "path" in arguments:
                    job, created = self.server.run_manager.submit(
                        arguments.get("idempotency_key", ""),
                        path=Path(arguments["path"]),
                    )
                else:
                    return self._error(request_id, -32602, "start_run requires document or path")
                value = {"created": created, "job": _job_status(job)}
            elif name == "get_run_status":
                try:
                    job = self.server.run_manager.refresh_result_status(arguments["mcp_job_id"])
                    value = _job_status(job)
                    value["results_ready"] = value["result_status"] == "available"
                except KeyError as exc:
                    return self._error(request_id, -32602, str(exc))
            elif name == "get_run_logs":
                try:
                    value = self.server.run_manager.get_logs(
                        arguments["mcp_job_id"],
                        int(arguments.get("offset", 0)),
                        int(arguments.get("limit", 65_536)),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    return self._error(request_id, -32602, str(exc))
            elif name == "get_run_summary":
                if "mcp_job_id" not in arguments:
                    return self._error(request_id, -32602, "mcp_job_id is required")
                value = self.server.run_manager.get_summary(arguments["mcp_job_id"])
            else:
                return self._error(request_id, -32602, "unknown tool")
        except JobConflictError as exc:
            return self._error(request_id, -32009, str(exc))
        except JobNotFoundError as exc:
            return self._error(request_id, -32004, str(exc))
        except OperationError as exc:
            return self._error(request_id, -32000, json.dumps(exc.as_dict()))
        return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def log_message(self, *_: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Crucible MCP service")
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--max-request-bytes", type=int, default=1_048_576)
    parser.add_argument("--crucible-home", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--max-run-file-bytes", type=int, default=1_048_576)
    parser.add_argument("--cdm-readiness-timeout", type=int, default=60)
    parser.add_argument("--cdm-url", default="http://127.0.0.1:3000")
    parser.add_argument("--log-db", type=Path)
    parser.add_argument("--audit-log", type=Path, default=Path("/var/lib/crucible/logs/mcp-audit.jsonl"))
    parser.add_argument("--audit-max-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--audit-retained-files", type=int, default=5)
    args = parser.parse_args()

    server_class = IPv6ThreadingHTTPServer if ":" in args.bind else ThreadingHTTPServer
    server = server_class((args.bind, args.port), MCPHandler)
    server.token_path = args.token_file
    server.jobs = JobStore(args.database)
    server.operations = CrucibleOperations(
        args.crucible_home,
        InputPolicy([args.input_root], args.max_run_file_bytes),
        cdm_base_url=args.cdm_url,
        log_db=args.log_db,
    )
    server.run_manager = RunManager(
        server.jobs,
        server.operations,
        args.database.parent / "runs",
        [str(args.crucible_home / "bin" / "crucible")],
        args.max_request_bytes,
        args.cdm_readiness_timeout,
    )
    server.audit = AuditLogger(args.audit_log, args.audit_max_bytes, args.audit_retained_files)
    server.run_manager.reconcile()
    server.max_request_bytes = args.max_request_bytes
    try:
        server.serve_forever()
    finally:
        server.jobs.close()
        server.server_close()


if __name__ == "__main__":
    main()
