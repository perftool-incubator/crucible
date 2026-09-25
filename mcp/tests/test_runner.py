import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from crucible_mcp.jobs import JobStore
from crucible_mcp.host import host_context_command
from crucible_mcp.models import JobState
from crucible_mcp.operations import (
    MAX_METADATA_RESPONSE_BYTES,
    CrucibleOperations,
    OperationError,
)
from crucible_mcp.runner import (
    MAX_LOG_REDACTION_CONTEXT_BYTES,
    MAX_LOG_REDACTION_LINES,
    RunManager,
)


class TestRunManager(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        benchmark = self.root / "subprojects" / "benchmarks" / "example"
        benchmark.mkdir(parents=True)
        (benchmark / "rickshaw.json").write_text('{"benchmark":"example"}', encoding="utf-8")
        schema = self.root / "subprojects" / "core" / "rickshaw" / "schema"
        schema.mkdir(parents=True)
        (schema / "run-file.json").write_text(
            '{"type":"object","required":["benchmarks"]}', encoding="utf-8"
        )
        self.store = JobStore(self.root / "jobs.db")
        event_script = (
            "import os; "
            "open(os.environ['CRUCIBLE_MCP_EVENT_FILE'], 'a').write("
            "'{\\\"state\\\":\\\"postprocessing\\\"}\\n')"
        )
        self.manager = RunManager(
            self.store,
            CrucibleOperations(self.root),
            self.root / "runs",
            [sys.executable, "-c", event_script],
            host_execution=False,
        )

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_runner_wraps_cli_commands_in_host_execution_context(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "host-execution-runs",
            ["/opt/crucible/bin/crucible"],
            host_execution=True,
        )

        command = ["/opt/crucible/bin/crucible", "run", "/var/lib/crucible/run-file.json"]
        self.assertEqual(
            manager._execution_context_command(command, str(self.root)),
            host_context_command(command, str(self.root)),
        )

    def test_runner_launch_preserves_job_correlation_in_host_context(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "host-launch-runs",
            ["/opt/crucible/bin/crucible"],
            host_execution=True,
        )
        job, _ = self.store.create_or_get("key-host-launch", {"run": True})
        job_directory = self.root / "host-launch-job"
        job_directory.mkdir()
        command = [
            "/opt/crucible/bin/crucible",
            "run",
            "/var/lib/crucible/mcp/runs/job/input/run-file.json",
        ]

        with (
            patch("crucible_mcp.runner.subprocess.Popen", return_value=Mock(pid=8765)) as popen,
            patch.object(manager, "_wait_for_completion"),
        ):
            manager._launch_command(
                job.mcp_job_id,
                "mcp-session-1",
                job_directory,
                ["run", command[-1]],
            )
            manager._threads[job.mcp_job_id].join(timeout=5)

        launched_command = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(
            launched_command,
            host_context_command(command, str(self.manager.operations.crucible_home)),
        )
        self.assertEqual(environment["CRUCIBLE_MCP_SESSION_ID"], "mcp-session-1")
        self.assertEqual(
            environment["CRUCIBLE_MCP_EVENT_FILE"],
            str(job_directory / "events.jsonl"),
        )
        self.assertNotIn("CONTAINER_HOST", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("SESSION_ID", environment)
        popen.call_args.kwargs["stdout"].close()

    def test_submission_is_idempotent_and_completes_asynchronously(self):
        document = {"benchmarks": [{"name": "example"}]}
        job, created = self.manager.submit("key-1", document=document)
        self.assertTrue(created)
        self.assertTrue(job.runner_pid)
        duplicate, duplicate_created = self.manager.submit("key-1", document=document)
        self.assertFalse(duplicate_created)
        self.assertEqual(job.mcp_job_id, duplicate.mcp_job_id)
        thread = self.manager._threads[job.mcp_job_id]
        thread.join(timeout=5)
        self.assertEqual(self.store.get(job.mcp_job_id).state.value, "completed")
        summary_path = self.root / "runs" / job.mcp_job_id / "run" / "result-summary.json"
        summary_path.parent.mkdir()
        summary_path.write_text(
            '{"run":"complete","credentials":{"password":"job-summary-secret"}}',
            encoding="utf-8",
        )
        refreshed = self.manager.refresh_result_status(job.mcp_job_id)
        self.assertEqual(refreshed.result_status.value, "available")
        summary = self.manager.get_summary(job.mcp_job_id)["summary"]
        self.assertEqual(summary["run"], "complete")
        self.assertEqual(summary["credentials"], "[redacted]")
        self.assertNotIn("job-summary-secret", json.dumps(summary))
        logs = self.manager.get_logs(job.mcp_job_id)
        self.assertTrue(logs["complete"])
        self.assertEqual(logs["text"], "")
        self.assertTrue((self.root / "runs" / job.mcp_job_id / "input" / "run-file.json").is_file())

    def test_log_redaction_uses_context_across_requested_byte_ranges(self):
        job, _ = self.store.create_or_get("log-context", {"operation": "run"})
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = b"--token=s3cr3t-value\nsafe output\n"
        log_path.write_bytes(raw_log)

        credential_length = raw_log.index(b"\n")
        for split in range(1, credential_length):
            first = self.manager.get_logs(job.mcp_job_id, offset=0, limit=split)
            second = self.manager.get_logs(
                job.mcp_job_id, offset=split, limit=credential_length - split
            )
            with self.subTest(split=split):
                self.assertNotIn("s3cr3t-value", first["text"])
                self.assertNotIn("s3cr3t-value", second["text"])
                self.assertEqual(first["next_offset"], split)
                self.assertEqual(second["next_offset"], credential_length)

        middle_of_secret = raw_log.index(b"s3cr3t-value") + 4
        page = self.manager.get_logs(
            job.mcp_job_id, offset=middle_of_secret, limit=5
        )
        self.assertNotIn("s3cr3t-value", page["text"])
        self.assertEqual(page["next_offset"], middle_of_secret + 5)

    def test_log_redaction_preserves_safe_lines_and_reports_redaction(self):
        job, _ = self.store.create_or_get("log-mixed-redaction", {"operation": "run"})
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = (
            b"worker initialized before the run\n"
            b"crucible run --to\\\n"
            b"ken=continued-secret\n"
            b'crucible run --to"\n'
            b'ken=assembled-secret"\n'
            b'crucible run --token="\n'
            b"quoted-shell-secret\n"
            b'closing quote"\n'
            b"TOKEN=$(cat <<EOF\n"
            b"heredoc-shell-secret\n"
            b"EOF\n"
            + b"crucible run --token=s3cr3t-value\n"
            + b'{\n  "password":\n  "json-secret"\n}\n'
            + b'{\n  "authorization":\n  {\n    "token": "nested-secret"\n  }\n}\n'
            + b"password: |-\n  yaml-block-secret\n  another-yaml-secret\nsafe yaml sibling: visible\n"
            + b"-----BEGIN PRIVATE KEY-----\n"
            + b"A" * 64
            + b"\n-----END PRIVATE KEY-----\n"
            + b"worker completed teardown successfully\n"
        )
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )

        self.assertIn("worker initialized before the run", page["text"])
        self.assertIn("worker completed teardown successfully", page["text"])
        self.assertIn("safe yaml sibling: visible", page["text"])
        self.assertNotIn("s3cr3t-value", page["text"])
        self.assertNotIn("continued-secret", page["text"])
        self.assertNotIn("assembled-secret", page["text"])
        self.assertNotIn("quoted-shell-secret", page["text"])
        self.assertNotIn("heredoc-shell-secret", page["text"])
        self.assertNotIn("json-secret", page["text"])
        self.assertNotIn("nested-secret", page["text"])
        self.assertNotIn("yaml-block-secret", page["text"])
        self.assertNotIn("another-yaml-secret", page["text"])
        self.assertNotIn("A" * 64, page["text"])
        self.assertTrue(page["redacted"])
        self.assertGreaterEqual(page["redacted_lines"], 3)
        self.assertEqual(page["next_offset"], len(raw_log))

    def test_log_redaction_preserves_safe_shell_continuations(self):
        job, _ = self.store.create_or_get(
            "log-redaction-safe-shell-continuation", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = b"ordinary diagnostic continues \\\nwith useful context\nfinished normally\n"
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )

        self.assertEqual(page["text"], raw_log.decode("utf-8"))
        self.assertFalse(page["redacted"])

    def test_log_redaction_fails_closed_for_quoted_sensitive_option_fragments(self):
        job, _ = self.store.create_or_get(
            "log-redaction-quoted-sensitive-option-fragment", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = (
            b'command "--pass\n'
            b'word=assembled-secret"\n'
            b"safe diagnostic after command\n"
        )
        log_path.write_bytes(raw_log)

        full_page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )
        continuation_offset = raw_log.index(b"word=assembled-secret")
        continuation_page = self.manager.get_logs(
            job.mcp_job_id,
            offset=continuation_offset,
            limit=len(b"word=assembled-secret"),
        )
        direct_redaction, _ = self.manager.operations.redact_log_text_with_stats(
            raw_log.decode("utf-8")
        )

        for result in (
            full_page["text"],
            continuation_page["text"],
            direct_redaction,
        ):
            self.assertNotIn("assembled-secret", result)
        self.assertIn("safe diagnostic after command", full_page["text"])

    def test_log_redaction_masks_credentials_assembled_across_shell_splices(self):
        job, _ = self.store.create_or_get(
            "log-redaction-shell-spliced-credentials", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = (
            b"command --t\\\noken=spliced-secret\n"
            b"https://alice:\\\npassword@example.com\n"
            b"safe diagnostics remain visible\n"
        )
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )
        token_fragment = b"oken=spliced-secret\n"
        token_page = self.manager.get_logs(
            job.mcp_job_id,
            offset=raw_log.index(token_fragment),
            limit=len(token_fragment),
        )
        url_fragment = b"password@example.com\n"
        url_page = self.manager.get_logs(
            job.mcp_job_id,
            offset=raw_log.index(url_fragment),
            limit=len(url_fragment),
        )

        self.assertNotIn("spliced-secret", page["text"])
        self.assertNotIn("alice:password", page["text"])
        self.assertNotIn("spliced-secret", token_page["text"])
        self.assertNotIn("password@example.com", url_page["text"])
        self.assertIn("safe diagnostics remain visible", page["text"])

    def test_log_redaction_preserves_sibling_after_sensitive_yaml_mapping(self):
        job, _ = self.store.create_or_get(
            "log-redaction-yaml-mapping-sibling", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = b"password:\n  nested: secret-value\nusername: alice\n"
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )

        self.assertNotIn("secret-value", page["text"])
        self.assertNotIn("nested: secret-value", page["text"])
        self.assertIn("username: alice", page["text"])

    def test_log_redaction_masks_all_items_in_sensitive_yaml_sequence(self):
        job, _ = self.store.create_or_get(
            "log-redaction-yaml-sensitive-sequence", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = (
            b"password:\n- first-secret\n- second-secret\nusername: alice\n"
        )
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )
        second_item = b"- second-secret\n"
        second_item_page = self.manager.get_logs(
            job.mcp_job_id,
            offset=raw_log.index(second_item),
            limit=len(second_item),
        )

        self.assertNotIn("first-secret", page["text"])
        self.assertNotIn("second-secret", page["text"])
        self.assertNotIn("second-secret", second_item_page["text"])
        self.assertIn("username: alice", page["text"])

    def test_log_redaction_masks_wrapped_sensitive_yaml_scalar(self):
        job, _ = self.store.create_or_get(
            "log-redaction-yaml-wrapped-scalar", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = (
            b"password: first-secret\n  continuation-secret\nusername: alice\n"
        )
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )
        continuation = b"  continuation-secret\n"
        continuation_page = self.manager.get_logs(
            job.mcp_job_id,
            offset=raw_log.index(continuation),
            limit=len(continuation),
        )

        self.assertNotIn("first-secret", page["text"])
        self.assertNotIn("continuation-secret", page["text"])
        self.assertNotIn("continuation-secret", continuation_page["text"])
        self.assertIn("username: alice", page["text"])

    def test_log_redaction_carries_split_json_member_keys_across_lines(self):
        job, _ = self.store.create_or_get(
            "log-redaction-split-json-key", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = (
            b'{\n  "password"\n  : "split-key-secret",\n'
            b'  "token"\n  :\n    "split-value-secret",\n'
            b'  "username": "alice"\n}\n'
        )
        json.loads(raw_log)
        log_path.write_bytes(raw_log)

        full_page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )
        value_offset = raw_log.index(b"split-value-secret")
        value_page = self.manager.get_logs(
            job.mcp_job_id,
            offset=value_offset,
            limit=len(b"split-value-secret"),
        )
        direct_redaction, _ = self.manager.operations.redact_log_text_with_stats(
            raw_log.decode("utf-8")
        )

        for result in (full_page["text"], value_page["text"], direct_redaction):
            with self.subTest(result=result):
                self.assertNotIn("split-key-secret", result)
                self.assertNotIn("split-value-secret", result)
        self.assertIn('"username": "alice"', full_page["text"])

    def test_log_redaction_carries_json_member_from_mixed_content_line(self):
        job, _ = self.store.create_or_get(
            "log-redaction-mixed-json-key-line", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = (
            b'{"token":\n'
            b'  "json-secret",\n'
            b'  "username": "alice"\n'
            b'}\n'
        )
        json.loads(raw_log)
        log_path.write_bytes(raw_log)

        full_page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )
        value_offset = raw_log.index(b"json-secret")
        value_page = self.manager.get_logs(
            job.mcp_job_id,
            offset=value_offset,
            limit=len(b"json-secret"),
        )
        direct_redaction, _ = self.manager.operations.redact_log_text_with_stats(
            raw_log.decode("utf-8")
        )

        for result in (full_page["text"], value_page["text"], direct_redaction):
            self.assertNotIn("json-secret", result)
        self.assertIn('"username": "alice"', full_page["text"])

    def test_log_redaction_carries_inline_json_container_state(self):
        job, _ = self.store.create_or_get(
            "log-redaction-inline-json-container", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        object_log = (
            b'{"token": {\n'
            b'  "description": "inline-object-secret",\n'
            b'  "nested": {"password": "inline-nested-secret"}\n'
            b'},\n"username": "alice"\n}\n'
        )
        array_log = (
            b'{"password": [\n'
            b'  "inline-array-secret",\n'
            b'  "inline-second-array-secret"\n'
            b'],\n"username": "alice"\n}\n'
        )
        json.loads(object_log)
        json.loads(array_log)

        for raw_log, secrets in (
            (
                object_log,
                ("inline-object-secret", "inline-nested-secret"),
            ),
            (
                array_log,
                ("inline-array-secret", "inline-second-array-secret"),
            ),
        ):
            with self.subTest(secrets=secrets):
                log_path.write_bytes(raw_log)
                full_page = self.manager.get_logs(
                    job.mcp_job_id, offset=0, limit=len(raw_log)
                )
                direct_redaction, _ = (
                    self.manager.operations.redact_log_text_with_stats(
                        raw_log.decode("utf-8")
                    )
                )
                paged_results = [
                    self.manager.get_logs(
                        job.mcp_job_id,
                        offset=raw_log.index(secret.encode("utf-8")),
                        limit=len(secret.encode("utf-8")),
                    )["text"]
                    for secret in secrets
                ]
                for result in (full_page["text"], direct_redaction, *paged_results):
                    for secret in secrets:
                        self.assertNotIn(secret, result)
                self.assertIn('"username": "alice"', full_page["text"])

    def test_log_redaction_carries_sensitive_option_values_to_next_line(self):
        job, _ = self.store.create_or_get(
            "log-redaction-option-value-on-next-line", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = b"command --token\ns3cr3t-value\nsafe diagnostic\n"
        log_path.write_bytes(raw_log)

        full_page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(raw_log)
        )
        value_offset = raw_log.index(b"s3cr3t-value")
        value_page = self.manager.get_logs(
            job.mcp_job_id,
            offset=value_offset,
            limit=len(b"s3cr3t-value"),
        )
        direct_redaction, _ = self.manager.operations.redact_log_text_with_stats(
            raw_log.decode("utf-8")
        )

        for result in (full_page["text"], value_page["text"], direct_redaction):
            self.assertNotIn("s3cr3t-value", result)
        self.assertIn("safe diagnostic", full_page["text"])

    def test_log_redaction_carries_structured_option_values_across_lines(self):
        job, _ = self.store.create_or_get(
            "log-redaction-structured-option-value", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        object_log = (
            b"command --token\n{\n"
            b'  "description": "object-secret",\n'
            b'  "nested": {"password": "nested-secret"}\n'
            b"}\nsafe object diagnostic\n"
        )
        array_log = (
            b"command --password\n[\n"
            b'  "array-secret",\n'
            b'  "second-array-secret"\n'
            b"]\nsafe array diagnostic\n"
        )

        for raw_log, secrets, safe_diagnostic in (
            (
                object_log,
                ("object-secret", "nested-secret"),
                "safe object diagnostic",
            ),
            (
                array_log,
                ("array-secret", "second-array-secret"),
                "safe array diagnostic",
            ),
        ):
            with self.subTest(safe_diagnostic=safe_diagnostic):
                log_path.write_bytes(raw_log)
                full_page = self.manager.get_logs(
                    job.mcp_job_id, offset=0, limit=len(raw_log)
                )
                direct_redaction, _ = (
                    self.manager.operations.redact_log_text_with_stats(
                        raw_log.decode("utf-8")
                    )
                )
                paged_results = [
                    self.manager.get_logs(
                        job.mcp_job_id,
                        offset=raw_log.index(secret.encode("utf-8")),
                        limit=len(secret.encode("utf-8")),
                    )["text"]
                    for secret in secrets
                ]
                for result in (full_page["text"], direct_redaction, *paged_results):
                    for secret in secrets:
                        self.assertNotIn(secret, result)
                self.assertIn(safe_diagnostic, full_page["text"])

    def test_log_redaction_carries_shell_and_yaml_state_across_pages(self):
        job, _ = self.store.create_or_get(
            "log-redaction-page-state", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        shell_header = b"crucible run --to\\\n"
        shell_body = b"ken=paged-shell-secret\nsafe shell line\n"
        quote_header = b'crucible run --to"\n'
        quote_body = (
            b'ken=paged-quoted-secret"\nsafe quoted line\n'
            b"operator couldn't connect\n"
        )
        heredoc_header = b"TOKEN=$(cat <<EOF\n"
        heredoc_body = b"paged-heredoc-secret\nEOF\nsafe heredoc line\n"
        yaml_header = b"password: >2+\n"
        yaml_body = b"    paged-yaml-secret\nsafe yaml line\n"
        raw_log = (
            shell_header
            + shell_body
            + quote_header
            + quote_body
            + heredoc_header
            + heredoc_body
            + yaml_header
            + yaml_body
        )
        log_path.write_bytes(raw_log)

        shell_first = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(shell_header)
        )
        shell_second = self.manager.get_logs(
            job.mcp_job_id,
            offset=len(shell_header),
            limit=len(shell_body),
        )
        quote_start = len(shell_header) + len(shell_body)
        quote_first = self.manager.get_logs(
            job.mcp_job_id, offset=quote_start, limit=len(quote_header)
        )
        quote_second = self.manager.get_logs(
            job.mcp_job_id,
            offset=quote_start + len(quote_header),
            limit=len(quote_body),
        )
        heredoc_start = quote_start + len(quote_header) + len(quote_body)
        heredoc_first = self.manager.get_logs(
            job.mcp_job_id, offset=heredoc_start, limit=len(heredoc_header)
        )
        heredoc_second = self.manager.get_logs(
            job.mcp_job_id,
            offset=heredoc_start + len(heredoc_header),
            limit=len(heredoc_body),
        )
        yaml_start = heredoc_start + len(heredoc_header) + len(heredoc_body)
        yaml_first = self.manager.get_logs(
            job.mcp_job_id, offset=yaml_start, limit=len(yaml_header)
        )
        yaml_second = self.manager.get_logs(
            job.mcp_job_id,
            offset=yaml_start + len(yaml_header),
            limit=len(yaml_body),
        )

        self.assertNotIn("paged-shell-secret", shell_first["text"])
        self.assertNotIn("paged-shell-secret", shell_second["text"])
        self.assertIn("safe shell line", shell_second["text"])
        self.assertNotIn("paged-quoted-secret", quote_first["text"])
        self.assertNotIn("paged-quoted-secret", quote_second["text"])
        self.assertIn("safe quoted line", quote_second["text"])
        self.assertIn("operator couldn't connect", quote_second["text"])
        self.assertNotIn("paged-heredoc-secret", heredoc_first["text"])
        self.assertNotIn("paged-heredoc-secret", heredoc_second["text"])
        self.assertIn("safe heredoc line", heredoc_second["text"])
        self.assertNotIn("paged-yaml-secret", yaml_first["text"])
        self.assertNotIn("paged-yaml-secret", yaml_second["text"])
        self.assertIn("safe yaml line", yaml_second["text"])

    def test_log_redaction_carries_whitespace_sensitive_quote_across_pages(self):
        job, _ = self.store.create_or_get(
            "log-redaction-whitespace-quoted-secret", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        header = b'password "part-one\n'
        continuation = b'part-two"\nsafe diagnostic after secret\n'
        log_path.write_bytes(header + continuation)

        first_page = self.manager.get_logs(
            job.mcp_job_id, offset=0, limit=len(header)
        )
        second_page = self.manager.get_logs(
            job.mcp_job_id, offset=len(header), limit=len(continuation)
        )

        self.assertNotIn("part-one", first_page["text"])
        self.assertNotIn("part-two", second_page["text"])
        self.assertIn("safe diagnostic after secret", second_page["text"])

    def test_log_redaction_fails_closed_when_prefix_context_is_incomplete(self):
        job, _ = self.store.create_or_get(
            "log-redaction-incomplete-prefix", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        padding = b'  "padding": "visible",\n' * 60_000
        prefix = b'"password": {\n' + padding
        page_tail = (
            b'  "token": "deep-prefix-secret",\n'
            b'  "password": "second-deep-secret"\n'
            b"}\ncompleted\n"
        )
        log_path.write_bytes(prefix + page_tail)

        page = self.manager.get_logs(
            job.mcp_job_id, offset=len(prefix), limit=len(page_tail)
        )

        self.assertNotIn("deep-prefix-secret", page["text"])
        self.assertNotIn("second-deep-secret", page["text"])
        self.assertEqual(page["text"].count("[redacted]"), 4)

    def test_log_redaction_keeps_safe_pages_beyond_prefix_context_cap(self):
        job, _ = self.store.create_or_get(
            "log-redaction-large-safe-prefix", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        prefix = b"ordinary startup diagnostic\n" * 50_000
        page_text = b"late safe diagnostic\nsecond safe diagnostic\n"
        log_path.write_bytes(prefix + page_text)

        page = self.manager.get_logs(
            job.mcp_job_id, offset=len(prefix), limit=len(page_text)
        )

        self.assertEqual(page["text"], page_text.decode("utf-8"))

    def test_log_redaction_keeps_safe_json_when_opener_is_in_prefix(self):
        job, _ = self.store.create_or_get(
            "log-redaction-safe-json-retained-opener", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        padding = b"safe prelude diagnostic\n" * 60_000
        json_open = b"{\n"
        members = b'  "status": "ready",\n' * 1_000
        page_text = b'  "message": "visible after retained opener"\n}\n'
        raw_log = padding + json_open + members + page_text
        offset = len(padding) + len(json_open) + len(members)
        self.assertGreater(offset, MAX_LOG_REDACTION_CONTEXT_BYTES)
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(
            job.mcp_job_id, offset=offset, limit=len(page_text)
        )

        self.assertEqual(page["text"], page_text.decode("utf-8"))
        self.assertFalse(page["redacted"])

    def test_log_redaction_fails_closed_when_shell_quote_opener_is_outside_context(self):
        job, _ = self.store.create_or_get(
            "log-redaction-truncated-shell-quote-context", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        opener = b'command --password "initial-secret\n'
        intervening_line = b"x" * (MAX_LOG_REDACTION_CONTEXT_BYTES + 100) + b"\n"
        secret_line = b"continuation-secret-fragment\n"
        raw_log = opener + intervening_line + secret_line
        log_path.write_bytes(raw_log)

        secret_offset = raw_log.index(b"continuation-secret-fragment") + 5
        page = self.manager.get_logs(
            job.mcp_job_id,
            offset=secret_offset,
            limit=len(secret_line) - 5,
        )

        self.assertNotIn("continuation-secret-fragment", page["text"])
        self.assertTrue(page["redacted"])

    def test_log_redaction_tracks_unindented_multiline_json_values(self):
        job, _ = self.store.create_or_get(
            "log-redaction-unindented-json-context", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        members = b'"item": "safe",\n' * 70_000
        secret_line = b'"password": "unindented-json-secret"\n'
        raw_log = b'{"token":\n{\n' + members + secret_line + b'}\n}\n'
        log_path.write_bytes(raw_log)

        secret_offset = raw_log.index(b"unindented-json-secret") + 4
        page = self.manager.get_logs(
            job.mcp_job_id,
            offset=secret_offset,
            limit=len(b"unindented-json-secret") - 4,
        )

        self.assertNotIn("unindented-json-secret", page["text"])
        self.assertTrue(page["redacted"])

    def test_log_prefix_state_replay_stops_at_line_budget(self):
        job, _ = self.store.create_or_get(
            "log-redaction-prefix-line-budget", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = b"\n" * (
            MAX_LOG_REDACTION_CONTEXT_BYTES + MAX_LOG_REDACTION_LINES + 10
        )
        log_path.write_bytes(raw_log)
        offset = MAX_LOG_REDACTION_CONTEXT_BYTES + MAX_LOG_REDACTION_LINES + 5

        page = self.manager.get_logs(job.mcp_job_id, offset=offset, limit=1)

        self.assertTrue(page["redacted"])

    def test_log_redaction_fails_closed_when_yaml_opener_is_cut_off(self):
        job, _ = self.store.create_or_get(
            "log-redaction-cutoff-yaml-opener", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        prelude = b"safe prefix\n"
        opener = b"password: |-\n"
        scalar_line_size = MAX_LOG_REDACTION_CONTEXT_BYTES - len(opener)
        scalar_line = (
            b"  "
            + b"x" * (scalar_line_size - 3)
            + b"\n"
        )
        secret_line = b"  deep-yaml-secret\n"
        log_path.write_bytes(prelude + opener + scalar_line + secret_line)
        offset = len(prelude) + MAX_LOG_REDACTION_CONTEXT_BYTES

        page = self.manager.get_logs(
            job.mcp_job_id, offset=offset, limit=len(secret_line)
        )

        self.assertNotIn("deep-yaml-secret", page["text"])
        self.assertTrue(page["redacted"])

    def test_log_redaction_fails_closed_when_cutoff_is_inside_yaml_scalar(self):
        job, _ = self.store.create_or_get(
            "log-redaction-cutoff-inside-yaml-scalar", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        prelude = b"safe prefix\n"
        opener = b"password: |-\n"
        first_scalar_line = (
            b"  "
            + b"x" * (MAX_LOG_REDACTION_CONTEXT_BYTES + 100)
            + b"\n"
        )
        second_scalar_line = b"  deep-yaml-secret\n"
        log_path.write_bytes(
            prelude + opener + first_scalar_line + second_scalar_line
        )
        offset = (
            len(prelude)
            + len(opener)
            + len(first_scalar_line)
            + len(b"  ")
        )

        page = self.manager.get_logs(
            job.mcp_job_id,
            offset=offset,
            limit=len(second_scalar_line) - len(b"  "),
        )

        self.assertNotIn("deep-yaml-secret", page["text"])
        self.assertTrue(page["redacted"])

    def test_quoted_heredoc_marker_does_not_hide_later_diagnostics(self):
        job, _ = self.store.create_or_get(
            "log-quoted-heredoc-marker", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = (
            b"cmd --token=quoted-marker-secret "
            b"--note='literal <<EOF marker'\n"
            b"startup completed normally\n"
        )
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(job.mcp_job_id, offset=0, limit=len(raw_log))

        self.assertNotIn("quoted-marker-secret", page["text"])
        self.assertIn("startup completed normally", page["text"])

    def test_log_redaction_splits_bare_carriage_return_records(self):
        job, _ = self.store.create_or_get("log-bare-cr", {"operation": "run"})
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        raw_log = b"startup\rcmd --token=bare-cr-secret\rcompleted\r"
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(job.mcp_job_id, offset=0, limit=len(raw_log))

        self.assertEqual(page["text"], "startup\r[redacted]\rcompleted\r")
        self.assertNotIn("bare-cr-secret", page["text"])

    def test_log_redaction_fails_closed_deep_inside_multiline_private_key(self):
        job, _ = self.store.create_or_get("log-private-key", {"operation": "run"})
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        payload_line = b"A" * 64 + b"\n"
        payload = payload_line * 50_000
        begin = b"-----BEGIN PRIVATE KEY-----\n"
        end = b"-----END PRIVATE KEY-----\n"
        log_path.write_bytes(
            b"ordinary prefix\n" + begin + payload + end + b"ordinary suffix\n"
        )

        # Both PEM markers are beyond the bounded context window from this
        # page, which is several lines into the private-key payload.
        offset = len(b"ordinary prefix\n") + len(begin) + len(payload_line) * 25_000 + 20
        page = self.manager.get_logs(job.mcp_job_id, offset=offset, limit=12)

        self.assertEqual(page["text"], "[redacted]")
        self.assertTrue(page["redacted"])
        self.assertEqual(page["redacted_lines"], 1)
        self.assertEqual(page["next_offset"], offset + 12)

    def test_log_redaction_masks_markerless_key_payload_only(self):
        job, _ = self.store.create_or_get(
            "log-markerless-private-key", {"operation": "run"}
        )
        log_path = self.manager.run_root / job.mcp_job_id / "runner.log"
        log_path.parent.mkdir(parents=True)
        payload = b"A" * 64 + b"==\n"
        raw_log = b"key export started\n" + payload + b"key export finished\n"
        log_path.write_bytes(raw_log)

        page = self.manager.get_logs(job.mcp_job_id, offset=0, limit=len(raw_log))

        self.assertEqual(
            page["text"],
            "key export started\n[redacted]\nkey export finished\n",
        )
        self.assertTrue(page["redacted"])
        self.assertEqual(page["redacted_lines"], 1)
        self.assertNotIn(payload.decode().strip(), page["text"])

    def test_submission_verifies_plan_digest_before_launch(self):
        document = {"benchmarks": [{"name": "example"}]}
        plan = {
            "input_digest": "digest",
            "validation": {"valid": True},
        }
        with patch.object(self.manager.operations, "prepare_run", Mock(return_value=plan)) as planner:
            job, created = self.manager.submit(
                "key-planned", document=document, plan_digest="digest"
            )

        self.assertTrue(created)
        planner.assert_called_once_with(document)
        self.manager._threads[job.mcp_job_id].join(timeout=5)

    def test_submission_reuses_verified_plan(self):
        document = {"benchmarks": [{"name": "example"}]}
        plan = {
            "input_digest": "digest",
            "validation": {"valid": True},
            "totals": {"global_iteration_count": 1},
        }
        with patch.object(self.manager.operations, "prepare_run") as planner:
            job, created = self.manager.submit(
                "key-verified-plan",
                document=document,
                plan_digest="digest",
                verified_plan=plan,
            )

        self.assertTrue(created)
        planner.assert_not_called()
        self.assertEqual(self.store.get(job.mcp_job_id).plan_summary["totals"], {
            "global_iteration_count": 1
        })
        self.manager._threads[job.mcp_job_id].join(timeout=5)

    def test_idempotent_retry_skips_planning(self):
        document = {"benchmarks": [{"name": "example"}]}
        plan = {
            "input_digest": "digest",
            "validation": {"valid": True},
        }
        with patch.object(self.manager.operations, "prepare_run", Mock(return_value=plan)) as planner:
            job, created = self.manager.submit(
                "key-retry", document=document, plan_digest="digest"
            )
            self.assertTrue(created)
            planner.assert_called_once_with(document)

            planner.reset_mock()
            planner.side_effect = AssertionError("retry replanned the run")
            duplicate, duplicate_created = self.manager.submit(
                "key-retry", document=document, plan_digest="digest"
            )

        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.mcp_job_id, job.mcp_job_id)
        planner.assert_not_called()
        self.manager._threads[job.mcp_job_id].join(timeout=5)

    def test_planned_path_submission_uses_path_size_policy(self):
        input_root = self.root / "mcp" / "inputs"
        input_root.mkdir(parents=True)
        path = input_root / "planned.json"
        document = {"benchmarks": [{"name": "example"}]}
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o600)
        self.manager.max_inline_bytes = 1
        plan = {
            "input_digest": "digest",
            "validation": {"valid": True},
        }

        with patch.object(self.manager.operations, "prepare_run", Mock(return_value=plan)):
            job, created = self.manager.submit(
                "key-planned-path", path=path, plan_digest="digest"
            )

        self.assertTrue(created)
        self.manager._threads[job.mcp_job_id].join(timeout=5)

    def test_planned_staging_failure_preserves_plan_metadata(self):
        document = {"benchmarks": [{"name": "example"}]}
        plan = {
            "contract_version": "1",
            "input_digest": "digest",
            "validation": {"valid": True},
            "totals": {"global_iteration_count": 1},
        }
        broken_root = self.root / "not-a-directory"
        broken_root.write_text("occupied", encoding="utf-8")
        self.manager.run_root = broken_root

        with patch.object(self.manager.operations, "prepare_run", Mock(return_value=plan)):
            job, created = self.manager.submit(
                "key-planned-staging-failure",
                document=document,
                plan_digest="digest",
            )

        self.assertTrue(created)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertEqual(job.plan_digest, "digest")
        self.assertEqual(job.plan_summary["totals"], {"global_iteration_count": 1})

    def test_submission_rejects_stale_plan_digest(self):
        document = {"benchmarks": [{"name": "example"}]}
        plan = {
            "input_digest": "current",
            "validation": {"valid": True},
        }
        with patch.object(self.manager.operations, "prepare_run", Mock(return_value=plan)):
            with self.assertRaises(OperationError) as raised:
                self.manager.submit(
                    "key-stale-plan", document=document, plan_digest="old"
                )

        self.assertEqual(raised.exception.code, "stale_plan")

    def test_failed_runner_is_persisted(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "failed-runs",
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            host_execution=False,
        )
        job, _ = manager.submit("key-2", document={"benchmarks": [{"name": "example"}]})
        manager._threads[job.mcp_job_id].join(timeout=5)
        failed = self.store.get(job.mcp_job_id)
        self.assertEqual(failed.state.value, "failed")
        self.assertEqual(failed.exit_code, 7)

    def test_cdm_failure_after_indexing_is_not_benchmark_failure(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "cdm-failed-runs",
            [
                sys.executable,
                "-c",
                "import json, os, sys; "
                "open(os.environ['CRUCIBLE_MCP_EVENT_FILE'], 'a').write("
                "json.dumps({'state': 'postprocessing'}) + '\\n' + "
                "json.dumps({'state': 'indexing'}) + '\\n'); "
                "import time; time.sleep(0.2); sys.exit(1)",
            ],
            cdm_readiness_timeout=0.01,
            host_execution=False,
        )
        job, _ = manager.submit(
            "key-cdm-failure", document={"benchmarks": [{"name": "example"}]}
        )
        manager._threads[job.mcp_job_id].join(timeout=5)
        completed = self.store.get(job.mcp_job_id)
        self.assertEqual(completed.state, JobState.COMPLETED)
        self.assertEqual(completed.result_status.value, "unavailable")
        self.assertEqual(completed.error_category, "cdm")

    def test_lifecycle_event_persists_correlation_identifiers(self):
        job, _ = self.store.create_or_get("key-identifiers", {"run": 1})
        event_path = self.root / "events.jsonl"
        event_path.write_text(
            '{"state":"starting","run_directory":"/runs/one",'
            '"rickshaw_run_id":"rickshaw-1","cdm_run_id":"cdm-1",'
            '"runner_container_id":"container-1"}\n',
            encoding="utf-8",
        )
        self.manager._consume_events(job.mcp_job_id, event_path, 0)
        updated = self.store.get(job.mcp_job_id)
        self.assertEqual(updated.rickshaw_run_id, "rickshaw-1")
        self.assertEqual(updated.cdm_run_id, "cdm-1")
        self.assertEqual(updated.runner_container_id, "container-1")

    def test_completed_run_backfills_rickshaw_and_cdm_identifiers(self):
        job, _ = self.store.create_or_get("key-backfill", {"run": 1})
        run_directory = self.root / "backfill-run"
        (run_directory / "run").mkdir(parents=True)
        (run_directory / "run" / "rickshaw-run.json").write_text(
            '{"run-id":"rickshaw-2"}', encoding="utf-8"
        )
        (run_directory / "run" / "result-summary.json").write_text(
            '{"runs":[{"run-id":"cdm-2"}]}', encoding="utf-8"
        )
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(run_directory),
        )
        self.manager._backfill_identifiers(job.mcp_job_id)
        updated = self.store.get(job.mcp_job_id)
        self.assertEqual(updated.rickshaw_run_id, "rickshaw-2")
        self.assertEqual(updated.cdm_run_id, "cdm-2")

    def test_backfill_does_not_choose_between_multiple_cdm_run_ids(self):
        job, _ = self.store.create_or_get("key-backfill-ambiguous", {"run": 1})
        run_directory = self.root / "ambiguous-backfill-run"
        (run_directory / "run").mkdir(parents=True)
        (run_directory / "run" / "result-summary.json").write_text(
            '{"runs":[{"run-id":"cdm-1"},{"run-id":"cdm-2"}]}',
            encoding="utf-8",
        )
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(run_directory),
        )

        self.manager._backfill_identifiers(job.mcp_job_id)

        self.assertIsNone(self.store.get(job.mcp_job_id).cdm_run_id)

    def test_staging_failure_is_persisted_as_infrastructure_failure(self):
        document = {"benchmarks": [{"name": "example"}]}
        with patch.object(Path, "write_text", side_effect=OSError("no space left on device")):
            job, created = self.manager.submit("key-staging-failure", document=document)

        self.assertTrue(created)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertEqual(job.error_category, "infrastructure")
        self.assertIn("could not stage run input", job.error_message)
        duplicate, duplicate_created = self.manager.submit(
            "key-staging-failure", document=document
        )
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.mcp_job_id, job.mcp_job_id)
        self.assertEqual(duplicate.state, JobState.FAILED)

    def test_processing_setup_failure_is_persisted_as_infrastructure_failure(self):
        target = self.root / "run" / "result"
        target.mkdir(parents=True)
        with patch.object(Path, "mkdir", side_effect=OSError("no space left on device")):
            job, created = self.manager.submit_processing(
                "key-processing-staging-failure", "index", target
            )

        self.assertTrue(created)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertEqual(job.error_category, "infrastructure")
        self.assertIn("could not create processing supervision directory", job.error_message)

    def test_indexed_deletion_is_idempotent_and_uses_cli_arguments(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "delete-indexed",
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            host_execution=False,
        )
        job, created = manager.submit_indexed_deletion("key-delete-indexed", "run-1")
        self.assertTrue(created)
        manager._threads[job.mcp_job_id].join(timeout=5)
        completed = self.store.get(job.mcp_job_id)
        self.assertEqual(completed.state, JobState.COMPLETED)
        self.assertIsNone(completed.run_directory)
        duplicate, duplicate_created = manager.submit_indexed_deletion("key-delete-indexed", "run-1")
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.mcp_job_id, job.mcp_job_id)

    def test_local_archive_operation_is_idempotent(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "archive-jobs",
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            host_execution=False,
        )
        run_path = self.root / "run" / "archive-me"
        run_path.mkdir(parents=True)
        job, created = manager.submit_archive_operation(
            "key-archive-local", "archive_local_run", run_path
        )
        self.assertTrue(created)
        manager._threads[job.mcp_job_id].join(timeout=5)
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.COMPLETED)
        self.assertTrue(
            (self.root / "archive-jobs" / job.mcp_job_id / "processing-complete").is_file()
        )
        duplicate, duplicate_created = manager.submit_archive_operation(
            "key-archive-local", "archive_local_run", run_path
        )
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.mcp_job_id, job.mcp_job_id)

    def test_archive_retry_returns_completed_job_after_source_is_removed(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "archive-retry",
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            host_execution=False,
        )
        run_path = self.root / "run" / "archive-retry-source"
        run_path.mkdir(parents=True)
        job, _ = manager.submit_archive_operation("key-archive-retry", "archive_local_run", run_path)
        manager._threads[job.mcp_job_id].join(timeout=5)
        run_path.rmdir()

        duplicate, created = manager.submit_archive_operation(
            "key-archive-retry", "archive_local_run", run_path
        )

        self.assertFalse(created)
        self.assertEqual(duplicate.state, JobState.COMPLETED)

    def test_maintenance_jobs_persist_identity_for_recovery(self):
        job, _ = self.store.create_or_get(
            "key-maintenance-recovery", {"run": "run-1"}, "delete_indexed_result"
        )
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            runner_pid=1234,
            run_directory="run-1",
            supervision_directory=str(self.root / "maintenance-job"),
        )
        with (
            patch.object(self.manager, "_process_exists", return_value=True),
            patch.object(self.manager, "_runner_identity_matches", return_value=True),
            patch.object(self.manager, "_reattach") as reattach,
        ):
            changed = self.manager.reconcile()
        self.assertEqual(changed, [])
        reattach.assert_called_once_with(self.store.get(job.mcp_job_id))

    def test_indexed_deletion_recovery_identity_uses_supervision_marker(self):
        job, _ = self.store.create_or_get(
            "key-delete-recovery-identity", {"run": "run-1"}, "delete_indexed_result"
        )
        supervision = self.root / "delete-recovery-identity"
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            runner_pid=1234,
            supervision_directory=str(supervision),
        )
        command_line = (
            f"python\0-c\0wrapper\0{supervision / 'processing-complete'}\0"
            "crucible\0rm\0--run\0run-1\0"
        ).encode()
        with patch("pathlib.Path.read_bytes", return_value=command_line):
            self.assertTrue(self.manager._runner_identity_matches(self.store.get(job.mcp_job_id)))

    def test_maintenance_completion_marker_resolves_recovery(self):
        job, _ = self.store.create_or_get(
            "key-maintenance-marker", {"run": "run-2"}, "delete_indexed_result"
        )
        supervision = self.root / "maintenance-marker"
        supervision.mkdir()
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            runner_pid=1234,
            run_directory="run-2",
            supervision_directory=str(supervision),
        )
        (supervision / "processing-complete").write_text("delete_indexed_result\n")

        recovered = self.manager._resolve_or_fail(self.store.get(job.mcp_job_id))

        self.assertEqual(recovered.state, JobState.COMPLETED)

    def test_orphaned_processing_resolution_backfills_identifiers(self):
        job, _ = self.store.create_or_get(
            "key-orphaned-processing-identifiers", {"operation": "index"}, "index"
        )
        target = self.root / "orphaned-processing-run"
        (target / "run").mkdir(parents=True)
        (target / "run" / "rickshaw-run.json").write_text(
            '{"run-id":"rickshaw-orphaned"}', encoding="utf-8"
        )
        (target / "run" / "result-summary.json").write_text(
            '{"cdm_run_id":"cdm-orphaned"}', encoding="utf-8"
        )
        supervision = self.root / "orphaned-processing-job"
        supervision.mkdir()
        (supervision / "processing-complete").write_text("index\n", encoding="utf-8")
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(target),
            supervision_directory=str(supervision),
        )
        self.store.transition(
            job.mcp_job_id,
            JobState.INDEXING,
            run_directory=str(target),
            supervision_directory=str(supervision),
        )

        recovered = self.manager._resolve_or_fail(self.store.get(job.mcp_job_id))

        self.assertEqual(recovered.state, JobState.COMPLETED)
        self.assertEqual(recovered.rickshaw_run_id, "rickshaw-orphaned")
        self.assertEqual(recovered.cdm_run_id, "cdm-orphaned")

    def test_orphaned_run_resolution_backfills_identifiers(self):
        job, _ = self.store.create_or_get(
            "key-orphaned-run-identifiers", {"operation": "run"}
        )
        target = self.root / "orphaned-run"
        (target / "run").mkdir(parents=True)
        (target / "run" / "rickshaw-run.json").write_text(
            '{"run-id":"rickshaw-direct"}', encoding="utf-8"
        )
        (target / "run" / "result-summary.json").write_text(
            '{"cdm_run_id":"cdm-direct"}', encoding="utf-8"
        )
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(target),
        )
        self.store.transition(job.mcp_job_id, JobState.INDEXING, exit_code=17)

        recovered = self.manager._resolve_or_fail(self.store.get(job.mcp_job_id))

        self.assertEqual(recovered.state, JobState.COMPLETED)
        self.assertEqual(recovered.rickshaw_run_id, "rickshaw-direct")
        self.assertEqual(recovered.cdm_run_id, "cdm-direct")
        self.assertEqual(recovered.exit_code, 17)

    def test_recovery_backfill_invalid_utf8_reaches_terminal_state(self):
        job, _ = self.store.create_or_get(
            "key-invalid-recovery-artifact", {"operation": "index"}, "index"
        )
        target = self.root / "invalid-recovery-run"
        (target / "run").mkdir(parents=True)
        (target / "run" / "rickshaw-run.json").write_bytes(b"{\xff")
        (target / "run" / "result-summary.json").write_text(
            '{"cdm_run_id":"cdm-invalid-metadata"}', encoding="utf-8"
        )
        supervision = self.root / "invalid-recovery-job"
        supervision.mkdir()
        (supervision / "processing-complete").write_text("index\n", encoding="utf-8")
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(target),
            supervision_directory=str(supervision),
        )
        self.store.transition(job.mcp_job_id, JobState.INDEXING)

        recovered = self.manager._resolve_or_fail(self.store.get(job.mcp_job_id))

        self.assertEqual(recovered.state, JobState.COMPLETED)
        self.assertEqual(recovered.cdm_run_id, "cdm-invalid-metadata")

    def test_processing_jobs_report_operation_lifecycle_state(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "processing-lifecycle",
            [sys.executable, "-c", "import time; time.sleep(0.3)"],
            host_execution=False,
        )
        target = self.root / "run" / "lifecycle-result"
        target.mkdir(parents=True)
        job, _ = manager.submit_processing("key-processing-lifecycle", "index", target)
        time.sleep(0.05)
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.INDEXING)
        manager._threads[job.mcp_job_id].join(timeout=5)

    def test_processing_recovery_does_not_trust_existing_summary(self):
        job, _ = self.store.create_or_get(
            "key-processing-recovery-summary", {"operation": "index"}, "index"
        )
        target = self.root / "run" / "existing-result"
        (target / "run").mkdir(parents=True)
        (target / "run" / "result-summary.json").write_text("{}", encoding="utf-8")
        supervision = self.root / "processing-recovery-job"
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(target),
            supervision_directory=str(supervision),
        )
        self.store.transition(
            job.mcp_job_id,
            JobState.INDEXING,
            run_directory=str(target),
            supervision_directory=str(supervision),
        )

        recovered = self.manager._resolve_or_fail(self.store.get(job.mcp_job_id))
        self.assertEqual(recovered.state, JobState.FAILED)

    def test_failed_standalone_index_is_not_reported_completed(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "failed-index",
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            host_execution=False,
        )
        target = self.root / "run" / "failed-index-result"
        target.mkdir(parents=True)
        job, _ = manager.submit_processing("key-failed-index", "index", target)
        manager._threads[job.mcp_job_id].join(timeout=5)
        failed = self.store.get(job.mcp_job_id)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.exit_code, 7)

    def test_input_path_outside_approved_root_returns_authorization_error(self):
        path = self.root / "outside.json"
        path.write_text('{"benchmarks":[{"name":"example"}]}', encoding="utf-8")
        with self.assertRaises(OperationError) as raised:
            self.manager.submit("key-outside", path=path)
        self.assertEqual(raised.exception.category, "authorization")
        self.assertEqual(raised.exception.code, "input_path_rejected")

    def test_reconcile_resolves_previous_recovery_states(self):
        job, _ = self.store.create_or_get("key-recovery", {"run": 1})
        self.store.transition(job.mcp_job_id, JobState.UNKNOWN_AFTER_CRASH)
        changed = self.manager.reconcile()
        self.assertEqual([item.mcp_job_id for item in changed], [job.mcp_job_id])
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.FAILED)

        job, _ = self.store.create_or_get("key-recovery-2", {"run": 2})
        self.store.transition(job.mcp_job_id, JobState.RECOVERY_REQUIRED)
        changed = self.manager.reconcile()
        self.assertEqual([item.mcp_job_id for item in changed], [job.mcp_job_id])
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.FAILED)

    def test_reconcile_reattaches_verified_live_runner(self):
        job, _ = self.store.create_or_get("key-live", {"run": 1})
        run_directory = self.root / "live-run"
        (run_directory / "input").mkdir(parents=True)
        (run_directory / "input" / "run-file.json").write_text("{}", encoding="utf-8")
        self.store.transition(
            job.mcp_job_id, JobState.STARTING, runner_pid=1234, run_directory=str(run_directory)
        )
        with (
            patch.object(self.manager, "_process_exists", return_value=True),
            patch.object(self.manager, "_runner_identity_matches", return_value=True),
            patch.object(self.manager, "_reattach") as reattach,
        ):
            changed = self.manager.reconcile()
        self.assertEqual(changed, [])
        reattach.assert_called_once_with(self.store.get(job.mcp_job_id))

    def test_recovery_uses_processing_supervision_directory(self):
        job, _ = self.store.create_or_get(
            "key-processing-recovery", {"operation": "index"}, "index"
        )
        target = self.root / "target-run"
        supervision = self.root / "mcp-job"
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(target),
            supervision_directory=str(supervision),
        )
        with patch("crucible_mcp.runner.threading.Thread") as thread:
            self.manager._reattach(self.store.get(job.mcp_job_id))
        self.assertEqual(
            thread.call_args.kwargs["args"][1], supervision / "events.jsonl"
        )

    def test_reconcile_marks_unverified_runner_failed(self):
        job, _ = self.store.create_or_get("key-unverified", {"run": 1})
        self.store.transition(job.mcp_job_id, JobState.STARTING, runner_pid=1234)
        with patch.object(self.manager, "_process_exists", return_value=True):
            changed = self.manager.reconcile()
        self.assertEqual([item.mcp_job_id for item in changed], [job.mcp_job_id])
        self.assertEqual(self.store.get(job.mcp_job_id).state, JobState.FAILED)

    def test_summary_wait_uses_configured_cdm_readiness_timeout(self):
        manager = RunManager(
            self.store,
            self.manager.operations,
            self.root / "summary-timeout-runs",
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            cdm_readiness_timeout=0.01,
            host_execution=False,
        )
        job, _ = manager.submit("key-summary-timeout", document={"benchmarks": [{"name": "example"}]})
        manager._threads[job.mcp_job_id].join(timeout=5)
        started = time.monotonic()
        with self.assertRaises(OperationError) as raised:
            manager.get_summary(job.mcp_job_id)
        self.assertEqual(raised.exception.code, "result_unavailable")
        self.assertLess(time.monotonic() - started, 1)

    def test_summary_response_is_bounded_after_redaction(self):
        job, _ = self.store.create_or_get("key-summary-response-bound", {"run": 1})
        run_directory = self.root / "summary-response-bound-run"
        summary_path = run_directory / "run" / "result-summary.json"
        summary_path.parent.mkdir(parents=True)
        raw_summary = json.dumps(
            {f"token{index}": "x" for index in range(45_000)},
            separators=(",", ":"),
        )
        self.assertLess(len(raw_summary.encode("utf-8")), MAX_METADATA_RESPONSE_BYTES)
        summary_path.write_text(raw_summary, encoding="utf-8")
        self.store.transition(
            job.mcp_job_id,
            JobState.STARTING,
            run_directory=str(run_directory),
        )
        self.store.transition(job.mcp_job_id, JobState.RUNNING)
        self.store.transition(job.mcp_job_id, JobState.COMPLETED)

        with self.assertRaises(OperationError) as raised:
            self.manager.get_summary(job.mcp_job_id, request_id="summary-request")

        self.assertEqual(raised.exception.code, "result_too_large")


if __name__ == "__main__":
    unittest.main()
