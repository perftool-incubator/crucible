import os
import stat
import tempfile
import unittest
from pathlib import Path

from crucible_mcp.policy import InputPolicy, PolicyError, read_token, rotate_token, token_matches


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

    def test_token_rotation_is_atomic_and_private(self):
        token_path = self.root / "mcp-server.token"
        first = rotate_token(token_path)
        second = rotate_token(token_path)
        self.assertNotEqual(first, second)
        self.assertEqual(read_token(token_path), second)
        self.assertTrue(token_matches(second, second))
        self.assertFalse(token_matches(first, second))
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
