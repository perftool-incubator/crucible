"""Typed, non-execution Crucible operations for the MCP contract."""

import errno
import json
import lzma
import os
import re
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from jsonschema import Draft201909Validator

from .documentation import DocumentationCatalog
from .policy import InputPolicy, PolicyError

MAX_LOG_RESPONSE_BYTES = 1_048_576
MAX_ARTIFACT_LIST_LIMIT = 1000
MAX_ARTIFACT_OFFSET = 1_073_741_824
MAX_ARTIFACT_SCAN_FILES = 100_000
MAX_ARTIFACT_DIRECTORY_DEPTH = 64
MAX_ARTIFACT_METADATA_BYTES = 262_144
MAX_ARTIFACT_READ_BYTES = 131_072
MAX_ARTIFACT_RESPONSE_BYTES = 1_048_576
MAX_METADATA_RESPONSE_BYTES = 1_048_576
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
    r"password|private|secret|token|jwt|bearer)[^\"]*\"\s*:"
)
_METADATA_SENSITIVE_TEXT = re.compile(
    r"(?:auth|authorization|credential|credentials|passphrase|pass|pwd|"
    r"password|private|secret|token|jwt|bearer|api[_-]?key|access[_-]?key|secret[_-]?key)",
    re.IGNORECASE,
)
_METADATA_QUOTED_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?:\\?[\"'])(?:auth|authorization|credential|credentials|passphrase|"
    r"pass|pwd|password|private|secret|token|jwt|bearer|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key)[^\"']*"
    r"(?:\\?[\"'])\s*[:=]",
    re.IGNORECASE,
)


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
    ):
        self.crucible_home = Path(crucible_home).resolve()
        self.cdm_base_url = cdm_base_url.rstrip("/")
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

    def crucible_info(self) -> dict[str, Any]:
        return {
            "name": "crucible",
            "mcp_contract_version": "2",
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
        return {"query": query, "resources": resources, "count": len(resources)}

    def list_local_run_tags(self, run_directory: Path) -> dict[str, Any]:
        _, document = self._load_run_metadata(run_directory)
        return {"run_path": str(run_directory), "tags": self._validated_tags(document)}

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
            self._write_run_metadata(path, document)
            return {"run_path": str(run_directory), "tags": current}

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
            self._write_run_metadata(path, document)
            return {"run_path": str(run_directory), "tags": document["tags"]}

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

    def list_local_runs(self, limit: int = 1000) -> dict[str, Any]:
        """List local run directories without querying indexed result data."""

        if limit < 1 or limit > 1000:
            raise OperationError("user", "limit must be between 1 and 1000", "invalid_limit")
        entries: list[dict[str, Any]] = []
        seen: set[Path] = set()
        root = self.local_run_root
        if not root.is_dir():
            return {"runs": [], "count": 0}
        for directory in sorted(root.iterdir(), key=lambda path: path.name):
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                canonical = directory.resolve(strict=True)
            except OSError:
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
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
                entries.append(entry)
                if len(entries) >= limit:
                    break
                continue
            entry["status"] = "complete" if metadata_path.parent == canonical / "run" else "incomplete"
            entry["run_id"] = metadata.get("run-id") or metadata.get("id")
            try:
                entry["tags"] = self._validated_tags(metadata)
            except OperationError:
                entry["status"] = "incomplete"
                entry["tags"] = []
            entries.append(entry)
            if len(entries) >= limit:
                break
        return {"runs": entries, "count": len(entries)}

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
                field_variant = (
                    cls._metadata_field_variant(key) if isinstance(key, str) else ""
                )
                if (
                    isinstance(item, str)
                    and field_variant in {"user", "username"}
                    and cls._is_user_credential_value(item)
                ):
                    redacted[key] = cls._redact_user_credential_value(item)
                elif (
                    has_user_name_descriptor
                    and isinstance(item, str)
                    and field_variant in _METADATA_VALUE_FIELD_VARIANTS
                    and cls._is_user_credential_value(item)
                ):
                    redacted[key] = cls._redact_user_credential_value(item)
                elif isinstance(key, str) and cls._metadata_key_is_sensitive(key):
                    redacted[key] = "[redacted]"
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
                    redacted[key] = "[redacted]"
                else:
                    redacted[key] = cls._redact_metadata(item, depth + 1, budget)
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
            r"password|private|secret|token|jwt|bearer|api[_-]?key|"
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
                    "retrievable": self._is_retrievable_artifact(relative),
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
            if offset > size:
                raise OperationError(
                    "user", "offset is beyond the artifact size", "invalid_offset"
                )
            stream.seek(offset)
            encoded = stream.read(limit)
        except OperationError:
            raise
        except OSError as exc:
            raise OperationError(
                "framework", "artifact could not be read", "artifact_unavailable"
            ) from exc
        finally:
            stream.close()

        text, consumed = self._decode_artifact_slice(
            encoded, offset, offset + len(encoded) >= size
        )

        def build_result(selected_text: str) -> dict[str, Any]:
            selected_consumed = len(selected_text.encode("utf-8"))
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

        result = build_result(text)
        if self._mcp_response_size(result, request_id) <= MAX_ARTIFACT_RESPONSE_BYTES:
            return result
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = build_result(text[:middle])
            if self._mcp_response_size(candidate, request_id) <= MAX_ARTIFACT_RESPONSE_BYTES:
                low = middle
            else:
                high = middle - 1
        if low == 0:
            raise OperationError(
                "framework", "artifact response exceeds size limit", "result_too_large"
            )
        return build_result(text[:low])

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

    @staticmethod
    def _is_sensitive_artifact(relative: str) -> bool:
        for component in Path(relative).parts:
            name = component.lower()
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
                archives.append({"name": path.name, "path": str(path.resolve()), "size": path.stat().st_size})
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
                entries.append(metadata)
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
                {
                    "name": tool_name,
                    "description": metadata.get("description"),
                    "metadata": metadata,
                }
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
        return {"run_ids": run_ids[:limit], "count": min(len(run_ids), limit)}

    def get_indexed_result(self, run: str) -> dict[str, Any]:
        """Return structured metadata for one historical CDM run."""

        self._require_text(run, "run")
        encoded_run = quote(run, safe="")
        matches = self.list_indexed_results(run=run, limit=1)["run_ids"]
        if not matches:
            raise OperationError("user", f"unknown result run: {run}", "not_found")
        prefix = f"/api/v1/run/{encoded_run}"
        return {
            "run_id": run,
            "tags": self._cdm_request(f"{prefix}/tags").get("tags", []),
            "benchmark": self._cdm_request(f"{prefix}/benchmark").get("benchmark"),
            "partial_status": self._cdm_request(f"{prefix}/partial-status"),
            "iterations": self._cdm_request(f"{prefix}/iterations").get("iterations", []),
            "metric_sources": self._cdm_request(f"{prefix}/metric-sources").get("sources", []),
            "periods": self.list_indexed_periods(run)["periods"],
        }

    def list_indexed_periods(self, run: str) -> dict[str, Any]:
        """List every primary period and sample associated with a run."""

        self._require_text(run, "run")
        encoded_run = quote(run, safe="")
        prefix = f"/api/v1/run/{encoded_run}"
        iterations = self._cdm_request(f"{prefix}/iterations").get("iterations", [])
        if not isinstance(iterations, list) or not all(isinstance(item, str) for item in iterations):
            raise OperationError("framework", "CDM returned invalid iteration data", "invalid_result_response")
        if not iterations:
            return {"run_id": run, "periods": []}

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
        return {"run_id": run, "periods": periods}

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
        return self._cdm_request("/api/v1/metric-data", method="POST", body=body)

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
                    "command": row[3],
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
                for timestamp, line_stream, line in rows:
                    line = line or ""
                    if pattern is not None and pattern.search(line) is None:
                        continue
                    if matched < offset:
                        matched += 1
                        continue
                    if len(lines) >= limit:
                        complete = False
                        break
                    candidate = {"timestamp": timestamp, "stream": line_stream, "line": line}
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
                "command": metadata[2],
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
                                 lines.line, sources.source, commands.command
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
                for row in connection.execute(sql, params):
                    line = row[3] or ""
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
                        "stream": row[2], "line": line,
                        "source": row[4], "command": row[5],
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
                "query": query,
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
        return metadata

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
                    benchmark_errors.append(f"benchmark is not installed: {name!r}")

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

    @staticmethod
    def _format_validation_error(error: Any) -> str:
        path = ".".join(str(item) for item in error.path)
        return f"{path}: {error.message}" if path else error.message
