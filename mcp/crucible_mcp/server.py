"""Small, dependency-free MCP HTTP bootstrap.

This is the transport and health-check foundation for the service.  The
operation handlers are intentionally narrow until the typed operation layer is
implemented; unsupported tools return structured errors rather than invoking
shell commands or exposing arbitrary filesystem access.
"""

import argparse
import base64
import json
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft201909Validator

from .jobs import JobConflictError, JobNotFoundError, JobStore
from .models import Job, JobState
from .operations import (
    CrucibleOperations,
    OperationError,
    MAX_LOG_RESPONSE_BYTES,
    MAX_PLAN_RESPONSE_BYTES,
)
from .policy import InputPolicy, PolicyError, read_token, token_matches
from .runner import RunManager
from .audit import AuditLogger


TOOL_NAMES = (
    "crucible_info",
    "list_tools",
    "list_benchmarks",
    "list_local_runs",
    "list_active_runs",
    "get_local_run_summary",
    "get_local_run_metadata",
    "list_run_artifacts",
    "get_run_artifact",
    "list_local_archives",
    "archive_local_run",
    "unarchive_local_run",
    "list_indexed_results",
    "get_indexed_result",
    "list_indexed_periods",
    "get_indexed_metric",
    "list_log_sessions",
    "get_log_info",
    "get_log_session",
    "search_logs",
    "describe_benchmark",
    "list_endpoints",
    "validate_run",
    "prepare_run",
    "estimate_run",
    "start_run",
    "get_run_status",
    "get_run_logs",
    "get_run_summary",
    "postprocess_local_run",
    "index_local_run",
    "delete_indexed_result",
    "list_local_run_tags",
    "add_local_run_tags",
    "remove_local_run_tags",
    "search_documentation",
)

