"""Bounded, credential-safe audit logging for MCP requests."""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, path: Path, max_bytes: int = 10 * 1024 * 1024, retained_files: int = 5):
        if max_bytes <= 0 or retained_files < 1:
            raise ValueError("invalid audit log bounds")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.retained_files = retained_files
        self._lock = threading.Lock()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def record(
        self,
        *,
        operation: str,
        outcome: str,
        source_address: str,
        error_category: str | None = None,
        job_id: str | None = None,
        token: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "operation": operation,
            "outcome": outcome,
            "source_address": source_address,
        }
        if error_category:
            record["error_category"] = error_category
        if job_id:
            record["job_id"] = job_id
        if token:
            record["token_id"] = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(encoded))
            with self.path.open("ab") as audit:
                audit.write(encoded)
                audit.flush()
                os.fsync(audit.fileno())

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if not self.path.exists() or self.path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        for index in range(self.retained_files, 1, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            destination = self.path.with_name(f"{self.path.name}.{index}")
            previous = self.path.with_name(f"{self.path.name}.{index - 1}")
            if source.exists():
                source.unlink()
            if previous.exists():
                previous.replace(destination)
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
