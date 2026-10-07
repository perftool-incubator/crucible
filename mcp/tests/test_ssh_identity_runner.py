import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crucible_mcp.jobs import JobConflictError, JobStore, request_hash
from crucible_mcp.runner import RunManager


class _Operations:
    def validate_run(self, _document):
        return {"valid": True}


class _Profiles:
    def __init__(self):
        self.version = 1

    def snapshot(self, document):
        if not document.get("endpoints"):
            return {}
        return {
            "management": {
                "version": self.version,
                "fingerprint": "SHA256:" + "A" * 43,
                "revoked": False,
            }
        }

    def validate_available(self, _pins):
        return None


class TestSSHIdentityRunPins(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = JobStore(self.root / "jobs.sqlite")
        self.profiles = _Profiles()
        self.manager = RunManager(
            self.store,
            _Operations(),
            self.root / "runs",
            ["crucible"],
            host_execution=False,
            ssh_identity_profiles=self.profiles,
        )

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_submission_persists_profile_version_and_retries_idempotently(self):
        document = {
            "benchmarks": [],
            "endpoints": [{"type": "kube", "ssh-identity-profile": "management"}],
        }
        with patch.object(self.manager, "_launch"):
            job, created = self.manager.submit("profile-key", document=document)
            retry, retry_created = self.manager.submit("profile-key", document=document)

        lock_path = self.root / "runs" / job.mcp_job_id / "ssh-identities.lock.json"
        self.assertTrue(created)
        self.assertFalse(retry_created)
        self.assertEqual(job.mcp_job_id, retry.mcp_job_id)
        self.assertEqual(
            json.loads(lock_path.read_text(encoding="utf-8")),
            {
                "profiles": {
                    "management": {
                        "version": 1,
                        "fingerprint": "SHA256:" + "A" * 43,
                        "revoked": False,
                    }
                }
            },
        )

    def test_profile_version_change_changes_idempotency_signature(self):
        document = {
            "benchmarks": [],
            "endpoints": [{"type": "kube", "ssh-identity-profile": "management"}],
        }
        with patch.object(self.manager, "_launch"):
            first, _created = self.manager.submit("profile-version-key", document=document)
            self.profiles.version = 2
            with self.assertRaises(JobConflictError):
                self.manager.submit("profile-version-key", document=document)

    def test_profileless_request_hash_keeps_legacy_shape(self):
        document = {"benchmarks": [], "endpoints": []}
        with patch.object(self.manager, "_launch"):
            job, _created = self.manager.submit("legacy-key", document=document)
        self.assertEqual(job.request_hash, request_hash({"run_document": document}))


if __name__ == "__main__":
    unittest.main()
