import os
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from crucible_mcp.policy import (
    InputPolicy,
    PolicyError,
    read_token,
    rotate_token,
    token_matches,
    validate_token_rotation_path,
)


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_input_policy_rejects_symlink_escape(self):
        allowed = self.root / "inputs"
        allowed.mkdir()
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        link = allowed / "run.json"
        link.symlink_to(outside)

        # Resolution is allowed only if the resolved target remains under root.
        with self.assertRaises(PolicyError):
            InputPolicy([allowed]).canonical_input(link)

    def test_input_policy_rejects_world_writable_file(self):
        allowed = self.root / "inputs"
        allowed.mkdir()
        run_file = allowed / "run.json"
        run_file.write_text("{}", encoding="utf-8")
        run_file.chmod(0o602)
        with self.assertRaises(PolicyError):
            InputPolicy([allowed]).canonical_input(run_file)

    def test_input_policy_rejects_group_writable_file(self):
        allowed = self.root / "inputs"
        allowed.mkdir()
        run_file = allowed / "run.json"
        run_file.write_text("{}", encoding="utf-8")
        run_file.chmod(0o620)
        with self.assertRaises(PolicyError):
            InputPolicy([allowed]).canonical_input(run_file)

    def test_token_rotation_is_atomic_and_private(self):
        token_path = self.root / "mcp-server.token"
        first = rotate_token(token_path)
        second = rotate_token(token_path)
        self.assertNotEqual(first, second)
        self.assertEqual(read_token(token_path), second)
        self.assertTrue(token_matches(second, second))
        self.assertFalse(token_matches(first, second))
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

    def test_token_rotation_rejects_symlinked_parent(self):
        real_parent = self.root / "real"
        real_parent.mkdir()
        linked_parent = self.root / "linked"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(PolicyError):
            validate_token_rotation_path(linked_parent / "mcp-server.token")

    def test_token_rotation_rejects_writable_parent(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)
        with patch.object(Path, "lstat", return_value=metadata):
            with self.assertRaises(PolicyError):
                validate_token_rotation_path(Path("/safe/mcp-server.token"))

    def test_token_rotation_does_not_require_optional_host_packages(self):
        token_path = self.root / "host-mcp-server.token"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                "import sys; from pathlib import Path; from crucible_mcp.policy import rotate_token; rotate_token(Path(sys.argv[1]))",
                str(token_path),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(token_path.is_file())


if __name__ == "__main__":
    unittest.main()
