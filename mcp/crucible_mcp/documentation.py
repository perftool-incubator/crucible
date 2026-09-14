"""Curated user-facing Crucible documentation exposed through MCP resources."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
DOCUMENT_CHUNK_BYTES = 512 * 1024
_RESOURCE_URI = re.compile(r"^crucible://docs/([^/]+)(?:/chunk/([1-9][0-9]*))?$")


@dataclass(frozen=True)
class DocumentationEntry:
    slug: str
    filename: str
    title: str
    description: str

    @property
    def uri(self) -> str:
        return f"crucible://docs/{self.slug}"


DOCUMENTATION_ENTRIES = (
    DocumentationEntry(
        "architecture-overview",
        "crucible-architecture-overview.md",
        "Crucible Architecture Overview",
        "How Crucible components fit together and how a run moves through the system.",
    ),
    DocumentationEntry(
        "run-files",
        "how-run-files-work.md",
        "How Run Files Work",
        "The user-facing run-file format and the options used to describe a workload.",
    ),
    DocumentationEntry(
        "benchmark-execution",
        "how-benchmark-execution-works.md",
        "How Benchmark Execution Works",
        "Benchmark validation, orchestration, execution phases, and result generation.",
    ),
    DocumentationEntry(
        "tool-collection",
        "how-tool-collection-works.md",
        "How Tool Collection Works",
        "How system tools collect performance data and how that data reaches CDM.",
    ),
    DocumentationEntry(
        "endpoints",
        "how-endpoints-work.md",
        "How Endpoints Work",
        "The supported endpoint types and how Crucible deploys workloads to them.",
    ),
    DocumentationEntry(
        "engines",
        "how-engines-work.md",
        "How Engines Work",
        "Engine bootstrap, workload execution, data collection, and archival.",
    ),
    DocumentationEntry(
        "cdm",
        "how-cdm-works.md",
        "How CDM Works",
        "The CommonDataModel hierarchy, indexing pipeline, and query model.",
    ),
    DocumentationEntry(
        "services",
        "how-services-work.md",
        "How Services Work",
        "Crucible service roles, configuration, startup, and shutdown behavior.",
    ),
    DocumentationEntry(
        "image-sourcing",
        "how-image-sourcing-works.md",
        "How Image Sourcing Works",
        "How controller and engine images are sourced and built.",
    ),
    DocumentationEntry(
        "roadblock",
        "how-roadblock-works.md",
        "How Roadblock Works",
        "Distributed synchronization used during coordinated benchmark execution.",
    ),
    DocumentationEntry(
        "logger",
        "how-the-logger-works.md",
        "How the Logger Works",
        "Crucible's session and output logging model.",
    ),
    DocumentationEntry(
        "repository-system",
        "how-the-repo-system-works.md",
        "How the Repository System Works",
        "How Crucible discovers, activates, and updates its subprojects.",
    ),
    DocumentationEntry(
        "releases",
        "how-releases-work.md",
        "How Releases Work",
        "Release tracking, version selection, and update behavior.",
    ),
    DocumentationEntry(
        "installer",
        "how-the-installer-works.md",
        "How the Installer Works",
        "Installation flow, prerequisites, and controller setup.",
    ),
)


class DocumentationCatalog:
    """Read only a fixed set of Markdown files below the Crucible docs root."""

    def __init__(self, crucible_home: Path, max_document_bytes: int = MAX_DOCUMENT_BYTES):
        if max_document_bytes < 4:
            raise ValueError("max_document_bytes must be at least 4")
        self.docs_root = (Path(crucible_home) / "docs").resolve()
        self.max_document_bytes = max_document_bytes
        self._entries = {entry.slug: entry for entry in DOCUMENTATION_ENTRIES}

    def list_resources(self) -> list[dict[str, Any]]:
        resources = []
        for entry in DOCUMENTATION_ENTRIES:
            path = self._path_for(entry)
            if path is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            try:
                ranges = self._chunk_ranges(path, size)
            except OSError:
                continue
            if len(ranges) == 1:
                resources.append(self._resource_metadata(entry, size))
                continue
            for index, (start, end) in enumerate(ranges, start=1):
                resources.append(
                    self._resource_metadata(
                        entry,
                        end - start,
                        chunk_index=index,
                        chunk_count=len(ranges),
                    )
                )
        return resources

    def read_resource(self, uri: str) -> dict[str, Any]:
        match = _RESOURCE_URI.fullmatch(uri) if isinstance(uri, str) else None
        if match is None:
            raise ValueError("unknown documentation resource")
        slug = match.group(1)
        chunk_index = int(match.group(2)) if match.group(2) else None
        entry = self._entries.get(slug)
        if entry is None:
            raise ValueError("unknown documentation resource")
        path = self._path_for(entry)
        if path is None:
            raise FileNotFoundError(entry.filename)
        try:
            size = path.stat().st_size
            ranges = self._chunk_ranges(path, size)
            if chunk_index is None:
                if len(ranges) != 1:
                    raise ValueError("documentation resource is chunked; read its chunks")
                start, end = ranges[0]
                resource_uri = entry.uri
            else:
                if chunk_index > len(ranges):
                    raise ValueError("unknown documentation resource chunk")
                start, end = ranges[chunk_index - 1]
                resource_uri = f"{entry.uri}/chunk/{chunk_index}"
            with path.open("rb") as document:
                document.seek(start)
                text = document.read(end - start).decode("utf-8")
        except OSError as exc:
            raise FileNotFoundError(entry.filename) from exc
        except UnicodeDecodeError as exc:
            raise ValueError("documentation resource is not valid UTF-8") from exc
        return {"uri": resource_uri, "mimeType": "text/markdown", "text": text}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query is required")
        terms = tuple(dict.fromkeys(query.casefold().split()))
        matches = []
        previous_document = None
        overlap = ""
        for resource in self.list_resources():
            document_slug = resource.get("document", resource["name"])
            entry = self._entries[document_slug]
            if document_slug != previous_document:
                overlap = ""
                previous_document = document_slug
            try:
                text = self.read_resource(resource["uri"])["text"].casefold()
            except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
                continue
            haystack = (
                f"{entry.title} {entry.description} {overlap}{text}"
            ).casefold()
            score = sum(haystack.count(term) for term in terms)
            if score:
                matches.append((score, resource))
            overlap_length = max(len(term) for term in terms) - 1
            overlap = text[-overlap_length:] if overlap_length else ""
        matches.sort(key=lambda item: (-item[0], item[1]["name"]))
        return [resource for _, resource in matches[:limit]]

    def _resource_metadata(
        self,
        entry: DocumentationEntry,
        size: int,
        *,
        chunk_index: int | None = None,
        chunk_count: int | None = None,
    ) -> dict[str, Any]:
        if chunk_index is None:
            return {
                "uri": entry.uri,
                "name": entry.slug,
                "title": entry.title,
                "description": entry.description,
                "mimeType": "text/markdown",
                "size": size,
            }
        return {
            "uri": f"{entry.uri}/chunk/{chunk_index}",
            "name": f"{entry.slug}-chunk-{chunk_index}",
            "title": f"{entry.title} (part {chunk_index} of {chunk_count})",
            "description": entry.description,
            "mimeType": "text/markdown",
            "size": size,
            "document": entry.slug,
            "chunk": chunk_index,
            "chunks": chunk_count,
        }

    def _chunk_ranges(self, path: Path, size: int) -> list[tuple[int, int]]:
        if size <= self.max_document_bytes:
            return [(0, size)]
        chunk_size = min(DOCUMENT_CHUNK_BYTES, self.max_document_bytes)
        ranges = []
        with path.open("rb") as document:
            start = 0
            while start < size:
                end = min(start + chunk_size, size)
                if end < size:
                    while end > start:
                        document.seek(end)
                        byte = document.read(1)
                        if not byte or byte[0] & 0xC0 != 0x80:
                            break
                        end -= 1
                ranges.append((start, end))
                start = end
        return ranges

    def _path_for(self, entry: DocumentationEntry) -> Path | None:
        path = self.docs_root / entry.filename
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != self.docs_root or resolved.suffix != ".md":
            return None
        return resolved
