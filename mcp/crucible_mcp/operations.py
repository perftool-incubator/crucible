"""Typed, non-execution Crucible operations for the MCP contract."""

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

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

    def __init__(
        self,
        crucible_home: Path,
        input_policy: InputPolicy | None = None,
        cdm_base_url: str = "http://127.0.0.1:3000",
        log_db: Path | None = None,
    ):
        self.crucible_home = Path(crucible_home).resolve()
        self.cdm_base_url = cdm_base_url.rstrip("/")
        self.log_db = Path(log_db) if log_db else None
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
                "list_tools",
                "list_results",
                "get_result",
                "list_run_periods",
                "get_metric",
                "list_log_sessions",
                "get_log_info",
                "list_containers",
                "list_images",
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

    def list_results(
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

    def get_result(self, run: str) -> dict[str, Any]:
        """Return structured metadata for one historical CDM run."""

        self._require_text(run, "run")
        encoded_run = quote(run, safe="")
        matches = self.list_results(run=run, limit=1)["run_ids"]
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
            "periods": self.list_run_periods(run)["periods"],
        }

    def list_run_periods(self, run: str) -> dict[str, Any]:
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

    def get_metric(
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
            "period": period,
            "begin": begin,
            "end": end,
            "source": source,
            "type": metric_type,
            "resolution": resolution,
            "breakout": breakout or [],
            "filter": filter,
            "aggregation": aggregation,
            "distribution-stats": distribution_stats,
            "allow-incompatible-aggregation": allow_incompatible_aggregation,
        }
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

    def list_containers(self) -> dict[str, Any]:
        return {"containers": self._podman_json(["ps", "--filter", "name=crucible"])}

    def list_images(self) -> dict[str, Any]:
        images = self._podman_json(["images"])
        return {"images": [image for image in images if "crucible" in json.dumps(image).lower()]}

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

    @staticmethod
    def _podman_json(arguments: list[str]) -> list[dict[str, Any]]:
        try:
            completed = subprocess.run(
                ["podman", *arguments, "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            payload = json.loads(completed.stdout or "[]")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise OperationError("framework", "container runtime is unavailable", "runtime_unavailable") from exc
        if not isinstance(payload, list):
            raise OperationError("framework", "container runtime returned an invalid response", "invalid_runtime_response")
        return [item for item in payload if isinstance(item, dict)]

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