_INDEXED_RESULT_TOOLS = {
    "list_indexed_results",
    "get_indexed_result",
    "list_indexed_periods",
    "get_indexed_metric",
}
_RESULT_SERVICE_START_LOCK = threading.Lock()
_RESULT_SERVICE_START_TIMEOUT = 240

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
    {
        "name": "list_endpoints",
        "description": "List installed endpoint implementations, schemas, and coarse capabilities.",
        "inputSchema": _EMPTY_INPUT,
    },
    {
        "name": "list_active_runs",
        "description": "List active MCP jobs, including runs and maintenance operations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cursor": {"type": "string", "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {"name": "list_benchmarks", "description": "List installed Crucible benchmarks.", "inputSchema": _EMPTY_INPUT},
    {
        "name": "list_local_runs",
        "description": "List local run artifacts from approved run roots.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1000000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_local_run_summary",
        "description": "Read a completed result summary from an approved local run artifact.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_path": {"type": "string", "minLength": 1}},
            "required": ["run_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_local_run_metadata",
        "description": "Read rickshaw run metadata from an approved local run artifact.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_path": {"type": "string", "minLength": 1}},
            "required": ["run_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_run_artifacts",
        "description": "List metadata for approved artifacts in a local run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_path": {"type": "string", "minLength": 1},
                "mcp_job_id": {"type": "string", "minLength": 1},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 99999,
                    "description": "Traversal position returned by a previous page.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "oneOf": [{"required": ["run_path"]}, {"required": ["mcp_job_id"]}],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_run_artifact",
        "description": "Read a bounded UTF-8 slice of an approved local run artifact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_path": {"type": "string", "minLength": 1},
                "mcp_job_id": {"type": "string", "minLength": 1},
                "artifact_path": {"type": "string", "minLength": 1},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1073741824},
                "limit": {"type": "integer", "minimum": 1, "maximum": 131072},
            },
            "required": ["artifact_path"],
            "oneOf": [{"required": ["run_path"]}, {"required": ["mcp_job_id"]}],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_local_archives",
        "description": "List local run archives without accessing remote archive backends.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
            "additionalProperties": False,
        },
    },
    {
        "name": "archive_local_run",
        "description": "Archive an approved local run and remove the live run after success.",
        "inputSchema": {"type": "object", "properties": {
            "idempotency_key": {"type": "string", "minLength": 1},
            "run_path": {"type": "string", "minLength": 1}},
            "required": ["idempotency_key", "run_path"], "additionalProperties": False},
    },
    {
        "name": "unarchive_local_run",
        "description": "Restore a local run archive into the approved run root.",
        "inputSchema": {"type": "object", "properties": {
            "idempotency_key": {"type": "string", "minLength": 1},
            "archive_path": {"type": "string", "minLength": 1}},
            "required": ["idempotency_key", "archive_path"], "additionalProperties": False},
    },
    {
        "name": "list_indexed_results",
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
        "name": "get_indexed_result",
        "description": "Get structured metadata for a historical CDM run.",
        "inputSchema": {"type": "object", "properties": {"run": {"type": "string", "minLength": 1}}, "required": ["run"], "additionalProperties": False},
    },
    {
        "name": "list_indexed_periods",
        "description": "List the primary periods and samples associated with a historical run.",
        "inputSchema": {"type": "object", "properties": {"run": {"type": "string", "minLength": 1}}, "required": ["run"], "additionalProperties": False},
    },
    {
        "name": "get_indexed_metric",
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
        "name": "get_log_session",
        "description": "Read a bounded structured slice of a Crucible logger session.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
            "stream": {"type": "string", "enum": ["stdout", "stderr"]},
            "grep": {"type": "string", "maxLength": 256}},
            "required": ["session_id"], "additionalProperties": False},
    },
    {
        "name": "search_logs",
        "description": "Search Crucible logger lines across sessions.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 256},
            "session_id": {"type": "string", "minLength": 1},
            "stream": {"type": "string", "enum": ["stdout", "stderr"]},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
            "since": {"type": "number"},
            "until": {"type": "number"}},
            "required": ["query"], "additionalProperties": False},
    },
    {
        "name": "describe_benchmark",
        "description": "Describe an installed benchmark, including accepted parameter validation rules when available.",
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
        "name": "prepare_run",
        "description": "Build a bounded, side-effect-free plan for an inline run document or approved run-file path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document": {"type": "object"},
                "path": {"type": "string", "minLength": 1},
                "max_parameter_sets": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_engine_ids": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_tool_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_response_bytes": {"type": "integer", "minimum": 1024, "maximum": MAX_PLAN_RESPONSE_BYTES},
            },
            "additionalProperties": False,
            "oneOf": [{"required": ["document"]}, {"required": ["path"]}],
        },
    },
    {
        "name": "estimate_run",
        "description": "Estimate static run counts and report runtime confidence without executing the run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document": {"type": "object"},
                "path": {"type": "string", "minLength": 1},
                "max_parameter_sets": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_engine_ids": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_tool_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_response_bytes": {"type": "integer", "minimum": 1024, "maximum": MAX_PLAN_RESPONSE_BYTES},
            },
            "additionalProperties": False,
            "oneOf": [{"required": ["document"]}, {"required": ["path"]}],
        },
    },
    {
        "name": "start_run",
        "description": "Start an idempotent asynchronous Crucible run, optionally verifying a prepared plan digest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string", "minLength": 1},
                "plan_digest": {"type": "string", "minLength": 1, "maxLength": 128},
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
    {
        "name": "postprocess_local_run",
        "description": "Post-process an approved Crucible run directory.",
        "inputSchema": {"type": "object", "properties": {
            "idempotency_key": {"type": "string", "minLength": 1},
            "run_path": {"type": "string", "minLength": 1},
            "mcp_job_id": {"type": "string", "minLength": 1}},
            "required": ["idempotency_key"], "oneOf": [{"required": ["run_path"]}, {"required": ["mcp_job_id"]}],
            "additionalProperties": False},
    },
    {
        "name": "index_local_run",
        "description": "Index an approved Crucible run directory into CDM.",
        "inputSchema": {"type": "object", "properties": {
            "idempotency_key": {"type": "string", "minLength": 1},
            "run_path": {"type": "string", "minLength": 1},
            "mcp_job_id": {"type": "string", "minLength": 1}},
            "required": ["idempotency_key"], "oneOf": [{"required": ["run_path"]}, {"required": ["mcp_job_id"]}],
            "additionalProperties": False},
    },
    {
        "name": "delete_indexed_result",
        "description": "Delete one indexed CDM result without removing local run artifacts.",
        "inputSchema": {"type": "object", "properties": {
            "idempotency_key": {"type": "string", "minLength": 1},
            "run": {"type": "string", "minLength": 1}},
            "required": ["idempotency_key", "run"], "additionalProperties": False},
    },
    {
        "name": "list_local_run_tags",
        "description": "List tags from an approved local run result.",
        "inputSchema": {"type": "object", "properties": {
            "run_path": {"type": "string", "minLength": 1},
            "mcp_job_id": {"type": "string", "minLength": 1}},
            "oneOf": [{"required": ["run_path"]}, {"required": ["mcp_job_id"]}],
            "additionalProperties": False},
    },
    {
        "name": "add_local_run_tags",
        "description": "Add or replace tags on an approved local run result.",
        "inputSchema": {"type": "object", "properties": {
            "run_path": {"type": "string", "minLength": 1},
            "mcp_job_id": {"type": "string", "minLength": 1},
            "tags": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}},
            "required": ["tags"], "oneOf": [{"required": ["run_path"]}, {"required": ["mcp_job_id"]}],
            "additionalProperties": False},
    },
    {
        "name": "remove_local_run_tags",
        "description": "Remove named tags from an approved local run result.",
        "inputSchema": {"type": "object", "properties": {
            "run_path": {"type": "string", "minLength": 1},
            "mcp_job_id": {"type": "string", "minLength": 1},
            "names": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}},
            "required": ["names"], "oneOf": [{"required": ["run_path"]}, {"required": ["mcp_job_id"]}],
            "additionalProperties": False},
    },
    {
        "name": "search_documentation",
        "description": "Search curated user-facing Crucible documentation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 4096},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
)
_TOOL_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOL_DEFINITIONS}


