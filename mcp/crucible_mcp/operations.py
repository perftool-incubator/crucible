"""Typed, non-execution Crucible operations for the MCP contract."""

import copy
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import lzma
import logging
import os
import re
import sqlite3
import stat
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from jsonschema import Draft201909Validator, SchemaError

from .documentation import DocumentationCatalog
from .policy import InputPolicy, PolicyError

_PLANNING_LOCK = threading.RLock()

MAX_LOG_RESPONSE_BYTES = 1_048_576
MAX_LOG_PRIVATE_KEY_MARKERS_PER_LINE = 256
MAX_LOG_REDACTION_CONTEXT_LINES = 10_000
MAX_LOG_REDACTION_CONTEXT_BYTES = 4_194_304
_UNRESOLVED_PRIVATE_KEY_STATE = "\x00unresolved-private-key-context"
MAX_ARTIFACT_LIST_LIMIT = 1000
MAX_ARTIFACT_OFFSET = 1_073_741_824
MAX_ARTIFACT_SCAN_FILES = 100_000
MAX_ARTIFACT_DIRECTORY_DEPTH = 64
MAX_ARTIFACT_METADATA_BYTES = 262_144
MAX_ARTIFACT_READ_BYTES = 131_072
MAX_ARTIFACT_REDACTION_BYTES = 8_388_608
MAX_ARTIFACT_RESPONSE_BYTES = 1_048_576
MAX_METADATA_RESPONSE_BYTES = 1_048_576
MAX_PLAN_RESPONSE_BYTES = 1_048_576
MAX_PLAN_BENCHMARKS = 100
MAX_PLAN_PARAMETER_WORK = 100_000
MAX_PLAN_PARAMETER_ENTRY_WORK = 1_000_000
MAX_PLAN_RAW_PARAMETER_BYTES = 262_144
MAX_PLAN_RAW_PARAMETER_ENTRIES = 10_000
MAX_PLAN_REQUIREMENTS_BYTES = 262_144
MAX_BENCHMARK_VALIDATION_RULES = 100
MAX_BENCHMARK_VALIDATION_DETAIL_BYTES = 64 * 1024
MAX_BENCHMARK_VALIDATION_PATTERN_CHARS = 512
MAX_PLAN_MATERIALIZED_BYTES = 8 * MAX_PLAN_RESPONSE_BYTES
MAX_PLAN_ENGINE_ID_TOKEN_CHARS = 256
MAX_PLAN_ENGINE_ID_BYTES = 8 * MAX_PLAN_RESPONSE_BYTES
MAX_LOCAL_RUN_OFFSET = 1_000_000
# XZ preset 9 uses a 64 MiB dictionary and needs additional decoder memory;
# keep the decompressed-output bound separate so valid high-preset metadata is
# accepted without allowing an unbounded expansion.
MAX_METADATA_DECOMPRESSOR_MEMORY = 128 * 1_048_576
MAX_ENDPOINT_COUNT = 100
MAX_ENDPOINT_SCHEMA_BYTES = 262_144
MAX_ENDPOINT_MODULE_BYTES = 1_048_576
MAX_ENDPOINT_SCHEMA_PROPERTIES = 100
MAX_ENDPOINT_DESCRIPTION_CHARS = 1024
MAX_ENDPOINT_PROPERTY_NAME_CHARS = 128
MAX_ENDPOINT_TITLE_CHARS = 256
MAX_METADATA_DEPTH = 64
MAX_METADATA_JSON_FRAGMENTS = 4096
MAX_METADATA_REDACTION_WORK = 16 * 1024

_ARTIFACT_ROOTS = (
    "run/iterations",
    "run/tool-data",
    "run/sysinfo",
    "run/opensearch",
)


@dataclass(frozen=True)
class _LogRedactionState:
    private_key_label: str | None = None
    pending_sensitive_indent: int | None = None
    sensitive_structure_depth: int = 0
    pending_sensitive_yaml_indent: int | None = None
    shell_continuation: bool = False
    shell_quote: str | None = None
    pending_sensitive_heredocs: tuple[tuple[str, bool], ...] | None = ()
    pending_sensitive_json_key: tuple[int, bool] | None = None
    pending_sensitive_log_value: bool = False
    unknown: bool = False


_TEXT_ARTIFACT_SUFFIXES = {
    ".csv": "text/csv",
    ".err": "text/plain",
    ".json": "application/json",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".ndjson": "application/x-ndjson",
    ".out": "text/plain",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}
_ARTIFACT_SUFFIX_MEDIA_TYPES = {
    **_TEXT_ARTIFACT_SUFFIXES,
    ".xz": "application/x-xz",
    ".tgz": "application/gzip",
    ".gz": "application/gzip",
    ".tar": "application/x-tar",
}
_SENSITIVE_ARTIFACT_SUFFIXES = {
    ".asc",
    ".cer",
    ".crt",
    ".der",
    ".gpg",
    ".jks",
    ".key",
    ".kdb",
    ".p12",
    ".pfx",
    ".pem",
}
_SENSITIVE_ARTIFACT_NAMES = {
    ".env",
    ".netrc",
    "config",
    "config.ini",
    "config.json",
    "config.toml",
    "config.yaml",
    "config.yml",
    "credentials",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "engine-env",
    "engine-env.txt",
    "secret",
    "secret.json",
    "secret.yaml",
    "secret.yml",
    "token",
    "token.json",
    "token.txt",
    "token.yaml",
    "token.yml",
}
_SENSITIVE_METADATA_KEY_PARTS = {
    "auth",
    "authorization",
    "credential",
    "credentials",
    "creds",
    "cookie",
    "passphrase",
    "pass",
    "pwd",
    "password",
    "private",
    "secret",
    "session",
    "signature",
    "hmac",
    "token",
    "jwt",
    "bearer",
}
_METADATA_SECRET_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>--?)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)"
    r"(?P<separator>\s*[:=]\s*|\s+)"
)
_METADATA_SHELL_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_\\'\"-])(?P<prefix>--?)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_\\'\"$-]*)"
    r"(?P<separator>\s*[:=]\s*|\s+)"
)
_METADATA_QUOTED_SHELL_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_\\'\"-])(?P<prefix>--?)"
    r"(?P<key>(?:\\.|'[^']*'|\"[^\"]*\"|\$'[^']*')"
    r"[A-Za-z0-9_\\'\"$-]*)"
    r"(?P<separator>\s*[:=]\s*|\s+)"
)
_METADATA_URL_CREDENTIALS = re.compile(
    r"(?P<prefix>\b[A-Za-z][A-Za-z0-9+.-]*://)[^/\s]+@"
    r"(?=[^/\s]+(?:[/\s]|$))"
)
_METADATA_STANDALONE_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{20,}"
    r"|xapp-[A-Za-z0-9-]{20,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}"
    r"|sk-(?:proj-)?[A-Za-z0-9_-]{20,}"
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"
    r")(?![A-Za-z0-9_-])"
)
_METADATA_USER_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>--?)?"
    r"(?P<key>user(?:name)?)(?P<separator>\s*[:=]\s*|\s+)"
    r"(?P<user>[^:\s]+):(?P<secret>[^\s;]+)",
    re.IGNORECASE,
)
_METADATA_QUOTED_USER_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?P<quote>[\"'])(?P<key>user(?:name)?)(?P=quote)"
    r"(?P<separator>\s*:\s*)(?P<value_quote>[\"'])"
    r"(?P<user>[^:'\"\s]+):(?P<secret>[^'\"\s]+)(?P=value_quote)",
    re.IGNORECASE,
)
_METADATA_UNSUPPORTED_SHELL_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_=\-\\$'\"`(){};&|<>])"
    r"(?:--?[^\s=:{},\[\]]{0,256}|"
    r"[A-Za-z][^\s=:{},\[\]]{0,256})"
    r"[\\$'\"`(){};&|<>][^\s=:{},\[\]]{0,256}="
)
_METADATA_UNSUPPORTED_SHELL_COMMAND = re.compile(
    r"(?<![A-Za-z0-9_=\-\\$'\"`(){};&|<>])"
    r"(?:--?|[A-Za-z])[^=\n]{0,256}\$\([^=\n]{0,256}\)[^=\n]{0,256}="
)
_METADATA_UNSUPPORTED_SHELL_OPTION = re.compile(
    r"(?<![A-Za-z0-9_-])--[^\n]{0,256}"
    r"(?:\$\([^\n)]{0,256}\)|`[^\n`]{0,256}`)"
    r"[^\n]{0,256}(?:\s+|:|$)"
)
_METADATA_UNSUPPORTED_SHELL_WORD = re.compile(
    r"(?<![A-Za-z0-9_-])(?:--?|[A-Za-z])[^\n=]{0,256}"
    r"(?:\$\([^\n)]{0,256}\)|`[^\n`]{0,256}`)"
    r"[^\n=]{0,256}(?:\s+|:|$)"
)
_METADATA_UNSUPPORTED_SHELL_PARAMETER = re.compile(
    r"(?<![A-Za-z0-9_-])--[^\n=]{0,256}\$\{[^\n}]{0,256}\}"
    r"[^\n=]{0,256}(?:=|\s+|:)"
)
_METADATA_UNSUPPORTED_SHELL_PARAMETER_WORD = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z][^\n=]{0,256}\$\{[^\n}]{0,256}\}"
    r"[^\n=]{0,256}(?:=|\s+|:)"
)
_METADATA_UNSUPPORTED_ANSI_C_WORD = re.compile(
    r"(?<![A-Za-z0-9_-])\$'[^'\n]{1,256}'\s+"
)
_METADATA_UNSUPPORTED_QUOTED_WORD = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<quote>[\"'])(?P<word>[^\"'\n]{1,256})"
    r"(?P=quote)\s+"
)
_METADATA_UNSUPPORTED_ASSEMBLED_SHELL_WORD = re.compile(
    r"(?<![A-Za-z0-9_=\-])(?P<token>[^\s=]{1,256})\s+"
)
_METADATA_UNSUPPORTED_COMMAND_ASSEMBLED_WORD = re.compile(
    r"(?<![A-Za-z0-9_=\-])(?P<token>(?:\$\([^\n)]{1,256}\)|`[^\n`]{1,256}`)"
    r"[A-Za-z0-9_.-]{1,256})\s+"
)
_METADATA_UNSUPPORTED_COMMAND_WORD = re.compile(
    r"(?<![A-Za-z0-9_-])\$\([^\n)]{1,256}\)\s+"
)
_METADATA_PUNCTUATED_OPTION_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>--?)(?P<key>[A-Za-z][^\s=:]{0,256})"
    r"(?P<separator>\s*[:=]|\s+)"
)
_METADATA_PUNCTUATED_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_={}\[\]\"'])"
    r"(?P<key>[A-Za-z][^\s=:={}\[\]\"']{0,256})"
    r"(?P<separator>\s*[:=]|\s+)"
)
_METADATA_BRACKETED_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>--?)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_-]{0,256})"
    r"\[(?P<index>[^\]\n]{1,256})\]"
    r"(?P<separator>\s*[:=]\s*)"
)
_METADATA_NAME_FIELD_VARIANTS = frozenset(
    {
        "arg",
        "args",
        "argument",
        "arguments",
        "field",
        "fields",
        "flag",
        "flags",
        "header",
        "headers",
        "headername",
        "headernames",
        "httpheader",
        "httpheaders",
        "key",
        "keys",
        "name",
        "names",
        "option",
        "options",
        "param",
        "params",
        "parameter",
        "parameters",
        "argname",
        "fieldname",
        "flagname",
        "keyname",
        "namefield",
        "optionname",
        "parametername",
    }
)
_METADATA_AMBIGUOUS_NAME_FIELD_VARIANTS = frozenset(
    {
        "arg",
        "args",
        "argument",
        "arguments",
        "headers",
        "httpheaders",
        "params",
        "parameters",
    }
)
_METADATA_VALUE_FIELD_VARIANTS = frozenset(
    {
        "default",
        "val",
        "value",
        "vals",
        "values",
        "args",
        "arguments",
        "argument",
        "argvalue",
        "argvalues",
        "parametervalue",
        "parametervalues",
        "optionvalue",
        "optionvalues",
        "flagvalue",
        "flagvalues",
    }
)
_METADATA_ROOT_FIELD_VARIANTS = frozenset(
    {
        "benchmark",
        "benchmarks",
        "endpoints",
        "iterations",
        "registries",
        "runid",
        "samples",
        "tags",
        "tools",
    }
)
_METADATA_UNKNOWN_VALUE_FIELD_VARIANTS = frozenset(
    {"body", "content", "data", "payload", "raw", "result"}
)
_METADATA_JSON_SENSITIVE_KEY = re.compile(
    r"\"(?:auth|authorization|credential|credentials|passphrase|pass|pwd|"
    r"password|private|secret|signature|hmac|token|jwt|bearer)[^\"]*\"\s*:"
)
_METADATA_SENSITIVE_TEXT = re.compile(
    r"(?:auth|authorization|credential|credentials|passphrase|pass|pwd|"
    r"password|private|secret|signature|hmac|token|jwt|bearer|api[_-]?key|access[_-]?key|secret[_-]?key)",
    re.IGNORECASE,
)
_METADATA_QUOTED_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?:\\?[\"'])(?:auth|authorization|credential|credentials|passphrase|"
    r"pass|pwd|password|private|secret|signature|hmac|token|jwt|bearer|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key)[^\"']*"
    r"(?:\\?[\"'])\s*[:=]",
    re.IGNORECASE,
)
_METADATA_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----.*?"
    r"(?:-----END (?P=label)-----|$)",
    re.IGNORECASE | re.DOTALL,
)
_METADATA_PRIVATE_KEY_MARKER = re.compile(
    r"-----\s*(?P<kind>BEGIN|END)\s+"
    r"(?P<label>[A-Z0-9 ]*PRIVATE KEY)\s*-----",
    re.IGNORECASE,
)
_METADATA_LOG_FIELD = re.compile(
    r"^\s*(?:[\"'](?P<quoted>[^\"']+)[\"']|(?P<plain>[-A-Za-z0-9_.]+))"
    r"\s*(?P<separator>[:=])\s*(?P<value>.*?)\s*,?\s*$"
)
_METADATA_JSON_KEY_ONLY = re.compile(r'^\s*(?P<key>"(?:\\.|[^"\\])*")\s*$')
_METADATA_JSON_MEMBER = re.compile(
    r'(?:^|[,{])\s*(?P<key>"(?:\\.|[^"\\])*")\s*:'
    r'\s*(?P<value>.*?)\s*,?\s*$'
)
_YAML_BLOCK_SCALAR = re.compile(
    r"^[|>](?:[+-]?[1-9]?|[1-9]?[+-]?)(?:[ \t]+#.*)?$"
)
_LOG_SHELL_OPTION_QUOTE = re.compile(r"(?<!\S)--?[A-Za-z0-9_-]*[\"']")
_LOG_QUOTED_PARTIAL_SENSITIVE_OPTION = re.compile(
    r"(?<!\S)(?P<quote>[\"'])(?P<key>--?[A-Za-z0-9_-]+)$"
)
_LOG_TRAILING_SENSITIVE_KEY = re.compile(
    r"(?<!\S)(?P<key>--?[A-Za-z_][A-Za-z0-9_-]*)$"
)
_LOG_HEREDOC_OPERATOR = re.compile(
    r"(?<!<)<<(?!<)(?P<strip_tabs>-)?[ \t]*"
    r"(?:'(?P<single>[^'\r\n]*)'|\"(?P<double>[^\"\r\n]*)\"|"
    r"(?P<plain>[A-Za-z0-9_.-]+))"
)
MAX_LOG_REDACTION_LINES = 65_536


def _decode_metadata_unicode_escapes(value: str) -> str:
    return re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        value,
    )


def _normalize_metadata_shell_key(value: str) -> str:
    def decode_ansi_c(match: re.Match[str]) -> str:
        decoded = match.group(1)
        decoded = re.sub(
            r"\\x([0-9a-fA-F]{2})",
            lambda item: chr(int(item.group(1), 16)),
            decoded,
        )
        decoded = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda item: chr(int(item.group(1), 16)),
            decoded,
        )
        decoded = re.sub(
            r"\\([0-7]{1,3})",
            lambda item: chr(int(item.group(1), 8)),
            decoded,
        )
        return decoded

    value = re.sub(r"\$'([^']*)'", decode_ansi_c, value)

    def decode_command_substitution(match: re.Match[str]) -> str:
        words = re.findall(r"[A-Za-z0-9_]+", match.group(1))
        return words[-1] if words else ""

    value = re.sub(r"\$\(([^)\n]{0,256})\)", decode_command_substitution, value)
    value = re.sub(r"`([^`\n]{0,256})`", decode_command_substitution, value)
    value = re.sub(r"\\(.)", r"\1", value)
    return value.replace("'", "").replace('"', "")


class OperationError(RuntimeError):
    """A structured operation failure safe to return to an MCP client."""

    def __init__(self, category: str, message: str, code: str = "operation_error"):
        super().__init__(message)
        self.category = category
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"category": self.category, "code": self.code, "message": self.message}


