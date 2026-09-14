"""Typed, non-execution Crucible operations for the MCP contract."""

import json
import lzma
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from jsonschema import Draft201909Validator

from .documentation import DocumentationCatalog
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
        run_root: Path | None = None,
    ):
        self.crucible_home = Path(crucible_home).resolve()
        self.cdm_base_url = cdm_base_url.rstrip("/")
        self.log_db = Path(log_db) if log_db else None
        self.documentation = DocumentationCatalog(self.crucible_home)
        self.local_run_root = (Path(run_root) if run_root else self.crucible_home / "run").resolve()
        self.archive_root = self.local_run_root.parent / "archive"
        self.input_policy = input_policy or InputPolicy(
            [self.crucible_home / "mcp" / "inputs"]
        )
        self.run_policy = InputPolicy([run_root or self.crucible_home / "run"])

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
                "list_local_runs",
                "get_local_run_summary",
                "get_local_run_metadata",
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
        return {"run_path": str(run_directory), "tags": document.get("tags", [])}

    def add_local_run_tags(self, run_directory: Path, tags: list[str]) -> dict[str, Any]:
        path, document = self._load_run_metadata(run_directory)
        current = document.setdefault("tags", [])
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
        path, document = self._load_run_metadata(run_directory)
        if any(not re.fullmatch(r"[a-zA-Z0-9-_\s]+", name) for name in names):
            raise OperationError("user", "tag names must not include values", "invalid_tag")
        existing = document.get("tags", [])
        document["tags"] = [tag for tag in existing if tag.get("name") not in names]
        if len(document["tags"]) == len(existing):
            raise OperationError("user", "no matching tags were found", "tag_not_found")
        self._write_run_metadata(path, document)
        return {"run_path": str(run_directory), "tags": document["tags"]}

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
            tags = metadata.get("tags", [])
            entry["tags"] = tags if isinstance(tags, list) else []
            entries.append(entry)
            if len(entries) >= limit:
                break
        return {"runs": entries, "count": len(entries)}

    def get_local_run_summary(self, run_path: Path, max_bytes: int = 1_048_576) -> dict[str, Any]:
        """Read a bounded summary from an approved local run artifact."""

        try:
            canonical = self._canonical_run_directory(run_path)
            summary_path = self._safe_artifact_path(canonical, "run/result-summary.json")
            size = summary_path.stat().st_size
            if size > max_bytes:
                raise OperationError(
                    "framework", "result summary exceeds size limit", "result_too_large"
                )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except OperationError:
            raise
        except FileNotFoundError as exc:
            raise OperationError(
                "user", "local run summary is unavailable", "result_unavailable"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationError(
                "user", "local run summary is not valid JSON", "invalid_result"
            ) from exc
        if not isinstance(summary, dict):
            raise OperationError("user", "local run summary must be a JSON object", "invalid_result")
        return {
            "run_path": str(canonical),
            "result_status": "available",
            "summary": summary,
        }

    def get_local_run_metadata(self, run_path: Path, max_bytes: int = 1_048_576) -> dict[str, Any]:
        """Read the bounded rickshaw run metadata from an approved local run."""

        try:
            canonical = self._canonical_run_directory(run_path)
            metadata_path = self._run_metadata_path(canonical)
            if metadata_path.stat().st_size > max_bytes:
                raise OperationError(
                    "framework", "run metadata exceeds size limit", "result_too_large"
                )
            if metadata_path.suffix == ".xz":
                with lzma.open(metadata_path, "rt", encoding="utf-8") as stream:
                    encoded = stream.read(max_bytes + 1)
                    if len(encoded.encode("utf-8")) > max_bytes:
                        raise OperationError(
                            "framework", "run metadata exceeds size limit", "result_too_large"
                        )
                    metadata = json.loads(encoded)
            else:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except OperationError:
            raise
        except (OSError, lzma.LZMAError, json.JSONDecodeError) as exc:
            raise OperationError(
                "user", "run metadata is not valid JSON", "invalid_run"
            ) from exc
        if not isinstance(metadata, dict):
            raise OperationError("user", "run metadata must be a JSON object", "invalid_run")
        return {
            "run_path": str(canonical),
            "metadata_path": str(metadata_path),
            "metadata": metadata,
        }

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
        return resolved

    @staticmethod
    def _run_metadata_path(run_directory: Path) -> Path:
        canonical = run_directory.resolve(strict=True)
        for relative in ("run/rickshaw-run.json.xz", "run/rickshaw-run.json",
                         "config/rickshaw-run.json.xz", "config/rickshaw-run.json"):
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
        path = run_directory / relative
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
            path = self._run_metadata_path(canonical)
            opener = lzma.open if path.suffix == ".xz" else open
            with opener(path, "rt", encoding="utf-8") as stream:
                document = json.load(stream)
        except OperationError:
            raise
        except (OSError, lzma.LZMAError, json.JSONDecodeError) as exc:
            raise OperationError("user", "run metadata is not valid JSON", "invalid_run") from exc
        if not isinstance(document, dict):
            raise OperationError("user", "run metadata must be a JSON object", "invalid_run")
        return path, document

    def _canonical_run_directory(self, run_directory: Path) -> Path:
        try:
            return self.run_policy.canonical_directory(run_directory)
        except PolicyError as exc:
            raise OperationError("authorization", str(exc), "run_path_rejected") from exc

    @staticmethod
    def _write_run_metadata(path: Path, document: dict[str, Any]) -> None:
        backup = path.with_name(f"{path.name}.mcp-backup-{time.time_ns()}")
        temporary = None
        try:
            shutil.copy2(path, backup)
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
    ) -> dict[str, Any]:
        """Return a bounded, structured slice of one logger session."""

        self._require_text(session_id, "session_id")
        if offset < 0 or limit < 1 or limit > 10000:
            raise OperationError("user", "offset must be nonnegative and limit must be 1..10000", "invalid_bounds")
        if stream is not None and stream not in {"stdout", "stderr"}:
            raise OperationError("user", "stream must be stdout or stderr", "invalid_stream")
        pattern = None
        if grep is not None:
            if len(grep) > 256:
                raise OperationError("user", "grep pattern is too long", "invalid_grep")
            try:
                pattern = re.compile(grep)
            except re.error as exc:
                raise OperationError("user", "grep pattern is invalid", "invalid_grep") from exc
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
                    lines.append({"timestamp": timestamp, "stream": line_stream, "line": line})
                    matched += 1
        except OperationError:
            raise
        except sqlite3.Error as exc:
            raise OperationError("framework", "log database is unavailable", "log_unavailable") from exc
        return {
            "session_id": session_id,
            "timestamp": metadata[0],
            "source": metadata[1],
            "command": metadata[2],
            "offset": offset,
            "next_offset": offset + len(lines),
            "complete": complete,
            "lines": lines,
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
    ) -> dict[str, Any]:
        """Search logger lines with bounded, structured results."""

        if not isinstance(query, str) or not query or len(query) > 256:
            raise OperationError("user", "query must be 1..256 characters", "invalid_query")
        try:
            pattern = re.compile(query)
        except re.error as exc:
            raise OperationError("user", "query is not a valid regular expression", "invalid_query") from exc
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
                    matches.append({
                        "session_id": row[0], "timestamp": row[1],
                        "stream": row[2], "line": line,
                        "source": row[4], "command": row[5],
                    })
                    matched += 1
        except sqlite3.Error as exc:
            raise OperationError("framework", "log database is unavailable", "log_unavailable") from exc
        return {
            "query": query, "offset": offset,
            "next_offset": offset + len(matches),
            "complete": complete, "matches": matches,
        }

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
