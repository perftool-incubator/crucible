import json
import tempfile
import unittest
from pathlib import Path

from crucible_mcp.audit import AuditLogger


class TestAuditLogger(unittest.TestCase):
    def test_records_safe_fields_and_rotates(self):
        directory = tempfile.TemporaryDirectory()
        try:
            path = Path(directory.name) / "audit.jsonl"
            logger = AuditLogger(path, max_bytes=140, retained_files=2)
            logger.record(
                operation="tools/call",
                outcome="success",
                source_address="127.0.0.1",
                job_id="job-1",
                token="secret-token",
            )
            logger.record(
                operation="tools/call",
                outcome="success",
                source_address="127.0.0.1",
                job_id="job-2",
                token="secret-token",
            )
            records = []
            for candidate in path.parent.glob("audit.jsonl*"):
                records.extend(json.loads(line) for line in candidate.read_text().splitlines())
            self.assertTrue(any(record["job_id"] == "job-2" for record in records))
            self.assertTrue(all("secret-token" not in json.dumps(record) for record in records))
            self.assertTrue(all(len(record.get("token_id", "")) == 16 for record in records))
            self.assertTrue((path.parent / "audit.jsonl.1").exists())
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