class CrucibleOperations:
    """Read-only discovery and validation operations.

    Execution is intentionally not implemented here yet.  Keeping these
    operations separate from the HTTP transport lets the eventual runner use
    the same typed validation result without passing user strings to a shell.
    """

    def __init__(
        self,
        crucible_home: Path,
        input_policy: InputPolicy | None = None,
        cdm_base_url: str = "http://127.0.0.1:3000",
        log_db: Path | None = None,
        run_root: Path | None = None,
        result_services_ensurer: Callable[[], None] | None = None,
    ):
        self.crucible_home = Path(crucible_home).resolve()
        self.cdm_base_url = cdm_base_url.rstrip("/")
        self._result_services_ensurer = result_services_ensurer
        self.log_db = Path(log_db) if log_db else None
        self.documentation = DocumentationCatalog(self.crucible_home)
        configured_run_root = Path(run_root) if run_root else self.crucible_home / "run"
        if not configured_run_root.is_absolute():
            configured_run_root = self.crucible_home / configured_run_root
        self.archive_root = (configured_run_root.parent / "archive").resolve()
        self.local_run_root = configured_run_root.resolve()
        self.input_policy = input_policy or InputPolicy(
            [self.crucible_home / "mcp" / "inputs"]
        )
        self.run_policy = InputPolicy([run_root or self.crucible_home / "run"])
        self._tag_locks: dict[Path, threading.Lock] = {}
        self._tag_locks_guard = threading.Lock()

    def ensure_result_services(self) -> None:
        """Ensure the MCP server's direct CDM-query dependencies are ready."""

        if self._result_services_ensurer is not None:
            self._result_services_ensurer()

    def set_result_services_ensurer(self, ensurer: Callable[[], None]) -> None:
        """Install the service-start bridge used by the running MCP server."""

        self._result_services_ensurer = ensurer

    def crucible_info(self) -> dict[str, Any]:
        return {
            "name": "crucible",
            "mcp_contract_version": "3",
            "execution_supported": True,
            "capabilities": [
                "crucible_info",
                "list_benchmarks",
                "describe_benchmark",
                "list_tools",
                "list_endpoints",
                "list_active_runs",
                "list_local_runs",
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
            ],
        }

    def list_documentation(self) -> list[dict[str, Any]]:
        """List curated user-facing documentation as MCP resources."""

        return self.documentation.list_resources()

    def read_documentation(self, uri: str) -> dict[str, Any]:
        """Read one curated documentation resource by stable URI."""

        try:
            return self.documentation.read_resource(uri)
        except (FileNotFoundError, ValueError) as exc:
            raise OperationError(
                "user", str(exc), "documentation_not_found"
            ) from exc

    def search_documentation(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Search curated documentation without accepting filesystem paths."""

        if limit < 1 or limit > 20:
            raise OperationError(
                "user", "limit must be between 1 and 20", "invalid_limit"
            )
        try:
            resources = self.documentation.search(query, limit)
        except ValueError as exc:
            raise OperationError("user", str(exc), "invalid_query") from exc
        return {
            "query": self._redact_metadata(query),
            "resources": resources,
            "count": len(resources),
        }

    def list_local_run_tags(self, run_directory: Path) -> dict[str, Any]:
        _, document = self._load_run_metadata(run_directory)
        tags = self._validated_tags(document)
        return {
            "run_path": str(run_directory),
            "tags": self._redact_metadata(tags),
        }

    def add_local_run_tags(self, run_directory: Path, tags: list[str]) -> dict[str, Any]:
        canonical = self._canonical_run_directory(run_directory)
        with self._tag_lock(canonical):
            path, document = self._load_run_metadata(canonical)
            current = self._validated_tags(document)
            for raw_tag in tags:
                match = re.fullmatch(r"([a-zA-Z0-9-_\s]+):([a-zA-Z0-9-_:\s\\/\.]+)", raw_tag)
                if match is None:
                    raise OperationError("user", f"invalid tag: {raw_tag}", "invalid_tag")
                existing = next((tag for tag in current if tag.get("name") == match.group(1)), None)
                if existing is None:
                    current.append({"name": match.group(1), "val": match.group(2)})
                else:
                    existing["val"] = match.group(2)
            response_tags = self._redact_metadata(current)
            self._write_run_metadata(path, document)
            return {
                "run_path": str(run_directory),
                "tags": response_tags,
            }

    def remove_local_run_tags(self, run_directory: Path, names: list[str]) -> dict[str, Any]:
        canonical = self._canonical_run_directory(run_directory)
        with self._tag_lock(canonical):
            path, document = self._load_run_metadata(canonical)
            if any(not re.fullmatch(r"[a-zA-Z0-9-_\s]+", name) for name in names):
                raise OperationError("user", "tag names must not include values", "invalid_tag")
            existing = self._validated_tags(document)
            document["tags"] = [tag for tag in existing if tag.get("name") not in names]
            if len(document["tags"]) == len(existing):
                raise OperationError("user", "no matching tags were found", "tag_not_found")
            response_tags = self._redact_metadata(document["tags"])
            self._write_run_metadata(path, document)
            return {
                "run_path": str(run_directory),
                "tags": response_tags,
            }

    def _tag_lock(self, run_directory: Path) -> threading.Lock:
        with self._tag_locks_guard:
            return self._tag_locks.setdefault(run_directory, threading.Lock())

    @staticmethod
    def _validated_tags(document: dict[str, Any]) -> list[dict[str, Any]]:
        tags = document.setdefault("tags", [])
        if (
            not isinstance(tags, list)
            or any(
                not isinstance(tag, dict)
                or set(tag) != {"name", "val"}
                or not isinstance(tag["name"], str)
                or not tag["name"]
                or not isinstance(tag["val"], str)
                or not tag["val"]
                for tag in tags
            )
        ):
            raise OperationError(
                "user", "run metadata tags do not match the run schema", "invalid_run"
            )
        return tags

    def list_local_runs(
        self,
        limit: int = 1000,
        offset: int = 0,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """List local run directories without querying indexed result data."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise OperationError("user", "limit must be between 1 and 1000", "invalid_limit")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or offset > MAX_LOCAL_RUN_OFFSET
        ):
            raise OperationError(
                "user",
                f"offset must be between 0 and {MAX_LOCAL_RUN_OFFSET}",
                "invalid_offset",
            )

        def build_result(
            runs: list[dict[str, Any]],
            complete: bool,
            truncated: bool,
            next_offset: int,
        ) -> dict[str, Any]:
            return {
                "runs": runs,
                "count": len(runs),
                "offset": offset,
                "next_offset": next_offset,
                "complete": complete,
                "truncated": truncated,
            }

        def build_bounded_result(
            runs: list[dict[str, Any]],
            complete: bool,
            truncated: bool,
            next_offset: int,
        ) -> dict[str, Any]:
            result = build_result(runs, complete, truncated, next_offset)
            if self._mcp_response_size(result, request_id) > MAX_METADATA_RESPONSE_BYTES:
                raise OperationError(
                    "framework",
                    "local run list response exceeds size limit",
                    "result_too_large",
                )
            return result

        entries: list[dict[str, Any]] = []
        seen: set[Path] = set()
        root = self.local_run_root
        if not root.is_dir():
            return build_bounded_result([], True, False, offset)
        directories = [
            directory
            for directory in sorted(root.iterdir(), key=lambda path: path.name)
            if not directory.is_symlink() and directory.is_dir()
        ]
        complete = True
        truncated = False
        next_offset = offset
        entry_response_bytes = 0
        for index in range(offset, len(directories)):
            directory = directories[index]
            next_offset = index + 1
            try:
                canonical = directory.resolve(strict=True)
            except OSError:
                complete = False
                truncated = True
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            if (
                self._redact_metadata(directory.name) != directory.name
                or self._redact_metadata(str(canonical)) != str(canonical)
            ):
                # Discovery must not disclose credentials embedded in a run
                # name or path; still advance the directory-based cursor.
                continue
            entry: dict[str, Any] = {
                "name": directory.name,
                "path": str(canonical),
                "status": "incomplete",
                "run_id": None,
                "tags": [],
            }
            try:
                metadata_path, metadata = self._load_run_metadata(canonical)
            except OperationError:
                pass
            else:
                entry["status"] = "complete" if metadata_path.parent == canonical / "run" else "incomplete"
                entry["run_id"] = metadata.get("run-id") or metadata.get("id")
                if (
                    isinstance(entry["run_id"], str)
                    and self._redact_metadata(entry["run_id"]) != entry["run_id"]
                ):
                    continue
                try:
                    entry["tags"] = self._redact_metadata(
                        self._validated_tags(metadata)
                    )
                except OperationError:
                    entry["status"] = "incomplete"
                    entry["tags"] = []
            entry_bytes = 3 * len(json.dumps(entry).encode("utf-8")) + 64
            base_response_bytes = self._mcp_response_size(
                build_result([], False, truncated, next_offset), request_id
            )
            if (
                base_response_bytes + entry_response_bytes + entry_bytes
                > MAX_METADATA_RESPONSE_BYTES
            ):
                minimal_entry = dict(entry)
                if entry["tags"]:
                    minimal_entry["tags"] = []
                    minimal_entry["tags_truncated"] = True
                minimal_entry_bytes = 3 * len(json.dumps(minimal_entry).encode("utf-8")) + 64
                minimal_base_response_bytes = self._mcp_response_size(
                    build_result([], False, True, next_offset), request_id
                )
                if (
                    minimal_base_response_bytes + entry_response_bytes + minimal_entry_bytes
                    <= MAX_METADATA_RESPONSE_BYTES
                ):
                    entry = minimal_entry
                    entry_bytes = minimal_entry_bytes
                    truncated = True
                elif entries:
                    complete = False
                    truncated = True
                    next_offset = index
                    break
                else:
                    raise OperationError(
                        "framework",
                        "local run list response exceeds size limit",
                        "result_too_large",
                    )
            entries.append(entry)
            entry_response_bytes += entry_bytes
            if len(entries) >= limit:
                complete = complete and next_offset >= len(directories)
                if not complete:
                    truncated = True
                break
        return build_bounded_result(entries, complete, truncated, next_offset)

    def get_local_run_summary(
        self,
        run_path: Path,
        max_bytes: int = 1_048_576,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """Read a bounded summary from an approved local run artifact."""

        try:
            canonical = self._canonical_run_directory(run_path)
            summary_path = self._safe_artifact_path(canonical, "run/result-summary.json")
            relative = summary_path.relative_to(canonical).as_posix()
            stream = self._open_artifact_readonly(canonical, relative)
            try:
                size = os.fstat(stream.fileno()).st_size
                if size > max_bytes:
                    raise OperationError(
                        "framework", "result summary exceeds size limit", "result_too_large"
                    )
                encoded = stream.read(max_bytes + 1)
            finally:
                stream.close()
            if len(encoded) > max_bytes:
                raise OperationError(
                    "framework", "result summary exceeds size limit", "result_too_large"
                )
            summary = json.loads(encoded.decode("utf-8"))
        except OperationError:
            raise
        except FileNotFoundError as exc:
            raise OperationError(
                "user", "local run summary is unavailable", "result_unavailable"
            ) from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperationError(
                "user", "local run summary is not valid JSON", "invalid_result"
            ) from exc
        if not isinstance(summary, dict):
            raise OperationError("user", "local run summary must be a JSON object", "invalid_result")
        try:
            summary = self._redact_summary(summary)
        except RecursionError as exc:
            raise OperationError(
                "framework", "result summary exceeds nesting limit", "result_too_large"
            ) from exc
        result = {
            "run_path": str(canonical),
            "result_status": "available",
            "summary": summary,
        }
        if self._mcp_response_size(result, request_id) > MAX_METADATA_RESPONSE_BYTES:
            raise OperationError(
                "framework", "run summary response exceeds size limit", "result_too_large"
            )
        return result

    def get_local_run_metadata(
        self,
        run_path: Path,
        max_bytes: int = 1_048_576,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """Read the bounded rickshaw run metadata from an approved local run."""

        try:
            canonical = self._canonical_run_directory(run_path)
            metadata_path, metadata = self._read_run_metadata(canonical, max_bytes)
        except OperationError:
            raise
        except RecursionError as exc:
            raise OperationError(
                "framework",
                "run metadata exceeds nesting limit",
                "result_too_large",
            ) from exc
        except (
            OSError,
            UnicodeDecodeError,
            lzma.LZMAError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise OperationError(
                "user", "run metadata is not valid JSON", "invalid_run"
            ) from exc
        try:
            if not isinstance(metadata, dict):
                raise OperationError(
                    "user", "run metadata must be a JSON object", "invalid_run"
                )
            result = {
                "run_path": str(canonical),
                "metadata_path": str(metadata_path),
                "metadata": self._redact_metadata(metadata),
            }
        except RecursionError as exc:
            raise OperationError(
                "framework",
                "run metadata exceeds nesting limit",
                "result_too_large",
            ) from exc
        if self._mcp_response_size(result, request_id) > MAX_METADATA_RESPONSE_BYTES:
            raise OperationError(
                "framework", "run metadata response exceeds size limit", "result_too_large"
            )
        return result

    @classmethod
    def _redact_metadata(
        cls,
        value: Any,
        depth: int = 0,
        budget: list[int] | None = None,
    ) -> Any:
        """Remove credential-like metadata values before returning them to MCP."""

        if budget is None:
            budget = [MAX_METADATA_REDACTION_WORK]
        if depth > MAX_METADATA_DEPTH:
            raise OperationError(
                "framework",
                "run metadata exceeds nesting limit",
                "result_too_large",
            )
        if isinstance(value, dict):
            redacted: dict[Any, Any] = {}
            has_value_field = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key) in _METADATA_VALUE_FIELD_VARIANTS
                for key in value
            )
            is_metadata_root = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key) in _METADATA_ROOT_FIELD_VARIANTS
                for key in value
            )
            has_sensitive_key = any(
                isinstance(key, str) and cls._metadata_key_is_sensitive(key)
                for key in value
            )
            has_sensitive_descriptor_key = any(
                isinstance(key, str)
                and cls._metadata_key_is_sensitive(key)
                and (
                    (
                        isinstance(item, str)
                        and cls._metadata_key_is_sensitive(item)
                    )
                    or (
                        isinstance(item, (dict, list))
                        and cls._metadata_value_contains_sensitive_name(item)
                    )
                )
                for key, item in value.items()
            )
            has_sensitive_name_field = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
                and cls._metadata_direct_value_contains_sensitive_name(
                    item, cls._metadata_field_variant(key)
                )
                for key, item in value.items()
            )
            has_unknown_sibling = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key)
                not in _METADATA_NAME_FIELD_VARIANTS
                and cls._metadata_field_variant(key)
                not in _METADATA_VALUE_FIELD_VARIANTS
                and cls._metadata_field_variant(key)
                not in _METADATA_ROOT_FIELD_VARIANTS
                for key in value
            )
            has_explicit_unknown_value_field = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key)
                in _METADATA_UNKNOWN_VALUE_FIELD_VARIANTS
                for key in value
            )
            has_bare_sensitive_descriptor_collection = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
                and isinstance(item, list)
                and any(
                    isinstance(child, dict)
                    and cls._metadata_item_has_sensitive_parameter_name(child)
                    for child in item
                )
                for key, item in value.items()
            )
            sensitive_parameter = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
                and cls._metadata_direct_value_contains_sensitive_name(
                    item, cls._metadata_field_variant(key)
                )
                and not (
                    cls._metadata_field_variant(key)
                    in {"headers", "httpheaders", "parameters", "params"}
                    and isinstance(item, list)
                    and any(isinstance(child, dict) for child in item)
                    and not has_value_field
                    and is_metadata_root
                )
                for key, item in value.items()
            ) or (
                has_sensitive_key
                and (has_value_field or has_unknown_sibling)
                and not is_metadata_root
            )
            sensitive_parameter = sensitive_parameter or (
                is_metadata_root
                and has_sensitive_name_field
                and (
                    has_explicit_unknown_value_field
                    or (
                        has_unknown_sibling
                        and has_bare_sensitive_descriptor_collection
                    )
                )
            )
            sensitive_parameter = sensitive_parameter or (
                is_metadata_root
                and has_sensitive_key
                and (
                    has_value_field
                    or has_explicit_unknown_value_field
                    or (
                        has_unknown_sibling
                        and has_sensitive_descriptor_key
                    )
                )
            )
            has_unambiguous_sensitive_name = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
                and cls._metadata_field_variant(key)
                not in _METADATA_AMBIGUOUS_NAME_FIELD_VARIANTS
                and cls._metadata_direct_value_contains_sensitive_name(
                    item, cls._metadata_field_variant(key)
                )
                for key, item in value.items()
            )
            has_user_name_descriptor = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
                and isinstance(item, str)
                and cls._metadata_field_variant(item) in {"user", "username"}
                for key, item in value.items()
            )
            for key, item in value.items():
                output_key = key
                if isinstance(key, str):
                    output_key = cls._redact_metadata(key, depth + 1, budget)
                    if output_key in redacted:
                        base_key = output_key
                        suffix = 2
                        while output_key in redacted:
                            output_key = f"{base_key} ({suffix})"
                            suffix += 1
                field_variant = (
                    cls._metadata_field_variant(key) if isinstance(key, str) else ""
                )
                if (
                    isinstance(item, str)
                    and field_variant in {"user", "username"}
                    and cls._is_user_credential_value(item)
                ):
                    redacted[output_key] = cls._redact_user_credential_value(item)
                elif (
                    has_user_name_descriptor
                    and isinstance(item, str)
                    and field_variant in _METADATA_VALUE_FIELD_VARIANTS
                    and cls._is_user_credential_value(item)
                ):
                    redacted[output_key] = cls._redact_user_credential_value(item)
                elif isinstance(key, str) and cls._metadata_key_is_sensitive(key):
                    redacted[output_key] = "[redacted]"
                elif (
                    sensitive_parameter
                    and isinstance(key, str)
                    and not (
                        cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
                        and cls._metadata_value_contains_sensitive_name(item)
                        and (
                            cls._metadata_field_variant(key)
                            not in _METADATA_AMBIGUOUS_NAME_FIELD_VARIANTS
                            or not has_unambiguous_sensitive_name
                        )
                    )
                    and not (
                        is_metadata_root
                        and cls._metadata_field_variant(key)
                        in _METADATA_ROOT_FIELD_VARIANTS
                    )
                ):
                    redacted[output_key] = "[redacted]"
                else:
                    redacted[output_key] = cls._redact_metadata(item, depth + 1, budget)
            return redacted
        if isinstance(value, list):
            redacted_list: list[Any] = []
            pending_sensitive_option = False
            pending_multi_token = False
            for item_index, item in enumerate(value):
                if pending_sensitive_option:
                    redacted_list.append("[redacted]")
                    if pending_multi_token:
                        continue
                    pending_sensitive_option = bool(
                        isinstance(item, str)
                        and cls._is_sensitive_option_flag(item)
                    )
                    continue
                redacted_list.append(cls._redact_metadata(item, depth + 1, budget))
                if isinstance(item, str) and (
                    cls._is_sensitive_option_flag(item)
                    or cls._is_short_sensitive_option_flag(item)
                ):
                    pending_sensitive_option = True
                    pending_multi_token = False
                else:
                    authorization_descriptor = (
                        cls._metadata_item_has_authorization_parameter_name(item)
                    )
                    sensitive_descriptor = (
                        cls._metadata_item_has_sensitive_parameter_name(item)
                    )
                    sensitive_string = (
                        isinstance(item, str)
                        and cls._metadata_key_is_sensitive(item)
                    )
                    if not (
                        authorization_descriptor
                        or sensitive_descriptor
                        or sensitive_string
                    ):
                        continue
                    pending_sensitive_option = True
                    pending_multi_token = (
                        authorization_descriptor
                        or sensitive_descriptor
                        or
                        item_index == 0
                        or (
                            isinstance(item, str)
                            and cls._metadata_field_variant(item) == "authorization"
                        )
                    )
            return redacted_list
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    return "[redacted]"
                if isinstance(parsed, (dict, list)):
                    redacted = cls._redact_metadata(parsed, depth + 1, budget)
                    return json.dumps(redacted, separators=(",", ":"))
            if len(value) > MAX_METADATA_REDACTION_WORK:
                if cls._long_metadata_string_has_sensitive_candidate(value):
                    raise OperationError(
                        "framework",
                        "run metadata redaction exceeds work limit",
                        "result_too_large",
                    )
                return value
            string_budget = [MAX_METADATA_REDACTION_WORK]
            string_budget[0] -= len(value)
            if "\n" in value and any(
                marker in value for marker in ("$(", "`", "'", '"', "\\")
            ):
                # Newlines can split shell syntax across tokens in ways that
                # static word matching cannot safely reconstruct.
                return "[redacted]"
            value = cls._redact_short_password_options(value)
            value = cls._redact_user_options(value)
            value = _METADATA_USER_CREDENTIAL_ASSIGNMENT.sub(
                lambda match: (
                    f"{match.group('prefix') or ''}{match.group('key')}"
                    f"{match.group('separator')}{match.group('user')}:[redacted]"
                ),
                value,
            )
            value = _METADATA_QUOTED_USER_CREDENTIAL_ASSIGNMENT.sub(
                lambda match: (
                    f"{match.group('quote')}{match.group('key')}"
                    f"{match.group('quote')}{match.group('separator')}"
                    f"{match.group('value_quote')}{match.group('user')}"
                    f":[redacted]{match.group('value_quote')}"
                ),
                value,
            )
            bracketed_parts: list[str] = []
            bracketed_cursor = 0
            for match in _METADATA_BRACKETED_ASSIGNMENT.finditer(value):
                if match.start() < bracketed_cursor:
                    continue
                if not cls._metadata_key_is_sensitive(match.group("key")):
                    continue
                value_start = match.end()
                value_end = cls._sensitive_value_end(
                    value,
                    value_start,
                    allow_leading_dash=(
                        ":" in match.group("separator")
                        or "=" in match.group("separator")
                    ),
                )
                bracketed_parts.append(value[bracketed_cursor:match.start()])
                bracketed_parts.append(
                    f"{match.group('prefix') or ''}{match.group('key')}"
                    f"[{match.group('index')}]"
                    f"{match.group('separator')}[redacted]"
                )
                bracketed_cursor = value_end
            if bracketed_parts:
                bracketed_parts.append(value[bracketed_cursor:])
                return "".join(bracketed_parts)
            embedded = cls._redact_embedded_json(value, depth, string_budget)
            if embedded != value:
                if embedded == "[redacted]":
                    return embedded
                value = embedded
            if (
                _METADATA_UNSUPPORTED_SHELL_ASSIGNMENT.search(value)
                or _METADATA_UNSUPPORTED_SHELL_COMMAND.search(value)
                or _METADATA_UNSUPPORTED_SHELL_OPTION.search(value)
                or _METADATA_UNSUPPORTED_SHELL_WORD.search(value)
                or _METADATA_UNSUPPORTED_SHELL_PARAMETER.search(value)
                or _METADATA_UNSUPPORTED_SHELL_PARAMETER_WORD.search(value)
                or cls._has_unsupported_shell_assembly(value)
                or _METADATA_UNSUPPORTED_ANSI_C_WORD.search(value)
                or cls._has_sensitive_assembled_shell_word(value)
                or (
                    (quoted_word := _METADATA_UNSUPPORTED_QUOTED_WORD.search(value))
                    is not None
                    and cls._metadata_key_is_sensitive(
                        _normalize_metadata_shell_key(quoted_word.group("word"))
                    )
                )
                or _METADATA_UNSUPPORTED_COMMAND_WORD.search(value)
            ):
                return "[redacted]"
            decoded = _decode_metadata_unicode_escapes(value)
            if decoded != value:
                for match in _METADATA_SECRET_ASSIGNMENT.finditer(decoded):
                    if cls._metadata_key_is_sensitive(match.group("key")):
                        return "[redacted]"
            continued = re.sub(r"\\\r?\n", "", value)
            if continued != value:
                for match in _METADATA_SECRET_ASSIGNMENT.finditer(continued):
                    if cls._metadata_key_is_sensitive(match.group("key")):
                        return "[redacted]"
            url_redacted = _METADATA_URL_CREDENTIALS.sub(
                r"\g<prefix>[redacted]@", value
            )
            if url_redacted != value:
                value = url_redacted
            value = _METADATA_STANDALONE_CREDENTIAL.sub("[redacted]", value)
            for matcher in (
                _METADATA_SHELL_ASSIGNMENT,
                _METADATA_QUOTED_SHELL_ASSIGNMENT,
            ):
                for match in matcher.finditer(value):
                    raw_key = match.group("key")
                    if raw_key[0].isalpha() and raw_key.endswith(("'", '"')):
                        continue
                    key = _normalize_metadata_shell_key(raw_key)
                    if key != raw_key and cls._metadata_key_is_sensitive(key):
                        return "[redacted]"
            for matcher in (
                _METADATA_PUNCTUATED_OPTION_ASSIGNMENT,
                _METADATA_PUNCTUATED_ASSIGNMENT,
            ):
                for match in matcher.finditer(value):
                    key = match.group("key")
                    if (
                        re.search(r"[^A-Za-z0-9_-]", key)
                        and cls._metadata_key_is_sensitive(key)
                    ):
                        return "[redacted]"
            redacted_parts: list[str] = []
            cursor = 0
            for match in _METADATA_SECRET_ASSIGNMENT.finditer(value):
                if match.start() < cursor:
                    continue
                if not cls._metadata_key_is_sensitive(match.group("key")):
                    continue
                value_start = match.end()
                separator = match.group("separator")
                value_end = cls._sensitive_value_end(
                    value,
                    value_start,
                    allow_leading_dash=":" in separator or "=" in separator,
                )
                redacted_parts.append(value[cursor:match.start()])
                redacted_parts.append(
                    f"{match.group('prefix') or ''}{match.group('key')}"
                    f"{match.group('separator')}[redacted]"
                )
                if value_end == value_start and value_start < len(value):
                    redacted_parts.append(" ")
                cursor = value_end
            if not redacted_parts:
                return value
            redacted_parts.append(value[cursor:])
            return "".join(redacted_parts)
        return value

    @classmethod
    def _redact_summary(cls, value: Any) -> Any:
        """Redact summary fields independently while retaining safe siblings."""

        if isinstance(value, dict):
            has_sensitive_descriptor = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
                and cls._metadata_direct_value_contains_sensitive_name(
                    item, cls._metadata_field_variant(key)
                )
                for key, item in value.items()
            )
            has_value_field = any(
                isinstance(key, str)
                and cls._metadata_field_variant(key) in _METADATA_VALUE_FIELD_VARIANTS
                for key in value
            )
            contextual_values: dict[str, Any] = {}
            if has_sensitive_descriptor and has_value_field:
                # Redact descriptor/value fields together, then merge only the
                # affected values so unrelated summary siblings remain useful.
                contextual_fields = {
                    key: item
                    for key, item in value.items()
                    if isinstance(key, str)
                    and cls._metadata_field_variant(key)
                    in (_METADATA_NAME_FIELD_VARIANTS | _METADATA_VALUE_FIELD_VARIANTS)
                }
                contextual_result = cls._redact_metadata(contextual_fields)
                contextual_values = dict(
                    zip(contextual_fields, contextual_result.values())
                )
            redacted: dict[Any, Any] = {}
            for key, item in value.items():
                field = cls._redact_metadata({key: item})
                output_key, output_value = next(iter(field.items()))
                if (
                    isinstance(key, str)
                    and cls._metadata_field_variant(key) in _METADATA_VALUE_FIELD_VARIANTS
                    and key in contextual_values
                ):
                    output_value = contextual_values[key]
                if output_key in redacted:
                    base_key = output_key
                    suffix = 2
                    while output_key in redacted:
                        output_key = f"{base_key} ({suffix})"
                        suffix += 1
                redacted[output_key] = output_value
            return redacted
        return cls._redact_metadata(value)

    @classmethod
    def redact_log_text(cls, value: str) -> str:
        """Redact credential-like content from runner logs before MCP exposure."""

        if not isinstance(value, str):
            raise OperationError("framework", "runner log text is invalid", "invalid_log")
        value = _METADATA_PRIVATE_KEY_BLOCK.sub("[redacted private key]", value)
        try:
            redacted = cls._redact_metadata(value)
        except OperationError:
            # Logs are untrusted, and an over-complex line must not bypass the
            # credential policy merely because metadata redaction hit its work cap.
            return "[redacted]"
        return redacted if isinstance(redacted, str) else "[redacted]"

    @classmethod
    def _redact_log_line_with_context(
        cls, value: str, private_key_label: str | None = None
    ) -> tuple[str, str | None]:
        """Redact one logger line while carrying PEM block state across rows."""

        if private_key_label == _UNRESOLVED_PRIVATE_KEY_STATE:
            return "[redacted private key]", private_key_label

        marker_count = 0
        if private_key_label is not None:
            for marker in _METADATA_PRIVATE_KEY_MARKER.finditer(value):
                marker_count += 1
                if marker_count > MAX_LOG_PRIVATE_KEY_MARKERS_PER_LINE:
                    return "[redacted]", _UNRESOLVED_PRIVATE_KEY_STATE
                if marker.group("kind").upper() == "BEGIN":
                    # A nested block is malformed or ambiguous. Do not let an
                    # inner END marker close the still-open outer key block.
                    return "[redacted private key]", _UNRESOLVED_PRIVATE_KEY_STATE
                if marker.group("label").casefold() == private_key_label.casefold():
                    suffix, suffix_label = cls._redact_log_line_with_context(
                        value[marker.end():]
                    )
                    return "[redacted private key]" + suffix, suffix_label
            return "[redacted private key]", private_key_label

        next_label = None
        for marker in _METADATA_PRIVATE_KEY_MARKER.finditer(value):
            marker_count += 1
            if marker_count > MAX_LOG_PRIVATE_KEY_MARKERS_PER_LINE:
                return "[redacted]", _UNRESOLVED_PRIVATE_KEY_STATE
            if marker.group("kind").upper() == "BEGIN":
                if next_label is not None:
                    return "[redacted private key]", _UNRESOLVED_PRIVATE_KEY_STATE
                next_label = marker.group("label")
            elif (
                next_label is not None
                and marker.group("label").casefold() == next_label.casefold()
            ):
                next_label = None
        return cls.redact_log_text(value), next_label

    @classmethod
    def _redact_log_line_with_stats(
        cls,
        value: str,
        private_key_label: str | None = None,
        pending_sensitive_indent: int | None = None,
        sensitive_structure_depth: int = 0,
        pending_sensitive_yaml_indent: int | None = None,
        shell_continuation: bool = False,
        has_line_ending: bool = True,
        shell_quote: str | None = None,
        pending_sensitive_heredocs: tuple[tuple[str, bool], ...] | None = (),
        pending_sensitive_json_key: tuple[int, bool] | None = None,
        pending_sensitive_log_value: bool = False,
    ) -> tuple[
        str,
        str | None,
        int | None,
        int | None,
        int,
        bool,
        str | None,
        tuple[tuple[str, bool], ...] | None,
        bool,
        tuple[int, bool] | None,
        bool,
    ]:
        """Redact a line and carry state across shell/YAML continuations."""

        result = cls._redact_log_line_state(
            value,
            private_key_label,
            pending_sensitive_indent,
            sensitive_structure_depth,
            pending_sensitive_yaml_indent,
        )
        (
            redacted,
            next_private_key_label,
            next_pending_sensitive_indent,
            next_pending_sensitive_yaml_indent,
            next_sensitive_structure_depth,
            changed,
        ) = result
        next_pending_sensitive_json_key = None
        if pending_sensitive_json_key is not None:
            if not value.strip():
                next_pending_sensitive_json_key = pending_sensitive_json_key
            elif not pending_sensitive_json_key[1]:
                separator = re.fullmatch(r"\s*:\s*(?P<value>.*?)\s*,?\s*", value)
                if separator is not None:
                    redacted = "[redacted]"
                    changed = True
                    field_value = separator.group("value").strip()
                    if not field_value:
                        next_pending_sensitive_json_key = (
                            pending_sensitive_json_key[0],
                            True,
                        )
                    elif field_value[0] in "[{":
                        depth = cls._log_json_nesting_delta(field_value)
                        if depth > 0:
                            next_sensitive_structure_depth = depth
            else:
                redacted = "[redacted]"
                changed = True
                field_value = value.strip()
                if field_value[0] in "[{":
                    depth = cls._log_json_nesting_delta(field_value)
                    if depth > 0:
                        next_sensitive_structure_depth = depth

        if next_pending_sensitive_json_key is None and (
            pending_sensitive_json_key is None
            or (value.strip() and not redacted == "[redacted]")
        ):
            member_match = _METADATA_JSON_MEMBER.search(value)
            if member_match is not None:
                try:
                    key = json.loads(member_match.group("key"))
                except (json.JSONDecodeError, TypeError):
                    key = None
                if isinstance(key, str) and cls._metadata_key_is_sensitive(key):
                    redacted = "[redacted]"
                    changed = True
                    field_value = member_match.group("value").strip()
                    if field_value.startswith(("[", "{")):
                        depth = cls._log_json_open_container_depth(field_value)
                        if depth > 0:
                            next_sensitive_structure_depth = depth
                    elif not field_value:
                        next_pending_sensitive_json_key = (
                            len(value) - len(value.lstrip()),
                            True,
                        )
            if next_pending_sensitive_json_key is None:
                key_match = _METADATA_JSON_KEY_ONLY.fullmatch(value)
                if key_match is not None:
                    try:
                        key = json.loads(key_match.group("key"))
                    except (json.JSONDecodeError, TypeError):
                        key = None
                    if isinstance(key, str) and cls._metadata_key_is_sensitive(key):
                        redacted = "[redacted]"
                        changed = True
                        next_pending_sensitive_json_key = (
                            len(value) - len(value.lstrip()),
                            False,
                        )
        next_pending_sensitive_log_value = False
        if pending_sensitive_log_value:
            if not value.strip():
                next_pending_sensitive_log_value = True
            else:
                redacted = "[redacted]"
                changed = True
                field_value = value.strip()
                if field_value.startswith(("[", "{")):
                    depth = cls._log_json_nesting_delta(field_value)
                    if depth > 0:
                        next_sensitive_structure_depth = depth
        if has_line_ending and cls._log_line_has_trailing_sensitive_key(value):
            next_pending_sensitive_log_value = True
        shell_line_continues = (
            has_line_ending and cls._log_line_has_shell_continuation(value)
        )
        next_shell_quote = cls._log_shell_quote_state(value, shell_quote)
        has_sensitive_shell_assignment = (
            cls._log_line_has_sensitive_shell_assignment(value)
        )
        continues_shell = shell_line_continues and (
            shell_continuation
            or has_sensitive_shell_assignment
            or cls._log_line_has_partial_sensitive_shell_key(value)
            or shell_quote is not None
            or next_shell_quote is not None
        )
        next_sensitive_heredocs = pending_sensitive_heredocs
        redacted_heredoc_line = False
        if pending_sensitive_heredocs is None:
            redacted_heredoc_line = True
        elif pending_sensitive_heredocs:
            delimiter, strip_tabs = pending_sensitive_heredocs[0]
            candidate = value.lstrip("\t") if strip_tabs else value
            redacted_heredoc_line = True
            if has_line_ending and candidate == delimiter:
                next_sensitive_heredocs = pending_sensitive_heredocs[1:]
        elif has_line_ending and (
            has_sensitive_shell_assignment
            or shell_continuation
            or shell_quote is not None
        ):
            next_sensitive_heredocs = cls._log_sensitive_heredoc_starts(value)
            redacted_heredoc_line = next_sensitive_heredocs != ()
        if (
            shell_continuation
            or continues_shell
            or shell_quote is not None
            or next_shell_quote is not None
            or redacted_heredoc_line
        ):
            # Continued words, multiline quotes, and heredoc bodies can
            # assemble sensitive values across physical lines. Hide the span.
            redacted = "[redacted]"
            changed = True
        return (
            redacted,
            next_private_key_label,
            next_pending_sensitive_indent,
            next_pending_sensitive_yaml_indent,
            next_sensitive_structure_depth,
            continues_shell,
            next_shell_quote,
            next_sensitive_heredocs,
            changed,
            next_pending_sensitive_json_key,
            next_pending_sensitive_log_value,
        )

    @classmethod
    def _redact_log_physical_line_with_state(
        cls,
        value: str,
        state: _LogRedactionState,
        has_line_ending: bool = True,
    ) -> tuple[str, _LogRedactionState, bool]:
        """Redact one logger record and preserve every multiline context."""

        if state.unknown:
            return "[redacted]", state, True
        result = cls._redact_log_line_with_stats(
            value,
            private_key_label=state.private_key_label,
            pending_sensitive_indent=state.pending_sensitive_indent,
            sensitive_structure_depth=state.sensitive_structure_depth,
            pending_sensitive_yaml_indent=state.pending_sensitive_yaml_indent,
            shell_continuation=state.shell_continuation,
            has_line_ending=has_line_ending,
            shell_quote=state.shell_quote,
            pending_sensitive_heredocs=state.pending_sensitive_heredocs,
            pending_sensitive_json_key=state.pending_sensitive_json_key,
            pending_sensitive_log_value=state.pending_sensitive_log_value,
        )
        return (
            result[0],
            _LogRedactionState(
                private_key_label=result[1],
                pending_sensitive_indent=result[2],
                pending_sensitive_yaml_indent=result[3],
                sensitive_structure_depth=result[4],
                shell_continuation=result[5],
                shell_quote=result[6],
                pending_sensitive_heredocs=result[7],
                pending_sensitive_json_key=result[9],
                pending_sensitive_log_value=result[10],
            ),
            result[8],
        )

    @classmethod
    def _redact_log_record_with_state(
        cls,
        value: str,
        state: _LogRedactionState | None = None,
    ) -> tuple[str, _LogRedactionState, bool]:
        """Redact a logger row while carrying state across embedded records."""

        state = state or _LogRedactionState()
        if state.unknown:
            return "[redacted]", state, True

        output: list[str] = []
        changed = False
        position = 0
        processed_lines = 0
        for separator in re.finditer(r"\r\n|\r|\n", value):
            if processed_lines >= MAX_LOG_REDACTION_LINES:
                return "[redacted]", _LogRedactionState(unknown=True), True
            safe_line, state, line_changed = cls._redact_log_physical_line_with_state(
                value[position : separator.start()], state
            )
            output.append(safe_line)
            output.append(separator.group())
            changed = changed or line_changed
            position = separator.end()
            processed_lines += 1

        if position < len(value) or processed_lines == 0:
            if processed_lines >= MAX_LOG_REDACTION_LINES:
                return "[redacted]", _LogRedactionState(unknown=True), True
            safe_line, state, line_changed = cls._redact_log_physical_line_with_state(
                value[position:], state
            )
            output.append(safe_line)
            changed = changed or line_changed
        return "".join(output), state, changed

    @staticmethod
    def _log_line_has_shell_continuation(value: str) -> bool:
        """Whether a line ends with an unescaped shell continuation slash."""

        slash_count = len(value) - len(value.rstrip("\\"))
        return slash_count % 2 == 1

    @classmethod
    def _log_record_opens_redaction_state(cls, value: str) -> bool:
        """Whether discarding this record could lose multiline secret state."""

        result = cls._redact_log_line_with_stats(value, has_line_ending=True)
        return (
            result[1] is not None
            or result[2] is not None
            or result[3] is not None
            or result[4] != 0
            or result[5]
            or result[6] is not None
            or result[7] is None
            or bool(result[7])
            or result[9] is not None
            or result[10]
            or cls._log_line_has_shell_continuation(value)
        )

    @classmethod
    def _log_line_has_sensitive_shell_assignment(cls, value: str) -> bool:
        return any(
            cls._metadata_key_is_sensitive(match.group("key"))
            for match in _METADATA_SECRET_ASSIGNMENT.finditer(value)
        )

    @classmethod
    def _log_line_has_trailing_sensitive_key(cls, value: str) -> bool:
        """Whether a line ends in a key whose whitespace-delimited value follows."""

        stripped = value.strip()
        match = _LOG_TRAILING_SENSITIVE_KEY.search(stripped)
        if match is None:
            return False
        key = match.group("key")
        if not key.startswith("-") and stripped != key:
            return False
        return cls._metadata_key_is_sensitive(key)

    @classmethod
    def _log_line_has_partial_sensitive_shell_key(cls, value: str) -> bool:
        """Recognize a sensitive option name split at a shell continuation."""

        if not cls._log_line_has_shell_continuation(value):
            return False
        prefix = value.rstrip()[:-1].rstrip()
        match = re.search(r"(?<!\S)(?P<key>--?[A-Za-z0-9_-]+)$", prefix)
        if match is not None and cls._is_sensitive_shell_option_prefix(
            match.group("key")
        ):
            return True
        # A URL user-info password may begin on the next physical line after
        # the colon; the first half cannot be classified by assignment-key
        # matching alone, so carry the continuation as sensitive.
        return re.search(
            r"(?i)(?:https?|ftp)://[^\s/:@]+:$", prefix
        ) is not None

    @staticmethod
    def _is_sensitive_shell_option_prefix(value: str) -> bool:
        key = value.lstrip("-").lower().replace("_", "")
        if key in {"p", "u"}:
            return True
        if len(key) < 2:
            return False
        sensitive_names = _SENSITIVE_METADATA_KEY_PARTS | {
            "accesskey",
            "apikey",
            "authtoken",
            "clientsecret",
            "clienttoken",
            "secretkey",
        }
        return any(
            name.replace("_", "").startswith(key)
            for name in sensitive_names
        )

    @classmethod
    def _log_sensitive_shell_continuation_lines(cls, value: str) -> set[int]:
        """Find continued shell records that become sensitive when rejoined."""

        sensitive_lines: set[int] = set()
        position = 0
        scanned_lines = 0
        while position < len(value) and scanned_lines < MAX_LOG_REDACTION_LINES:
            line_starts: list[int] = []
            logical_parts: list[str] = []
            has_continuation = False
            continuation_open = False
            while position < len(value) and scanned_lines < MAX_LOG_REDACTION_LINES:
                line_start = position
                lf = value.find("\n", position)
                cr = value.find("\r", position)
                endings = [index for index in (lf, cr) if index >= 0]
                if not endings:
                    end = len(value)
                else:
                    ending_start = min(endings)
                    end = ending_start + 1
                    if value[ending_start] == "\r" and value[end : end + 1] == "\n":
                        end += 1
                raw_line = value[position:end]
                if raw_line.endswith("\r\n"):
                    content = raw_line[:-2]
                    has_line_ending = True
                elif raw_line.endswith(("\n", "\r")):
                    content = raw_line[:-1]
                    has_line_ending = True
                else:
                    content = raw_line
                    has_line_ending = False
                continues = (
                    has_line_ending and cls._log_line_has_shell_continuation(content)
                )
                line_starts.append(scanned_lines)
                if continues:
                    has_continuation = True
                    continuation_open = True
                    logical_parts.append(content[:-1])
                else:
                    continuation_open = False
                    logical_parts.append(content)
                position = end
                scanned_lines += 1
                if not continues:
                    break
            if not has_continuation:
                continue
            if continuation_open:
                # If the bounded scan ended mid-command, its sensitivity is
                # unknowable; keep the observed portion hidden.
                sensitive_lines.update(line_starts)
                continue
            logical_command = "".join(logical_parts)
            if cls.redact_log_text(logical_command) != logical_command:
                sensitive_lines.update(line_starts)
        return sensitive_lines

    @classmethod
    def _log_sensitive_heredoc_starts(
        cls,
        value: str,
    ) -> tuple[tuple[str, bool], ...] | None:
        """Return delimiters, or None when a sensitive heredoc is ambiguous."""

        starts = cls._log_heredoc_operator_positions(value)
        if not starts:
            return ()
        delimiters: list[tuple[str, bool]] = []
        for start in starts:
            operator = _LOG_HEREDOC_OPERATOR.match(value, start)
            if operator is None:
                return None
            delimiter = (
                operator.group("single")
                if operator.group("single") is not None
                else operator.group("double")
                if operator.group("double") is not None
                else operator.group("plain")
            )
            if not delimiter:
                return None
            delimiters.append((delimiter, operator.group("strip_tabs") is not None))
        return tuple(delimiters)

    @staticmethod
    def _log_heredoc_operator_positions(value: str) -> list[int]:
        """Find heredoc operators outside shell quotes and escaped words."""

        positions: list[int] = []
        quote: str | None = None
        escaped = False
        index = 0
        while index < len(value):
            character = value[index]
            if quote == "'":
                if character == "'":
                    quote = None
                index += 1
                continue
            if escaped:
                escaped = False
                index += 1
                continue
            if character == "\\":
                escaped = True
                index += 1
                continue
            if quote == '"':
                if character == '"':
                    quote = None
                index += 1
                continue
            if character in {"'", '"'}:
                quote = character
                index += 1
                continue
            if value.startswith("<<<", index):
                index += 3
                continue
            if value.startswith("<<", index):
                positions.append(index)
                index += 2
                continue
            index += 1
        return positions

    @classmethod
    def _log_shell_quote_state(
        cls, value: str, quote_state: str | None = None
    ) -> str | None:
        """Track quoted sensitive assignments so multiline values fail closed."""

        if quote_state is None:
            has_sensitive_assignment = any(
                cls._metadata_key_is_sensitive(match.group("key"))
                and (
                    match.group("prefix") is not None
                    or ":" in match.group("separator")
                    or "=" in match.group("separator")
                    or (
                        match.group("separator").isspace()
                        and value[match.end() :].lstrip().startswith(
                            ("'", '"', "$'")
                        )
                    )
                )
                for match in _METADATA_SECRET_ASSIGNMENT.finditer(value)
            )
            field = _METADATA_LOG_FIELD.fullmatch(value)
            has_sensitive_field = (
                field is not None
                and cls._metadata_key_is_sensitive(
                    field.group("quoted") or field.group("plain") or ""
                )
            )
            quoted_partial_option = _LOG_QUOTED_PARTIAL_SENSITIVE_OPTION.search(value)
            has_partial_shell_option = (
                _LOG_SHELL_OPTION_QUOTE.search(value) is not None
                or (
                    quoted_partial_option is not None
                    and cls._is_sensitive_shell_option_prefix(
                        quoted_partial_option.group("key")
                    )
                )
            )
            if not (
                has_sensitive_assignment
                or has_sensitive_field
                or has_partial_shell_option
            ):
                return None

        escaped = False
        for character in value:
            if quote_state == "'":
                if character == "'":
                    quote_state = None
                continue
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
            elif quote_state == '"':
                if character == '"':
                    quote_state = None
            elif character in {"'", '"'}:
                quote_state = character
        return quote_state

    @classmethod
    def _redact_log_line_state(
        cls,
        value: str,
        private_key_label: str | None = None,
        pending_sensitive_indent: int | None = None,
        sensitive_structure_depth: int = 0,
        pending_sensitive_yaml_indent: int | None = None,
    ) -> tuple[str, str | None, int | None, int | None, int, bool]:
        """Redact one complete log line and report whether it changed."""

        redacted, next_label = cls._redact_log_line_with_context(
            value, private_key_label
        )
        stripped = value.strip()
        indent = len(value) - len(value.lstrip())

        if sensitive_structure_depth:
            next_depth = max(
                0,
                sensitive_structure_depth + cls._log_json_nesting_delta(value),
            )
            return "[redacted]", next_label, None, None, next_depth, True

        if pending_sensitive_yaml_indent is not None:
            if not stripped:
                return (
                    redacted,
                    next_label,
                    None,
                    pending_sensitive_yaml_indent,
                    0,
                    redacted != value,
                )
            if indent > pending_sensitive_yaml_indent:
                return "[redacted]", next_label, None, pending_sensitive_yaml_indent, 0, True
            # A dedented line is outside the scalar. Process it normally so a
            # safe sibling field is not hidden with the secret block.
            pending_sensitive_yaml_indent = None

        if pending_sensitive_indent is not None:
            if not stripped:
                return (
                    redacted,
                    next_label,
                    pending_sensitive_indent,
                    pending_sensitive_yaml_indent,
                    0,
                    redacted != value,
                )
            value_depth = cls._log_json_nesting_delta(value)
            if stripped[0] in "[{" and value_depth > 0:
                return "[redacted]", next_label, None, None, value_depth, True
            same_indent_field = (
                indent == pending_sensitive_indent
                and _METADATA_LOG_FIELD.fullmatch(value) is not None
            )
            same_indent_sequence_item = (
                indent == pending_sensitive_indent
                and stripped.startswith("-")
                and (len(stripped) == 1 or stripped[1] in " \t")
            )
            if indent > pending_sensitive_indent or same_indent_sequence_item:
                # Deeper YAML content and indentless sequence items belong to
                # the sensitive field until a sibling mapping is reached.
                return (
                    "[redacted]",
                    next_label,
                    pending_sensitive_indent,
                    None,
                    0,
                    True,
                )
            if indent == pending_sensitive_indent and not same_indent_field:
                # At equal indentation, consume one scalar line, as with the
                # common pretty-printed JSON form. A mapping field is instead
                # a sibling and must be reprocessed under normal redaction.
                return "[redacted]", next_label, None, None, 0, True
            # A dedented line or same-indent mapping is outside the sensitive
            # value. Clear state and process it normally below.
            pending_sensitive_indent = None

        has_marker = _METADATA_PRIVATE_KEY_MARKER.search(value) is not None
        if has_marker and redacted == value:
            # A BEGIN marker changes the carried state but is not itself a
            # secret; hide it together with the key body for consistent output.
            redacted = "[redacted private key]"
        field = _METADATA_LOG_FIELD.fullmatch(value)
        if field is not None:
            key = field.group("quoted") or field.group("plain") or ""
            field_value = field.group("value").strip()
            if cls._metadata_key_is_sensitive(key):
                if not field_value:
                    return "[redacted]", next_label, indent, None, 0, True
                if field_value[0] in "[{":
                    depth = cls._log_json_nesting_delta(field_value)
                    if depth > 0:
                        return "[redacted]", next_label, None, None, depth, True
                if _YAML_BLOCK_SCALAR.fullmatch(field_value):
                    return "[redacted]", next_label, None, indent, 0, True
                if (
                    field.group("separator") == ":"
                    and field_value[0] not in "\"'[{"
                ):
                    # Plain YAML scalars may continue on more-indented lines;
                    # retain the sensitive context through that continuation.
                    return "[redacted]", next_label, None, indent, 0, True
        return redacted, next_label, None, None, 0, redacted != value

    @staticmethod
    def _log_json_nesting_delta(value: str) -> int:
        """Count JSON-like container nesting without counting quoted braces."""

        depth = 0
        quote: str | None = None
        escaped = False
        for character in value:
            if escaped:
                escaped = False
            elif quote is not None:
                if character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
            elif character in {"\"", "'"}:
                quote = character
            elif character in "[{":
                depth += 1
            elif character in "]}":
                depth -= 1
        return depth

    @staticmethod
    def _log_json_open_container_depth(value: str) -> int:
        """Return the remaining depth of a JSON container starting this value."""

        value = value.lstrip()
        if not value.startswith(("[", "{")):
            return 0

        depth = 0
        quote: str | None = None
        escaped = False
        for character in value:
            if escaped:
                escaped = False
            elif quote is not None:
                if character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
            elif character in {"\"", "'"}:
                quote = character
            elif character in "[{":
                depth += 1
            elif character in "]}":
                depth -= 1
                if depth <= 0:
                    return 0
        return max(depth, 0)

    @classmethod
    def redact_log_text_with_stats(cls, value: str) -> tuple[str, int]:
        """Return credential-redacted text and the number of affected lines."""

        if not isinstance(value, str):
            raise OperationError("framework", "runner log text is invalid", "invalid_log")
        sensitive_shell_continuation_lines = (
            cls._log_sensitive_shell_continuation_lines(value)
        )
        redacted_parts: list[str] = []
        redacted_lines = 0
        processed_lines = 0
        private_key_label: str | None = None
        pending_sensitive_indent: int | None = None
        pending_sensitive_yaml_indent: int | None = None
        pending_sensitive_json_key: tuple[int, bool] | None = None
        pending_sensitive_log_value = False
        sensitive_structure_depth = 0
        shell_continuation = False
        shell_quote: str | None = None
        pending_sensitive_heredocs: tuple[tuple[str, bool], ...] | None = ()
        position = 0
        while position < len(value):
            if processed_lines >= MAX_LOG_REDACTION_LINES:
                remaining = value[position:]
                redacted_parts.append("[redacted]")
                line_breaks = (
                    remaining.count("\n")
                    + remaining.count("\r")
                    - remaining.count("\r\n")
                )
                redacted_lines += line_breaks + bool(
                    remaining and not remaining.endswith(("\n", "\r"))
                )
                break
            lf = value.find("\n", position)
            cr = value.find("\r", position)
            endings = [index for index in (lf, cr) if index >= 0]
            if not endings:
                end = len(value)
            else:
                ending_start = min(endings)
                end = ending_start + 1
                if value[ending_start] == "\r" and value[end : end + 1] == "\n":
                    end += 1
            raw_line = value[position:end]
            if raw_line.endswith("\r\n"):
                content, ending = raw_line[:-2], "\r\n"
            elif raw_line.endswith(("\n", "\r")):
                content, ending = raw_line[:-1], raw_line[-1:]
            else:
                content, ending = raw_line, ""
            (
                redacted,
                private_key_label,
                pending_sensitive_indent,
                pending_sensitive_yaml_indent,
                sensitive_structure_depth,
                shell_continuation,
                shell_quote,
                pending_sensitive_heredocs,
                changed,
                pending_sensitive_json_key,
                pending_sensitive_log_value,
            ) = cls._redact_log_line_with_stats(
                content,
                private_key_label,
                pending_sensitive_indent,
                sensitive_structure_depth,
                pending_sensitive_yaml_indent,
                shell_continuation,
                bool(ending),
                shell_quote,
                pending_sensitive_heredocs,
                pending_sensitive_json_key,
                pending_sensitive_log_value,
            )
            if processed_lines in sensitive_shell_continuation_lines:
                redacted = "[redacted]"
                changed = True
            redacted_parts.append(redacted + ending)
            if changed:
                redacted_lines += 1
            position = end
            processed_lines += 1
        return "".join(redacted_parts), redacted_lines

    @classmethod
    def _log_redaction_state_before(
        cls,
        connection: sqlite3.Connection,
        session: int,
        stream: int,
        before_id: int,
        budget: dict[str, int] | None = None,
    ) -> _LogRedactionState:
        """Recover redaction state from bounded rows preceding a search window."""

        return cls._log_redaction_state_in_range(
            connection,
            session,
            stream,
            after_id=None,
            before_id=before_id,
            state=_LogRedactionState(),
            budget=budget,
        )

    @classmethod
    def _log_redaction_state_between(
        cls,
        connection: sqlite3.Connection,
        session: int,
        stream: int,
        after_id: int,
        before_id: int,
        state: _LogRedactionState,
        budget: dict[str, int],
    ) -> _LogRedactionState:
        """Apply excluded logger rows between results in insertion order."""

        return cls._log_redaction_state_in_range(
            connection,
            session,
            stream,
            after_id=after_id,
            before_id=before_id,
            state=state,
            budget=budget,
        )

    @classmethod
    def _log_redaction_state_in_range(
        cls,
        connection: sqlite3.Connection,
        session: int,
        stream: int,
        after_id: int | None,
        before_id: int,
        state: _LogRedactionState,
        budget: dict[str, int] | None,
    ) -> _LogRedactionState:
        """Process a bounded insertion-order slice for one logger stream."""

        if state.unknown:
            return state
        if after_id is not None and before_id <= after_id + 1:
            return state

        if budget is None:
            budget = {
                "lines": MAX_LOG_REDACTION_CONTEXT_LINES,
                "bytes": MAX_LOG_REDACTION_CONTEXT_BYTES,
            }

        index = connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'index' AND name = 'idx_lines_session_stream_id'"""
        ).fetchone()
        if index is None:
            # Older or externally-created logger databases may not have the
            # bounded lookup index yet. Never fall back to scanning their history.
            return _LogRedactionState(unknown=True)

        line_budget = min(
            MAX_LOG_REDACTION_CONTEXT_LINES, budget.get("lines", 0)
        )
        byte_budget = min(
            MAX_LOG_REDACTION_CONTEXT_BYTES, budget.get("bytes", 0)
        )
        if line_budget <= 0 or byte_budget < 0:
            return _LogRedactionState(unknown=True)

        where = "session = ? AND stream = ? AND id < ?"
        params: list[int] = [session, stream, before_id]
        if after_id is not None:
            where += " AND id > ?"
            params.append(after_id)
        direction = "ASC" if after_id is not None else "DESC"
        context_rows = connection.execute(
            f"""SELECT id, length(CAST(line AS BLOB)) FROM lines
                WHERE {where} ORDER BY id {direction} LIMIT ?""",
            (*params, line_budget + 1),
        ).fetchall()
        if (
            len(context_rows) > line_budget
            or sum(row[1] or 0 for row in context_rows) > byte_budget
        ):
            # An omitted row could open any supported sensitive structure;
            # fail closed instead of guessing the state at the search window.
            budget["lines"] = 0
            budget["bytes"] = 0
            return _LogRedactionState(unknown=True)

        budget["lines"] -= len(context_rows)
        budget["bytes"] -= sum(row[1] or 0 for row in context_rows)
        if after_id is None:
            context_lines = connection.execute(
                f"""SELECT line FROM lines
                    WHERE id IN (
                        SELECT id FROM lines WHERE {where}
                        ORDER BY id DESC LIMIT ?
                    )
                    ORDER BY id""",
                (*params, line_budget),
            ).fetchall()
        else:
            context_lines = connection.execute(
                f"""SELECT line FROM lines WHERE {where} ORDER BY id ASC""",
                params,
            ).fetchall()
        for (line,) in context_lines:
            _, state, _ = cls._redact_log_record_with_state(line or "", state)
        return state

    @classmethod
    def _metadata_item_has_sensitive_parameter_name(cls, value: Any) -> bool:
        """Identify descriptor objects whose following list item is secret."""

        if isinstance(value, list):
            return any(
                (
                    isinstance(item, str)
                    and cls._metadata_key_is_sensitive(item)
                )
                or cls._metadata_item_has_sensitive_parameter_name(item)
                for item in value
            )
        if not isinstance(value, dict):
            return False
        has_sensitive_name = any(
            isinstance(key, str)
            and cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
            and cls._metadata_value_contains_sensitive_name(item)
            for key, item in value.items()
        )
        has_sensitive_key = any(
            isinstance(key, str)
            and cls._metadata_key_is_sensitive(key)
            and (
                (
                    isinstance(item, str)
                    and cls._metadata_key_is_sensitive(item)
                )
                or (
                    isinstance(item, (dict, list))
                    and cls._metadata_value_contains_sensitive_name(item)
                )
            )
            for key, item in value.items()
        )
        has_value_field = any(
            isinstance(key, str)
            and cls._metadata_field_variant(key) in _METADATA_VALUE_FIELD_VARIANTS
            and not (
                cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
                and cls._metadata_value_contains_sensitive_name(item)
            )
            for key, item in value.items()
        )
        return (has_sensitive_name or has_sensitive_key) and not has_value_field

    @classmethod
    def _metadata_value_contains_authorization(cls, value: Any) -> bool:
        if isinstance(value, str):
            return cls._metadata_field_variant(value) == "authorization"
        if isinstance(value, dict):
            return any(
                cls._metadata_value_contains_authorization(item)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(
                cls._metadata_value_contains_authorization(item) for item in value
            )
        return False

    @staticmethod
    def _is_user_credential_value(value: str) -> bool:
        separator = value.find(":")
        return separator >= 0 and separator + 1 < len(value)

    @staticmethod
    def _redact_user_credential_value(value: str) -> str:
        separator = value.find(":")
        if separator < 0 or separator + 1 >= len(value):
            return value
        return f"{value[:separator + 1]}[redacted]"

    @classmethod
    def _metadata_item_has_authorization_parameter_name(cls, value: Any) -> bool:
        if isinstance(value, list):
            return any(
                cls._metadata_item_has_authorization_parameter_name(item)
                for item in value
            )
        if not isinstance(value, dict):
            return False
        return any(
            isinstance(key, str)
            and cls._metadata_field_variant(key) in _METADATA_NAME_FIELD_VARIANTS
            and cls._metadata_value_contains_authorization(item)
            for key, item in value.items()
        ) or any(
            isinstance(key, str)
            and cls._metadata_key_is_sensitive(key)
            and (
                (
                    isinstance(item, str)
                    and cls._metadata_field_variant(item) == "authorization"
                )
                or cls._metadata_value_contains_authorization(item)
            )
            for key, item in value.items()
        )

    @classmethod
    def _metadata_value_contains_sensitive_name(cls, value: Any) -> bool:
        if isinstance(value, str):
            return cls._metadata_key_is_sensitive(value)
        if isinstance(value, dict):
            return any(
                cls._metadata_value_contains_sensitive_name(item)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(
                cls._metadata_value_contains_sensitive_name(item) for item in value
            )
        return False

    @classmethod
    def _metadata_direct_value_contains_sensitive_name(
        cls, value: Any, field_variant: str = ""
    ) -> bool:
        return cls._metadata_value_contains_sensitive_name(value)

    @classmethod
    def _redact_embedded_json(
        cls, value: str, depth: int, budget: list[int]
    ) -> str:
        """Redact object/array JSON fragments embedded in shell-like text."""

        def suspicious(fragment: str) -> bool:
            decoded = re.sub(
                r"\\u([0-9a-fA-F]{4})",
                lambda match: chr(int(match.group(1), 16)),
                fragment,
            )
            return bool(
                _METADATA_QUOTED_SENSITIVE_ASSIGNMENT.search(decoded)
                or (
                    any(character in decoded for character in "[{")
                    and _METADATA_SENSITIVE_TEXT.search(decoded)
                )
            )

        decoder = json.JSONDecoder()
        fragments = 0
        cursor = 0
        index = 0
        redacted_parts: list[str] = []
        while index < len(value):
            if value[index] not in "[{":
                index += 1
                continue
            fragments += 1
            if fragments > MAX_METADATA_JSON_FRAGMENTS:
                return "[redacted]"
            try:
                parsed, end = decoder.raw_decode(value, index)
            except (json.JSONDecodeError, ValueError):
                index += 1
                continue
            if not isinstance(parsed, (dict, list)):
                index = end
                continue
            if suspicious(value[cursor:index]):
                return "[redacted]"
            redacted_parts.append(value[cursor:index])
            redacted = cls._redact_metadata(parsed, depth + 1, budget)
            redacted_parts.append(json.dumps(redacted, separators=(",", ":")))
            cursor = end
            index = end
        if not redacted_parts:
            if suspicious(value):
                return "[redacted]"
            return value
        if suspicious(value[cursor:]):
            return "[redacted]"
        redacted_parts.append(value[cursor:])
        return "".join(redacted_parts)

    @classmethod
    def _shell_word_end(
        cls, value: str, start: int, allow_leading_dash: bool = False
    ) -> int:
        """Find a shell-like option value boundary without evaluating it."""

        index = start
        quote: str | None = None
        escaped = False
        while index < len(value):
            character = value[index]
            if escaped:
                escaped = False
            elif quote == "'":
                if character == "'":
                    quote = None
            elif quote == '"':
                if character == "\\":
                    escaped = True
                elif character == '"':
                    quote = None
            elif character == "\\":
                escaped = True
            elif character in {"'", '"'}:
                quote = character
            elif character == "-" and index == start and not allow_leading_dash:
                option = re.match(r"--[A-Za-z][A-Za-z0-9_-]*", value[index:])
                if option:
                    break
            elif character.isspace() or character == ";":
                break
            index += 1
        return index

    @classmethod
    def _sensitive_value_end(
        cls,
        value: str,
        start: int,
        allow_leading_dash: bool = False,
        single_word: bool = False,
    ) -> int:
        """Consume a sensitive value through quoted and whitespace-separated words."""

        end = cls._shell_word_end(value, start, allow_leading_dash)
        if end == start and not allow_leading_dash:
            option = re.match(r"--[A-Za-z][A-Za-z0-9_-]*", value[start:])
            if option and cls._is_sensitive_option_flag(option.group(0)):
                return len(value)
            end = cls._shell_word_end(value, start, allow_leading_dash=True)
        if end == start or single_word:
            return end
        while end < len(value) and value[end].isspace():
            next_start = end
            while next_start < len(value) and value[next_start].isspace():
                next_start += 1
            if next_start >= len(value):
                return len(value)
            if value.startswith(";", next_start):
                return end
            option = re.match(r"--[A-Za-z][A-Za-z0-9_-]*", value[next_start:])
            assignment = re.match(
                r"[A-Za-z][A-Za-z0-9_-]*\s*[:=]", value[next_start:]
            )
            if option or assignment:
                return end
            next_end = cls._shell_word_end(value, next_start, allow_leading_dash=True)
            if next_end == next_start:
                return end
            end = next_end
        return end

    @classmethod
    def _redact_short_password_options(cls, value: str) -> str:
        """Redact values accepted by common short credential options."""

        redacted_parts: list[str] = []
        cursor = 0
        search_start = 0
        while search_start < len(value):
            match = re.search(
                r"(?<![A-Za-z0-9_-])-(?P<options>[A-Za-z]*[pu])"
                r"(?P<attached>[^;\s]*)",
                value[search_start:],
            )
            if match is None:
                break
            option_start = search_start + match.start()
            full_option = re.match(
                r"-[A-Za-z][A-Za-z0-9_-]*(?:=[^;\s]*)?",
                value[option_start:],
            )
            full_name = (
                full_option.group(0)[1:].split("=", 1)[0].lower()
                if full_option
                else ""
            )
            if (
                full_name
                and (
                    "_" in full_name
                    or "-" in full_name
                    or full_name
                    in {
                        "auth",
                        "authorization",
                        "apikey",
                        "accesskey",
                        "clientsecret",
                        "clienttoken",
                        "credential",
                        "credentials",
                        "creds",
                        "jwt",
                        "password",
                        "passwd",
                        "passphrase",
                        "private",
                        "pwd",
                        "secret",
                        "secretkey",
                        "token",
                        "user",
                        "username",
                    }
                )
            ):
                # Let the general assignment redactor handle single-dash
                # long options. Otherwise a short-option prefix such as
                # ``-ap`` can consume only part of ``-api_key`` and expose its
                # value.
                search_start = option_start + len(full_option.group(0))
                continue
            option_end = option_start + 1 + len(match.group("options"))
            attached = match.group("attached")
            if attached and not attached.startswith("=") and "-" in attached:
                return "[redacted]"
            if attached and not attached.startswith("=") and not (
                match.group("options").lower() in {"p", "u", "ap"}
                or (
                    match.group("options").lower() == "au"
                    and ":" in attached
                )
            ):
                # A multi-letter single-dash token such as ``-auth`` is
                # ambiguous: treating its final ``u`` as a short flag leaves
                # the remainder attached and can expose the following word.
                # Fail closed rather than guessing at the shell's option
                # grammar.
                return "[redacted]"
            value_start = option_end
            if value_start < len(value) and value[value_start] == "=":
                value_start += 1
                while value_start < len(value) and value[value_start].isspace():
                    value_start += 1
            elif match.group("attached"):
                value_start = option_end
            elif value_start < len(value) and value[value_start].isspace():
                while value_start < len(value) and value[value_start].isspace():
                    value_start += 1
            if value_start >= len(value) or value[value_start] == ";":
                search_start = option_end
                continue
            value_end = cls._shell_word_end(
                value, value_start, allow_leading_dash=True
            )
            if value_end == value_start:
                search_start = option_end
                continue
            if (
                match.group("options").lower() in {"p", "u"}
                and match.group("attached")
                and not match.group("attached").startswith("=")
                and value_end < len(value)
                and value[value_end].isspace()
            ):
                return "[redacted]"
            redacted_parts.append(value[cursor:value_start])
            redacted_parts.append("[redacted]")
            cursor = value_end
            search_start = value_end
        if not redacted_parts:
            return value
        redacted_parts.append(value[cursor:])
        return "".join(redacted_parts)

    @classmethod
    def _redact_user_options(cls, value: str) -> str:
        """Redact credentials passed through curl-style user options."""

        redacted_parts: list[str] = []
        cursor = 0
        search_start = 0
        while search_start < len(value):
            match = re.search(
                r"(?<![A-Za-z0-9_-])(?:--user(?=[=;\s]|$)|-u)",
                value[search_start:],
            )
            if match is None:
                break
            option_start = search_start + match.start()
            option_end = search_start + match.end()
            value_start = option_end
            if value_start < len(value) and value[value_start] == "=":
                value_start += 1
                while value_start < len(value) and value[value_start].isspace():
                    value_start += 1
            elif value_start < len(value) and value[value_start].isspace():
                while value_start < len(value) and value[value_start].isspace():
                    value_start += 1
            if value_start >= len(value) or value[value_start] == ";":
                search_start = option_end
                continue
            value_end = cls._shell_word_end(
                value, value_start, allow_leading_dash=True
            )
            if value_end == value_start:
                search_start = option_end
                continue
            redacted_parts.append(value[cursor:value_start])
            redacted_parts.append("[redacted]")
            cursor = value_end
            search_start = value_end
        if not redacted_parts:
            return value
        redacted_parts.append(value[cursor:])
        return "".join(redacted_parts)

    @classmethod
    def _is_sensitive_option_flag(cls, value: str) -> bool:
        if cls._is_user_option_flag(value) or cls._is_short_sensitive_option_flag(value):
            return True
        match = re.fullmatch(r"--?([A-Za-z][A-Za-z0-9_-]*)", value)
        return bool(match and cls._metadata_key_is_sensitive(match.group(1)))

    @staticmethod
    def _long_metadata_string_has_sensitive_candidate(value: str) -> bool:
        sensitive_name = (
            r"(?:auth|authorization|credential|credentials|passphrase|pass|pwd|"
            r"password|private|secret|signature|hmac|token|jwt|bearer|api[_-]?key|"
            r"access[_-]?key|secret[_-]?key)"
        )
        if _METADATA_JSON_SENSITIVE_KEY.search(value):
            return True
        if re.search(
            r"[\"'](?:user|username)[\"']\s*:\s*[\"'][^\"']+:[^\"']+[\"']",
            value,
            re.IGNORECASE,
        ):
            return True
        if any(quote in value for quote in ("'", '"')) and re.search(
            r"(?:pass|word|secret|token|auth|cred|bearer|jwt)",
            value,
            re.IGNORECASE,
        ):
            return True
        for descriptor in re.finditer(
            r"[\"'][A-Za-z_][A-Za-z0-9_-]*[\"']\s*:\s*"
            r"[\"'](?P<name>[A-Za-z_][A-Za-z0-9_-]*)[\"']",
            value,
        ):
            if CrucibleOperations._metadata_key_is_sensitive(
                descriptor.group("name")
            ):
                return True
        if re.search(
            rf"(?<![A-Za-z0-9_-])(?:--)?{sensitive_name}\s*[:=]",
            value,
            re.IGNORECASE,
        ):
            return True
        assignments = re.finditer(
            r"(?<![A-Za-z0-9_-])(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*[:=]",
            value,
        )
        if any(
            CrucibleOperations._metadata_key_is_sensitive(match.group("key"))
            for match in assignments
        ):
            return True
        if re.search(
            rf"(?<![A-Za-z0-9_-])--{sensitive_name}(?=\s|=|:)",
            value,
            re.IGNORECASE,
        ):
            return True
        if re.search(
            rf"(?<![A-Za-z0-9_-])-{sensitive_name}(?=\s|=|:)",
            value,
            re.IGNORECASE,
        ):
            return True
        for matcher in (
            _METADATA_PUNCTUATED_OPTION_ASSIGNMENT,
            _METADATA_PUNCTUATED_ASSIGNMENT,
        ):
            for match in matcher.finditer(value):
                key = match.group("key")
                if (
                    re.search(r"[^A-Za-z0-9_-]", key)
                    and CrucibleOperations._metadata_key_is_sensitive(key)
                ):
                    return True
        for match in _METADATA_BRACKETED_ASSIGNMENT.finditer(value):
            if CrucibleOperations._metadata_key_is_sensitive(match.group("key")):
                return True
        plain_assignments = re.finditer(
            rf"(?<![A-Za-z0-9_-]){sensitive_name}\s+(?P<value>\S+)",
            value,
            re.IGNORECASE,
        )
        if next(plain_assignments, None) is not None:
            return True
        if _METADATA_URL_CREDENTIALS.search(value):
            return True
        if _METADATA_USER_CREDENTIAL_ASSIGNMENT.search(value):
            return True
        for compound in re.finditer(
            r"(?<![A-Za-z0-9_-])(?P<key>[A-Za-z_][A-Za-z0-9_-]{0,256})"
            r"\s+(?P<value>\S+)",
            value,
        ):
            if not CrucibleOperations._metadata_key_is_sensitive(
                compound.group("key")
            ):
                continue
            return True
        if re.search(
            r"(?<![A-Za-z0-9_-])-[A-Za-z]*[pu](?:[=\s]|[^;\s])",
            value,
        ):
            return True
        return any(marker in value for marker in ("$", "`", "\\"))

    @staticmethod
    def _is_short_sensitive_option_flag(value: str) -> bool:
        return bool(re.fullmatch(r"-[A-Za-z]*[pu]", value))

    @staticmethod
    def _is_user_option_flag(value: str) -> bool:
        return value in {"-u", "--user", "--username", "user", "username"}

    @classmethod
    def _has_sensitive_assembled_shell_word(cls, value: str) -> bool:
        for match in _METADATA_UNSUPPORTED_COMMAND_ASSEMBLED_WORD.finditer(value):
            # The command's output is unknowable without executing it.  Once
            # it is concatenated with a key fragment, fail closed rather than
            # attempting to infer whether shell escapes produce a secret name.
            if match.group("token"):
                return True
        for match in _METADATA_UNSUPPORTED_ASSEMBLED_SHELL_WORD.finditer(value):
            token = match.group("token")
            if any(marker in token for marker in "{}[]"):
                continue
            if not any(marker in token for marker in "\\'\"$`()"):
                continue
            if "$" in token and "(" not in token:
                # Scalar expansions can assemble a sensitive name without
                # leaving a statically recognizable spelling in the token.
                return True
            if "$" in token and "(" in token:
                if not re.search(r"\)[A-Za-z]|[A-Za-z]\$\(", token):
                    continue
            elif not any(token.count(quote) >= 2 for quote in "'\""):
                continue
            if cls._metadata_key_is_sensitive(_normalize_metadata_shell_key(token)):
                return True
        return False

    @staticmethod
    def _has_unsupported_shell_assembly(value: str) -> bool:
        """Detect quoted shell-word assignments with linear work."""

        for token in value.split():
            equals = token.find("=")
            if equals <= 0:
                continue
            if "'" in token[:equals] or '"' in token[:equals]:
                return True
        return False

    @staticmethod
    def _is_option_flag(value: str) -> bool:
        return bool(re.fullmatch(r"--?[A-Za-z][A-Za-z0-9_-]*", value))

    @staticmethod
    def _metadata_key_is_sensitive(key: str) -> bool:
        if re.fullmatch(r"-[A-Za-z]*[pu]", key):
            return True
        key = _decode_metadata_unicode_escapes(key)
        normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
        normalized = re.sub(r"[^a-z0-9]+", "_", normalized.lower())
        if any(
            marker in normalized
            for marker in ("api_key", "access_key", "secret_key")
        ):
            return True
        if any(
            marker in normalized
            for marker in (
                "apikey",
                "accesskey",
                "secretkey",
                "clientsecret",
                "clienttoken",
                "authtoken",
                "passwd",
            )
        ):
            return True
        compact = normalized.replace("_", "")
        if compact == "sig":
            return True
        if any(
            marker in compact
            for marker in (
                "auth",
                "authorization",
                "credential",
                "credentials",
                "creds",
                "cookie",
                "passphrase",
                "pass",
                "pwd",
                "password",
                "passwd",
                "private",
                "secret",
                "signature",
                "hmac",
                "session",
                "token",
                "jwt",
                "bearer",
                "apikey",
                "accesskey",
                "secretkey",
                "clientsecret",
                "clienttoken",
                "authtoken",
            )
        ):
            return True
        parts = {
            part
            for part in re.split(r"[^a-z0-9]+", normalized)
            if part
        }
        return bool(
            parts
            & (_SENSITIVE_METADATA_KEY_PARTS | {"passwords", "secrets", "tokens"})
        )

    @staticmethod
    def _metadata_field_variant(key: str) -> str:
        key = _decode_metadata_unicode_escapes(key)
        normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
        normalized = re.sub(r"[^a-z0-9]+", "_", normalized.lower())
        return normalized.replace("_", "")

    def list_run_artifacts(
        self, run_path: Path, offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """List metadata for approved artifacts without returning their contents."""

        if offset < 0 or offset > MAX_ARTIFACT_OFFSET:
            raise OperationError(
                "user", f"offset must be between 0 and {MAX_ARTIFACT_OFFSET}", "invalid_offset"
            )
        if offset >= MAX_ARTIFACT_SCAN_FILES:
            raise OperationError(
                "framework",
                "artifact listing exceeds the traversal limit",
                "result_too_large",
            )
        if limit < 1 or limit > MAX_ARTIFACT_LIST_LIMIT:
            raise OperationError(
                "user",
                f"limit must be between 1 and {MAX_ARTIFACT_LIST_LIMIT}",
                "invalid_limit",
            )

        canonical = self._canonical_run_directory(run_path)
        artifacts: list[dict[str, Any]] = []
        scanned = 0
        metadata_bytes = len(str(canonical).encode("utf-8"))
        complete = True
        next_offset = offset
        scan_limit = MAX_ARTIFACT_SCAN_FILES

        def mark_walk_error(_error: OSError) -> None:
            nonlocal complete
            complete = False

        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        root_fd = None
        try:
            root_fd = os.open(canonical, directory_flags)
            root_iterator = os.scandir(root_fd)
        except OSError as exc:
            if root_fd is not None:
                os.close(root_fd)
            raise OperationError(
                "framework",
                f"unable to traverse run artifacts: {exc}",
                "artifact_traversal_failed",
            ) from exc

        stack: list[tuple[int, str, Any]] = []
        if root_iterator is not None:
            stack.append((root_fd, ".", root_iterator))
        scanned = 1
        try:
            while stack:
                current_fd, current_relative, entries = stack[-1]
                try:
                    entry = next(entries)
                except StopIteration:
                    entries.close()
                    stack.pop()
                    os.close(current_fd)
                    continue
                except OSError as exc:
                    mark_walk_error(exc)
                    next_offset = max(next_offset, offset + 1, scanned)
                    entries.close()
                    stack.pop()
                    os.close(current_fd)
                    continue

                scanned += 1
                if scanned > scan_limit:
                    raise OperationError(
                        "framework",
                        "artifact listing exceeds the traversal limit",
                        "result_too_large",
                    )
                relative = (
                    entry.name
                    if current_relative == "."
                    else f"{current_relative}/{entry.name}"
                )
                try:
                    is_symlink = entry.is_symlink()
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    complete = False
                    next_offset = scanned
                    continue

                if is_directory and not is_symlink:
                    if not self._artifact_directory_may_contain(
                        current_relative, entry.name
                    ):
                        continue
                    if len(stack) >= MAX_ARTIFACT_DIRECTORY_DEPTH:
                        raise OperationError(
                            "framework",
                            "artifact listing exceeds the directory depth limit",
                            "result_too_large",
                        )
                    child_fd = None
                    try:
                        child_fd = os.open(
                            entry.name, directory_flags, dir_fd=current_fd
                        )
                        child_iterator = os.scandir(child_fd)
                    except OSError as exc:
                        mark_walk_error(exc)
                        next_offset = scanned
                        if child_fd is not None:
                            os.close(child_fd)
                        continue
                    stack.append((child_fd, relative, child_iterator))
                    continue

                file_offset = scanned
                try:
                    is_regular = entry.is_file(follow_symlinks=False)
                except OSError:
                    complete = False
                    next_offset = file_offset
                    continue
                if is_symlink or not is_regular:
                    next_offset = file_offset
                    continue
                if file_offset <= offset:
                    next_offset = file_offset
                    continue
                if not self._is_approved_artifact(relative):
                    next_offset = file_offset
                    continue
                if self._redact_metadata(relative) != relative:
                    # Artifact names are returned as client-visible paths too;
                    # do not disclose a recognized token embedded in one.
                    next_offset = file_offset
                    continue
                if len(artifacts) >= limit:
                    complete = False
                    next_offset = file_offset - 1
                    break
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    complete = False
                    next_offset = file_offset
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    next_offset = file_offset
                    continue
                artifact = {
                    "artifact_path": relative,
                    "name": entry.name,
                    "media_type": self._artifact_media_type(Path(entry.name)),
                    "size": metadata.st_size,
                    "modified_at": int(metadata.st_mtime * 1000),
                    "retrievable": (
                        self._is_retrievable_artifact(relative)
                        and metadata.st_size <= MAX_ARTIFACT_REDACTION_BYTES
                    ),
                }
                artifact_bytes = len(
                    json.dumps(artifact, separators=(",", ":")).encode("utf-8")
                )
                if (
                    artifacts
                    and metadata_bytes + artifact_bytes > MAX_ARTIFACT_METADATA_BYTES
                ):
                    complete = False
                    next_offset = file_offset - 1
                    break
                artifacts.append(artifact)
                metadata_bytes += artifact_bytes
                next_offset = file_offset
        finally:
            for directory_fd, _relative, entries in stack:
                entries.close()
                os.close(directory_fd)

        return {
            "run_path": str(canonical),
            "offset": offset,
            "next_offset": next_offset,
            "complete": complete,
            "artifacts": artifacts,
            "count": len(artifacts),
        }

    @staticmethod
    def _artifact_directory_may_contain(current: str, child: str) -> bool:
        candidate = child if current == "." else f"{current}/{child}"
        return any(
            candidate.startswith(f"{root}/")
            or root == candidate
            or root.startswith(f"{candidate}/")
            for root in _ARTIFACT_ROOTS
        )

    def get_run_artifact(
        self,
        run_path: Path,
        artifact_path: str,
        offset: int = 0,
        limit: int = MAX_ARTIFACT_READ_BYTES,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """Read a bounded UTF-8 slice of one approved text artifact."""

        if offset < 0 or offset > MAX_ARTIFACT_OFFSET:
            raise OperationError(
                "user", f"offset must be between 0 and {MAX_ARTIFACT_OFFSET}", "invalid_offset"
            )
        if limit < 1 or limit > MAX_ARTIFACT_READ_BYTES:
            raise OperationError(
                "user",
                f"limit must be between 1 and {MAX_ARTIFACT_READ_BYTES}",
                "invalid_limit",
            )
        if "\x00" in artifact_path:
            raise OperationError(
                "user", "artifact path contains a NUL character", "invalid_artifact_path"
            )
        canonical = self._canonical_run_directory(run_path)
        try:
            artifact = self._safe_artifact_path(canonical, artifact_path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise OperationError(
                "user", "artifact does not exist", "artifact_not_found"
            ) from exc
        except OSError as exc:
            raise OperationError(
                "user", "artifact path could not be resolved", "invalid_artifact_path"
            ) from exc
        relative = artifact.relative_to(canonical).as_posix()
        if not self._is_approved_artifact(relative):
            raise OperationError(
                "authorization", "artifact is outside the approved artifact set", "path_rejected"
            )
        if not self._is_text_artifact(relative):
            raise OperationError(
                "user", "artifact is not an approved UTF-8 text artifact", "artifact_not_text"
            )
        if not self._is_retrievable_artifact(relative):
            raise OperationError(
                "authorization",
                "sensitive artifacts are not retrievable",
                "artifact_not_retrievable",
            )
        try:
            stream = self._open_artifact_readonly(canonical, relative)
        except FileNotFoundError as exc:
            raise OperationError(
                "user", "artifact does not exist", "artifact_not_found"
            ) from exc
        except (NotADirectoryError, PermissionError) as exc:
            raise OperationError(
                "framework", "artifact could not be opened", "artifact_unavailable"
            ) from exc
        except OSError as exc:
            raise OperationError(
                "framework", "artifact could not be opened", "artifact_unavailable"
            ) from exc
        try:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_ARTIFACT_REDACTION_BYTES:
                raise OperationError(
                    "framework",
                    "artifact exceeds the bounded redaction scan size",
                    "result_too_large",
                )
            stream.seek(0)
            encoded = stream.read(MAX_ARTIFACT_REDACTION_BYTES + 1)
            if len(encoded) > MAX_ARTIFACT_REDACTION_BYTES:
                raise OperationError(
                    "framework",
                    "artifact exceeds the bounded redaction scan size",
                    "result_too_large",
                )
            size = len(encoded)
            if offset > size:
                raise OperationError(
                    "user", "offset is beyond the artifact size", "invalid_offset"
                )
        except OperationError:
            raise
        except OSError as exc:
            raise OperationError(
                "framework", "artifact could not be read", "artifact_unavailable"
            ) from exc
        finally:
            stream.close()

        redacted_ranges = self._artifact_redacted_ranges(encoded)
        text, consumed = self._render_artifact_slice(
            encoded, redacted_ranges, offset, limit
        )

        def build_result(selected_text: str, selected_consumed: int) -> dict[str, Any]:
            selected_offset = offset + selected_consumed
            return {
                "run_path": str(canonical),
                "artifact_path": relative,
                "media_type": self._artifact_media_type(artifact),
                "size": size,
                "offset": offset,
                "next_offset": selected_offset,
                "complete": selected_offset >= size,
                "text": selected_text,
            }

        result = build_result(text, consumed)
        if self._mcp_response_size(result, request_id) <= MAX_ARTIFACT_RESPONSE_BYTES:
            return result
        low, high = 0, min(limit, size - offset)
        best: dict[str, Any] | None = None
        while low <= high:
            middle = (low + high) // 2
            candidate_text, candidate_consumed = self._render_artifact_slice(
                encoded, redacted_ranges, offset, middle
            )
            if candidate_consumed == 0:
                low = middle + 1
                continue
            candidate = build_result(candidate_text, candidate_consumed)
            if self._mcp_response_size(candidate, request_id) <= MAX_ARTIFACT_RESPONSE_BYTES:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best is None:
            raise OperationError(
                "framework", "artifact response exceeds size limit", "result_too_large"
            )
        return best

    @staticmethod
    def _is_approved_artifact(relative: str) -> bool:
        if relative == "run/result-summary.json":
            return True
        return any(
            relative == root or relative.startswith(f"{root}/")
            for root in _ARTIFACT_ROOTS
        )

    @staticmethod
    def _is_text_artifact(relative: str) -> bool:
        if relative == "run/result-summary.json":
            return True
        if not any(
            relative == root or relative.startswith(f"{root}/")
            for root in _ARTIFACT_ROOTS
        ):
            return False
        return Path(relative).suffix.lower() in _TEXT_ARTIFACT_SUFFIXES

    @classmethod
    def _is_sensitive_artifact(cls, relative: str) -> bool:
        for component in Path(relative).parts:
            name = component.lower()
            if cls._redact_metadata(component) != component:
                return True
            if name in _SENSITIVE_ARTIFACT_NAMES:
                return True
            if Path(name).suffix.lower() in _SENSITIVE_ARTIFACT_SUFFIXES:
                return True
            if any(
                marker in name
                for marker in ("credential", "password", "secret", "token")
            ):
                return True
        return False

    @classmethod
    def _is_retrievable_artifact(cls, relative: str) -> bool:
        return cls._is_text_artifact(relative) and not cls._is_sensitive_artifact(relative)

    @staticmethod
    def _artifact_media_type(path: Path) -> str:
        return _ARTIFACT_SUFFIX_MEDIA_TYPES.get(
            path.suffix.lower(), "application/octet-stream"
        )

    @classmethod
    def _artifact_redacted_ranges(cls, encoded: bytes) -> list[tuple[int, int]]:
        """Find raw-byte ranges to redact before exposing a text artifact."""

        try:
            text = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OperationError(
                "user", "artifact is not valid UTF-8", "invalid_artifact"
            ) from exc
        if not text:
            return []

        line_count = text.count("\n") + text.count("\r") - text.count("\r\n")
        if not text.endswith(("\n", "\r")):
            line_count += 1
        if line_count > MAX_LOG_REDACTION_LINES:
            # A very fragmented document exceeds the same bounded scanner
            # budget as logs; hide it rather than return uninspected lines.
            return [(0, len(encoded))]

        sensitive_continuation_lines = cls._log_sensitive_shell_continuation_lines(text)
        ranges: list[tuple[int, int]] = []
        state = _LogRedactionState()
        byte_position = 0
        text_position = 0
        line_number = 0
        for separator in re.finditer(r"\r\n|\r|\n", text):
            content = text[text_position : separator.start()]
            ending = separator.group()
            content_bytes = len(content.encode("utf-8"))
            content_end = byte_position + content_bytes
            _, state, changed = cls._redact_log_physical_line_with_state(
                content, state, has_line_ending=True
            )
            if changed or line_number in sensitive_continuation_lines:
                if content_end > byte_position:
                    ranges.append((byte_position, content_end))
            byte_position = content_end + len(ending.encode("ascii"))
            text_position = separator.end()
            line_number += 1

        if text_position < len(text) or line_number == 0:
            content = text[text_position:]
            content_bytes = len(content.encode("utf-8"))
            content_end = byte_position + content_bytes
            _, _, changed = cls._redact_log_physical_line_with_state(
                content, state, has_line_ending=False
            )
            if changed or line_number in sensitive_continuation_lines:
                if content_end > byte_position:
                    ranges.append((byte_position, content_end))
        return ranges

    @classmethod
    def _render_artifact_slice(
        cls,
        encoded: bytes,
        redacted_ranges: list[tuple[int, int]],
        offset: int,
        limit: int,
    ) -> tuple[str, int]:
        """Render a UTF-8 page while keeping its cursor in original bytes."""

        raw_page = encoded[offset : offset + limit]
        at_eof = offset + len(raw_page) >= len(encoded)
        _, consumed = cls._decode_artifact_slice(raw_page, offset, at_eof)
        if consumed == 0:
            return "", 0

        page_end = offset + consumed
        parts: list[str] = []
        cursor = offset
        for start, end in redacted_ranges:
            if end <= cursor:
                continue
            if start >= page_end:
                break
            overlap_start = max(start, offset)
            overlap_end = min(end, page_end)
            if overlap_start >= overlap_end:
                continue
            if cursor < overlap_start:
                parts.append(encoded[cursor:overlap_start].decode("utf-8"))
            parts.append("[redacted]")
            cursor = overlap_end
        if cursor < page_end:
            parts.append(encoded[cursor:page_end].decode("utf-8"))
        return "".join(parts), consumed

    @staticmethod
    def _decode_artifact_slice(
        encoded: bytes, offset: int, at_eof: bool
    ) -> tuple[str, int]:
        if not encoded:
            return "", 0
        end = len(encoded)
        while end:
            try:
                return encoded[:end].decode("utf-8"), end
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data" and exc.start == 0:
                    if CrucibleOperations._utf8_character_length(encoded[0]) is not None:
                        if at_eof:
                            raise OperationError(
                                "user",
                                "artifact is not valid UTF-8",
                                "invalid_artifact",
                            ) from exc
                        raise OperationError(
                            "user",
                            "limit is too small for the next UTF-8 character",
                            "result_too_large",
                        ) from exc
                if exc.reason != "unexpected end of data" or exc.start == 0:
                    code = "invalid_offset" if offset else "invalid_artifact"
                    message = (
                        "offset is not at a UTF-8 character boundary"
                        if offset
                        else "artifact is not valid UTF-8"
                    )
                    raise OperationError("user", message, code) from exc
                if at_eof:
                    raise OperationError(
                        "user",
                        "artifact is not valid UTF-8",
                        "invalid_artifact",
                    ) from exc
                end = exc.start
        raise OperationError(
            "user",
            "limit is too small for the next UTF-8 character",
            "result_too_large",
        )

    @staticmethod
    def _utf8_character_length(first_byte: int) -> int | None:
        if first_byte <= 0x7F:
            return 1
        if 0xC2 <= first_byte <= 0xDF:
            return 2
        if 0xE0 <= first_byte <= 0xEF:
            return 3
        if 0xF0 <= first_byte <= 0xF4:
            return 4
        return None

    @staticmethod
    def _open_artifact_readonly(run_directory: Path, relative: str):
        """Open an approved artifact without reopening a replaceable pathname."""

        parts = Path(relative).parts
        if not parts:
            raise FileNotFoundError(relative)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        close_on_exec = getattr(os, "O_CLOEXEC", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        nonblocking = getattr(os, "O_NONBLOCK", 0)
        flags = os.O_RDONLY | no_follow | close_on_exec
        directory_fd = os.open(run_directory, flags | directory_flag)
        file_fd = None
        try:
            for component in parts[:-1]:
                next_directory_fd = os.open(
                    component,
                    flags | directory_flag,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_directory_fd
            # A raced FIFO must not block the request worker before fstat rejects it.
            file_fd = os.open(parts[-1], flags | nonblocking, dir_fd=directory_fd)
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise OperationError(
                    "user", "artifact is not a regular file", "artifact_not_found"
                )
            stream = os.fdopen(file_fd, "rb")
            file_fd = None
            return stream
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OperationError(
                    "authorization",
                    "artifact path changed to a symlink",
                    "path_rejected",
                ) from exc
            raise
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(directory_fd)

    def list_local_archives(self, limit: int = 1000) -> dict[str, Any]:
        """List local archives without accessing configured remote backends."""

        if limit < 1 or limit > 1000:
            raise OperationError("user", "limit must be between 1 and 1000", "invalid_limit")
        if not self.archive_root.is_dir():
            return {"archives": [], "count": 0}
        archives = []
        for path in sorted(self.archive_root.glob("*.tar.xz"), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                canonical = path.resolve()
                if (
                    self._redact_metadata(path.name) != path.name
                    or self._redact_metadata(str(canonical)) != str(canonical)
                ):
                    continue
                archives.append({"name": path.name, "path": str(canonical), "size": path.stat().st_size})
            except OSError:
                continue
            if len(archives) >= limit:
                break
        return {"archives": archives, "count": len(archives)}

    def canonical_local_archive(self, requested_path: Path) -> Path:
        """Resolve an archive path while keeping it inside the local archive root."""

        candidate = requested_path if requested_path.is_absolute() else self.archive_root / requested_path
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise OperationError("user", "archive does not exist", "not_found") from exc
        if not resolved.is_file() or resolved.suffixes[-2:] != [".tar", ".xz"]:
            raise OperationError("user", "archive must be a .tar.xz file", "invalid_archive")
        if self.archive_root not in resolved.parents:
            raise OperationError("authorization", "archive is outside the local archive root", "path_rejected")
        self._validate_legacy_basename(resolved, "archive")
        return resolved

    def canonical_archive_run(self, requested_path: Path) -> Path:
        """Resolve only a direct child of the real run root for archiving."""

        try:
            candidate = self.run_policy.canonical_directory(requested_path)
        except PolicyError as exc:
            raise OperationError("authorization", str(exc), "run_path_rejected") from exc
        if candidate.parent != self.local_run_root:
            raise OperationError(
                "authorization",
                "archive target must be a direct child of the local run root",
                "run_path_rejected",
            )
        self._validate_legacy_basename(candidate, "run")
        return candidate

    @staticmethod
    def _validate_legacy_basename(path: Path, label: str) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.name) is None:
            raise OperationError(
                "user",
                f"{label} name contains unsupported characters",
                "invalid_path",
            )

    @staticmethod
    def _run_metadata_path(run_directory: Path) -> Path:
        canonical = run_directory.resolve(strict=True)
        for relative in ("run/rickshaw-run.json", "run/rickshaw-run.json.xz",
                         "config/rickshaw-run.json", "config/rickshaw-run.json.xz"):
            path = run_directory / relative
            if path.is_file():
                resolved = path.resolve(strict=True)
                if canonical not in resolved.parents:
                    raise OperationError(
                        "authorization", "run metadata is outside the run directory", "path_rejected"
                    )
                return resolved
        raise OperationError("user", "run metadata is unavailable", "invalid_run")

    @staticmethod
    def _safe_artifact_path(run_directory: Path, relative: str) -> Path:
        canonical = run_directory.resolve(strict=True)
        relative_path = Path(relative)
        if relative_path.is_absolute() or any(
            part in {".", ".."} for part in relative_path.parts
        ):
            raise OperationError(
                "authorization", "run artifact path is not relative", "path_rejected"
            )
        path = run_directory / relative
        current = canonical
        for part in relative_path.parts:
            current /= part
            if current.is_symlink():
                raise OperationError(
                    "authorization", "run artifact path contains a symlink", "path_rejected"
                )
        resolved = path.resolve(strict=True)
        if canonical not in resolved.parents:
            raise OperationError(
                "authorization", "run artifact is outside the run directory", "path_rejected"
            )
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved

    def _load_run_metadata(self, run_directory: Path) -> tuple[Path, dict[str, Any]]:
        try:
            canonical = self._canonical_run_directory(run_directory)
            path, document = self._read_run_metadata(canonical, 1_048_576)
            self._validate_metadata_depth(document)
        except OperationError:
            raise
        except (
            RecursionError,
            OSError,
            UnicodeDecodeError,
            lzma.LZMAError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            if isinstance(exc, RecursionError):
                raise OperationError(
                    "framework",
                    "run metadata exceeds nesting limit",
                    "result_too_large",
                ) from exc
            raise OperationError("user", "run metadata is not valid JSON", "invalid_run") from exc
        if not isinstance(document, dict):
            raise OperationError("user", "run metadata must be a JSON object", "invalid_run")
        return path, document

    @staticmethod
    def _validate_metadata_depth(value: Any) -> None:
        pending: list[tuple[Any, int]] = [(value, 0)]
        while pending:
            current, depth = pending.pop()
            if depth > MAX_METADATA_DEPTH:
                raise OperationError(
                    "framework",
                    "run metadata exceeds nesting limit",
                    "result_too_large",
                )
            if isinstance(current, dict):
                pending.extend((child, depth + 1) for child in current.values())
            elif isinstance(current, list):
                pending.extend((child, depth + 1) for child in current)

    def _read_run_metadata(
        self, run_directory: Path, max_bytes: int
    ) -> tuple[Path, Any]:
        path = self._run_metadata_path(run_directory)
        if path.suffix != ".xz" and path.stat().st_size > max_bytes:
            raise OperationError(
                "framework", "run metadata exceeds size limit", "result_too_large"
            )
        relative = path.relative_to(run_directory)
        stream = self._open_artifact_readonly(run_directory, str(relative))
        try:
            if path.suffix == ".xz":
                decoder = lzma.LZMADecompressor(
                    format=lzma.FORMAT_XZ,
                    memlimit=MAX_METADATA_DECOMPRESSOR_MEMORY,
                )
                decoded_parts: list[bytes] = []
                decoded_size = 0
                while not decoder.eof and decoded_size <= max_bytes:
                    compressed = stream.read(65_536)
                    if not compressed:
                        break
                    remaining = max_bytes + 1 - decoded_size
                    decoded = decoder.decompress(compressed, max_length=remaining)
                    decoded_parts.append(decoded)
                    decoded_size += len(decoded)
                encoded = b"".join(decoded_parts)
                if not decoder.eof and decoded_size <= max_bytes:
                    raise lzma.LZMAError("truncated metadata compression stream")
            else:
                encoded = stream.read(max_bytes + 1)
        finally:
            stream.close()
        if len(encoded) > max_bytes:
            raise OperationError(
                "framework", "run metadata exceeds size limit", "result_too_large"
            )
        return path, json.loads(encoded.decode("utf-8"))

    def _canonical_run_directory(self, run_directory: Path) -> Path:
        try:
            return self.run_policy.canonical_directory(run_directory)
        except PolicyError as exc:
            raise OperationError("authorization", str(exc), "run_path_rejected") from exc
        except ValueError as exc:
            raise OperationError(
                "user", "run path contains an invalid character", "run_path_rejected"
            ) from exc

    @staticmethod
    def _write_run_metadata(path: Path, document: dict[str, Any]) -> None:
        temporary = None
        try:
            fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            os.close(fd)
            temporary = Path(temporary_name)
            if path.suffix == ".xz":
                with lzma.open(temporary, "wt", encoding="utf-8") as stream:
                    json.dump(document, stream, indent=4, sort_keys=True)
            else:
                temporary.write_text(json.dumps(document, indent=4, sort_keys=True), encoding="utf-8")
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        except (OSError, lzma.LZMAError, TypeError) as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise OperationError("framework", "could not update run metadata", "write_failed") from exc

    def list_benchmarks(self) -> list[dict[str, Any]]:
        root = self.crucible_home / "subprojects" / "benchmarks"
        if not root.is_dir():
            return []
        entries = []
        for directory in sorted(root.iterdir(), key=lambda path: path.name):
            safe_directory = self._benchmark_directory(directory.name)
            if safe_directory is None:
                continue
            metadata = self._benchmark_metadata(safe_directory)
            if metadata is not None:
                entries.append(self._redact_summary(metadata))
        return entries

    def list_tools(self, name: str | None = None) -> list[dict[str, Any]]:
        """List installed tools without invoking the host CLI or a shell."""

        root = self.crucible_home / "subprojects" / "tools"
        if not root.is_dir():
            return []
        repository_root = self.crucible_home / "repos"
        entries = []
        for directory in sorted(root.iterdir(), key=lambda path: path.name):
            if not directory.is_dir():
                continue
            try:
                resolved = directory.resolve(strict=True)
            except FileNotFoundError:
                continue
            if not (
                self._under_managed_root(resolved, root)
                or self._under_managed_root(resolved, repository_root)
            ):
                continue
            try:
                rickshaw = json.loads(
                    (directory / "rickshaw.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            tool_name = rickshaw.get("tool") if isinstance(rickshaw, dict) else None
            if not isinstance(tool_name, str) or (name is not None and tool_name != name):
                continue
            metadata: dict[str, Any] = {}
            metadata_path = directory / "tool-metadata.json"
            if metadata_path.is_file():
                try:
                    loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        metadata = loaded
                except (OSError, json.JSONDecodeError):
                    pass
            entries.append(
                self._redact_summary(
                    {
                        "name": tool_name,
                        "description": metadata.get("description"),
                        "metadata": metadata,
                    }
                )
            )
        return entries

    def list_endpoints(self) -> dict[str, Any]:
        """List installed endpoint implementations and their schemas."""

        endpoint_root = self.crucible_home / "subprojects" / "core" / "rickshaw" / "endpoints"
        schema_root = self.crucible_home / "subprojects" / "core" / "rickshaw" / "schema"
        try:
            endpoint_root_resolved = endpoint_root.resolve(strict=True)
            schema_root_resolved = schema_root.resolve(strict=True)
        except FileNotFoundError:
            # A checkout without activated subprojects has broken Rickshaw
            # symlinks.  That is a valid installation state for discovery:
            # there are no installed endpoints to report yet.
            return {"endpoints": [], "count": 0, "complete": True}
        except OSError as exc:
            raise OperationError(
                "framework", "endpoint discovery is unavailable", "discovery_unavailable"
            ) from exc
        if not endpoint_root_resolved.is_dir() or not schema_root_resolved.is_dir():
            return {"endpoints": [], "count": 0, "complete": True}

        entries: list[dict[str, Any]] = []
        complete = True
        try:
            with os.scandir(endpoint_root_resolved) as candidates:
                for candidate in candidates:
                    try:
                        if candidate.is_symlink() or not candidate.is_dir(follow_symlinks=False):
                            continue
                        if self._redact_metadata(candidate.name) != candidate.name:
                            # Endpoint names are returned identifiers and may
                            # otherwise disclose credentials embedded in them.
                            complete = False
                            continue
                        directory = Path(candidate.path)
                        module_path = directory / f"{candidate.name}.py"
                        if module_path.is_symlink() or not module_path.is_file():
                            module_path = directory / candidate.name
                            if module_path.is_symlink() or not module_path.is_file():
                                continue
                        module_resolved = module_path.resolve(strict=True)
                        if not self._under_managed_root(module_resolved, endpoint_root_resolved):
                            continue
                    except OSError:
                        complete = False
                        continue
                    if len(entries) >= MAX_ENDPOINT_COUNT:
                        complete = False
                        break

                    schema_info: dict[str, Any] | None = None
                    schema_logical = (
                        self.crucible_home
                        / "subprojects"
                        / "core"
                        / "rickshaw"
                        / "schema"
                        / f"{candidate.name}.json"
                    )
                    schema_path = schema_root_resolved / f"{candidate.name}.json"
                    try:
                        if schema_path.is_symlink() or not schema_path.is_file():
                            schema_path = None
                            complete = False
                        else:
                            schema_resolved = schema_path.resolve(strict=True)
                            if not self._under_managed_root(schema_resolved, schema_root_resolved):
                                schema_path = None
                                complete = False
                    except OSError:
                        schema_path = None
                        complete = False
                    if schema_path is not None:
                        try:
                            schema = json.loads(
                                self._read_bounded_utf8(schema_path, MAX_ENDPOINT_SCHEMA_BYTES)
                            )
                            if not isinstance(schema, dict):
                                complete = False
                            else:
                                properties = schema.get("properties", {})
                                property_names = sorted(properties) if isinstance(properties, dict) else []
                                if len(property_names) > MAX_ENDPOINT_SCHEMA_PROPERTIES:
                                    property_names = property_names[:MAX_ENDPOINT_SCHEMA_PROPERTIES]
                                    complete = False
                                if any(
                                    len(name) > MAX_ENDPOINT_PROPERTY_NAME_CHARS
                                    for name in property_names
                                ):
                                    property_names = [
                                        name[:MAX_ENDPOINT_PROPERTY_NAME_CHARS]
                                        for name in property_names
                                    ]
                                    complete = False
                                description = schema.get("description")
                                if isinstance(description, str):
                                    description = description[:MAX_ENDPOINT_DESCRIPTION_CHARS]
                                else:
                                    description = None
                                title = schema.get("title")
                                if isinstance(title, str):
                                    title = title[:MAX_ENDPOINT_TITLE_CHARS]
                                else:
                                    title = None
                                schema_info = {
                                    "path": self._relative_crucible_path(schema_logical),
                                    "title": title,
                                    "description": description,
                                    "properties": property_names,
                                }
                                redacted_schema_info = self._redact_summary(schema_info)
                                if redacted_schema_info != schema_info:
                                    complete = False
                                schema_info = redacted_schema_info
                        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                            complete = False

                    capabilities, capabilities_complete = self._endpoint_capabilities(module_resolved)
                    if not capabilities_complete:
                        complete = False
                    entries.append(
                        {
                            "name": candidate.name,
                            "implementation": self._relative_crucible_path(
                                self.crucible_home
                                / "subprojects"
                                / "core"
                                / "rickshaw"
                                / "endpoints"
                                / candidate.name
                                / module_path.name
                            ),
                            "schema": schema_info,
                            "capabilities": capabilities,
                        }
                    )
        except OSError as exc:
            raise OperationError(
                "framework", "endpoint discovery is unavailable", "discovery_unavailable"
            ) from exc
        entries.sort(key=lambda entry: entry["name"])
        return {"endpoints": entries, "count": len(entries), "complete": complete}

    def _endpoint_capabilities(self, module_path: Path) -> tuple[list[str], bool]:
        """Infer only coarse capabilities from trusted endpoint source markers."""

        try:
            source = self._read_bounded_utf8(module_path, MAX_ENDPOINT_MODULE_BYTES)
        except (OSError, UnicodeDecodeError, ValueError):
            return [], False
        python_functions = set(
            re.findall(r"^def ([A-Za-z_][A-Za-z0-9_]*)\s*\(", source, re.MULTILINE)
        )
        shell_functions = set(
            re.findall(
                r"^(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*\))?\s*\{",
                source,
                re.MULTILINE,
            )
        )
        functions = python_functions | shell_functions
        capabilities: list[str] = []
        # The legacy shell OSP endpoint uses the shared ``do_validate`` flag
        # instead of exposing a function named ``validate``.
        if (
            "validate" in functions
            or any(name.endswith("_validate") for name in functions)
            or re.search(r"\bdo_validate\b", source)
        ):
            capabilities.append("validate")
        if "engine_init" in functions or any(
            name.endswith("_engine_init") for name in functions
        ):
            capabilities.append("engine_deployment")
        if (
            {"test_start", "test_stop"}.issubset(functions)
            or any(name.endswith("_test_start") for name in functions)
            and any(name.endswith("_test_stop") for name in functions)
        ):
            capabilities.append("test_lifecycle")
        if any(name.endswith("_cleanup") or name == "cleanup" for name in functions):
            capabilities.append("cleanup")
        return capabilities, True

    @staticmethod
    def _read_bounded_utf8(path: Path, max_bytes: int) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                encoded = stream.read(max_bytes + 1)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(encoded) > max_bytes:
            raise ValueError("endpoint metadata exceeds size limit")
        return encoded.decode("utf-8")

    def _relative_crucible_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.crucible_home).as_posix()
        except ValueError:
            return path.as_posix()

    def list_indexed_results(
        self,
        *,
        run: str | None = None,
        name: str | None = None,
        email: str | None = None,
        harness: str | None = None,
        benchmark: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """List historical run IDs through the read-only CDM API."""

        if limit < 1 or limit > 1000:
            raise OperationError("user", "limit must be between 1 and 1000", "invalid_limit")
        filters = {
            key: value
            for key, value in {
                "run": run,
                "name": name,
                "email": email,
                "harness": harness,
                "benchmark": benchmark,
            }.items()
            if value is not None
        }
        query = f"?{urlencode(filters)}" if filters else ""
        payload = self._cdm_request(f"/api/v1/runs{query}")
        run_ids = payload.get("runIds") if isinstance(payload, dict) else None
        if not isinstance(run_ids, list) or not all(isinstance(item, str) for item in run_ids):
            raise OperationError(
                "framework", "CDM result search returned an invalid response", "invalid_result_response"
            )
        return self._redact_summary(
            {"run_ids": run_ids[:limit], "count": min(len(run_ids), limit)}
        )

    def get_indexed_result(self, run: str) -> dict[str, Any]:
        """Return structured metadata for one historical CDM run."""

        self._require_text(run, "run")
        encoded_run = quote(run, safe="")
        matches = self.list_indexed_results(run=run, limit=1)["run_ids"]
        if not matches:
            raise OperationError("user", f"unknown result run: {run}", "not_found")
        prefix = f"/api/v1/run/{encoded_run}"
        periods = self.list_indexed_periods(run)["periods"]
        tags = self._cdm_request(f"{prefix}/tags").get("tags", [])
        result = self._redact_summary({
            "run_id": run,
            "tags": tags,
            "benchmark": self._cdm_request(f"{prefix}/benchmark").get("benchmark"),
            "partial_status": self._cdm_request(f"{prefix}/partial-status"),
            "iterations": self._cdm_request(f"{prefix}/iterations").get("iterations", []),
            "metric_sources": self._cdm_request(f"{prefix}/metric-sources").get("sources", []),
        })
        result["periods"] = periods
        return result

    def list_indexed_periods(self, run: str) -> dict[str, Any]:
        """List every primary period and sample associated with a run."""

        self._require_text(run, "run")
        encoded_run = quote(run, safe="")
        prefix = f"/api/v1/run/{encoded_run}"
        iterations = self._cdm_request(f"{prefix}/iterations").get("iterations", [])
        if not isinstance(iterations, list) or not all(isinstance(item, str) for item in iterations):
            raise OperationError("framework", "CDM returned invalid iteration data", "invalid_result_response")
        if not iterations:
            return self._redact_summary({"run_id": run, "periods": []})

        def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
            return self._cdm_request(path, method="POST", body=body)

        samples = post(f"{prefix}/iterations/samples", {"iterations": iterations}).get("samples", [])
        statuses = post(f"{prefix}/samples/statuses", {"sampleIds": samples}).get("statuses", [])
        period_names = post(
            f"{prefix}/iterations/primary-period-name", {"iterations": iterations}
        ).get("periodNames", [])
        period_ids = post(
            f"{prefix}/samples/primary-period-id",
            {"sampleIds": samples, "periodNames": period_names},
        ).get("periodIds", [])
        ranges = post(f"{prefix}/periods/range", {"periodIds": period_ids}).get("ranges", [])

        periods = []
        for iteration_index, iteration_id in enumerate(iterations):
            iteration_samples = samples[iteration_index] if iteration_index < len(samples) else []
            iteration_statuses = statuses[iteration_index] if iteration_index < len(statuses) else []
            iteration_period_ids = period_ids[iteration_index] if iteration_index < len(period_ids) else []
            iteration_ranges = ranges[iteration_index] if iteration_index < len(ranges) else []
            for sample_index, period_id in enumerate(iteration_period_ids):
                if not isinstance(period_id, str) or not period_id:
                    continue
                period_range = iteration_ranges[sample_index] if sample_index < len(iteration_ranges) else {}
                periods.append(
                    {
                        "iteration_id": iteration_id,
                        "sample_id": iteration_samples[sample_index]
                        if sample_index < len(iteration_samples)
                        else None,
                        "primary_period_id": period_id,
                        "status": iteration_statuses[sample_index]
                        if sample_index < len(iteration_statuses)
                        else None,
                        "begin": period_range.get("begin") if isinstance(period_range, dict) else None,
                        "end": period_range.get("end") if isinstance(period_range, dict) else None,
                    }
                )
        return self._redact_summary({"run_id": run, "periods": periods})

    def get_indexed_metric(
        self,
        *,
        run: str,
        source: str,
        metric_type: str,
        period: str | None = None,
        begin: int | None = None,
        end: int | None = None,
        resolution: int = 1,
        breakout: list[str] | None = None,
        filter: str | None = None,
        aggregation: str | None = None,
        distribution_stats: str | None = None,
        allow_incompatible_aggregation: bool = False,
    ) -> dict[str, Any]:
        """Query bounded metric data through the CDM API."""

        for value, label in ((run, "run"), (source, "source"), (metric_type, "type")):
            self._require_text(value, label)
        if period is None and (begin is None or end is None):
            raise OperationError("user", "provide period or both begin and end", "invalid_metric_range")
        if resolution < 1 or resolution > 100_000:
            raise OperationError("user", "resolution is outside configured bounds", "invalid_resolution")
        body: dict[str, Any] = {
            "run": run,
            "source": source,
            "type": metric_type,
            "resolution": resolution,
            "breakout": breakout or [],
            "allow-incompatible-aggregation": allow_incompatible_aggregation,
        }
        optional_fields = {
            "period": period,
            "begin": begin,
            "end": end,
            "filter": filter,
            "aggregation": aggregation,
            "distribution-stats": distribution_stats,
        }
        body.update({key: value for key, value in optional_fields.items() if value is not None})
        return self._redact_metadata(
            self._cdm_request("/api/v1/metric-data", method="POST", body=body)
        )

    def list_log_sessions(self, limit: int = 100) -> dict[str, Any]:
        """List recent Crucible logger sessions without reading log contents."""

        if self.log_db is None:
            raise OperationError("framework", "log database is not configured", "log_unavailable")
        if limit < 1 or limit > 1000:
            raise OperationError("user", "limit must be between 1 and 1000", "invalid_limit")
        try:
            with sqlite3.connect(f"file:{self.log_db}?mode=ro", uri=True) as connection:
                rows = connection.execute(
                    """
                    SELECT sessions.session_id, sessions.timestamp,
                           sources.source, commands.command,
                           COUNT(lines.id) AS line_count
                    FROM sessions
                    JOIN sources ON sources.id = sessions.source
                    JOIN commands ON commands.id = sessions.command
                    LEFT JOIN lines ON lines.session = sessions.id
                    GROUP BY sessions.id
                    ORDER BY sessions.timestamp DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise OperationError("framework", "log database is unavailable", "log_unavailable") from exc
        return {
            "sessions": [
                {
                    "session_id": row[0],
                    "timestamp": row[1],
                    "source": row[2],
                    "command": self.redact_log_text(row[3] or ""),
                    "line_count": row[4],
                }
                for row in rows
            ]
        }

    def get_log_info(self) -> dict[str, Any]:
        """Return aggregate logger database information."""

        if self.log_db is None:
            raise OperationError("framework", "log database is not configured", "log_unavailable")
        try:
            with sqlite3.connect(f"file:{self.log_db}?mode=ro", uri=True) as connection:
                sessions = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                lines = connection.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
                sources = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        except sqlite3.Error as exc:
            raise OperationError("framework", "log database is unavailable", "log_unavailable") from exc
        return {"sessions": sessions, "lines": lines, "sources": sources}

    def get_log_session(
        self,
        session_id: str,
        offset: int = 0,
        limit: int = 1000,
        stream: str | None = None,
        grep: str | None = None,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """Return a bounded, structured slice of one logger session."""

        self._require_text(session_id, "session_id")
        if offset < 0 or limit < 1 or limit > 10000:
            raise OperationError("user", "offset must be nonnegative and limit must be 1..10000", "invalid_bounds")
        if stream is not None and stream not in {"stdout", "stderr"}:
            raise OperationError("user", "stream must be stdout or stderr", "invalid_stream")
        pattern = None
        if grep is not None:
            pattern = self._compile_log_pattern(grep, "grep")
        if self.log_db is None:
            raise OperationError("framework", "log database is not configured", "log_unavailable")
        try:
            with sqlite3.connect(f"file:{self.log_db}?mode=ro", uri=True) as connection:
                metadata = connection.execute(
                    """SELECT sessions.timestamp, sources.source, commands.command
                       FROM sessions JOIN sources ON sources.id = sessions.source
                       JOIN commands ON commands.id = sessions.command
                       WHERE sessions.session_id = ?""",
                    (session_id,),
                ).fetchone()
                if metadata is None:
                    raise OperationError("user", f"unknown log session: {session_id}", "not_found")
                query = """SELECT lines.timestamp, streams.stream, lines.line
                           FROM sessions JOIN lines ON lines.session = sessions.id
                           JOIN streams ON streams.id = lines.stream
                           WHERE sessions.session_id = ?"""
                params: list[Any] = [session_id]
                if stream is not None:
                    query += " AND streams.stream = ?"
                    params.append(stream.upper())
                query += " ORDER BY lines.id"
                rows = connection.execute(query, params)
                lines = []
                matched = 0
                response_bytes = 0
                complete = True
                redaction_states: dict[str, _LogRedactionState] = {}
                for timestamp, line_stream, line in rows:
                    line = line or ""
                    stream_key = str(line_stream).upper()
                    safe_line, redaction_states[stream_key], _ = (
                        self._redact_log_record_with_state(
                            line, redaction_states.get(stream_key)
                        )
                    )
                    if pattern is not None and pattern.search(line) is None:
                        continue
                    if matched < offset:
                        matched += 1
                        continue
                    if len(lines) >= limit:
                        complete = False
                        break
                    candidate = {
                        "timestamp": timestamp,
                        "stream": line_stream,
                        "line": safe_line,
                    }
                    candidate_bytes = len(json.dumps(candidate, separators=(",", ":")).encode("utf-8"))
                    if response_bytes + candidate_bytes > MAX_LOG_RESPONSE_BYTES:
                        if not lines:
                            raise OperationError(
                                "framework",
                                "log response contains a line larger than the response limit",
                                "result_too_large",
                            )
                        complete = False
                        break
                    lines.append(candidate)
                    response_bytes += candidate_bytes
                    matched += 1
        except OperationError:
            raise
        except sqlite3.Error as exc:
            raise OperationError("framework", "log database is unavailable", "log_unavailable") from exc
        def build_session_result(selected: list[dict[str, Any]], result_complete: bool) -> dict[str, Any]:
            return {
                "session_id": session_id,
                "timestamp": metadata[0],
                "source": metadata[1],
                "command": self.redact_log_text(metadata[2] or ""),
                "offset": offset,
                "next_offset": offset + len(selected),
                "complete": result_complete,
                "lines": selected,
            }

        lines, trimmed = self._bound_log_items(lines, build_session_result, complete, request_id)
        return {
            **build_session_result(lines, complete and not trimmed),
        }

    def search_logs(
        self,
        query: str,
        session_id: str | None = None,
        stream: str | None = None,
        offset: int = 0,
        limit: int = 1000,
        since: float | None = None,
        until: float | None = None,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """Search logger lines with bounded, structured results."""

        if not isinstance(query, str) or not query or len(query) > 256:
            raise OperationError("user", "query must be 1..256 characters", "invalid_query")
        pattern = self._compile_log_pattern(query, "query")
        if offset < 0 or limit < 1 or limit > 10000:
            raise OperationError("user", "offset must be nonnegative and limit must be 1..10000", "invalid_bounds")
        if stream is not None and stream not in {"stdout", "stderr"}:
            raise OperationError("user", "stream must be stdout or stderr", "invalid_stream")
        if self.log_db is None:
            raise OperationError("framework", "log database is not configured", "log_unavailable")
        try:
            with sqlite3.connect(f"file:{self.log_db}?mode=ro", uri=True) as connection:
                sql = """SELECT sessions.session_id, lines.timestamp, streams.stream,
                                 lines.line, sources.source, commands.command,
                                 sessions.id, streams.id, lines.id
                          FROM sessions JOIN lines ON lines.session = sessions.id
                          JOIN streams ON streams.id = lines.stream
                          JOIN sources ON sources.id = sessions.source
                          JOIN commands ON commands.id = sessions.command
                          WHERE 1 = 1"""
                params: list[Any] = []
                if session_id is not None:
                    self._require_text(session_id, "session_id")
                    sql += " AND sessions.session_id = ?"
                    params.append(session_id)
                if stream is not None:
                    sql += " AND streams.stream = ?"
                    params.append(stream.upper())
                if since is not None:
                    sql += " AND lines.timestamp >= ?"
                    params.append(since)
                if until is not None:
                    sql += " AND lines.timestamp <= ?"
                    params.append(until)
                sql += " ORDER BY lines.id"
                matches = []
                matched = 0
                response_bytes = 0
                complete = True
                redaction_states: dict[tuple[str, str], _LogRedactionState] = {}
                redaction_last_ids: dict[tuple[str, str], int] = {}
                redaction_context_budget = {
                    "lines": MAX_LOG_REDACTION_CONTEXT_LINES,
                    "bytes": MAX_LOG_REDACTION_CONTEXT_BYTES,
                }
                for row in connection.execute(sql, params):
                    line = row[3] or ""
                    state_key = (row[0], str(row[2]).upper())
                    if since is not None or until is not None:
                        if state_key not in redaction_states:
                            # Seed once per pair from bounded indexed history.
                            # Either timestamp bound can exclude rows that are
                            # still earlier in insertion order when clocks move.
                            redaction_states[state_key] = (
                                self._log_redaction_state_before(
                                    connection,
                                    row[6],
                                    row[7],
                                    row[8],
                                    redaction_context_budget,
                                )
                            )
                        else:
                            redaction_states[state_key] = (
                                self._log_redaction_state_between(
                                    connection,
                                    row[6],
                                    row[7],
                                    redaction_last_ids[state_key],
                                    row[8],
                                    redaction_states[state_key],
                                    redaction_context_budget,
                                )
                            )
                    else:
                        redaction_states.setdefault(
                            state_key, _LogRedactionState()
                        )
                    safe_line, redaction_states[state_key], _ = (
                        self._redact_log_record_with_state(
                            line, redaction_states[state_key]
                        )
                    )
                    redaction_last_ids[state_key] = row[8]
                    if pattern.search(line) is None:
                        continue
                    if matched < offset:
                        matched += 1
                        continue
                    if len(matches) >= limit:
                        complete = False
                        break
                    candidate = {
                        "session_id": row[0], "timestamp": row[1],
                        "stream": row[2], "line": safe_line,
                        "source": row[4],
                        "command": self.redact_log_text(row[5] or ""),
                    }
                    candidate_bytes = len(json.dumps(candidate, separators=(",", ":")).encode("utf-8"))
                    if response_bytes + candidate_bytes > MAX_LOG_RESPONSE_BYTES:
                        if not matches:
                            raise OperationError(
                                "framework",
                                "log response contains a line larger than the response limit",
                                "result_too_large",
                            )
                        complete = False
                        break
                    matches.append(candidate)
                    response_bytes += candidate_bytes
                    matched += 1
        except OperationError:
            raise
        except sqlite3.Error as exc:
            raise OperationError("framework", "log database is unavailable", "log_unavailable") from exc
        def build_search_result(selected: list[dict[str, Any]], result_complete: bool) -> dict[str, Any]:
            return {
                "query": self._redact_metadata(query),
                "offset": offset,
                "next_offset": offset + len(selected),
                "complete": result_complete,
                "matches": selected,
            }

        matches, trimmed = self._bound_log_items(matches, build_search_result, complete, request_id)
        return {
            **build_search_result(matches, complete and not trimmed),
        }

    @staticmethod
    def _compile_log_pattern(value: str, name: str) -> re.Pattern[str]:
        """Compile a regular-expression subset with bounded matching work."""

        if len(value) > 256:
            raise OperationError("user", f"{name} pattern is too long", f"invalid_{name}")
        escaped = False
        in_character_class = False
        for character in value:
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == "[":
                in_character_class = True
                continue
            if character == "]" and in_character_class:
                in_character_class = False
                continue
            if not in_character_class and character in "*+?{|()":
                raise OperationError(
                    "user",
                    f"{name} pattern grouping and alternation operators are not supported",
                    f"invalid_{name}",
                )
        try:
            return re.compile(value)
        except re.error as exc:
            raise OperationError("user", f"{name} pattern is invalid", f"invalid_{name}") from exc

    @staticmethod
    def _mcp_response_size(value: dict[str, Any], request_id: Any = None) -> int:
        text = json.dumps(value)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "structuredContent": value,
            },
        }
        return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    @classmethod
    def _bound_log_items(
        cls,
        items: list[dict[str, Any]],
        build: Any,
        complete: bool,
        request_id: Any = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        if cls._mcp_response_size(build(items, complete), request_id) <= MAX_LOG_RESPONSE_BYTES:
            return items, False
        if not items:
            raise OperationError(
                "framework", "log response exceeds size limit", "result_too_large"
            )
        low, high = 0, len(items)
        while low < high:
            middle = (low + high + 1) // 2
            if cls._mcp_response_size(build(items[:middle], False), request_id) <= MAX_LOG_RESPONSE_BYTES:
                low = middle
            else:
                high = middle - 1
        if low == 0:
            raise OperationError(
                "framework",
                "log response contains a line larger than the response limit",
                "result_too_large",
            )
        return items[:low], True

    def _cdm_request(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if encoded is not None else {}
        request = Request(f"{self.cdm_base_url}{path}", data=encoded, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                raw = response.read(2_097_153)
            if len(raw) > 2_097_152:
                raise OperationError("framework", "CDM response exceeds size limit", "result_too_large")
            payload = json.loads(raw.decode("utf-8"))
        except OperationError:
            raise
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise OperationError("framework", "CDM query is unavailable", "result_query_failed") from exc
        if not isinstance(payload, dict):
            raise OperationError("framework", "CDM query returned an invalid response", "invalid_result_response")
        return payload

    @staticmethod
    def _require_text(value: Any, label: str) -> None:
        if not isinstance(value, str) or not value:
            raise OperationError("user", f"{label} is required", "missing_argument")

    def describe_benchmark(self, name: str) -> dict[str, Any]:
        directory = self._benchmark_directory(name)
        if directory is None:
            raise OperationError("user", "benchmark name is invalid", "invalid_name")
        if not directory.exists():
            raise OperationError("user", f"unknown benchmark: {name}", "not_found")
        metadata = self._benchmark_metadata(directory)
        if metadata is None:
            raise OperationError("framework", f"benchmark metadata is unavailable: {name}")
        parameter_validation = self._benchmark_parameter_validation(directory)
        metadata["parameter_validation"] = parameter_validation
        redacted = self._redact_summary(metadata)
        redacted_validation = redacted.get("parameter_validation")
        if (
            isinstance(redacted_validation, dict)
            and redacted_validation != parameter_validation
        ):
            redacted_validation["complete"] = False
        return redacted

    def validate_run(self, document: Any) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise OperationError("user", "run document must be a JSON object", "invalid_json")
        schema_path = self.crucible_home / "subprojects" / "core" / "rickshaw" / "schema" / "run-file.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationError("framework", "run-file schema is unavailable") from exc

        validator = Draft201909Validator(schema)
        errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
        benchmark_errors = []
        benchmarks = document.get("benchmarks", [])
        if isinstance(benchmarks, list):
            for benchmark in benchmarks:
                name = benchmark.get("name") if isinstance(benchmark, dict) else None
                if not isinstance(name, str) or self._benchmark_directory(name) is None:
                    safe_name = self._redact_metadata(name) if isinstance(name, str) else name
                    benchmark_errors.append(f"benchmark is not installed: {safe_name!r}")

        messages = [self._format_validation_error(error) for error in errors]
        messages.extend(benchmark_errors)
        return {"valid": not messages, "errors": messages}

    def validate_run_file(self, path: Path) -> dict[str, Any]:
        try:
            canonical = self.input_policy.canonical_input(path)
            document = json.loads(canonical.read_text(encoding="utf-8"))
        except PolicyError as exc:
            raise OperationError("authorization", str(exc), "input_path_rejected") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationError("user", "run-file is not valid JSON", "invalid_json") from exc
        return self.validate_run(document)

    def prepare_run(
        self,
        document: Any,
        max_parameter_sets: int = 1000,
        max_engine_ids: int = 1000,
        max_tool_entries: int = 1000,
        max_response_bytes: int = MAX_PLAN_RESPONSE_BYTES,
        *,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """Return a bounded, side-effect-free plan for an inline run document."""

        self._validate_plan_response_limit(max_response_bytes)
        plan = self._build_run_plan(
            document, max_parameter_sets, max_engine_ids, max_tool_entries
        )
        return self._bound_plan_response(plan, max_response_bytes, request_id)

    def _build_run_plan(
        self,
        document: Any,
        max_parameter_sets: int = 1000,
        max_engine_ids: int = 1000,
        max_tool_entries: int = 1000,
    ) -> dict[str, Any]:
        """Build a plan without applying an operation-specific response projection."""

        # Let the planner produce its structured validation result for invalid
        # documents before applying expansion limits.  Otherwise a malformed
        # document can be reported as a resource-limit failure merely because
        # it contains a large invalid benchmark list.
        input_validation = None
        if isinstance(document, dict):
            input_validation = self.validate_run(document)
            if input_validation.get("valid"):
                self._validate_plan_work(document, max_parameter_sets, max_engine_ids)

        rickshaw_dir = self.crucible_home / "subprojects" / "core" / "rickshaw"
        multiplex_dir = self.crucible_home / "subprojects" / "core" / "multiplex"
        for path in (multiplex_dir, rickshaw_dir):
            path_string = str(path)
            if path_string in sys.path:
                sys.path.remove(path_string)
            sys.path.insert(0, path_string)
        try:
            import importlib

            for module_name, module_root in (
                ("rickshaw_lib.run_planner", rickshaw_dir),
                ("rickshaw_lib", rickshaw_dir),
                ("multiplex", multiplex_dir),
            ):
                loaded = sys.modules.get(module_name)
                if loaded is not None and not self._module_under_root(loaded, module_root):
                    del sys.modules[module_name]
            planner_module = importlib.import_module("rickshaw_lib.run_planner")
            multiplex = importlib.import_module("multiplex")
            if not self._module_under_root(planner_module, rickshaw_dir) or not self._module_under_root(
                multiplex, multiplex_dir
            ):
                raise ImportError("planner dependencies are not from managed checkouts")
            self._require_planner_api(multiplex)
            PlannerLimits = planner_module.PlannerLimits
            RunPlanner = planner_module.RunPlanner
        except (ImportError, AttributeError) as exc:
            raise OperationError(
                "framework",
                "Rickshaw run planner is unavailable",
                "planner_unavailable",
            ) from exc
        try:
            planner = RunPlanner(
                self.crucible_home / "subprojects" / "core" / "rickshaw",
                multiplex,
                self.crucible_home / "subprojects" / "benchmarks",
                benchmark_resolver=self._benchmark_directory,
            )
            # Multiplex's general expansion path has its own internal lock,
            # but apply_flat_params() also mutates module-global validation
            # state without taking that lock. Serialize both planner phases
            # through one process-wide lock so a tool plan cannot overwrite
            # another request's requirements while it is being expanded.
            with _PLANNING_LOCK, self._suppress_planner_diagnostics(multiplex) as diagnostics:
                plan = planner.plan(
                    document,
                    PlannerLimits(
                        max_parameter_sets=max_parameter_sets,
                        max_engine_ids=max_engine_ids,
                        max_tool_entries=max_tool_entries,
                    ),
                )
            self._enrich_parameter_expansion_errors(
                document, plan, diagnostics["parameter_validation_failed"]
            )
            for benchmark in plan.get("benchmarks", []):
                parameter_sets = benchmark.get("parameter_sets", {})
                if "items" in parameter_sets:
                    parameter_sets["items"] = self._redact_metadata(
                        parameter_sets["items"]
                    )
            if "tools" in plan and "entries" in plan["tools"]:
                plan["tools"]["entries"] = self._redact_metadata(
                    plan["tools"]["entries"]
                )
            if plan.get("validation", {}).get("valid"):
                integration_errors = self._validate_and_resolve_plan_inputs(
                    document, plan, multiplex, max_tool_entries
                )
                if integration_errors:
                    plan["validation"]["valid"] = False
                    plan["validation"].setdefault("errors", []).extend(
                        integration_errors
                    )
            if input_validation is not None and not input_validation.get("valid"):
                plan_validation = plan.get("validation")
                if isinstance(plan_validation, dict):
                    plan_validation["valid"] = False
                    plan_validation["errors"] = [
                        {"code": "invalid_input", "message": message}
                        for message in input_validation["errors"]
                    ]
            plan_validation = plan.get("validation")
            errors = (
                plan_validation.get("errors")
                if isinstance(plan_validation, dict)
                else None
            )
            if isinstance(errors, list):
                for error in errors:
                    if (
                        isinstance(error, dict)
                        and error.get("code") == "expansion_failed"
                    ):
                        # Multiplex exception text can contain rejected input
                        # values without a sensitive field name. Keep the
                        # actionable error category, but not the raw detail.
                        error["message"] = "parameter expansion failed"
            return self._redact_summary(plan)
        except OperationError:
            raise
        except ImportError as exc:
            raise OperationError(
                "framework", "Multiplex expansion library is unavailable", "planner_unavailable"
            ) from exc
        except ValueError as exc:
            raise OperationError(
                "user", "run planning failed validation", "invalid_plan"
            ) from exc
        except OSError as exc:
            raise OperationError(
                "framework", "run planner is unavailable", "planner_unavailable"
            ) from exc

    @staticmethod
    def _enrich_parameter_expansion_errors(
        document: Any, plan: dict[str, Any], parameter_validation_failed: bool
    ) -> None:
        """Classify known value-validation failures without exposing values."""

        if not parameter_validation_failed:
            return
        validation = plan.get("validation")
        errors = validation.get("errors") if isinstance(validation, dict) else None
        benchmarks = document.get("benchmarks") if isinstance(document, dict) else None
        if not isinstance(errors, list) or not isinstance(benchmarks, list):
            return
        names = [
            entry.get("name")
            for entry in benchmarks
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        ]
        for error in errors:
            if not isinstance(error, dict) or error.get("code") != "expansion_failed":
                continue
            message = error.get("message")
            if not isinstance(message, str):
                continue
            for name in names:
                if message.startswith(f"benchmark {name} parameter expansion failed:"):
                    error.update({
                        "code": "invalid_parameter",
                        "benchmark": name,
                        "guidance_tool": "describe_benchmark",
                        "guidance_field": "parameter_validation.rules",
                        "message": (
                            f"parameter validation failed for benchmark {name}; "
                            "inspect describe_benchmark parameter_validation.rules "
                            "for accepted forms"
                        ),
                    })
                    break

    def _benchmark_parameter_validation(self, directory: Path) -> dict[str, Any]:
        """Expose bounded validation metadata from the benchmark's source file."""

        result: dict[str, Any] = {
            "source": "multiplex.json",
            "available": False,
            "complete": True,
            "rules": [],
        }
        requirements_path = directory / "multiplex.json"
        try:
            requirements = json.loads(
                self._read_bounded_utf8(requirements_path, MAX_PLAN_REQUIREMENTS_BYTES)
            )
        except FileNotFoundError:
            return result
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            result["complete"] = False
            return result

        if not isinstance(requirements, dict):
            result["complete"] = False
            return result
        if "validations" not in requirements:
            result["available"] = True
            result["complete"] = False
            return result
        validations = requirements["validations"]
        if not isinstance(validations, dict):
            result["available"] = True
            result["complete"] = False
            return result

        result["available"] = True
        rules: list[dict[str, Any]] = []
        detail_bytes = 0
        for group, definition in sorted(validations.items(), key=lambda item: str(item[0])):
            if len(rules) >= MAX_BENCHMARK_VALIDATION_RULES:
                result["complete"] = False
                break
            if not isinstance(group, str) or not isinstance(definition, dict):
                result["complete"] = False
                continue
            args = definition.get("args")
            raw_patterns = definition.get("vals")
            patterns = [raw_patterns] if isinstance(raw_patterns, str) else raw_patterns
            if (
                not isinstance(args, list)
                or not args
                or any(not isinstance(arg, str) for arg in args)
                or not isinstance(patterns, list)
                or not patterns
                or any(not isinstance(pattern, str) for pattern in patterns)
            ):
                result["complete"] = False
                continue

            rule: dict[str, Any] = {
                "group": group[:128],
                "parameters": [arg[:128] for arg in args[:100]],
                "accepted_patterns": [
                    pattern[:MAX_BENCHMARK_VALIDATION_PATTERN_CHARS]
                    for pattern in patterns[:20]
                ],
                "repeatable": definition.get("repeatable", False) is True,
            }
            description = definition.get("description")
            if isinstance(description, str):
                rule["description"] = description[:512]
            if (
                len(group) > 128
                or len(args) > 100
                or any(len(arg) > 128 for arg in args)
                or len(patterns) > 20
                or any(len(pattern) > MAX_BENCHMARK_VALIDATION_PATTERN_CHARS for pattern in patterns)
                or (isinstance(description, str) and len(description) > 512)
                or ("repeatable" in definition and not isinstance(definition["repeatable"], bool))
            ):
                result["complete"] = False
            encoded_size = len(json.dumps(rule, separators=(",", ":")).encode("utf-8"))
            if detail_bytes + encoded_size > MAX_BENCHMARK_VALIDATION_DETAIL_BYTES:
                result["complete"] = False
                break
            rules.append(rule)
            detail_bytes += encoded_size
        result["rules"] = rules
        return result

    def _validate_and_resolve_plan_inputs(
        self,
        document: dict[str, Any],
        plan: dict[str, Any],
        multiplex: Any,
        max_tool_entries: int,
    ) -> list[dict[str, str]]:
        """Validate installed integrations and project their effective inputs."""

        errors: list[dict[str, str]] = []
        errors.extend(self._validate_plan_endpoints(document, plan))

        raw_tool_entries = document.get("tool-params")
        all_tool_entries = raw_tool_entries
        if all_tool_entries is None:
            all_tool_entries = plan.get("tools", {}).get("entries", [])
        if not isinstance(all_tool_entries, list):
            return errors + [{
                "code": "invalid_tool_params",
                "message": "tool-params must be an array",
            }]

        explicit_tools = raw_tool_entries is not None
        input_truncated = len(all_tool_entries) > max_tool_entries
        if explicit_tools:
            errors.extend(self._validate_tool_params_schema(all_tool_entries))

        installed_tools = {
            entry["name"]
            for entry in self.list_tools()
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        active_entries: list[tuple[int, dict[str, Any]]] = []
        tool_counts: dict[str, int] = {}
        malformed_count = 0
        invalid_name_count = 0
        unknown_tools: set[str] = set()
        for index, raw_entry in enumerate(all_tool_entries):
            if not isinstance(raw_entry, dict):
                malformed_count += 1
                continue
            if raw_entry.get("enabled") == "no":
                continue
            entry = copy.deepcopy(raw_entry)
            tool_name = entry.get("tool")
            if not isinstance(tool_name, str) or not tool_name:
                invalid_name_count += 1
                continue
            if tool_name not in installed_tools:
                unknown_tools.add(tool_name)
                continue
            active_entries.append((index, entry))
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1

        if malformed_count:
            errors.append({
                "code": "invalid_tool_params",
                "message": f"{malformed_count} tool-params entries must be objects",
            })
        if invalid_name_count:
            errors.append({
                "code": "invalid_tool_params",
                "message": f"{invalid_name_count} tool-params entries have no valid tool name",
            })
        if unknown_tools:
            errors.append({
                "code": "not_found",
                "message": f"{len(unknown_tools)} requested tool(s) are not installed",
            })

        seen_tool_ids: set[str] = set()
        invalid_entry_indexes: set[int] = set()
        resolved_entries: list[dict[str, Any]] = []
        for index, entry in active_entries:
            tool_name = entry["tool"]
            tool_id = entry.get("id")
            is_multiple = tool_counts[tool_name] > 1
            if is_multiple and not isinstance(tool_id, str):
                invalid_entry_indexes.add(index)
                errors.append({
                    "code": "invalid_tool_params",
                    "message": (
                        f"tool {tool_name} appears multiple times and each active "
                        "entry requires an id"
                    ),
                })
                continue
            if not is_multiple and tool_id is not None:
                invalid_entry_indexes.add(index)
                errors.append({
                    "code": "invalid_tool_params",
                    "message": f"tool {tool_name} has an id but is only specified once",
                })
                continue
            if tool_id is not None:
                if not isinstance(tool_id, str):
                    invalid_entry_indexes.add(index)
                    errors.append({
                        "code": "invalid_tool_params",
                        "message": f"tool id for {tool_name} must be a string",
                    })
                    continue
                if not tool_id.startswith(f"{tool_name}-") or len(tool_id) == len(tool_name) + 1:
                    invalid_entry_indexes.add(index)
                    errors.append({
                        "code": "invalid_tool_params",
                        "message": "tool id must start with its tool name and a hyphen",
                    })
                    continue
                if tool_id in seen_tool_ids:
                    invalid_entry_indexes.add(index)
                    errors.append({
                        "code": "invalid_tool_params",
                        "message": "tool ids must be unique",
                    })
                    continue
                seen_tool_ids.add(tool_id)

        for index, entry in active_entries:
            if index >= max_tool_entries or index in invalid_entry_indexes:
                continue

            tool_name = entry["tool"]
            tool_directory = self._tool_directory(tool_name)
            if tool_directory is None:
                errors.append({
                    "code": "not_found",
                    "message": f"tool is not installed: {tool_name}",
                })
                continue
            params = entry.get("params", [])
            if isinstance(params, list):
                params = [
                    {
                        key: value
                        for key, value in copy.deepcopy(param).items()
                        if key != "enabled"
                    }
                    for param in params
                    if isinstance(param, dict) and param.get("enabled") != "no"
                ]
            elif "params" in entry:
                errors.append({
                    "code": "invalid_tool_params",
                    "message": f"tool {tool_name} params must be an array",
                })
                continue
            multiplex_path = tool_directory / "multiplex.json"
            has_multiplex = multiplex_path.is_file()
            if has_multiplex:
                try:
                    requirements = json.loads(
                        self._read_bounded_utf8(
                            multiplex_path, MAX_PLAN_REQUIREMENTS_BYTES
                        )
                    )
                    with _PLANNING_LOCK, self._suppress_planner_diagnostics(multiplex):
                        expanded = multiplex.apply_flat_params(params, requirements)
                    if expanded is None:
                        raise ValueError("tool parameters produced no effective set")
                    params = expanded
                except (Exception, SystemExit):
                    errors.append({
                        "code": "invalid_tool_params",
                        "message": f"tool {tool_name} parameter expansion failed",
                    })
                    continue
            if "params" in entry or has_multiplex:
                entry["params"] = params
            entry.pop("enabled", None)
            resolved_entries.append(entry)

        tool_plan = plan.setdefault("tools", {})
        tool_plan["entries"] = self._redact_metadata(
            resolved_entries[:max_tool_entries]
        )
        tool_plan["truncated"] = (
            bool(tool_plan.get("truncated"))
            or input_truncated
            or len(resolved_entries) > max_tool_entries
        )
        plan.setdefault("limits", {})["truncated"] = (
            plan.get("limits", {}).get("truncated", False) or tool_plan["truncated"]
        )
        return errors

    @staticmethod
    def _module_under_root(module: Any, root: Path) -> bool:
        module_path = getattr(module, "__file__", None)
        if not isinstance(module_path, str):
            return False
        try:
            Path(module_path).resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    def _require_planner_api(multiplex: Any) -> None:
        if not callable(getattr(multiplex, "expand_parameters", None)) or not callable(
            getattr(multiplex, "apply_flat_params", None)
        ):
            raise ImportError("the installed Multiplex API is incomplete")

    @staticmethod
    @contextmanager
    def _suppress_planner_diagnostics(multiplex: Any):
        """Suppress planner logs while retaining only a safe validation signal."""

        logger_name = getattr(multiplex, "__name__", None)
        if not isinstance(logger_name, str):
            logger_name = "multiplex"
        logger = logging.getLogger(logger_name)
        previous_level = logger.level
        diagnostics = {"parameter_validation_failed": False}
        original_validator = getattr(multiplex, "param_validated", None)
        validator_patched = False
        if callable(original_validator):
            def track_validation_result(*args: Any, **kwargs: Any) -> Any:
                accepted = original_validator(*args, **kwargs)
                param = args[0] if args else kwargs.get("param")
                validation_dict = getattr(multiplex, "validation_dict", None)
                if (
                    not accepted
                    and isinstance(validation_dict, dict)
                    and param in validation_dict
                ):
                    diagnostics["parameter_validation_failed"] = True
                return accepted

            try:
                # The canonical expander resolves this module global. Observe
                # its final boolean result, not warnings for individual regex
                # alternatives, some of which may fail for an accepted value.
                setattr(multiplex, "param_validated", track_validation_result)
                validator_patched = True
            except (AttributeError, TypeError):
                pass
        logger.setLevel(logging.CRITICAL + 1)
        try:
            yield diagnostics
        finally:
            logger.setLevel(previous_level)
            if validator_patched:
                setattr(multiplex, "param_validated", original_validator)

    def _validate_plan_endpoints(
        self, document: dict[str, Any], plan: dict[str, Any]
    ) -> list[dict[str, str]]:
        try:
            discovery = self.list_endpoints()
        except OperationError as exc:
            plan.setdefault("topology", {})["confidence"] = "unknown"
            return [{"code": exc.code, "message": exc.message}]
        installed = {
            entry["name"]
            for entry in discovery.get("endpoints", [])
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and entry.get("schema") is not None
        }
        errors: list[dict[str, str]] = []
        schema_root = (
            self.crucible_home
            / "subprojects"
            / "core"
            / "rickshaw"
            / "schema"
        )
        schema_cache: dict[str, tuple[Any | None, str | None]] = {}
        for endpoint in document.get("endpoints", []):
            endpoint_type = endpoint.get("type") if isinstance(endpoint, dict) else None
            if not isinstance(endpoint_type, str) or endpoint_type not in installed:
                errors.append({
                    "code": "not_found",
                    "message": "endpoint type is not installed",
                })
                continue

            if endpoint_type not in schema_cache:
                schema_path = schema_root / f"{endpoint_type}.json"
                try:
                    if schema_path.is_symlink() or not schema_path.is_file():
                        raise OSError("endpoint schema is unavailable")
                    schema_resolved = schema_path.resolve(strict=True)
                    if not self._under_managed_root(schema_resolved, schema_root):
                        raise OSError("endpoint schema is outside the managed root")
                    schema_cache[endpoint_type] = (
                        json.loads(
                            self._read_bounded_utf8(
                                schema_resolved, MAX_ENDPOINT_SCHEMA_BYTES
                            )
                        ),
                        None,
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, OperationError) as exc:
                    schema_cache[endpoint_type] = (None, str(exc))
                except SchemaError as exc:
                    schema_cache[endpoint_type] = (None, str(exc))

            schema, schema_error = schema_cache[endpoint_type]
            if schema_error is not None or schema is None:
                errors.append({
                    "code": "planner_unavailable",
                    "message": f"endpoint schema is unavailable: {endpoint_type}",
                })
                continue
            try:
                endpoint_errors = sorted(
                    Draft201909Validator(schema).iter_errors(endpoint),
                    key=lambda error: list(error.path),
                )
            except SchemaError:
                endpoint_errors = []
                schema_cache[endpoint_type] = (None, "invalid endpoint schema")
            if schema_cache[endpoint_type][0] is None:
                errors.append({
                    "code": "planner_unavailable",
                    "message": f"endpoint schema is unavailable: {endpoint_type}",
                })
                continue
            for endpoint_error in endpoint_errors[:32]:
                location = ".".join(str(part) for part in endpoint_error.absolute_path)
                errors.append({
                    "code": "invalid_endpoint",
                    "message": (
                        f"endpoint {endpoint_type} configuration is invalid"
                        + (f" at {location}" if location else "")
                    ),
                })
        if errors:
            topology = plan.setdefault("topology", {})
            topology["confidence"] = "unknown"
            warnings = topology.setdefault("warnings", [])
            if any(error["code"] == "not_found" for error in errors) and \
                "one or more endpoint types are unavailable" not in warnings:
                warnings.append("one or more endpoint types are unavailable")
            if any(error["code"] == "invalid_endpoint" for error in errors) and \
                "one or more endpoint configurations are invalid" not in warnings:
                warnings.append("one or more endpoint configurations are invalid")
        return errors

    def _validate_tool_params_schema(self, entries: list[Any]) -> list[dict[str, str]]:
        schema_path = self.crucible_home / "subprojects" / "core" / "rickshaw" / "schema" / "tool-params.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return [{
                "code": "planner_unavailable",
                "message": "tool-params schema is unavailable",
            }]
        try:
            Draft201909Validator.check_schema(schema)
            errors = sorted(
                Draft201909Validator(schema).iter_errors(entries),
                key=lambda error: list(error.path),
            )
        except SchemaError:
            return [{
                "code": "planner_unavailable",
                "message": "tool-params schema is unavailable",
            }]
        return [
            {
                "code": "invalid_tool_params",
                "message": self._format_validation_error(error),
            }
            for error in errors[:32]
        ]

    def _tool_directory(self, name: str) -> Path | None:
        if not isinstance(name, str) or not name or Path(name).name != name:
            return None
        root = self.crucible_home / "subprojects" / "tools"
        candidate = root / name
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError):
            return None
        repository_root = self.crucible_home / "repos"
        if not (
            self._under_managed_root(resolved, root)
            or self._under_managed_root(resolved, repository_root)
        ) or not resolved.is_dir():
            return None
        return candidate

    def prepare_run_file(
        self,
        path: Path,
        max_parameter_sets: int = 1000,
        max_engine_ids: int = 1000,
        max_tool_entries: int = 1000,
        max_response_bytes: int = MAX_PLAN_RESPONSE_BYTES,
        *,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """Read an approved run file and return its bounded static plan."""

        return self.prepare_run(
            self._read_plan_run_file(path),
            max_parameter_sets,
            max_engine_ids,
            max_tool_entries,
            max_response_bytes,
            request_id=request_id,
        )

    def estimate_run_file(
        self,
        path: Path,
        max_parameter_sets: int = 1000,
        max_engine_ids: int = 1000,
        max_tool_entries: int = 1000,
        max_response_bytes: int = MAX_PLAN_RESPONSE_BYTES,
        *,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """Read an approved run file and return its bounded estimate."""

        return self.estimate_run(
            self._read_plan_run_file(path),
            max_parameter_sets=max_parameter_sets,
            max_engine_ids=max_engine_ids,
            max_tool_entries=max_tool_entries,
            max_response_bytes=max_response_bytes,
            request_id=request_id,
        )

    def _read_plan_run_file(self, path: Path) -> Any:
        try:
            canonical = self.input_policy.canonical_input(path)
            return json.loads(canonical.read_text(encoding="utf-8"))
        except PolicyError as exc:
            raise OperationError("authorization", str(exc), "input_path_rejected") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperationError("user", "run-file is not valid JSON", "invalid_json") from exc

    def read_run_document(self, path: Path) -> Any:
        """Read and authorize a run file for a caller that will reuse it."""

        return self._read_plan_run_file(path)

    def estimate_run(
        self,
        document: Any,
        max_response_bytes: int = MAX_PLAN_RESPONSE_BYTES,
        *,
        request_id: Any = None,
        **limits: int,
    ) -> dict[str, Any]:
        """Return only the derived counts and runtime confidence from a plan."""

        self._validate_plan_response_limit(max_response_bytes)
        plan = self._build_run_plan(
            document,
            limits.get("max_parameter_sets", 1000),
            limits.get("max_engine_ids", 1000),
            limits.get("max_tool_entries", 1000),
        )
        estimate = {
            "contract_version": plan["contract_version"],
            "input_digest": plan["input_digest"],
            "validation": plan["validation"],
            "totals": plan["totals"],
            "runtime": plan["runtime"],
            "limits": plan["limits"],
        }
        return self._bound_plan_response(estimate, max_response_bytes, request_id)

    @staticmethod
    def _validate_plan_response_limit(max_response_bytes: int) -> None:
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1024
            or max_response_bytes > MAX_PLAN_RESPONSE_BYTES
        ):
            raise OperationError(
                "user",
                f"max_response_bytes must be an integer between 1024 and {MAX_PLAN_RESPONSE_BYTES}",
                "invalid_limit",
            )

    def _validate_plan_work(
        self,
        document: Any,
        max_parameter_sets: int,
        max_engine_ids: int = 1000,
    ) -> None:
        """Reject worst-case expansion requests before the planner materializes them."""

        if not isinstance(document, dict):
            return
        benchmarks = document.get("benchmarks")
        if not isinstance(benchmarks, list):
            return
        benchmark_count = len(benchmarks)
        if benchmark_count > MAX_PLAN_BENCHMARKS:
            raise OperationError(
                "user",
                f"run planning supports at most {MAX_PLAN_BENCHMARKS} benchmark occurrences",
                "planning_limit",
            )
        if (
            isinstance(max_parameter_sets, bool)
            or not isinstance(max_parameter_sets, int)
            or max_parameter_sets < 1
        ):
            return
        if benchmark_count * max_parameter_sets > MAX_PLAN_PARAMETER_WORK:
            raise OperationError(
                "user",
                "requested benchmark expansion exceeds the aggregate planning work limit; "
                "reduce max_parameter_sets or the number of benchmark occurrences",
                "planning_limit",
            )
        parameter_entry_work = 0
        materialized_bytes = 0
        engine_id_bytes = 0
        for benchmark in benchmarks:
            raw_parameter_bytes = self._validate_raw_parameter_work(benchmark)
            parameter_entry_count, preset_bytes = self._plan_parameter_entry_upper_bound(benchmark)
            parameter_entry_work += max(1, parameter_entry_count) * max_parameter_sets
            materialized_bytes += (raw_parameter_bytes + preset_bytes) * max_parameter_sets
            engine_id_bytes += self._validate_engine_id_work(benchmark, max_engine_ids)
            if parameter_entry_work > MAX_PLAN_PARAMETER_ENTRY_WORK:
                raise OperationError(
                    "user",
                    "requested benchmark parameter entries exceed the aggregate planning "
                    "work limit; reduce max_parameter_sets or the parameter definitions",
                    "planning_limit",
                )
            if engine_id_bytes > MAX_PLAN_ENGINE_ID_BYTES:
                raise OperationError(
                    "user",
                    "expanded engine IDs exceed the planning work limit; reduce engine ID ranges",
                    "planning_limit",
                )
            if materialized_bytes > MAX_PLAN_MATERIALIZED_BYTES:
                raise OperationError(
                    "user",
                    "expanded benchmark details exceed the planning work limit; "
                    "reduce max_parameter_sets or parameter value sizes",
                    "planning_limit",
                )

    def _validate_raw_parameter_work(self, benchmark: Any) -> int:
        if not isinstance(benchmark, dict) or "mv-params" not in benchmark:
            return 0
        raw_parameters = benchmark["mv-params"]
        selected_parameters = (
            raw_parameters[0]
            if isinstance(raw_parameters, list)
            and raw_parameters
            and isinstance(raw_parameters[0], dict)
            else raw_parameters
        )
        try:
            encoded_size = len(
                json.dumps(
                    selected_parameters, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise OperationError(
                "user", "benchmark parameters are not JSON-serializable", "invalid_plan"
            ) from exc
        if encoded_size > MAX_PLAN_RAW_PARAMETER_BYTES:
            raise OperationError(
                "user",
                "raw benchmark parameter definitions exceed the planning work limit",
                "planning_limit",
            )
        if self._count_raw_parameter_entries(selected_parameters) > MAX_PLAN_RAW_PARAMETER_ENTRIES:
            raise OperationError(
                "user",
                "raw benchmark parameter entries exceed the planning work limit",
                "planning_limit",
            )
        return encoded_size

    @staticmethod
    def _count_raw_parameter_entries(value: Any) -> int:
        # Match the planner's legacy-array selection while bounding the full
        # parameter object that Multiplex will validate and expand.
        if isinstance(value, list):
            value = value[0] if value and isinstance(value[0], dict) else {}
        count = 0
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                if current.get("enabled") == "no":
                    continue
                if "arg" in current and ("val" in current or "vals" in current):
                    count += 1
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
        return count

    def _plan_parameter_entry_upper_bound(self, benchmark: Any) -> tuple[int, int]:
        if not isinstance(benchmark, dict):
            return 1, 0
        count = self._count_plan_parameter_entries(benchmark.get("mv-params"))
        name = benchmark.get("name")
        if not isinstance(name, str):
            return max(1, count), 0
        directory = self._benchmark_directory(name)
        if directory is None:
            return max(1, count), 0
        requirements_path = directory / "multiplex.json"
        try:
            if requirements_path.stat().st_size > MAX_PLAN_REQUIREMENTS_BYTES:
                raise OperationError(
                    "framework",
                    "benchmark requirements exceed the planning metadata limit",
                    "planning_limit",
                )
            requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return max(1, count), 0
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return max(1, count), 0
        presets = requirements.get("presets") if isinstance(requirements, dict) else None
        try:
            preset_bytes = len(
                json.dumps(presets, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        except (TypeError, ValueError):
            preset_bytes = 0
        return max(1, count + self._count_requirement_parameter_entries(presets)), preset_bytes

    @staticmethod
    def _validate_engine_id_work(benchmark: Any, max_engine_ids: int) -> int:
        if (
            not isinstance(benchmark, dict)
            or isinstance(max_engine_ids, bool)
            or not isinstance(max_engine_ids, int)
            or max_engine_ids < 1
        ):
            return 0
        raw_ids = benchmark.get("ids")
        raw_values = raw_ids if isinstance(raw_ids, list) else [raw_ids]
        estimated_bytes = 0
        for raw_value in raw_values:
            if not isinstance(raw_value, (str, int)) or isinstance(raw_value, bool):
                continue
            for token in re.split(r"[,+]", str(raw_value)):
                if len(token) > MAX_PLAN_ENGINE_ID_TOKEN_CHARS:
                    raise OperationError(
                        "user",
                        "engine ID token exceeds the planning work limit",
                        "planning_limit",
                    )
                range_match = re.fullmatch(r"(\d+)-(\d+)", token)
                if range_match:
                    start_text, end_text = range_match.groups()
                    start, end = int(start_text), int(end_text)
                    if start > end:
                        continue
                    count = min(max_engine_ids, end - start + 1)
                    width = max(len(str(start)), len(str(end)))
                elif token.isdigit():
                    count = 1
                    width = len(str(int(token)))
                else:
                    continue
                estimated_bytes += count * (width + 8)
                if estimated_bytes > MAX_PLAN_ENGINE_ID_BYTES:
                    return estimated_bytes
        return estimated_bytes

    @staticmethod
    def _count_enabled_parameter_list(entries: Any) -> int:
        if not isinstance(entries, list):
            return 0
        return sum(
            1
            for entry in entries
            if isinstance(entry, dict)
            and "arg" in entry
            and ("val" in entry or "vals" in entry)
            and entry.get("enabled") != "no"
        )

    @classmethod
    def _count_plan_parameter_entries(cls, value: Any) -> int:
        """Count effective entries from the representation multiplex expands."""

        # Rickshaw accepts the legacy mv-params array but selects only its
        # first object before passing it to multiplex.
        if isinstance(value, list):
            value = value[0] if value and isinstance(value[0], dict) else {}
        if not isinstance(value, dict):
            return 0

        global_options = {
            group.get("name"): group.get("params", [])
            for group in value.get("global-options", [])
            if isinstance(group, dict) and isinstance(group.get("name"), str)
        }
        sets = value.get("sets")
        if not isinstance(sets, list) or not sets:
            return cls._count_enabled_parameter_list(value.get("params"))

        counts = []
        for parameter_set in sets:
            if not isinstance(parameter_set, dict) or parameter_set.get("enabled") == "no":
                continue
            count = cls._count_enabled_parameter_list(parameter_set.get("params"))
            includes = parameter_set.get("include", [])
            if isinstance(includes, str):
                includes = [includes]
            if isinstance(includes, list):
                for name in includes:
                    count += cls._count_enabled_parameter_list(global_options.get(name))
            counts.append(count)
        return max(counts, default=0)

    @classmethod
    def _count_requirement_parameter_entries(cls, presets: Any) -> int:
        if not isinstance(presets, dict):
            return 0
        return sum(cls._count_enabled_parameter_list(entries) for entries in presets.values())

    def _bound_plan_response(
        self, plan: dict[str, Any], max_response_bytes: int, request_id: Any
    ) -> dict[str, Any]:
        bounded = dict(plan)
        if "benchmarks" in plan:
            bounded["benchmarks"] = []
        detail_lists: list[tuple[dict[str, Any], dict[str, Any], str, list[Any]]] = []

        for source_benchmark in plan.get("benchmarks", []):
            if not isinstance(source_benchmark, dict):
                bounded["benchmarks"].append(source_benchmark)
                continue
            target_benchmark = dict(source_benchmark)
            source_parameter_sets = source_benchmark.get("parameter_sets")
            if isinstance(source_parameter_sets, dict):
                target_parameter_sets = dict(source_parameter_sets)
                items = source_parameter_sets.get("items")
                if isinstance(items, list) and items:
                    target_parameter_sets["items"] = []
                    target_parameter_sets["returned"] = 0
                    target_parameter_sets["truncated"] = True
                    detail_lists.append(
                        (target_parameter_sets, source_parameter_sets, "items", items)
                    )
                target_benchmark["parameter_sets"] = target_parameter_sets

            source_engine_ids = source_benchmark.get("engine_ids")
            if isinstance(source_engine_ids, dict):
                target_engine_ids = dict(source_engine_ids)
                items = source_engine_ids.get("items")
                if isinstance(items, list) and items:
                    target_engine_ids["items"] = []
                    target_engine_ids["truncated"] = True
                    detail_lists.append(
                        (target_engine_ids, source_engine_ids, "items", items)
                    )
                target_benchmark["engine_ids"] = target_engine_ids
            bounded["benchmarks"].append(target_benchmark)

        source_tools = plan.get("tools")
        if isinstance(source_tools, dict):
            target_tools = dict(source_tools)
            entries = source_tools.get("entries")
            if isinstance(entries, list) and entries:
                target_tools["entries"] = []
                target_tools["truncated"] = True
                detail_lists.append((target_tools, source_tools, "entries", entries))
            bounded["tools"] = target_tools

        source_limits = plan.get("limits")
        if isinstance(source_limits, dict):
            bounded["limits"] = dict(source_limits)
            if isinstance(source_limits.get("warnings"), list):
                bounded["limits"]["warnings"] = list(source_limits["warnings"])

        # Estimate the detail contribution item-by-item.  This stops after the
        # response budget is exceeded, avoiding a full serialization of a
        # potentially enormous expansion just to discover that its prefix must
        # be omitted.
        base_size = self._mcp_response_size(bounded, request_id)
        if base_size > max_response_bytes:
            raise OperationError(
                "framework",
                "run plan exceeds the response-byte limit",
                "result_too_large",
            )
        estimated_size = base_size
        details_fit = True
        for _, _, _, items in detail_lists:
            for item in items:
                try:
                    item_json = json.dumps(item)
                    estimated_size += (
                        len(item_json.encode("utf-8"))
                        + len(json.dumps(item_json).encode("utf-8"))
                        + 64
                    )
                except (TypeError, ValueError) as exc:
                    raise OperationError(
                        "framework",
                        "run plan contains unserializable detail",
                        "invalid_plan",
                    ) from exc
                if estimated_size > max_response_bytes:
                    details_fit = False
                    break
            if not details_fit:
                break

        if details_fit:
            for target, source, key, items in detail_lists:
                target.clear()
                target.update(source)
                target[key] = list(items)
            if self._mcp_response_size(bounded, request_id) <= max_response_bytes:
                return bounded
            # The estimate is intentionally conservative, but retain a safe
            # fallback if JSON encoding has more envelope overhead than it
            # predicted.
            bounded = self._bound_plan_detail_skeleton(plan)

        limits = bounded.setdefault("limits", {})
        warnings = limits.setdefault("warnings", [])
        if "detail prefixes omitted to stay within the response-byte limit" not in warnings:
            warnings.append("detail prefixes omitted to stay within the response-byte limit")
        limits["truncated"] = True
        if self._mcp_response_size(bounded, request_id) > max_response_bytes:
            raise OperationError(
                "framework",
                "run plan exceeds the response-byte limit",
                "result_too_large",
            )
        return bounded

    def _bound_plan_detail_skeleton(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Copy plan structure while dropping potentially large detail lists."""

        bounded = dict(plan)
        if "benchmarks" in plan:
            bounded["benchmarks"] = []
        for source_benchmark in plan.get("benchmarks", []):
            if not isinstance(source_benchmark, dict):
                bounded["benchmarks"].append(source_benchmark)
                continue
            target_benchmark = dict(source_benchmark)
            for section_name in ("parameter_sets", "engine_ids"):
                source_section = source_benchmark.get(section_name)
                if not isinstance(source_section, dict):
                    continue
                target_section = dict(source_section)
                items = source_section.get("items")
                if isinstance(items, list) and items:
                    target_section["items"] = []
                    if section_name == "parameter_sets":
                        target_section["returned"] = 0
                    target_section["truncated"] = True
                target_benchmark[section_name] = target_section
            bounded["benchmarks"].append(target_benchmark)
        source_tools = plan.get("tools")
        if isinstance(source_tools, dict):
            bounded["tools"] = dict(source_tools)
            entries = source_tools.get("entries")
            if isinstance(entries, list) and entries:
                bounded["tools"]["entries"] = []
                bounded["tools"]["truncated"] = True
        source_limits = plan.get("limits")
        if isinstance(source_limits, dict):
            bounded["limits"] = dict(source_limits)
            if isinstance(source_limits.get("warnings"), list):
                bounded["limits"]["warnings"] = list(source_limits["warnings"])
        return bounded

    def _benchmark_directory(self, name: str) -> Path | None:
        """Resolve a benchmark name without allowing filesystem escapes.

        Active benchmark entries are symlinks into Crucible's managed
        ``repos`` clones, so both the logical benchmark root and that clone
        root are approved after resolution.  Arbitrary symlink targets are
        rejected.
        """

        if not isinstance(name, str) or not name or Path(name).name != name:
            return None
        benchmark_root = self.crucible_home / "subprojects" / "benchmarks"
        candidate = benchmark_root / name
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            return None
        approved_roots = (
            benchmark_root.resolve(),
            (self.crucible_home / "repos").resolve(),
        )
        if not any(root == resolved or root in resolved.parents for root in approved_roots):
            return None
        if not resolved.is_dir():
            return None
        return candidate

    @staticmethod
    def _under_managed_root(candidate: Path, root: Path) -> bool:
        resolved_root = root.resolve()
        return candidate == resolved_root or resolved_root in candidate.parents

    def _benchmark_metadata(self, directory: Path) -> dict[str, Any] | None:
        rickshaw_path = directory / "rickshaw.json"
        if not rickshaw_path.is_file():
            return None
        try:
            rickshaw = json.loads(rickshaw_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        name = rickshaw.get("benchmark")
        if not isinstance(name, str):
            return None
        result: dict[str, Any] = {"name": name, "description": None, "metadata": {}}
        metadata_path = directory / "benchmark-metadata.json"
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
            if isinstance(metadata, dict):
                result["description"] = metadata.get("description")
                result["metadata"] = metadata
        return result

    @classmethod
    def _format_validation_error(cls, error: Any) -> str:
        path_parts = []
        for item in error.path:
            if isinstance(item, str):
                item = cls._redact_metadata(item)
                if len(item) > 128:
                    item = item[:128] + "…"
            path_parts.append(str(item))
        path = ".".join(path_parts)
        # jsonschema's human-readable message interpolates the rejected instance
        # value. Run files can contain credentials, so expose only the schema
        # keyword and location rather than echoing that value.
        validator = (
            error.validator
            if isinstance(error.validator, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", error.validator)
            else "schema"
        )
        detail = f"does not satisfy the {validator} constraint"
        return f"{path}: {detail}" if path else detail
