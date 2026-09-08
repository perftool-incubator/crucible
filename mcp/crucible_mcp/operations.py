"""Typed, non-execution Crucible operations for the MCP contract."""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft201909Validator

from .policy import InputPolicy, PolicyError


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

    def __init__(self, crucible_home: Path, input_policy: InputPolicy | None = None):
        self.crucible_home = Path(crucible_home).resolve()
        self.input_policy = input_policy or InputPolicy(
            [self.crucible_home / "mcp" / "inputs"]
        )

    def crucible_info(self) -> dict[str, Any]:
        return {
            "name": "crucible",
            "mcp_contract_version": "1",
            "execution_supported": True,
            "capabilities": [
                "crucible_info",
                "list_benchmarks",
                "describe_benchmark",
                "validate_run",
                "start_run",
                "get_run_status",
                "get_run_logs",
                "get_run_summary",
            ],
        }

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
