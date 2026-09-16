import errno
import json
import lzma
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from crucible_mcp.operations import (
    MAX_ARTIFACT_READ_BYTES,
    MAX_ARTIFACT_RESPONSE_BYTES,
    CrucibleOperations,
    OperationError,
)
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
        self.assertEqual(list(metadata_directory.glob("*.mcp-backup-*")), [])
        removed = self.operations.remove_local_run_tags(run_directory, ["old"])
        self.assertEqual(removed["tags"], [{"name": "new", "val": "value"}])

    def test_run_tag_operations_prefer_live_plain_metadata(self):
        run_directory = self.root / "run" / "dual-metadata"
        run_metadata = run_directory / "run"
        run_metadata.mkdir(parents=True)
        plain_path = run_metadata / "rickshaw-run.json"
        compressed_path = run_metadata / "rickshaw-run.json.xz"
        plain_path.write_text(json.dumps({"tags": [{"name": "live", "val": "yes"}]}), encoding="utf-8")
        with lzma.open(compressed_path, "wt", encoding="utf-8") as stream:
            json.dump({"tags": [{"name": "stale", "val": "yes"}]}, stream)

        result = self.operations.add_local_run_tags(run_directory, ["updated:value"])

        self.assertIn({"name": "updated", "val": "value"}, result["tags"])
        self.assertIn("updated", plain_path.read_text(encoding="utf-8"))
        with lzma.open(compressed_path, "rt", encoding="utf-8") as stream:
            self.assertNotIn("updated", stream.read())

    def test_tag_operations_reject_malformed_existing_tags(self):
        run_directory = self.root / "run" / "malformed-tags"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        for malformed in (
            None,
            ["not-an-object"],
            [{}],
            [{"name": "name"}],
            [{"name": 1, "val": "value"}],
            [{"name": "name", "val": "value", "extra": "field"}],
            [{"name": "", "val": "value"}],
        ):
            metadata_path.write_text(json.dumps({"tags": malformed}), encoding="utf-8")
            with self.subTest(malformed=malformed):
                for operation in (
                    lambda: self.operations.list_local_run_tags(run_directory),
                    lambda: self.operations.add_local_run_tags(run_directory, ["new:value"]),
                    lambda: self.operations.remove_local_run_tags(run_directory, ["old"]),
                ):
                    with self.assertRaises(OperationError) as raised:
                        operation()
                    self.assertEqual(raised.exception.code, "invalid_run")

    def test_concurrent_run_tag_updates_preserve_both_changes(self):
        run_directory = self.root / "run" / "concurrent"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(json.dumps({"tags": []}), encoding="utf-8")
        original_write = self.operations._write_run_metadata

        def delayed_write(path, document):
            time.sleep(0.05)
            original_write(path, document)

        with patch.object(self.operations, "_write_run_metadata", side_effect=delayed_write):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(self.operations.add_local_run_tags, run_directory, ["one:1"]),
                    executor.submit(self.operations.add_local_run_tags, run_directory, ["two:2"]),
                ]
                [future.result() for future in futures]

        tags = self.operations.list_local_run_tags(run_directory)["tags"]
        self.assertEqual({tag["name"] for tag in tags}, {"one", "two"})

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

    def test_list_local_runs_limit_includes_incomplete_artifacts(self):
        run_root = self.root / "run"
        for name in ("partial-one", "partial-two", "partial-three"):
            (run_root / name).mkdir(parents=True)

        result = self.operations.list_local_runs(limit=1)

        self.assertEqual(result["count"], 1)

    def test_list_local_runs_keeps_config_only_metadata_incomplete(self):
        run_directory = self.root / "run" / "config-only"
        metadata_path = run_directory / "config" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(json.dumps({"run-id": "run-3"}), encoding="utf-8")

        result = self.operations.list_local_runs()

        self.assertEqual(result["runs"][0]["status"], "incomplete")

    def test_compressed_metadata_is_bounded_after_decompression(self):
        run_directory = self.root / "run" / "large-metadata"
        metadata_path = run_directory / "run" / "rickshaw-run.json.xz"
        metadata_path.parent.mkdir(parents=True)
        with lzma.open(metadata_path, "wt", encoding="utf-8") as stream:
            json.dump({"tags": [], "padding": "x" * 1_048_576}, stream)

        result = self.operations.list_local_runs()
        self.assertEqual(result["runs"][0]["status"], "incomplete")
        with self.assertRaises(OperationError) as raised:
            self.operations.list_local_run_tags(run_directory)
        self.assertEqual(raised.exception.code, "result_too_large")

    def test_list_local_runs_excludes_mcp_supervision_root(self):
        supervision_root = self.root / "mcp-runs"
        (supervision_root / "job-1").mkdir(parents=True)
        self.operations.run_policy = InputPolicy([self.root / "run", supervision_root])

        result = self.operations.list_local_runs()

        self.assertEqual(result["count"], 0)

    def test_get_local_run_summary_reads_local_artifact(self):
        run_directory = self.root / "run" / "completed-run"
        summary_path = run_directory / "run" / "result-summary.json"
        summary_path.parent.mkdir(parents=True)
        summary_path.write_text(json.dumps({"benchmark": "fio", "samples": 1}), encoding="utf-8")

        result = self.operations.get_local_run_summary(run_directory)

        self.assertEqual(result["run_path"], str(run_directory.resolve()))
        self.assertEqual(result["summary"], {"benchmark": "fio", "samples": 1})

    def test_get_local_run_metadata_reads_local_artifact(self):
        run_directory = self.root / "run" / "metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(json.dumps({"run-id": "run-2", "benchmarks": []}), encoding="utf-8")

        result = self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(result["metadata_path"], str(metadata_path))
        self.assertEqual(result["metadata"], {"run-id": "run-2", "benchmarks": []})

    def test_list_run_artifacts_returns_bounded_metadata_for_approved_paths(self):
        run_directory = self.root / "run" / "artifact-run"
        result_summary = run_directory / "run" / "result-summary.json"
        text_artifact = (
            run_directory
            / "run"
            / "iterations"
            / "iteration-1"
            / "sample-1"
            / "client"
            / "1"
            / "benchmark-result.json"
        )
        binary_artifact = run_directory / "run" / "tool-data" / "capture.bin"
        secret = run_directory / "config" / "secret.json"
        result_summary.parent.mkdir(parents=True)
        text_artifact.parent.mkdir(parents=True)
        binary_artifact.parent.mkdir(parents=True)
        secret.parent.mkdir(parents=True)
        result_summary.write_text("{}", encoding="utf-8")
        text_artifact.write_text('{"value": 1}', encoding="utf-8")
        binary_artifact.write_bytes(b"\x00\x01")
        secret.write_text('{"password": "do-not-list"}', encoding="utf-8")

        result = self.operations.list_run_artifacts(run_directory)

        self.assertEqual(result["count"], 3)
        paths = {item["artifact_path"] for item in result["artifacts"]}
        self.assertEqual(
            paths,
            {
                "run/result-summary.json",
                "run/iterations/iteration-1/sample-1/client/1/benchmark-result.json",
                "run/tool-data/capture.bin",
            },
        )
        retrievable = {
            item["artifact_path"]: item["retrievable"] for item in result["artifacts"]
        }
        self.assertTrue(retrievable["run/result-summary.json"])
        self.assertTrue(
            retrievable[
                "run/iterations/iteration-1/sample-1/client/1/benchmark-result.json"
            ]
        )
        self.assertFalse(retrievable["run/tool-data/capture.bin"])
        self.assertEqual(
            next(
                item
                for item in result["artifacts"]
                if item["artifact_path"] == "run/tool-data/capture.bin"
            )["media_type"],
            "application/octet-stream",
        )

    def test_list_run_artifacts_bounds_scan_work_independent_of_offset(self):
        run_directory = self.root / "run" / "scan-cap"
        iterations = run_directory / "run" / "iterations"
        iterations.mkdir(parents=True)
        for name in ("a.txt", "b.txt", "c.txt"):
            (iterations / name).write_text(name, encoding="utf-8")

        with patch("crucible_mcp.operations.MAX_ARTIFACT_SCAN_FILES", 2):
            with self.assertRaises(OperationError) as high_offset:
                self.operations.list_run_artifacts(run_directory, offset=2)
            self.assertEqual(high_offset.exception.code, "result_too_large")

            with self.assertRaises(OperationError) as too_many_files:
                self.operations.list_run_artifacts(run_directory, limit=10)
            self.assertEqual(too_many_files.exception.code, "result_too_large")

    def test_list_run_artifacts_bounds_empty_directory_traversal(self):
        run_directory = self.root / "run" / "empty-directory-cap"
        (run_directory / "run" / "iterations" / "empty" / "deep").mkdir(
            parents=True
        )

        with patch("crucible_mcp.operations.MAX_ARTIFACT_SCAN_FILES", 3):
            with self.assertRaises(OperationError) as raised:
                self.operations.list_run_artifacts(run_directory)
        self.assertEqual(raised.exception.code, "result_too_large")

    def test_list_run_artifacts_bounds_open_directory_depth(self):
        run_directory = self.root / "run" / "directory-depth-cap"
        (run_directory / "run" / "iterations" / "deep").mkdir(parents=True)

        with patch("crucible_mcp.operations.MAX_ARTIFACT_DIRECTORY_DEPTH", 2):
            with self.assertRaises(OperationError) as raised:
                self.operations.list_run_artifacts(run_directory)
        self.assertEqual(raised.exception.code, "result_too_large")

    def test_list_run_artifacts_reports_traversal_errors(self):
        run_directory = self.root / "run" / "traversal-error"
        run_directory.mkdir(parents=True)

        with patch(
            "crucible_mcp.operations.os.scandir",
            side_effect=PermissionError("unreadable"),
        ):
            result = self.operations.list_run_artifacts(run_directory)

        self.assertFalse(result["complete"])

    def test_list_run_artifacts_handles_entry_inspection_errors(self):
        run_directory = self.root / "run" / "entry-error"
        (run_directory / "run" / "iterations").mkdir(parents=True)

        class FailingEntry:
            name = "broken.txt"

            def is_symlink(self):
                return False

            def is_dir(self, follow_symlinks=False):
                return False

            def is_file(self, follow_symlinks=False):
                raise OSError("entry disappeared")

        class SingleEntryIterator:
            def __init__(self, entry):
                self.entry = entry
                self.consumed = False

            def __next__(self):
                if self.consumed:
                    raise StopIteration
                self.consumed = True
                return self.entry

            def close(self):
                return None

        real_scandir = os.scandir
        calls = 0

        def failing_scandir(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                return SingleEntryIterator(FailingEntry())
            return real_scandir(path)

        with patch("crucible_mcp.operations.os.scandir", side_effect=failing_scandir):
            result = self.operations.list_run_artifacts(run_directory)

        self.assertFalse(result["complete"])

    def test_list_run_artifacts_does_not_follow_raced_directory_symlinks(self):
        run_directory = self.root / "run" / "directory-symlink-race"
        (run_directory / "run").mkdir(parents=True)
        real_open = os.open

        def raced_open(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and path == "run":
                raise OSError(errno.ELOOP, "directory replaced by symlink")
            return real_open(path, flags, *args, **kwargs)

        with patch("crucible_mcp.operations.os.open", side_effect=raced_open):
            result = self.operations.list_run_artifacts(run_directory)

        self.assertFalse(result["complete"])
        self.assertGreater(result["next_offset"], 0)

    def test_get_run_artifact_returns_utf8_byte_slices(self):
        run_directory = self.root / "run" / "artifact-slice"
        artifact = run_directory / "run" / "iterations" / "sample.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("αβγ\n", encoding="utf-8")

        first = self.operations.get_run_artifact(
            run_directory, "run/iterations/sample.txt", limit=4
        )
        second = self.operations.get_run_artifact(
            run_directory,
            "run/iterations/sample.txt",
            offset=first["next_offset"],
            limit=4,
        )

        self.assertEqual(first["text"], "αβ")
        self.assertEqual(first["next_offset"], 4)
        self.assertFalse(first["complete"])
        self.assertEqual(second["text"], "γ\n")
        self.assertTrue(second["complete"])

    def test_get_run_artifact_rejects_unapproved_and_binary_paths(self):
        run_directory = self.root / "run" / "artifact-policy"
        secret = run_directory / "config" / "secret.json"
        binary = run_directory / "run" / "iterations" / "capture.bin"
        credentials = run_directory / "run" / "tool-data" / "credentials.json"
        token = run_directory / "run" / "tool-data" / "token.txt"
        engine_env = run_directory / "run" / "tool-data" / "engine-env.txt"
        nested_credential = (
            run_directory / "run" / "tool-data" / "credentials" / "private.txt"
        )
        secret.parent.mkdir(parents=True)
        binary.parent.mkdir(parents=True)
        credentials.parent.mkdir(parents=True)
        nested_credential.parent.mkdir(parents=True)
        secret.write_text("secret", encoding="utf-8")
        binary.write_bytes(b"\x00\x01")
        credentials.write_text('{"password": "secret"}', encoding="utf-8")
        token.write_text("secret-token", encoding="utf-8")
        engine_env.write_text("AWS_SECRET_ACCESS_KEY=secret", encoding="utf-8")
        nested_credential.write_text("private secret", encoding="utf-8")

        with self.assertRaises(OperationError) as rejected:
            self.operations.get_run_artifact(run_directory, "config/secret.json")
        self.assertEqual(rejected.exception.code, "path_rejected")

        with self.assertRaises(OperationError) as binary_error:
            self.operations.get_run_artifact(
                run_directory, "run/iterations/capture.bin"
            )
        self.assertEqual(binary_error.exception.code, "artifact_not_text")

        for sensitive in (
            "run/tool-data/credentials.json",
            "run/tool-data/token.txt",
            "run/tool-data/engine-env.txt",
            "run/tool-data/credentials/private.txt",
        ):
            with self.subTest(sensitive=sensitive):
                with self.assertRaises(OperationError) as sensitive_error:
                    self.operations.get_run_artifact(run_directory, sensitive)
                self.assertEqual(
                    sensitive_error.exception.code, "artifact_not_retrievable"
                )

        listed = self.operations.list_run_artifacts(run_directory)
        listed_by_path = {
            item["artifact_path"]: item for item in listed["artifacts"]
        }
        self.assertFalse(listed_by_path["run/tool-data/credentials.json"]["retrievable"])
        self.assertFalse(listed_by_path["run/tool-data/token.txt"]["retrievable"])
        self.assertFalse(listed_by_path["run/tool-data/engine-env.txt"]["retrievable"])
        self.assertFalse(
            listed_by_path["run/tool-data/credentials/private.txt"]["retrievable"]
        )

    def test_get_run_artifact_reports_missing_paths(self):
        run_directory = self.root / "run" / "missing-artifact"
        iterations = run_directory / "run" / "iterations"
        iterations.mkdir(parents=True)

        with self.assertRaises(OperationError) as missing:
            self.operations.get_run_artifact(
                run_directory, "run/iterations/nope.txt"
            )
        self.assertEqual(missing.exception.code, "artifact_not_found")

        (iterations / "directory.txt").mkdir()
        with self.assertRaises(OperationError) as directory:
            self.operations.get_run_artifact(
                run_directory, "run/iterations/directory.txt"
            )
        self.assertEqual(directory.exception.code, "artifact_not_found")

    def test_get_run_artifact_rejects_nul_paths(self):
        run_directory = self.root / "run" / "nul-artifact-path"
        (run_directory / "run" / "iterations").mkdir(parents=True)

        with self.assertRaises(OperationError) as raised:
            self.operations.get_run_artifact(
                run_directory, "run/iterations/bad\x00name.txt"
            )
        self.assertEqual(raised.exception.code, "invalid_artifact_path")

    def test_get_run_artifact_maps_open_failures(self):
        run_directory = self.root / "run" / "unreadable-artifact"
        artifact = run_directory / "run" / "iterations" / "result.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("result", encoding="utf-8")

        with patch.object(
            self.operations,
            "_open_artifact_readonly",
            side_effect=PermissionError("permission denied"),
        ):
            with self.assertRaises(OperationError) as raised:
                self.operations.get_run_artifact(
                    run_directory, "run/iterations/result.txt"
                )
        self.assertEqual(raised.exception.code, "artifact_unavailable")

    def test_get_run_artifact_maps_invalid_path_resolution(self):
        run_directory = self.root / "run" / "invalid-artifact-path"
        artifact = run_directory / "run" / "iterations" / "result.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("result", encoding="utf-8")

        with self.assertRaises(OperationError) as raised:
            self.operations.get_run_artifact(
                run_directory,
                f"run/iterations/{'a' * 256}.txt",
            )
        self.assertEqual(raised.exception.code, "invalid_artifact_path")

    def test_get_run_artifact_rejects_symlink_during_descriptor_open(self):
        run_directory = self.root / "run" / "artifact-symlink"
        artifact_directory = run_directory / "run" / "iterations"
        artifact_directory.mkdir(parents=True)
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (artifact_directory / "link.txt").symlink_to(outside)

        with self.assertRaises(OperationError) as rejected:
            CrucibleOperations._open_artifact_readonly(
                run_directory.resolve(), "run/iterations/link.txt"
            )
        self.assertEqual(rejected.exception.code, "path_rejected")

    def test_get_run_artifact_rejects_fifo_without_blocking(self):
        run_directory = self.root / "run" / "artifact-fifo"
        artifact_directory = run_directory / "run" / "iterations"
        artifact_directory.mkdir(parents=True)
        fifo = artifact_directory / "stream.txt"
        try:
            os.mkfifo(fifo)
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("FIFO creation is unavailable")

        with self.assertRaises(OperationError) as rejected:
            CrucibleOperations._open_artifact_readonly(
                run_directory.resolve(), "run/iterations/stream.txt"
            )
        self.assertEqual(rejected.exception.code, "artifact_not_found")

    def test_get_run_artifact_reports_small_utf8_limits(self):
        run_directory = self.root / "run" / "small-utf8"
        artifact = run_directory / "run" / "iterations" / "unicode.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("€", encoding="utf-8")

        with self.assertRaises(OperationError) as too_small:
            self.operations.get_run_artifact(
                run_directory, "run/iterations/unicode.txt", limit=1
            )
        self.assertEqual(too_small.exception.code, "result_too_large")

        with self.assertRaises(OperationError) as invalid_offset:
            self.operations.get_run_artifact(
                run_directory,
                "run/iterations/unicode.txt",
                offset=1,
                limit=2,
            )
        self.assertEqual(invalid_offset.exception.code, "invalid_offset")

    def test_get_run_artifact_reports_truncated_utf8_at_eof(self):
        run_directory = self.root / "run" / "truncated-utf8"
        artifact = run_directory / "run" / "iterations" / "truncated.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"\xe2")

        with self.assertRaises(OperationError) as raised:
            self.operations.get_run_artifact(
                run_directory,
                "run/iterations/truncated.txt",
                limit=131072,
            )
        self.assertEqual(raised.exception.code, "invalid_artifact")

    def test_get_run_artifact_bounds_serialized_control_bytes(self):
        run_directory = self.root / "run" / "large-control-artifact"
        artifact = run_directory / "run" / "iterations" / "control.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"\x00" * MAX_ARTIFACT_READ_BYTES)

        result = self.operations.get_run_artifact(
            run_directory,
            "run/iterations/control.txt",
            request_id="large-control-request",
        )

        self.assertFalse(result["complete"])
        self.assertLess(result["next_offset"], result["size"])
        self.assertLessEqual(
            self.operations._mcp_response_size(result, "large-control-request"),
            MAX_ARTIFACT_RESPONSE_BYTES,
        )

    def test_local_artifact_invalid_utf8_is_a_structured_artifact_error(self):
        summary_run = self.root / "run" / "invalid-summary"
        summary_path = summary_run / "run" / "result-summary.json"
        summary_path.parent.mkdir(parents=True)
        summary_path.write_bytes(b"{\xff}")
        with self.assertRaises(OperationError) as summary_error:
            self.operations.get_local_run_summary(summary_run)
        self.assertEqual(summary_error.exception.code, "invalid_result")

        metadata_run = self.root / "run" / "invalid-metadata"
        metadata_path = metadata_run / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_bytes(b"{\xff}")
        with self.assertRaises(OperationError) as metadata_error:
            self.operations.get_local_run_metadata(metadata_run)
        self.assertEqual(metadata_error.exception.code, "invalid_run")

    def test_local_run_path_rejections_are_structured_operation_errors(self):
        outside = self.root / "outside"
        outside.mkdir()
        for operation in (
            lambda: self.operations.list_local_run_tags(outside),
            lambda: self.operations.get_local_run_summary(outside),
            lambda: self.operations.get_local_run_metadata(outside),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(OperationError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, "run_path_rejected")

    def test_local_artifact_symlinks_cannot_escape_run_directory(self):
        run_directory = self.root / "run" / "symlink-run"
        (run_directory / "run").mkdir(parents=True)
        outside = self.root / "outside.json"
        outside.write_text('{"secret": true}', encoding="utf-8")
        (run_directory / "run" / "result-summary.json").symlink_to(outside)
        (run_directory / "run" / "rickshaw-run.json").symlink_to(outside)

        for operation in (
            lambda: self.operations.get_local_run_summary(run_directory),
            lambda: self.operations.get_local_run_metadata(run_directory),
            lambda: self.operations.list_local_run_tags(run_directory),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(OperationError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, "path_rejected")

    def test_list_local_archives_is_bounded_and_local_only(self):
        archive_root = self.root / "archive"
        archive_root.mkdir()
        (archive_root / "run-1.tar.xz").write_bytes(b"archive")
        (archive_root / "not-an-archive.txt").write_text("ignore", encoding="utf-8")

        result = self.operations.list_local_archives()

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["archives"][0]["name"], "run-1.tar.xz")

    def test_symlinked_run_root_uses_configured_archive_sibling(self):
        physical_run_root = self.root / "mounted-runs"
        physical_run_root.mkdir()
        (self.root / "run").symlink_to(physical_run_root, target_is_directory=True)
        archive_root = self.root / "archive"
        archive_root.mkdir()
        (archive_root / "run-1.tar.xz").write_bytes(b"archive")

        operations = CrucibleOperations(self.root, run_root=self.root / "run")

        result = operations.list_local_archives()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["archives"][0]["name"], "run-1.tar.xz")

    def test_archive_policy_rejects_approved_run_root(self):
        (self.root / "run").mkdir()
        with self.assertRaisesRegex(ValueError, "must be below"):
            self.operations.run_policy.canonical_child_directory(self.root / "run")

    def test_archive_policy_rejects_nested_run_directories(self):
        nested = self.root / "run" / "run-one" / "run"
        nested.mkdir(parents=True)
        with self.assertRaises(OperationError) as raised:
            self.operations.canonical_archive_run(nested)
        self.assertEqual(raised.exception.code, "run_path_rejected")

    def test_archive_paths_reject_shell_unsafe_basenames(self):
        unsafe_run = self.root / "run" / "run;touch-pwned"
        unsafe_run.mkdir(parents=True)
        with self.assertRaises(OperationError) as run_error:
            self.operations.canonical_archive_run(unsafe_run)
        self.assertEqual(run_error.exception.code, "invalid_path")

        archive_root = self.root / "archive"
        archive_root.mkdir()
        unsafe_archive = archive_root / "archive$(touch-pwned).tar.xz"
        unsafe_archive.write_bytes(b"archive")
        with self.assertRaises(OperationError) as archive_error:
            self.operations.canonical_local_archive(unsafe_archive)
        self.assertEqual(archive_error.exception.code, "invalid_path")

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

    def test_log_queries_reject_backtracking_repetition(self):
        with self.assertRaises(OperationError) as search_error:
            self.operations.search_logs("(a+)+$")
        self.assertEqual(search_error.exception.code, "invalid_query")

        with self.assertRaises(OperationError) as session_error:
            self.operations.get_log_session("session-1", grep="(a+)+$")
        self.assertEqual(session_error.exception.code, "invalid_grep")

        pathological = "(a|aa)" * 25 + "b"
        with self.assertRaises(OperationError) as alternation_error:
            self.operations.search_logs(pathological)
        self.assertEqual(alternation_error.exception.code, "invalid_query")

    def test_log_responses_are_byte_bounded_and_resumable(self):
        database = self.root / "large-logs.db"
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
            INSERT INTO sessions VALUES (1, 'large-session', '2026-09-09T00:00:00Z', 1, 1);
            """
        )
        large_line = "x" * 300_000
        connection.execute("INSERT INTO lines VALUES (?, ?, ?, ?, ?)", (1, 1, 1, "t1", large_line))
        connection.execute("INSERT INTO lines VALUES (?, ?, ?, ?, ?)", (2, 1, 1, "t2", large_line))
        connection.commit()
        connection.close()

        operations = CrucibleOperations(self.root, log_db=database)
        session = operations.get_log_session("large-session", grep="x")
        self.assertEqual(len(session["lines"]), 1)
        self.assertEqual(session["next_offset"], 1)
        self.assertFalse(session["complete"])
        self.assertLessEqual(self.operations._mcp_response_size(session), 1_048_576)
        search = operations.search_logs("x", session_id="large-session")
        self.assertEqual(len(search["matches"]), 1)
        self.assertEqual(search["next_offset"], 1)
        self.assertFalse(search["complete"])
        self.assertLessEqual(self.operations._mcp_response_size(search), 1_048_576)
        with self.assertRaises(OperationError) as request_id_error:
            operations.search_logs(
                "x", session_id="large-session", request_id="request-id" * 100_000
            )
        self.assertEqual(request_id_error.exception.code, "result_too_large")

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