class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server variant for IPv6 loopback bindings."""

    address_family = socket.AF_INET6


class TLSHTTPServerMixin:
    """Defer TLS handshakes until the connection has a worker thread."""

    tls_context: ssl.SSLContext
    tls_handshake_timeout = 10

    def get_request(self):  # noqa: D102 - socketserver API
        request, client_address = self.socket.accept()
        request = self.tls_context.wrap_socket(
            request,
            server_side=True,
            do_handshake_on_connect=False,
        )
        return request, client_address

    def process_request_thread(self, request, client_address):  # noqa: D102 - socketserver API
        try:
            request.settimeout(self.tls_handshake_timeout)
            request.do_handshake()
            request.settimeout(None)
        except (OSError, ssl.SSLError):
            request.close()
            return
        super().process_request_thread(request, client_address)


class TLSHTTPServer(TLSHTTPServerMixin, ThreadingHTTPServer):
    """Threaded HTTPS server with worker-bound TLS handshakes."""


class TLSIPv6ThreadingHTTPServer(TLSHTTPServerMixin, IPv6ThreadingHTTPServer):
    """IPv6 threaded HTTPS server with worker-bound TLS handshakes."""


def _job_status(job: Job) -> dict[str, Any]:
    return job.as_dict()


def _encode_active_cursor(job: Job) -> str:
    payload = json.dumps(
        [job.created_at, job.mcp_job_id], separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_active_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid active job cursor") from exc
    if (
        not isinstance(values, list)
        or len(values) != 2
        or not all(isinstance(value, str) and value for value in values)
    ):
        raise ValueError("invalid active job cursor")
    return values[0], values[1]


class MCPHandler(BaseHTTPRequestHandler):
    server_version = "CrucibleMCP/0.1"

    def _json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self._send_cors_headers()
        self.end_headers()

    def _send_cors_headers(self, *, preflight: bool = False) -> None:
        origin = self.headers.get("Origin")
        if origin is None or not self._origin_allowed():
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        if preflight:
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type, MCP-Protocol-Version, MCP-Session-Id",
            )

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/mcp":
            self._json(404, {"error": "not_found"})
            return
        if not self._origin_allowed():
            self._json(403, {"error": "origin_not_allowed"})
            return
        if self.headers.get("Access-Control-Request-Method") != "POST":
            self._empty(405)
            return
        requested_headers = {
            value.strip().lower()
            for value in self.headers.get("Access-Control-Request-Headers", "").split(",")
            if value.strip()
        }
        allowed_headers = {
            "authorization",
            "content-type",
            "mcp-protocol-version",
            "mcp-session-id",
        }
        if not requested_headers <= allowed_headers:
            self._json(403, {"error": "headers_not_allowed"})
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._send_cors_headers(preflight=True)
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
        operation, job_id = self._audit_context(request, response)
        self._audit(
            operation,
            "error" if "error" in response else "success",
            job_id=job_id,
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
        expected_scheme = "https" if getattr(self.server, "tls_enabled", False) else "http"
        if parsed.scheme != expected_scheme or parsed.username or parsed.password:
            return False
        if parsed.path or parsed.query or parsed.fragment:
            return False
        effective_port = port if port is not None else (443 if parsed.scheme == "https" else 80)
        configured_bind = getattr(self.server, "bind_host", "")
        if configured_bind in {"0.0.0.0", "::"}:
            for allowed in self.server.allowed_origins:
                try:
                    allowed_origin = urlsplit(allowed)
                    allowed_port = allowed_origin.port
                except ValueError:
                    continue
                if allowed_port is None:
                    allowed_port = 443 if allowed_origin.scheme == "https" else 80
                if (
                    allowed_origin.scheme == parsed.scheme
                    and allowed_origin.hostname == parsed.hostname
                    and allowed_port == effective_port
                    and allowed_origin.path in {"", "/"}
                    and not allowed_origin.query
                    and not allowed_origin.fragment
                    and not allowed_origin.username
                    and not allowed_origin.password
                ):
                    return True
            return False
        if effective_port != self.server.server_port:
            return False
        allowed_hosts = {"localhost", "127.0.0.1"}
        if isinstance(self.server, IPv6ThreadingHTTPServer):
            allowed_hosts.add("::1")
        if configured_bind:
            allowed_hosts.add(configured_bind)
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

    @staticmethod
    def _audit_context(request: Any, response: dict[str, Any]) -> tuple[str, str | None]:
        """Identify the MCP tool and job affected by a request."""

        if not isinstance(request, dict):
            return "invalid", None
        method = request.get("method", "invalid")
        if method != "tools/call":
            return method, None
        params = request.get("params")
        if not isinstance(params, dict):
            return method, None
        tool_name = params.get("name")
        operation = tool_name if isinstance(tool_name, str) else method
        arguments = params.get("arguments")
        job_id = arguments.get("mcp_job_id") if isinstance(arguments, dict) else None
        result = response.get("result") if isinstance(response, dict) else None
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        job = structured.get("job") if isinstance(structured, dict) else None
        generated_job_id = job.get("mcp_job_id") if isinstance(job, dict) else None
        if isinstance(generated_job_id, str):
            job_id = generated_job_id
        return operation, job_id

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
                    "capabilities": {"tools": {}, "resources": {}},
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
        if method == "resources/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"resources": self.server.operations.list_documentation()},
            }
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri:
                return self._error(request_id, -32602, "uri must be a string")
            try:
                resource = self.server.operations.read_documentation(uri)
            except OperationError as exc:
                return self._error(request_id, -32000, json.dumps(exc.as_dict()))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"contents": [resource]},
            }
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        with self.server.jobs.lifecycle_lock():
            return self._call_tool_locked(request_id, params)

    def _call_tool_locked(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
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
            # Service startup can take minutes; reject an invalid query window first.
            if (
                name == "get_indexed_metric"
                and arguments.get("period") is None
                and (arguments.get("begin") is None or arguments.get("end") is None)
            ):
                raise OperationError(
                    "user", "provide period or both begin and end", "invalid_metric_range"
                )
            if name in _INDEXED_RESULT_TOOLS:
                self.server.operations.ensure_result_services()
            if name == "crucible_info":
                value = self.server.operations.crucible_info()
            elif name == "list_tools":
                value = {"tools": self.server.operations.list_tools(arguments.get("name"))}
            elif name == "list_endpoints":
                value = self.server.operations.list_endpoints()
            elif name == "list_active_runs":
                cursor = arguments.get("cursor")
                limit = arguments.get("limit", 100)
                try:
                    after = _decode_active_cursor(cursor) if cursor else None
                except ValueError as exc:
                    return self._error(request_id, -32602, str(exc))
                jobs = self.server.jobs.list_active(limit=limit + 1, after=after)
                complete = len(jobs) <= limit
                if not complete:
                    jobs = jobs[:limit]
                next_cursor = _encode_active_cursor(jobs[-1]) if not complete else None
                value = {
                    "jobs": [_job_status(job) for job in jobs],
                    "cursor": cursor,
                    "next_cursor": next_cursor,
                    "complete": complete,
                    "count": len(jobs),
                }
            elif name == "list_benchmarks":
                value = {"benchmarks": self.server.operations.list_benchmarks()}
            elif name == "list_local_runs":
                value = self.server.operations.list_local_runs(
                    arguments.get("limit", 1000),
                    arguments.get("offset", 0),
                    request_id=request_id,
                )
            elif name == "get_local_run_summary":
                value = self.server.operations.get_local_run_summary(
                    Path(arguments["run_path"]), request_id=request_id
                )
            elif name == "get_local_run_metadata":
                value = self.server.operations.get_local_run_metadata(
                    Path(arguments["run_path"]), request_id=request_id
                )
            elif name in {"list_run_artifacts", "get_run_artifact"}:
                if "run_path" in arguments:
                    artifact_run_path = Path(arguments["run_path"])
                else:
                    source_job = self.server.jobs.get(arguments["mcp_job_id"])
                    if source_job.state not in {JobState.COMPLETED, JobState.FAILED}:
                        return self._error(request_id, -32000, "source job has not completed")
                    if not source_job.run_directory:
                        return self._error(request_id, -32000, "source job has no run directory")
                    artifact_run_path = Path(source_job.run_directory)
                if name == "list_run_artifacts":
                    value = self.server.operations.list_run_artifacts(
                        artifact_run_path,
                        arguments.get("offset", 0),
                        arguments.get("limit", 100),
                    )
                else:
                    value = self.server.operations.get_run_artifact(
                        artifact_run_path,
                        arguments["artifact_path"],
                        arguments.get("offset", 0),
                        arguments.get("limit", 131072),
                        request_id,
                    )
            elif name == "list_local_archives":
                value = self.server.operations.list_local_archives(arguments.get("limit", 1000))
            elif name in {"archive_local_run", "unarchive_local_run"}:
                path = Path(arguments["run_path"] if name == "archive_local_run" else arguments["archive_path"])
                job, created = self.server.run_manager.submit_archive_operation(
                    arguments["idempotency_key"], name, path
                )
                value = {"created": created, "job": _job_status(job)}
            elif name == "list_indexed_results":
                value = self.server.operations.list_indexed_results(
                    **{key: arguments[key] for key in ("run", "name", "email", "harness", "benchmark", "limit") if key in arguments}
                )
            elif name == "get_indexed_result":
                value = self.server.operations.get_indexed_result(arguments.get("run", ""))
            elif name == "list_indexed_periods":
                value = self.server.operations.list_indexed_periods(arguments.get("run", ""))
            elif name == "get_indexed_metric":
                value = self.server.operations.get_indexed_metric(
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
            elif name == "get_log_session":
                value = self.server.operations.get_log_session(
                    arguments["session_id"], arguments.get("offset", 0),
                    arguments.get("limit", 1000), arguments.get("stream"),
                    arguments.get("grep"), request_id,
                )
            elif name == "search_logs":
                value = self.server.operations.search_logs(
                    arguments["query"], arguments.get("session_id"),
                    arguments.get("stream"), arguments.get("offset", 0),
                    arguments.get("limit", 1000), arguments.get("since"),
                    arguments.get("until"), request_id,
                )
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
            elif name in {"prepare_run", "estimate_run"}:
                if "document" in arguments and "path" in arguments:
                    return self._error(request_id, -32602, "provide exactly one of document or path")
                limits = {
                    key: arguments[key]
                    for key in ("max_parameter_sets", "max_engine_ids", "max_tool_entries")
                    if key in arguments
                }
                response_limit = arguments.get("max_response_bytes", MAX_PLAN_RESPONSE_BYTES)
                if "document" in arguments:
                    value = getattr(self.server.operations, name)(
                        arguments["document"],
                        max_response_bytes=response_limit,
                        request_id=request_id,
                        **limits,
                    )
                elif "path" in arguments:
                    operation = (
                        self.server.operations.prepare_run_file
                        if name == "prepare_run"
                        else self.server.operations.estimate_run_file
                    )
                    value = operation(
                        Path(arguments["path"]),
                        max_response_bytes=response_limit,
                        request_id=request_id,
                        **limits,
                    )
                else:
                    return self._error(request_id, -32602, f"{name} requires document or path")
            elif name == "start_run":
                if "document" in arguments and "path" in arguments:
                    return self._error(request_id, -32602, "provide exactly one of document or path")
                if "path" in arguments and not isinstance(arguments["path"], str):
                    return self._error(request_id, -32602, "path must be a string")
                plan_digest = arguments.get("plan_digest")
                if "document" in arguments:
                    job, created = self.server.run_manager.submit(
                        arguments.get("idempotency_key", ""),
                        document=arguments["document"],
                        plan_digest=plan_digest,
                    )
                elif "path" in arguments:
                    job, created = self.server.run_manager.submit(
                        arguments.get("idempotency_key", ""),
                        path=Path(arguments["path"]),
                        plan_digest=plan_digest,
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
                    log_offset = int(arguments.get("offset", 0))
                    read_limit = int(arguments.get("limit", 65_536))
                    while True:
                        value = self.server.run_manager.get_logs(
                            arguments["mcp_job_id"], log_offset, read_limit
                        )
                        value["text"] = self.server.operations.redact_log_text(
                            value.get("text", "")
                        )
                        if self.server.operations._mcp_response_size(
                            value, request_id
                        ) <= MAX_LOG_RESPONSE_BYTES:
                            break
                        if read_limit <= 1:
                            raise OperationError(
                                "framework",
                                "log response exceeds size limit; retry with a smaller limit",
                                "result_too_large",
                            )
                        read_limit = max(1, read_limit // 2)
                except (KeyError, TypeError, ValueError) as exc:
                    return self._error(request_id, -32602, str(exc))
            elif name == "get_run_summary":
                if "mcp_job_id" not in arguments:
                    return self._error(request_id, -32602, "mcp_job_id is required")
                value = self.server.run_manager.get_summary(arguments["mcp_job_id"])
            elif name in {"postprocess_local_run", "index_local_run"}:
                if "run_path" in arguments:
                    processing_path = Path(arguments["run_path"])
                else:
                    source_job = self.server.jobs.get(arguments["mcp_job_id"])
                    if source_job.state != JobState.COMPLETED:
                        return self._error(request_id, -32000, "source job has not completed")
                    if not source_job.run_directory:
                        return self._error(request_id, -32000, "source job has no run directory")
                    processing_path = Path(source_job.run_directory)
                job, created = self.server.run_manager.submit_processing(
                    arguments["idempotency_key"],
                    "postprocess" if name == "postprocess_local_run" else "index",
                    processing_path,
                )
                value = {"created": created, "job": _job_status(job)}
            elif name == "delete_indexed_result":
                job, created = self.server.run_manager.submit_indexed_deletion(
                    arguments["idempotency_key"], arguments["run"]
                )
                value = {"created": created, "job": _job_status(job)}
            elif name in {"list_local_run_tags", "add_local_run_tags", "remove_local_run_tags"}:
                if "run_path" in arguments:
                    tag_path = Path(arguments["run_path"])
                else:
                    source_job = self.server.jobs.get(arguments["mcp_job_id"])
                    if source_job.state != JobState.COMPLETED:
                        return self._error(request_id, -32000, "source job has not completed")
                    if not source_job.run_directory:
                        return self._error(request_id, -32000, "source job has no run directory")
                    tag_path = Path(source_job.run_directory)
                if name == "list_local_run_tags":
                    value = self.server.operations.list_local_run_tags(tag_path)
                elif name == "add_local_run_tags":
                    value = self.server.operations.add_local_run_tags(tag_path, arguments["tags"])
                else:
                    value = self.server.operations.remove_local_run_tags(tag_path, arguments["names"])
            elif name == "search_documentation":
                value = self.server.operations.search_documentation(
                    arguments["query"], arguments.get("limit", 10)
                )
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


def _ensure_indexing_services(crucible_home: Path, operations: CrucibleOperations) -> None:
    """Use the host's service manager to ensure OpenSearch and CDM are ready."""

    home = Path(crucible_home).resolve()
    with _RESULT_SERVICE_START_LOCK:
        try:
            # The controller has a separate Podman store; join the host mount
            # namespace and root before invoking Crucible's service manager.
            result = subprocess.run(
                [
                    "nsenter",
                    "--mount=/proc/1/ns/mnt",
                    "--root=/proc/1/root",
                    "--wd=/",
                    "--",
                    str(home / "bin" / "crucible"),
                    "start",
                    "opensearch",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_RESULT_SERVICE_START_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperationError(
                "framework",
                "Host OpenSearch/CDM could not be started or made ready; run `crucible start opensearch` on the host and inspect service status and logs",
                "result_services_unavailable",
            ) from exc
        if result.returncode != 0:
            raise OperationError(
                "framework",
                "Host OpenSearch/CDM could not be started or made ready; run `crucible start opensearch` on the host and inspect service status and logs",
                "result_services_unavailable",
            )

        try:
            services = json.loads(
                (home / "config" / "services.json").read_text(encoding="utf-8")
            )
            port = services["cdm-server"]["port"]
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("invalid CDM server port")
            parsed_url = urlsplit(operations.cdm_base_url)
            if parsed_url.hostname in {"localhost", "127.0.0.1", "::1"}:
                host = parsed_url.hostname
                netloc = f"[{host}]" if ":" in host else host
                operations.cdm_base_url = urlunsplit(
                    (parsed_url.scheme, f"{netloc}:{port}", parsed_url.path, "", "")
                ).rstrip("/")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OperationError(
                "framework",
                "CDM service configuration could not be read after startup; inspect config/services.json",
                "result_services_unavailable",
            ) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Crucible MCP service")
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--allowed-origin", action="append", default=[])
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--max-request-bytes", type=int, default=1_048_576)
    parser.add_argument("--crucible-home", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--max-run-file-bytes", type=int, default=1_048_576)
    parser.add_argument("--cdm-readiness-timeout", type=int, default=60)
    parser.add_argument("--cdm-url", default="http://127.0.0.1:3000")
    parser.add_argument("--log-db", type=Path)
    parser.add_argument("--audit-log", type=Path, default=Path("/var/lib/crucible/logs/mcp-audit.jsonl"))
    parser.add_argument("--audit-max-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--audit-retained-files", type=int, default=5)
    args = parser.parse_args()

    if (args.tls_cert is None) != (args.tls_key is None):
        parser.error("--tls-cert and --tls-key must be supplied together")
    if args.bind not in {"127.0.0.1", "localhost", "::1"} and (
        args.tls_cert is None or args.tls_key is None
    ):
        parser.error("remote MCP binds require --tls-cert and --tls-key")
    if args.bind in {"0.0.0.0", "::"} and not args.allowed_origin:
        parser.error("wildcard MCP binds require at least one --allowed-origin")
    use_ipv6 = ":" in args.bind
    if args.tls_cert is not None and args.tls_key is not None:
        server_class = TLSIPv6ThreadingHTTPServer if use_ipv6 else TLSHTTPServer
    else:
        server_class = IPv6ThreadingHTTPServer if use_ipv6 else ThreadingHTTPServer
    server = server_class((args.bind, args.port), MCPHandler)
    if args.tls_cert is not None and args.tls_key is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.tls_context = context
    server.tls_enabled = args.tls_cert is not None
    server.bind_host = args.bind
    server.allowed_origins = tuple(args.allowed_origin)
    server.token_path = args.token_file
    server.jobs = JobStore(args.database)
    server.operations = CrucibleOperations(
        args.crucible_home,
        InputPolicy([args.input_root], args.max_run_file_bytes),
        cdm_base_url=args.cdm_url,
        run_root=args.run_root,
        log_db=args.log_db,
    )
    server.operations.set_result_services_ensurer(
        lambda: _ensure_indexing_services(args.crucible_home, server.operations)
    )
    server.operations.run_policy = InputPolicy(
        [args.run_root, args.database.parent / "runs"]
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
