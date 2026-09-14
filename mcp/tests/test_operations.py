import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_list_tools_returns_installed_tool_metadata(self):
        tool_repository = self.root / "repos" / "git@github.com:perftool-incubator/tool-sysstat.git"
        tool_repository.mkdir(parents=True)
        (tool_repository / "rickshaw.json").write_text('{"tool":"sysstat"}', encoding="utf-8")
        (tool_repository / "tool-metadata.json").write_text(
            '{"description":"System statistics"}', encoding="utf-8"
        )
        tool = self.root / "subprojects" / "tools" / "sysstat"
        tool.parent.mkdir(parents=True)
        tool.symlink_to(tool_repository, target_is_directory=True)

        self.assertEqual(
            self.operations.list_tools(),
            [{
                "name": "sysstat",
                "description": "System statistics",
                "metadata": {"description": "System statistics"},
            }],
        )
        self.assertEqual(self.operations.list_tools("missing"), [])

    def test_list_indexed_results_queries_cdm_with_bounded_filters(self):
        response = Mock()
        response.__enter__ = lambda value: response
        response.__exit__ = lambda *args: None
        response.read.return_value = b'{"runIds":["run-1","run-2"]}'
        with patch("crucible_mcp.operations.urlopen", return_value=response) as request:
            result = self.operations.list_indexed_results(benchmark="fio", limit=1)

        self.assertEqual(result, {"run_ids": ["run-1"], "count": 1})
        self.assertIn("benchmark=fio", request.call_args.args[0].full_url)

    def test_get_indexed_result_assembles_cdm_metadata(self):
        payloads = {
            "/api/v1/run/run-1/tags": {"tags": ["nightly"]},
            "/api/v1/run/run-1/benchmark": {"benchmark": "fio"},
            "/api/v1/run/run-1/partial-status": {"status": "complete"},
            "/api/v1/run/run-1/iterations": {"iterations": [1, 2]},
            "/api/v1/run/run-1/metric-sources": {"sources": ["latency"]},
        }
        with patch.object(self.operations, "list_indexed_results", return_value={"run_ids": ["run-1"]}), \
                patch.object(self.operations, "list_indexed_periods", return_value={"periods": []}), \
                patch.object(self.operations, "_cdm_request", side_effect=payloads.get) as request:
            result = self.operations.get_indexed_result("run-1")

        self.assertEqual(result["tags"], ["nightly"])
        self.assertEqual(result["benchmark"], "fio")
        self.assertEqual(result["partial_status"], {"status": "complete"})
        self.assertEqual(result["periods"], [])
        self.assertEqual(request.call_count, 5)

    def test_run_tag_operations_update_local_metadata(self):
        run_directory = self.root / "run" / "result"
        metadata_directory = run_directory / "run"
        metadata_directory.mkdir(parents=True)
        metadata_path = metadata_directory / "rickshaw-run.json"
        metadata_path.write_text(json.dumps({"tags": [{"name": "old", "val": "1"}]}), encoding="utf-8")

        self.assertEqual(self.operations.list_local_run_tags(run_directory)["tags"][0]["name"], "old")
        added = self.operations.add_local_run_tags(run_directory, ["old:2", "new:value"])
        self.assertEqual({tag["name"]: tag["val"] for tag in added["tags"]}, {"old": "2", "new": "value"})
        removed = self.operations.remove_local_run_tags(run_directory, ["old"])
        self.assertEqual(removed["tags"], [{"name": "new", "val": "value"}])

    def test_list_local_runs_reports_artifacts_without_latest_alias(self):
        run_root = self.root / "run"
        complete = run_root / "fio--2026-09-14--run-1"
        incomplete = run_root / "partial-run"
        (complete / "run").mkdir(parents=True)
        (incomplete / "run").mkdir(parents=True)
        (complete / "run" / "rickshaw-run.json").write_text(
            json.dumps({"run-id": "run-1", "tags": [{"name": "nightly", "val": "yes"}]}),
            encoding="utf-8",
        )
        (run_root / "latest").symlink_to(complete, target_is_directory=True)
        result = self.operations.list_local_runs()

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["runs"][0]["run_id"], "run-1")
        self.assertEqual(result["runs"][0]["status"], "complete")
        self.assertEqual(result["runs"][1]["status"], "incomplete")

    def test_get_local_run_summary_reads_local_artifact(self):
        run_directory = self.root / "run" / "completed-run"
        summary_path = run_directory / "run" / "result-summary.json"
        summary_path.parent.mkdir(parents=True)
        summary_path.write_text(json.dumps({"benchmark": "fio", "samples": 1}), encoding="utf-8")

        result = self.operations.get_local_run_summary(run_directory)

        self.assertEqual(result["run_path"], str(run_directory.resolve()))
        self.assertEqual(result["summary"], {"benchmark": "fio", "samples": 1})

    def test_list_indexed_periods_preserves_multiple_primary_periods(self):
        payloads = {
            "/api/v1/run/run-1/iterations": {"iterations": ["iteration-1"]},
            "/api/v1/run/run-1/iterations/samples": {"samples": [["sample-1", "sample-2"]]},
            "/api/v1/run/run-1/samples/statuses": {"statuses": [["pass", "pass"]]},
            "/api/v1/run/run-1/iterations/primary-period-name": {"periodNames": ["measurement"]},
            "/api/v1/run/run-1/samples/primary-period-id": {
                "periodIds": [["period-1", "period-2"]]
            },
            "/api/v1/run/run-1/periods/range": {
                "ranges": [[{"begin": 10, "end": 20}, {"begin": 30, "end": 40}]]
            },
        }

        def response(path, **_kwargs):
            return payloads[path]

        with patch.object(self.operations, "_cdm_request", side_effect=response):
            result = self.operations.list_indexed_periods("run-1")

        self.assertEqual([period["primary_period_id"] for period in result["periods"]], ["period-1", "period-2"])
        self.assertEqual(result["periods"][1]["begin"], 30)

    def test_get_indexed_metric_posts_typed_query(self):
        response = Mock()
        response.__enter__ = lambda value: response
        response.__exit__ = lambda *args: None
        response.read.return_value = b'{"values":[]}'
        with patch("crucible_mcp.operations.urlopen", return_value=response) as request:
            result = self.operations.get_indexed_metric(
                run="run-1", source="fio", metric_type="IOPS", period="measurement"
            )

        self.assertEqual(result, {"values": []})
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(sent["run"], "run-1")
        self.assertEqual(sent["source"], "fio")
        self.assertEqual(sent["type"], "IOPS")
        self.assertEqual(sent["period"], "measurement")
        self.assertNotIn("distribution-stats", sent)
        self.assertNotIn("filter", sent)

    def test_get_indexed_metric_requires_period_or_range(self):
        with self.assertRaises(OperationError) as context:
            self.operations.get_indexed_metric(run="run-1", source="fio", metric_type="IOPS")
        self.assertEqual(context.exception.code, "invalid_metric_range")

    def test_log_queries_are_read_only(self):
        database = self.root / "logs.db"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE sources (id INTEGER PRIMARY KEY, source TEXT);
            CREATE TABLE commands (id INTEGER PRIMARY KEY, command TEXT);
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY, session_id TEXT, timestamp TEXT,
                source INTEGER, command INTEGER
            );
            CREATE TABLE streams (id INTEGER PRIMARY KEY, stream TEXT);
            CREATE TABLE lines (id INTEGER PRIMARY KEY, session INTEGER, stream INTEGER, timestamp TEXT, line TEXT);
            INSERT INTO streams VALUES (1, 'STDOUT');
            INSERT INTO sources VALUES (1, 'runner');
            INSERT INTO commands VALUES (1, 'crucible run example.json');
            INSERT INTO sessions VALUES (1, 'session-1', '2026-09-09T00:00:00Z', 1, 1);
            INSERT INTO lines VALUES (1, 1, 1, '2026-09-09T00:00:01Z', 'done');
            INSERT INTO lines VALUES (2, 1, 1, '2026-09-09T00:00:02Z', 'complete');
            """
        )
        connection.commit()
        connection.close()

        operations = CrucibleOperations(self.root, log_db=database)
        self.assertEqual(operations.list_log_sessions()["sessions"][0]["line_count"], 2)
        self.assertEqual(operations.get_log_info()["sessions"], 1)
        self.assertEqual(operations.get_log_info()["lines"], 2)
        session = operations.get_log_session("session-1", limit=1)
        self.assertEqual(session["lines"][0]["line"], "done")
        self.assertFalse(session["complete"])
        search = operations.search_logs("complete")
        self.assertEqual(search["matches"][0]["session_id"], "session-1")

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
