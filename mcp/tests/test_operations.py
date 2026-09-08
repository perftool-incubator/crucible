import json
import tempfile
import unittest
from pathlib import Path

from crucible_mcp.operations import CrucibleOperations, OperationError
from crucible_mcp.policy import InputPolicy


class TestCrucibleOperations(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        benchmark = self.root / "subprojects" / "benchmarks" / "example"
        benchmark.mkdir(parents=True)
        (benchmark / "rickshaw.json").write_text(
            json.dumps({"benchmark": "example"}), encoding="utf-8"
        )
        (benchmark / "benchmark-metadata.json").write_text(
            json.dumps({"description": "Example benchmark"}), encoding="utf-8"
        )
        schema = self.root / "subprojects" / "core" / "rickshaw" / "schema"
        schema.mkdir(parents=True)
        schema_document = {
            "type": "object",
            "properties": {"benchmarks": {"type": "array"}},
            "required": ["benchmarks"],
        }
        (schema / "run-file.json").write_text(json.dumps(schema_document), encoding="utf-8")
        self.operations = CrucibleOperations(self.root, InputPolicy([self.root / "inputs"]))

    def tearDown(self):
        self.directory.cleanup()

    def test_list_and_describe_benchmark(self):
        benchmarks = self.operations.list_benchmarks()
        self.assertEqual(benchmarks[0]["name"], "example")
        self.assertEqual(self.operations.describe_benchmark("example")["description"], "Example benchmark")

    def test_validate_run_reports_schema_and_installed_benchmark_errors(self):
        result = self.operations.validate_run({"benchmarks": [{"name": "missing"}]})
        self.assertFalse(result["valid"])
        self.assertIn("benchmark is not installed", result["errors"][0])

    def test_benchmark_name_cannot_escape_subproject_root(self):
        with self.assertRaises(OperationError):
            self.operations.describe_benchmark("../example")

    def test_validate_run_rejects_absolute_and_symlink_escape_names(self):
        outside = self.root / "outside"
        outside.mkdir()
        escaped = self.root / "subprojects" / "benchmarks" / "escaped"
        escaped.symlink_to(outside, target_is_directory=True)

        result = self.operations.validate_run({"benchmarks": [{"name": "/tmp/evil"}]})
        self.assertFalse(result["valid"])
        self.assertIn("benchmark is not installed", result["errors"][-1])

    def test_validate_run_returns_schema_error_for_null_benchmarks(self):
        result = self.operations.validate_run({"benchmarks": None})
        self.assertFalse(result["valid"])
        self.assertTrue(result["errors"])

        result = self.operations.validate_run({"benchmarks": [{"name": "escaped"}]})
        self.assertFalse(result["valid"])
        self.assertIn("benchmark is not installed", result["errors"][-1])


if __name__ == "__main__":
    unittest.main()
