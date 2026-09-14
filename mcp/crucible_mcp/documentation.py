"""Curated user-facing Crucible documentation exposed through MCP resources."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


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
        self.docs_root = (Path(crucible_home) / "docs").resolve()
        self.max_document_bytes = max_document_bytes
        self._entries = {entry.slug: entry for entry in DOCUMENTATION_ENTRIES}

    def list_resources(self) -> list[dict[str, Any]]:
        resources = []
        for entry in DOCUMENTATION_ENTRIES:
            path = self._path_for(entry)
            if path is None:
                continue
            resource = {
                "uri": entry.uri,
                "name": entry.slug,
                "title": entry.title,
                "description": entry.description,
                "mimeType": "text/markdown",
            }
            try:
                resource["size"] = path.stat().st_size
            except OSError:
                continue
            resources.append(resource)
        return resources

    def read_resource(self, uri: str) -> dict[str, Any]:
        prefix = "crucible://docs/"
        if not isinstance(uri, str) or not uri.startswith(prefix):
            raise ValueError("unknown documentation resource")
        slug = uri[len(prefix):]
        entry = self._entries.get(slug)
        if entry is None:
            raise ValueError("unknown documentation resource")
        path = self._path_for(entry)
        if path is None:
            raise FileNotFoundError(entry.filename)
        try:
            if path.stat().st_size > self.max_document_bytes:
                raise ValueError("documentation resource exceeds size limit")
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FileNotFoundError(entry.filename) from exc
        return {"uri": entry.uri, "mimeType": "text/markdown", "text": text}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query is required")
        terms = tuple(dict.fromkeys(query.casefold().split()))
        matches = []
        for resource in self.list_resources():
            entry = self._entries[resource["name"]]
            path = self._path_for(entry)
            if path is None:
                continue
            try:
                text = path.read_text(encoding="utf-8").casefold()
            except OSError:
                continue
            haystack = f"{entry.title} {entry.description} {text}"
            score = sum(haystack.count(term) for term in terms)
            if score:
                matches.append((score, resource))
        matches.sort(key=lambda item: (-item[0], item[1]["name"]))
        return [resource for _, resource in matches[:limit]]

    def _path_for(self, entry: DocumentationEntry) -> Path | None:
        path = self.docs_root / entry.filename
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != self.docs_root or resolved.suffix != ".md":
            return None
        return resolved
