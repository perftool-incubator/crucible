import errno
import io
import json
import lzma
import logging
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from crucible_mcp.operations import (
    MAX_ARTIFACT_READ_BYTES,
    MAX_ARTIFACT_RESPONSE_BYTES,
    MAX_LOG_PRIVATE_KEY_MARKERS_PER_LINE,
    MAX_METADATA_DECOMPRESSOR_MEMORY,
    MAX_METADATA_JSON_FRAGMENTS,
    MAX_METADATA_REDACTION_WORK,
    MAX_METADATA_RESPONSE_BYTES,
    MAX_METADATA_DEPTH,
    MAX_PLAN_BENCHMARKS,
    MAX_PLAN_PARAMETER_WORK,
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
        (schema / "tool-params.json").write_text(
            json.dumps({"type": "array", "items": {"type": "object"}}),
            encoding="utf-8",
        )
        self.operations = CrucibleOperations(self.root, InputPolicy([self.root / "inputs"]))

    def tearDown(self):
        self.directory.cleanup()

    def test_list_and_describe_benchmark(self):
        benchmark = self.root / "subprojects" / "benchmarks" / "example"
        (benchmark / "multiplex.json").write_text(
            json.dumps({
                "validations": {
                    "positive_integer": {
                        "description": "a whole number greater than 0",
                        "args": ["seconds"],
                        "vals": "^[1-9][0-9]*$",
                    }
                }
            }),
            encoding="utf-8",
        )
        benchmarks = self.operations.list_benchmarks()
        self.assertEqual(benchmarks[0]["name"], "example")
        description = self.operations.describe_benchmark("example")
        self.assertEqual(description["description"], "Example benchmark")
        self.assertEqual(description["parameter_validation"], {
            "source": "multiplex.json",
            "available": True,
            "complete": True,
            "rules": [{
                "group": "positive_integer",
                "parameters": ["seconds"],
                "accepted_patterns": ["^[1-9][0-9]*$"],
                "repeatable": False,
                "description": "a whole number greater than 0",
            }],
        })

    def test_benchmark_discovery_redacts_credential_metadata(self):
        benchmark = self.root / "subprojects" / "benchmarks" / "example"
        secret = "benchmark-metadata-secret"
        (benchmark / "benchmark-metadata.json").write_text(
            json.dumps({
                "description": f"password={secret}",
                "metadata": {"credentials": {"token": secret}},
            }),
            encoding="utf-8",
        )
        pattern_secret = "pattern-value-secret"
        (benchmark / "multiplex.json").write_text(
            json.dumps({
                "validations": {
                    "literal_password": {
                        "args": ["password"],
                        "vals": f"^password={pattern_secret}$",
                    }
                }
            }),
            encoding="utf-8",
        )

        listed = self.operations.list_benchmarks()
        described = self.operations.describe_benchmark("example")

        for result in (listed, described):
            self.assertNotIn(secret, json.dumps(result))
            self.assertIn("[redacted]", json.dumps(result))
        self.assertNotIn(pattern_secret, json.dumps(described))
        self.assertFalse(described["parameter_validation"]["complete"])

    def test_documentation_search_does_not_echo_credential_query_values(self):
        secret = "documentation-query-secret"

        result = self.operations.search_documentation(f"password={secret}")

        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["query"], "password=[redacted]")

    def test_plan_schema_errors_do_not_echo_rejected_values(self):
        schema_path = (
            self.root
            / "subprojects"
            / "core"
            / "rickshaw"
            / "schema"
            / "run-file.json"
        )
        schema_path.write_text(
            json.dumps({
                "type": "object",
                "properties": {
                    "benchmarks": {"type": "array"},
                    "run-params": {"type": "object"},
                },
                "required": ["benchmarks"],
            }),
            encoding="utf-8",
        )
        secret = "unmarked-rejected-secret"
        planner_error = {
            "validation": {
                "valid": False,
                "errors": [{
                    "code": "invalid_input",
                    "message": f"{secret!r} is not of type 'object'",
                }],
                "warnings": [],
            },
            "benchmarks": [],
            "tools": {"entries": []},
        }

        class FakeLimits:
            def __init__(self, **_kwargs):
                pass

        class FakePlanner:
            def __init__(self, *_args, **_kwargs):
                pass

            def plan(self, *_args):
                return planner_error

        rickshaw_dir = self.root / "subprojects" / "core" / "rickshaw"
        multiplex_dir = self.root / "subprojects" / "core" / "multiplex"
        modules = {
            "rickshaw_lib.run_planner": SimpleNamespace(
                __file__=str(rickshaw_dir / "rickshaw_lib" / "run_planner.py"),
                PlannerLimits=FakeLimits,
                RunPlanner=FakePlanner,
            ),
            "multiplex": SimpleNamespace(
                __file__=str(multiplex_dir / "multiplex.py"),
                expand_parameters=lambda *_args, **_kwargs: [],
                apply_flat_params=lambda *_args, **_kwargs: [],
            ),
        }

        with patch("importlib.import_module", side_effect=modules.__getitem__):
            plan = self.operations._build_run_plan({
                "benchmarks": [],
                "run-params": secret,
            })

        self.assertNotIn(secret, json.dumps(plan))
        self.assertEqual(
            plan["validation"]["errors"],
            [{
                "code": "invalid_input",
                "message": "run-params: does not satisfy the type constraint",
            }],
        )

    def test_parameter_value_failure_has_safe_structured_guidance(self):
        plan = {
            "validation": {
                "valid": False,
                "errors": [{
                    "code": "expansion_failed",
                    "message": (
                        "benchmark sleep parameter expansion failed: "
                        "parameter expansion failed with exit code 4"
                    ),
                }],
            }
        }
        document = {"benchmarks": [{"name": "sleep"}]}

        self.operations._enrich_parameter_expansion_errors(document, plan, True)

        self.assertEqual(plan["validation"]["errors"], [{
            "code": "invalid_parameter",
            "benchmark": "sleep",
            "guidance_tool": "describe_benchmark",
            "guidance_field": "parameter_validation.rules",
            "message": (
                "parameter validation failed for benchmark sleep; inspect "
                "describe_benchmark parameter_validation.rules for accepted forms"
            ),
        }])

    def test_ambiguous_parameter_expansion_failure_stays_generic(self):
        error = {
            "code": "expansion_failed",
            "message": (
                "benchmark sleep parameter expansion failed: "
                "parameter expansion failed with exit code 4"
            ),
        }
        plan = {"validation": {"valid": False, "errors": [error]}}

        self.operations._enrich_parameter_expansion_errors(
            {"benchmarks": [{"name": "sleep"}]}, plan, False
        )

        self.assertEqual(error["code"], "expansion_failed")

    def test_planner_diagnostics_track_confirmed_rejection_only(self):
        logger_name = "test_multiplex_diagnostics"
        logger = logging.getLogger(logger_name)
        result = {"accepted": True}

        def validate_parameter(param, value):
            if result["accepted"]:
                # Multiplex can log a failed alternative even though a later
                # alternative accepts the value.
                logger.warning(
                    "Validation failed for param='%s', val='%s'. "
                    "Values didn't match the pattern '%s'.",
                    param,
                    value,
                    "first-pattern",
                )
            return result["accepted"]

        multiplex = SimpleNamespace(
            __name__=logger_name,
            param_validated=validate_parameter,
            validation_dict={"seconds": "^[1-9][0-9]*$"},
        )

        with self.operations._suppress_planner_diagnostics(multiplex) as diagnostics:
            self.assertTrue(multiplex.param_validated("seconds", "accepted"))
            self.assertFalse(diagnostics["parameter_validation_failed"])
            result["accepted"] = False
            self.assertFalse(multiplex.param_validated("seconds", "rejected"))
            self.assertTrue(diagnostics["parameter_validation_failed"])

        self.assertIs(multiplex.param_validated, validate_parameter)

        with self.operations._suppress_planner_diagnostics(multiplex) as diagnostics:
            self.assertFalse(multiplex.param_validated("unknown", "value"))
            self.assertFalse(diagnostics["parameter_validation_failed"])

    def test_missing_benchmark_validations_are_reported_incomplete(self):
        benchmark = self.root / "subprojects" / "benchmarks" / "example"
        (benchmark / "multiplex.json").write_text('{"presets": {}}', encoding="utf-8")

        self.assertEqual(
            self.operations._benchmark_parameter_validation(benchmark),
            {
                "source": "multiplex.json",
                "available": True,
                "complete": False,
                "rules": [],
            },
        )

    def test_plan_response_bound_drops_detail_prefixes(self):
        plan = {
            "contract_version": "1",
            "input_digest": "digest",
            "validation": {"valid": True, "errors": [], "warnings": []},
            "benchmarks": [{
                "parameter_sets": {
                    "count": 1,
                    "returned": 1,
                    "items": [{"arg": "payload", "val": "x" * 10000}],
                    "truncated": False,
                },
                "engine_ids": {"count": 1, "items": ["1"], "truncated": False},
            }],
            "tools": {"mode": "explicit", "entries": [{"tool": "sysstat"}], "truncated": False},
            "limits": {"truncated": False, "warnings": []},
        }

        bounded = self.operations._bound_plan_response(plan, 1024, "request")

        self.assertEqual(bounded["benchmarks"][0]["parameter_sets"]["items"], [])
        self.assertEqual(bounded["benchmarks"][0]["engine_ids"]["items"], [])
        self.assertEqual(bounded["tools"]["entries"], [])
        self.assertTrue(bounded["benchmarks"][0]["parameter_sets"]["truncated"])
        self.assertTrue(bounded["benchmarks"][0]["engine_ids"]["truncated"])
        self.assertTrue(bounded["tools"]["truncated"])
        self.assertTrue(bounded["limits"]["truncated"])
        self.assertLessEqual(
            self.operations._mcp_response_size(bounded, "request"), 1024
        )

    def test_plan_response_bound_does_not_serialize_oversized_detail_expansion(self):
        large_value = "x" * 250_000
        plan = {
            "contract_version": "1",
            "input_digest": "digest",
            "validation": {"valid": True, "errors": [], "warnings": []},
            "benchmarks": [{
                "parameter_sets": {
                    "count": 1000,
                    "returned": 1000,
                    "items": [
                        {"arg": "payload", "val": large_value}
                        for _ in range(1000)
                    ],
                    "truncated": False,
                },
                "engine_ids": {"count": 1, "items": ["1"], "truncated": False},
            }],
            "tools": {"mode": "explicit", "entries": [], "truncated": False},
            "limits": {"truncated": False, "warnings": []},
        }

        bounded = self.operations._bound_plan_response(
            plan, MAX_METADATA_RESPONSE_BYTES, "request"
        )

        self.assertEqual(bounded["benchmarks"][0]["parameter_sets"]["items"], [])
        self.assertLessEqual(
            self.operations._mcp_response_size(bounded, "request"),
            MAX_METADATA_RESPONSE_BYTES,
        )

    def test_plan_work_bound_rejects_aggregate_expansion(self):
        document = {"benchmarks": [{}] * MAX_PLAN_BENCHMARKS}
        with self.assertRaises(OperationError) as context:
            self.operations._validate_plan_work(
                document, MAX_PLAN_PARAMETER_WORK // MAX_PLAN_BENCHMARKS + 1
            )
        self.assertEqual(context.exception.code, "planning_limit")

        large_value = "x" * 200_000
        materialization_document = {
            "benchmarks": [{
                "mv-params": {
                    "sets": [{
                        "params": [
                            {"arg": "payload", "vals": [large_value]},
                            {"arg": "sweep", "vals": [str(index) for index in range(1000)]},
                        ]
                    }]
                }
            }]
        }
        with self.assertRaises(OperationError) as context:
            self.operations._validate_plan_work(materialization_document, 1000)
        self.assertEqual(context.exception.code, "planning_limit")

        benchmark = self.root / "subprojects" / "benchmarks" / "example"
        (benchmark / "multiplex.json").write_text(
            json.dumps({
                "presets": {
                    "defaults": [{"arg": "payload", "val": "x" * 200_000}]
                }
            }),
            encoding="utf-8",
        )
        preset_document = {
            "benchmarks": [{
                "name": "example",
                "ids": ["1"],
                "mv-params": {
                    "sets": [{
                        "params": [{"arg": "sweep", "vals": [str(index) for index in range(1000)]}]
                    }]
                },
            }]
        }
        with self.assertRaises(OperationError) as context:
            self.operations._validate_plan_work(preset_document, 1000)
        self.assertEqual(context.exception.code, "planning_limit")

    def test_plan_work_bounds_engine_id_materialization(self):
        wide_range = "1-" + ("9" * 200)
        document = {
            "benchmarks": [
                {"ids": [wide_range]}
                for _ in range(MAX_PLAN_BENCHMARKS)
            ]
        }

        with self.assertRaises(OperationError) as context:
            self.operations._validate_plan_work(document, 1)
        self.assertEqual(context.exception.code, "planning_limit")

        with self.assertRaises(OperationError) as context:
            self.operations._validate_plan_work(
                {"benchmarks": [{"ids": ["1-" + ("9" * 4000)]}]}, 1
            )
        self.assertEqual(context.exception.code, "planning_limit")

        document["benchmarks"].append({})
        with self.assertRaises(OperationError) as context:
            self.operations._validate_plan_work(document, 1)
        self.assertEqual(context.exception.code, "planning_limit")

        parameter_document = {
            "benchmarks": [{
                "mv-params": {
                    "sets": [{
                        "params": [
                            {"arg": f"arg-{index}", "vals": ["value"]}
                            for index in range(100)
                        ]
                    }]
                }
            } for _ in range(100)]
        }
        with self.assertRaises(OperationError) as context:
            self.operations._validate_plan_work(parameter_document, 101)
        self.assertEqual(context.exception.code, "planning_limit")

        effective = {
            "sets": [{
                "params": [
                    {"arg": "enabled", "vals": ["1"]},
                    *[
                        {"arg": f"disabled-{index}", "vals": ["x"], "enabled": "no"}
                        for index in range(1001)
                    ],
                ]
            }]
        }
        self.operations._validate_plan_work(
            {"benchmarks": [{"mv-params": [effective] + [effective] * 100}]}, 1
        )

        oversized_raw = {
            "sets": [{
                "params": [
                    {"arg": f"arg-{index}", "vals": ["value"]}
                    for index in range(10001)
                ]
            }]
        }
        with self.assertRaises(OperationError) as context:
            self.operations._validate_plan_work(
                {"benchmarks": [{"mv-params": oversized_raw}]}, 1
            )
        self.assertEqual(context.exception.code, "planning_limit")

    def test_prepare_run_file_rejects_invalid_utf8(self):
        run_file = self.root / "inputs" / "invalid-utf8.json"
        run_file.parent.mkdir(parents=True)
        run_file.write_bytes(b"\xff")
        run_file.chmod(0o600)

        with self.assertRaises(OperationError) as context:
            self.operations.prepare_run_file(run_file)
        self.assertEqual(context.exception.code, "invalid_json")

    def test_estimate_projects_before_applying_response_limit(self):
        plan = {
            "contract_version": "1",
            "input_digest": "digest",
            "validation": {"valid": True, "errors": [], "warnings": []},
            "benchmarks": [{"parameter_sets": {"items": ["x" * 10000]}}],
            "totals": {"global_iteration_count": 2},
            "runtime": {"confidence": "unavailable"},
            "limits": {"truncated": False, "warnings": []},
        }
        with patch.object(self.operations, "_build_run_plan", return_value=plan):
            estimate = self.operations.estimate_run({}, max_response_bytes=1500)

        self.assertEqual(set(estimate), {
            "contract_version", "input_digest", "validation", "totals", "runtime", "limits"
        })

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

    def test_tool_discovery_redacts_credential_metadata(self):
        tool_repository = self.root / "repos" / "tool-credential-test"
        tool_repository.mkdir(parents=True)
        (tool_repository / "rickshaw.json").write_text('{"tool":"credential-test"}', encoding="utf-8")
        secret = "tool-metadata-secret"
        (tool_repository / "tool-metadata.json").write_text(
            json.dumps({"description": f"token: {secret}"}), encoding="utf-8"
        )
        tool = self.root / "subprojects" / "tools" / "credential-test"
        tool.parent.mkdir(parents=True)
        tool.symlink_to(tool_repository, target_is_directory=True)

        result = self.operations.list_tools()

        self.assertNotIn(secret, json.dumps(result))
        self.assertIn("[redacted]", json.dumps(result))

    def test_plan_input_resolution_filters_disabled_and_unknown_tools(self):
        tool = self.root / "subprojects" / "tools" / "sysstat"
        tool.mkdir(parents=True)
        (tool / "rickshaw.json").write_text('{"tool":"sysstat"}', encoding="utf-8")
        endpoints = self.root / "subprojects" / "core" / "rickshaw" / "endpoints"
        endpoint = endpoints / "remotehosts"
        endpoint.mkdir(parents=True)
        (endpoint / "remotehosts.py").write_text("def validate(): pass\n", encoding="utf-8")
        (self.root / "subprojects" / "core" / "rickshaw" / "schema" / "remotehosts.json").write_text(
            "{}", encoding="utf-8"
        )
        document = {
            "endpoints": [{"type": "remotehosts"}],
            "tool-params": [
                {"tool": "sysstat"},
                {"tool": "disabled-unknown", "enabled": "no"},
                {"tool": "missing"},
            ],
        }
        plan = {
            "validation": {"valid": True, "errors": []},
            "tools": {
                "mode": "explicit",
                "entries": document["tool-params"],
                "truncated": False,
            },
            "topology": {"confidence": "derived", "warnings": []},
            "limits": {"truncated": False},
        }

        errors = self.operations._validate_and_resolve_plan_inputs(
            document, plan, Mock(), 100
        )

        self.assertEqual(plan["tools"]["entries"], [{"tool": "sysstat"}])
        self.assertTrue(any(error["code"] == "not_found" for error in errors))
        self.assertFalse(any("disabled-unknown" in error["message"] for error in errors))
        self.assertFalse(any("missing" in error["message"] for error in errors))

    def test_tool_parameter_expansion_errors_do_not_echo_rejected_values(self):
        tool = self.root / "subprojects" / "tools" / "sysstat"
        tool.mkdir(parents=True)
        (tool / "rickshaw.json").write_text('{"tool":"sysstat"}', encoding="utf-8")
        (tool / "multiplex.json").write_text("{}", encoding="utf-8")
        secret = "unmarked-multiplex-rejection-secret"
        document = {
            "endpoints": [],
            "tool-params": [{"tool": "sysstat", "params": [{"val": secret}]}],
        }
        plan = {
            "validation": {"valid": True, "errors": []},
            "tools": {"mode": "explicit", "entries": [], "truncated": False},
            "topology": {"confidence": "derived", "warnings": []},
            "limits": {"truncated": False},
        }
        multiplex = SimpleNamespace(
            __name__="test_multiplex",
            apply_flat_params=Mock(side_effect=ValueError(secret)),
        )

        errors = self.operations._validate_and_resolve_plan_inputs(
            document, plan, multiplex, 100
        )

        self.assertNotIn(secret, json.dumps(errors))
        self.assertEqual(errors, [{
            "code": "invalid_tool_params",
            "message": "tool sysstat parameter expansion failed",
        }])

    def test_plan_input_resolution_marks_unknown_endpoint_types(self):
        secret = "unmarked-endpoint-secret"
        document = {
            "endpoints": [{"type": secret}],
            "tool-params": [],
        }
        plan = {
            "validation": {"valid": True, "errors": []},
            "tools": {"mode": "explicit", "entries": [], "truncated": False},
            "topology": {"confidence": "derived", "warnings": []},
            "limits": {"truncated": False},
        }

        errors = self.operations._validate_and_resolve_plan_inputs(
            document, plan, Mock(), 100
        )

        self.assertNotIn(secret, json.dumps(errors))
        self.assertTrue(any("endpoint type is not installed" == error["message"] for error in errors))
        self.assertEqual(plan["topology"]["confidence"], "unknown")

    def test_duplicate_tool_ids_do_not_echo_user_values(self):
        tool = self.root / "subprojects" / "tools" / "sysstat"
        tool.mkdir(parents=True)
        (tool / "rickshaw.json").write_text('{"tool":"sysstat"}', encoding="utf-8")
        secret = "unmarked-tool-id-secret"
        document = {
            "endpoints": [],
            "tool-params": [
                {"tool": "sysstat", "id": f"sysstat-{secret}"},
                {"tool": "sysstat", "id": f"sysstat-{secret}"},
            ],
        }
        plan = {
            "validation": {"valid": True, "errors": []},
            "tools": {"mode": "explicit", "entries": [], "truncated": False},
            "topology": {"confidence": "derived", "warnings": []},
            "limits": {"truncated": False},
        }

        errors = self.operations._validate_and_resolve_plan_inputs(
            document, plan, Mock(), 100
        )

        self.assertNotIn(secret, json.dumps(errors))
        self.assertTrue(any(error["message"] == "tool ids must be unique" for error in errors))

    def test_plan_input_resolution_validates_endpoint_schema(self):
        endpoints = self.root / "subprojects" / "core" / "rickshaw" / "endpoints"
        schemas = self.root / "subprojects" / "core" / "rickshaw" / "schema"
        endpoint = endpoints / "remotehosts"
        endpoint.mkdir(parents=True)
        (endpoint / "remotehosts.py").write_text("def validate(): pass\n", encoding="utf-8")
        (schemas / "remotehosts.json").write_text(
            json.dumps({
                "type": "object",
                "properties": {"type": {"const": "remotehosts"}, "remotes": {"type": "array"}},
                "required": ["type", "remotes"],
            }),
            encoding="utf-8",
        )
        document = {"endpoints": [{"type": "remotehosts"}], "tool-params": []}
        plan = {
            "validation": {"valid": True, "errors": []},
            "tools": {"mode": "explicit", "entries": [], "truncated": False},
            "topology": {"confidence": "derived", "warnings": []},
            "limits": {"truncated": False},
        }

        errors = self.operations._validate_and_resolve_plan_inputs(
            document, plan, Mock(), 100
        )

        self.assertTrue(any(error["code"] == "invalid_endpoint" for error in errors))
        self.assertEqual(plan["topology"]["confidence"], "unknown")

    def test_plan_input_resolution_applies_tool_multiplexing(self):
        tool = self.root / "subprojects" / "tools" / "sysstat"
        tool.mkdir(parents=True)
        (tool / "rickshaw.json").write_text('{"tool":"sysstat"}', encoding="utf-8")
        (tool / "multiplex.json").write_text('{}', encoding="utf-8")
        document = {
            "endpoints": [],
            "tool-params": [{"tool": "sysstat", "params": []}],
        }
        plan = {
            "validation": {"valid": True, "errors": []},
            "tools": {"mode": "explicit", "entries": [], "truncated": False},
            "topology": {"confidence": "derived", "warnings": []},
            "limits": {"truncated": False},
        }
        multiplex = Mock()
        multiplex.apply_flat_params.return_value = [{"arg": "interval", "val": "3"}]

        errors = self.operations._validate_and_resolve_plan_inputs(
            document, plan, multiplex, 100
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            plan["tools"]["entries"],
            [{"tool": "sysstat", "params": [{"arg": "interval", "val": "3"}]}],
        )
        multiplex.apply_flat_params.assert_called_once_with([], {})

    def test_invalid_tool_params_schema_is_structured(self):
        schema_path = (
            self.root
            / "subprojects"
            / "core"
            / "rickshaw"
            / "schema"
            / "tool-params.json"
        )
        schema_path.write_text(json.dumps({"type": "not-a-json-schema-type"}), encoding="utf-8")

        self.assertEqual(
            self.operations._validate_tool_params_schema([]),
            [{
                "code": "planner_unavailable",
                "message": "tool-params schema is unavailable",
            }],
        )

    def test_tool_params_schema_errors_do_not_echo_rejected_values(self):
        secret = "unmarked-tool-parameter-secret"

        errors = self.operations._validate_tool_params_schema([secret])

        self.assertEqual(errors, [{
            "code": "invalid_tool_params",
            "message": "0: does not satisfy the type constraint",
        }])
        self.assertNotIn(secret, json.dumps(errors))

    def test_plan_input_resolution_preserves_default_tool_truncation(self):
        tool = self.root / "subprojects" / "tools" / "sysstat"
        tool.mkdir(parents=True)
        (tool / "rickshaw.json").write_text('{"tool":"sysstat"}', encoding="utf-8")
        document = {"endpoints": []}
        plan = {
            "validation": {"valid": True, "errors": []},
            "tools": {
                "mode": "default",
                "entries": [{"tool": "sysstat"}],
                "truncated": True,
            },
            "topology": {"confidence": "derived", "warnings": []},
            "limits": {"truncated": True},
        }

        errors = self.operations._validate_and_resolve_plan_inputs(
            document, plan, Mock(), 1
        )

        self.assertEqual(errors, [])
        self.assertTrue(plan["tools"]["truncated"])

    def test_planner_diagnostics_are_suppressed(self):
        logger_name = "test.multiplex"
        logger = logging.getLogger(logger_name)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            with self.operations._suppress_planner_diagnostics(
                SimpleNamespace(__name__=logger_name)
            ):
                logger.warning("raw-secret-value")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        self.assertEqual(stream.getvalue(), "")

    def test_planner_api_validation_rejects_incomplete_multiplex(self):
        incomplete = SimpleNamespace(expand_parameters=lambda *args: [])
        with self.assertRaises(ImportError):
            self.operations._require_planner_api(incomplete)

    def test_tool_multiplex_expansion_is_serialized(self):
        tool = self.root / "subprojects" / "tools" / "sysstat"
        tool.mkdir(parents=True)
        (tool / "rickshaw.json").write_text('{"tool":"sysstat"}', encoding="utf-8")
        (tool / "multiplex.json").write_text("{}", encoding="utf-8")
        document = {
            "endpoints": [],
            "tool-params": [{"tool": "sysstat", "params": []}],
        }

        calls = 0
        calls_lock = threading.Lock()
        first_started = threading.Event()
        release_first = threading.Event()

        def apply_flat_params(params, requirements):
            nonlocal calls
            with calls_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                first_started.set()
                release_first.wait(timeout=2)
            return [{"arg": "interval", "val": str(call_number)}]

        multiplex = Mock()
        multiplex.apply_flat_params.side_effect = apply_flat_params

        def resolve_plan():
            plan = {
                "validation": {"valid": True, "errors": []},
                "tools": {"mode": "explicit", "entries": [], "truncated": False},
                "topology": {"confidence": "derived", "warnings": []},
                "limits": {"truncated": False},
            }
            return self.operations._validate_and_resolve_plan_inputs(
                document, plan, multiplex, 100
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(resolve_plan)
            self.assertTrue(first_started.wait(timeout=1))
            second = executor.submit(resolve_plan)
            try:
                time.sleep(0.05)
                with calls_lock:
                    self.assertEqual(calls, 1)
            finally:
                release_first.set()
            self.assertEqual(first.result(timeout=1), [])
            self.assertEqual(second.result(timeout=1), [])

    def test_plan_input_resolution_bounds_tool_work_before_validation(self):
        document = {
            "endpoints": [],
            "tool-params": [{"tool": f"missing-{index}"} for index in range(1000)],
        }
        plan = {
            "validation": {"valid": True, "errors": []},
            "tools": {"mode": "explicit", "entries": [], "truncated": False},
            "topology": {"confidence": "derived", "warnings": []},
            "limits": {"truncated": False},
        }

        errors = self.operations._validate_and_resolve_plan_inputs(
            document, plan, Mock(), 1
        )

        self.assertEqual(len(errors), 1)
        self.assertTrue(plan["tools"]["truncated"])
        self.assertTrue(plan["limits"]["truncated"])

    def test_plan_input_resolution_validates_tools_beyond_response_prefix(self):
        tool = self.root / "subprojects" / "tools" / "sysstat"
        tool.mkdir(parents=True)
        (tool / "rickshaw.json").write_text('{"tool":"sysstat"}', encoding="utf-8")
        document = {
            "endpoints": [],
            "tool-params": [{"tool": "sysstat"}, {"tool": "missing"}],
        }
        plan = {
            "validation": {"valid": True, "errors": []},
            "tools": {"mode": "explicit", "entries": [], "truncated": False},
            "topology": {"confidence": "derived", "warnings": []},
            "limits": {"truncated": False},
        }

        errors = self.operations._validate_and_resolve_plan_inputs(
            document, plan, Mock(), 1
        )

        self.assertTrue(any("1 requested tool(s) are not installed" == error["message"] for error in errors))
        self.assertNotIn("missing", json.dumps(errors))
        self.assertEqual(plan["tools"]["entries"], [{"tool": "sysstat"}])
        self.assertTrue(plan["tools"]["truncated"])

    def test_list_endpoints_returns_safe_metadata_and_capabilities(self):
        endpoints = self.root / "subprojects" / "core" / "rickshaw" / "endpoints"
        schemas = self.root / "subprojects" / "core" / "rickshaw" / "schema"
        endpoint = endpoints / "remotehosts"
        endpoint.mkdir(parents=True)
        (endpoint / "remotehosts.py").write_text(
            "def validate():\n    pass\n\ndef engine_init():\n    pass\n\ndef test_start():\n    pass\n\ndef test_stop():\n    pass\n\ndef remote_cleanup():\n    pass\n",
            encoding="utf-8",
        )
        (schemas / "remotehosts.json").write_text(
            json.dumps({
                "title": "Remotehosts Endpoint",
                "description": "Deploy engines over SSH using password=endpoint-secret.",
                "properties": {"hosts": {}, "user": {}},
            }),
            encoding="utf-8",
        )
        (endpoints / "not-an-endpoint").mkdir()
        (endpoints / "not-an-endpoint" / "not-an-endpoint.py").write_bytes(b"\xff")
        shell_endpoint = endpoints / "shell"
        shell_endpoint.mkdir()
        (shell_endpoint / "shell").write_text(
            "#!/bin/sh\n"
            "do_validate=0\n"
            "function endpoint_shell_engine_init() { :; }\n"
            "function endpoint_shell_test_start() { :; }\n"
            "function endpoint_shell_test_stop() { :; }\n"
            "function endpoint_shell_cleanup() { :; }\n",
            encoding="utf-8",
        )
        (schemas / "shell.json").write_text("[]", encoding="utf-8")
        missing_schema = endpoints / "missing-schema"
        missing_schema.mkdir()
        (missing_schema / "missing-schema.py").write_text(
            "def validate():\n    pass\n", encoding="utf-8"
        )
        secret_name = "ghp_" + "A" * 36
        secret_endpoint = endpoints / secret_name
        secret_endpoint.mkdir()
        (secret_endpoint / f"{secret_name}.py").write_text(
            "def validate():\n    pass\n", encoding="utf-8"
        )
        (schemas / f"{secret_name}.json").write_text("{}", encoding="utf-8")

        result = self.operations.list_endpoints()

        self.assertEqual(result["count"], 4)
        self.assertFalse(result["complete"])
        self.assertNotIn(secret_name, json.dumps(result))
        remotehosts = next(item for item in result["endpoints"] if item["name"] == "remotehosts")
        self.assertEqual(
            remotehosts["implementation"],
            "subprojects/core/rickshaw/endpoints/remotehosts/remotehosts.py",
        )
        self.assertEqual(
            remotehosts["schema"],
            {
                "path": "subprojects/core/rickshaw/schema/remotehosts.json",
                "title": "Remotehosts Endpoint",
                "description": "Deploy engines over SSH using password=[redacted]",
                "properties": ["hosts", "user"],
            },
        )
        self.assertNotIn("endpoint-secret", json.dumps(remotehosts["schema"]))
        self.assertEqual(
            remotehosts["capabilities"],
            ["validate", "engine_deployment", "test_lifecycle", "cleanup"],
        )
        shell = next(item for item in result["endpoints"] if item["name"] == "shell")
        self.assertEqual(
            shell["implementation"],
            "subprojects/core/rickshaw/endpoints/shell/shell",
        )
        self.assertIsNone(shell["schema"])
        self.assertEqual(
            shell["capabilities"],
            ["validate", "engine_deployment", "test_lifecycle", "cleanup"],
        )
        broken = next(item for item in result["endpoints"] if item["name"] == "not-an-endpoint")
        self.assertEqual(broken["capabilities"], [])
        missing = next(item for item in result["endpoints"] if item["name"] == "missing-schema")
        self.assertIsNone(missing["schema"])
        self.assertEqual(missing["capabilities"], ["validate"])

    def test_list_endpoints_handles_unactivated_subprojects(self):
        crucible_home = self.root / "without-activated-subprojects"
        rickshaw_parent = crucible_home / "subprojects" / "core"
        rickshaw_parent.mkdir(parents=True)
        (rickshaw_parent / "rickshaw").symlink_to(
            crucible_home / "repos" / "missing-rickshaw", target_is_directory=True
        )

        operations = CrucibleOperations(crucible_home, InputPolicy([]))

        self.assertEqual(
            operations.list_endpoints(),
            {"endpoints": [], "count": 0, "complete": True},
        )

    def test_list_endpoints_marks_credential_shaped_name_omission_incomplete(self):
        endpoint_root = (
            self.root / "subprojects" / "core" / "rickshaw" / "endpoints"
        )
        schema_root = self.root / "subprojects" / "core" / "rickshaw" / "schema"
        token = "ghp_" + "A" * 36
        endpoint = endpoint_root / token
        endpoint.mkdir(parents=True)
        (endpoint / f"{token}.py").write_text(
            "def validate():\n    pass\n", encoding="utf-8"
        )
        (schema_root / f"{token}.json").write_text("{}", encoding="utf-8")

        result = self.operations.list_endpoints()

        self.assertEqual(result, {"endpoints": [], "count": 0, "complete": False})
        self.assertNotIn(token, json.dumps(result))

    def test_list_indexed_results_queries_cdm_with_bounded_filters(self):
        response = Mock()
        response.__enter__ = lambda value: response
        response.__exit__ = lambda *args: None
        response.read.return_value = b'{"runIds":["run-1","run-2"]}'
        with patch("crucible_mcp.operations.urlopen", return_value=response) as request:
            result = self.operations.list_indexed_results(benchmark="fio", limit=1)

        self.assertEqual(result, {"run_ids": ["run-1"], "count": 1})
        self.assertIn("benchmark=fio", request.call_args.args[0].full_url)

    def test_list_indexed_results_redacts_credential_shaped_ids(self):
        token = "ghp_" + "A" * 36
        with patch.object(
            self.operations,
            "_cdm_request",
            return_value={"runIds": [token, "ordinary-run"]},
        ):
            result = self.operations.list_indexed_results()

        self.assertNotIn(token, json.dumps(result))
        self.assertEqual(result["run_ids"], ["[redacted]", "ordinary-run"])

    def test_get_indexed_result_assembles_cdm_metadata(self):
        token = "ghp_" + "A" * 36
        payloads = {
            "/api/v1/run/run-1/tags": {
                "tags": ["nightly", "password:cdm-tag-secret", token]
            },
            "/api/v1/run/run-1/benchmark": {"benchmark": token},
            "/api/v1/run/run-1/partial-status": {"status": "complete"},
            "/api/v1/run/run-1/iterations": {"iterations": [token]},
            "/api/v1/run/run-1/metric-sources": {"sources": [token]},
        }
        with patch.object(self.operations, "list_indexed_results", return_value={"run_ids": ["run-1"]}), \
                patch.object(self.operations, "list_indexed_periods", return_value={"periods": []}), \
                patch.object(self.operations, "_cdm_request", side_effect=payloads.get) as request:
            result = self.operations.get_indexed_result("run-1")

        self.assertEqual(
            result["tags"],
            ["nightly", "password:[redacted]", "[redacted]"],
        )
        self.assertNotIn(token, json.dumps(result))
        self.assertNotIn("cdm-tag-secret", json.dumps(result))
        self.assertEqual(result["benchmark"], "[redacted]")
        self.assertEqual(result["partial_status"], {"status": "complete"})
        self.assertEqual(result["periods"], [])
        self.assertEqual(request.call_count, 5)

    def test_empty_indexed_periods_redacts_credential_shaped_run_id(self):
        token = "ghp_" + "A" * 36
        with patch.object(self.operations, "_cdm_request", return_value={"iterations": []}):
            result = self.operations.list_indexed_periods(token)

        self.assertNotIn(token, json.dumps(result))
        self.assertEqual(result, {"run_id": "[redacted]", "periods": []})

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

    def test_tag_reads_redact_credential_named_tag_values(self):
        run_directory = self.root / "run" / "credential-tag"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps({"tags": [{"name": "password", "val": "tag-secret"}]}),
            encoding="utf-8",
        )

        listed = self.operations.list_local_run_tags(run_directory)
        initial_run_listing = self.operations.list_local_runs()
        added = self.operations.add_local_run_tags(run_directory, ["password:new-secret"])
        updated_run_listing = self.operations.list_local_runs()
        removed = self.operations.remove_local_run_tags(run_directory, ["password"])

        for result in (listed, initial_run_listing, added, updated_run_listing, removed):
            self.assertNotIn("tag-secret", json.dumps(result))
            self.assertNotIn("new-secret", json.dumps(result))

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

    def test_local_run_discovery_omits_credential_shaped_names_and_ids(self):
        run_root = self.root / "run"
        token = "ghp_" + "A" * 36
        run_root.mkdir()
        (run_root / token).mkdir()
        safe_metadata = run_root / "safe-run" / "run" / "rickshaw-run.json"
        safe_metadata.parent.mkdir(parents=True)
        safe_metadata.write_text(
            json.dumps({"run-id": token, "tags": []}), encoding="utf-8"
        )
        ordinary = run_root / "z-ordinary"
        ordinary.mkdir()

        result = self.operations.list_local_runs()

        serialized = json.dumps(result)
        self.assertNotIn(token, serialized)
        self.assertEqual([entry["name"] for entry in result["runs"]], ["z-ordinary"])

    def test_list_local_runs_limit_includes_incomplete_artifacts(self):
        run_root = self.root / "run"
        for name in ("partial-one", "partial-two", "partial-three"):
            (run_root / name).mkdir(parents=True)

        result = self.operations.list_local_runs(limit=1)

        self.assertEqual(result["count"], 1)

    def test_list_local_runs_paginates_after_incomplete_entry(self):
        run_root = self.root / "run"
        (run_root / "a-incomplete").mkdir(parents=True)
        complete_metadata = run_root / "b-complete" / "run" / "rickshaw-run.json"
        complete_metadata.parent.mkdir(parents=True)
        complete_metadata.write_text(json.dumps({"run-id": "run-1", "tags": []}), encoding="utf-8")

        first = self.operations.list_local_runs(limit=1)
        second = self.operations.list_local_runs(limit=1, offset=first["next_offset"])

        self.assertFalse(first["complete"])
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual(second["runs"][0]["name"], "b-complete")

    def test_list_local_runs_marks_resolution_races_incomplete(self):
        run_root = self.root / "run"
        (run_root / "a-raced").mkdir(parents=True)
        (run_root / "b-present").mkdir(parents=True)

        with patch(
            "crucible_mcp.operations.Path.resolve",
            side_effect=[OSError("directory disappeared"), run_root / "b-present"],
        ), patch.object(
            self.operations,
            "_load_run_metadata",
            return_value=(
                run_root / "b-present" / "run" / "rickshaw-run.json",
                {"run-id": "run-1", "tags": []},
            ),
        ):
            result = self.operations.list_local_runs()

        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_offset"], 2)

    def test_list_local_runs_bounds_empty_response_for_large_request_id(self):
        request_id = "x" * 1_048_300
        with self.assertRaisesRegex(OperationError, "response exceeds size limit"):
            self.operations.list_local_runs(request_id=request_id)

        (self.root / "run").mkdir()
        with self.assertRaisesRegex(OperationError, "response exceeds size limit"):
            self.operations.list_local_runs(offset=1, request_id=request_id)

    def test_list_local_runs_bounds_incomplete_entry_response(self):
        run_root = self.root / "run"
        for index in range(100):
            (run_root / f"incomplete-{index:03d}").mkdir(parents=True)

        with patch("crucible_mcp.operations.MAX_METADATA_RESPONSE_BYTES", 10_000):
            result = self.operations.list_local_runs(
                limit=100, request_id="request-id"
            )

        self.assertLessEqual(
            self.operations._mcp_response_size(result, "request-id"),
            10_000,
        )
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertGreater(result["next_offset"], 0)

    def test_list_local_runs_bounds_large_tag_response(self):
        run_directory = self.root / "run" / "large-tags"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps({"run-id": "large-tags", "tags": [{"name": "tag", "val": "x" * 900_000}]}),
            encoding="utf-8",
        )

        result = self.operations.list_local_runs(request_id="request-id")

        self.assertLessEqual(
            self.operations._mcp_response_size(result, "request-id"),
            MAX_METADATA_RESPONSE_BYTES,
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(result["runs"][0]["tags"], [])
        self.assertTrue(result["runs"][0]["tags_truncated"])

    def test_list_local_runs_paginates_when_response_budget_is_exhausted(self):
        run_root = self.root / "run"
        for index in range(1100):
            metadata_path = run_root / f"run-{index:04d}" / "run" / "rickshaw-run.json"
            metadata_path.parent.mkdir(parents=True)
            metadata_path.write_text(
                json.dumps({"run-id": f"run-{index}", "tags": [{"name": "tag", "val": "x" * 2_000}]}),
                encoding="utf-8",
            )

        first = self.operations.list_local_runs(request_id="request-id")
        second = self.operations.list_local_runs(
            offset=first["next_offset"], request_id="request-id"
        )

        self.assertLessEqual(
            self.operations._mcp_response_size(first, "request-id"),
            MAX_METADATA_RESPONSE_BYTES,
        )
        self.assertFalse(first["complete"])
        self.assertGreater(first["next_offset"], first["offset"])
        self.assertLessEqual(
            self.operations._mcp_response_size(second, "request-id"),
            MAX_METADATA_RESPONSE_BYTES,
        )
        self.assertEqual(
            {run["name"] for run in first["runs"]}
            & {run["name"] for run in second["runs"]},
            set(),
        )

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

    def test_get_local_run_summary_redacts_credential_fields(self):
        run_directory = self.root / "run" / "summary-with-credentials"
        summary_path = run_directory / "run" / "result-summary.json"
        summary_path.parent.mkdir(parents=True)
        summary_path.write_text(
            json.dumps(
                {
                    "benchmark": "fio",
                    "configuration": {"password": "summary-secret"},
                }
            ),
            encoding="utf-8",
        )

        result = self.operations.get_local_run_summary(run_directory)

        self.assertEqual(
            result["summary"]["configuration"]["password"], "[redacted]"
        )
        self.assertNotIn("summary-secret", json.dumps(result))

    def test_get_local_run_summary_bounds_serialized_response(self):
        run_directory = self.root / "run" / "large-summary"
        summary_path = run_directory / "run" / "result-summary.json"
        summary_path.parent.mkdir(parents=True)
        summary_path.write_text(
            json.dumps({"payload": "x" * 700_000}), encoding="utf-8"
        )

        with self.assertRaises(OperationError) as raised:
            self.operations.get_local_run_summary(
                run_directory, request_id="large-summary-request"
            )
        self.assertEqual(raised.exception.code, "result_too_large")

    def test_get_local_run_metadata_reads_local_artifact(self):
        run_directory = self.root / "run" / "metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "run-id": "run-2",
                    "benchmarks": [],
                    "roadblock-password": "do-not-return",
                    "accessToken": "do-not-return",
                    "clientSecret": "do-not-return",
                    "pull-secrets": ["do-not-return"],
                    "api-key": "do-not-return",
                    "apiKey": "do-not-return",
                    "access-key": "do-not-return",
                    "apikey": "do-not-return",
                    "accesskey": "do-not-return",
                    "secretkey": "do-not-return",
                    "clientsecret": "do-not-return",
                    "passwd": "do-not-return",
                    "dbpassword": "do-not-return",
                    "roadblockpassword": "do-not-return",
                    "registrycredentials": "do-not-return",
                    "DB_PASS": "do-not-return",
                    "user-pass": "do-not-return",
                    "serialized-json": '{"password":"do-not-return"}',
                    "embedded-json": "--config '{\"password\":\"do-not-return\"}'",
                    "escaped-config": "--config \"{'password':'do-not-return'}\"",
                    "mixed-malformed-json": (
                        'prefix {"safe":1} suffix {"password":"do-not-return",}'
                    ),
                    "quoted-assignment": 'prefix "password": "do-not-return"',
                    "escaped-assignment": r"\u0070assword=do-not-return",
                    "escaped-shell-option": r"--pa\ssword=do-not-return",
                    "quoted-shell-option": '--pa"ssword"=do-not-return',
                    "ansi-c-shell-option": "--pa$'ss'word=do-not-return",
                    "single-quoted-shell-option": "--'pass'word=do-not-return",
                    "command-shell-option": "--$(printf password)=do-not-return",
                    "command-shell-option-with-space": (
                        "--$(printf password) do-not-return"
                    ),
                    "embedded-command-shell-option": (
                        "--pa$(printf ss)word do-not-return"
                    ),
                    "command-shell-colon-option": (
                        "--pa$(printf ss)word:do-not-return"
                    ),
                    "bracketed-option": "password[0]=do-not-return",
                    "backtick-shell-option": "--pa`printf ss`word do-not-return",
                    "parameter-expansion-shell-option": (
                        "--pa${SUFFIX}word=do-not-return"
                    ),
                    "parameter-expansion-colon-option": (
                        "--pa${SUFFIX}ssword:do-not-return"
                    ),
                    "unprefixed-command-shell-option": (
                        "pa$(printf ss)word do-not-return"
                    ),
                    "unprefixed-backtick-shell-option": (
                        "pa`printf ss`word do-not-return"
                    ),
                    "unprefixed-parameter-expansion": (
                        "pa${SUFFIX}word do-not-return"
                    ),
                    "ansi-c-hex-shell-option": r"--$'\x70assword' do-not-return",
                    "ansi-c-octal-shell-option": r"--$'\160assword' do-not-return",
                    "ansi-c-shell-word": "$'password' do-not-return",
                    "quoted-double-shell-word": '"password" do-not-return',
                    "quoted-single-shell-word": "'password' do-not-return",
                    "command-shell-word": "$(printf password) do-not-return",
                    "assembled-shell-assignment": (
                        'x "pa"ssword=do-not-return'
                    ),
                    "underscore-assignment": "_PASSWORD=do-not-return",
                    "underscore-option": "--_password do-not-return",
                    "whitespace-shell-option": "--'pass'word do-not-return",
                    "whitespace-escaped-shell-option": (
                        r"--pa\ssword do-not-return"
                    ),
                    "escaped-shell-assignment": r"pa\ssword=do-not-return",
                    "quoted-shell-assignment": 'pa"ss"word=do-not-return',
                    "line-continuation-assignment": (
                        "pa\\" + "\n" + "ssword=do-not-return"
                    ),
                    "repository-url": (
                        "https://user:do-not-return@example.com/repository"
                    ),
                    "repository-url-with-at": (
                        "https://user:P@SSWORD@example.com/repository"
                    ),
                    "escaped-unicode-malformed": (
                        r'prefix {"\u0070wd":"do-not-return",}'
                    ),
                    "embedded-json-with-assignment": (
                        'prefix {"password":"do-not-return"} password=do-not-return'
                    ),
                    "nested-descriptor-name-list": [
                        {"name": ["password"]},
                        "do-not-return",
                    ],
                    "structured-name-list-object": {
                        "name": ["password"],
                        "value": "do-not-return",
                    },
                    "descriptor-with-payload": {
                        "name": "password",
                        "payload": "do-not-return",
                    },
                    "nested-descriptor-list": {
                        "args": [{"name": "password"}],
                        "value": "do-not-return",
                    },
                    "parameters-with-value": {
                        "parameters": [{"name": "password"}],
                        "value": "do-not-return",
                    },
                    "params-with-value": {
                        "params": [{"name": "password"}],
                        "value": "do-not-return",
                    },
                    "parameters-with-payload": {
                        "parameters": [{"name": "password"}],
                        "payload": "do-not-return",
                    },
                    "header-descriptor": {
                        "authorization_header": "Authorization",
                        "value": "do-not-return",
                    },
                    "header-descriptor-payload": {
                        "authorization_header": "Authorization",
                        "payload": "do-not-return",
                    },
                    "escaped-descriptor-name": {
                        r"\u006eame": "password",
                        "value": "do-not-return",
                    },
                    "root-bare-header-descriptor": {
                        "benchmarks": [],
                        "headers": [{"name": "password"}],
                        "opaque": "do-not-return",
                    },
                    "escaped-unicode-key": {
                        r"\u0070assword": "do-not-return",
                    },
                    "malformed-json": '{"password":"do-not-return",}',
                    "malformed-api-json": '{"apiKey":"do-not-return",}',
                    "quoted-access-assignment": (
                        'prefix "access-key": "do-not-return"'
                    ),
                    "option-array": [
                        "--password",
                        "do-not-return",
                        "--token",
                        "do-not-return",
                    ],
                    "option-array-with-dash": ["--password", "-secret"],
                    "option-array-with-other": ["--password", "--other", "secret"],
                    "structured-list-args": {"key": "password", "args": ["secret"]},
                    "structured-list-option": {"option": ["password", "secret"]},
                    "nested-option-object": ["--password", {"value": "secret"}],
                    "header-option": ["Authorization", "Bearer", "secret"],
                    "request-headers": [
                        {"header": "Authorization", "value": "do-not-return"}
                    ],
                    "sequenced-header": [
                        {"header": "Authorization"},
                        "do-not-return",
                    ],
                    "sequenced-argument": [
                        {"argument": "password"},
                        "do-not-return",
                    ],
                    "sequenced-params": [
                        {"params": "password"},
                        "do-not-return",
                    ],
                    "prefixed-header-sequence": [
                        "prefix",
                        {"header": "Authorization"},
                        "Bearer",
                        "do-not-return",
                    ],
                    "structured-sensitive-key-sequence": {
                        "args": [{"password": "password"}, "do-not-return"]
                    },
                    "split-structured-list": [
                        {"name": "password"},
                        "do-not-return",
                    ],
                    "nested-split-list": [["password"], "do-not-return"],
                    "nested-non-leading-list": [
                        ["command", "password"],
                        "do-not-return",
                    ],
                    "nested-nested-descriptor": [
                        [{"name": "password"}],
                        "do-not-return",
                    ],
                    "non-leading-sensitive-list": [
                        "command",
                        "password",
                        "secret",
                        "tail",
                    ],
                    "parameters": [
                        {"arg": "password", "val": "do-not-return"},
                        {"argument": "password", "value": "do-not-return"},
                        {"key": "password", "value": "do-not-return"},
                        {"option": "password", "value": "do-not-return"},
                        {"flag": "api-key", "value": "do-not-return"},
                        {"arg": "pwd", "val": "do-not-return"},
                        {"param": "password", "value": "do-not-return"},
                        {"field": "password", "argument": "do-not-return"},
                        {"arg": "password", "values": ["do-not-return"]},
                        {"parameterName": "password", "parameterValue": "do-not-return"},
                        [
                            "prefix",
                            {"authorization": "Authorization"},
                            "Bearer",
                            "do-not-return",
                        ],
                        '"pa"ssword do-not-return',
                        "$'pa'ssword do-not-return",
                        "$(printf pa)ssword do-not-return",
                        "`printf pa`ssword do-not-return",
                        "$(printf pa)-ssword do-not-return",
                        "`printf pa`.ssword do-not-return",
                        "password.foo=do-not-return",
                        "password.foo: do-not-return",
                        r"$(printf 'pa\n')ssword do-not-return",
                        r"`printf 'pa\n'`ssword do-not-return",
                        r'--pa"$SUFFIX"ssword do-not-return',
                        "--$(printf pa\n)ssword do-not-return",
                        "`printf pa\n`ssword do-not-return",
                        '--"pa\\\n"ssword do-not-return',
                    ],
                    "opts": (
                        "host=example password=do-not-return api-key=do-not-return "
                        "--api-key whitespace-secret --password whitespace-secret "
                        "--roadblock-password compound-secret --client-secret client-secret "
                        "--roadblockPassword camel-secret --registryCredentials registry-secret "
                        r'''--password='a'"'"'b' --token "quoted secret" --access-token="a\"b" --password=secret,remaining --roadblockpassword lower-secret --registrycredentials lower-secret --password --token separate-secret --password=-secret --token=-another-secret --password -secret Authorization: Bearer super-secret Authorization=Bearer secret --region us-east-1 DB_PASS=env-secret --pass option-secret user-pass=user-secret pwd=serialized-secret --password multiword secret --region us-east-1 --bearer=bearer-secret'''
                    ),
                    "registries": {
                        "public": {
                            "push-token": "do-not-return",
                            "token-file": "/secret/token",
                        }
                    },
                    "endpoints": [
                        {"auth": {"password": "do-not-return"}},
                        {"opts": "--password.foo=do-not-return"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(result["metadata_path"], str(metadata_path))
        self.assertEqual(result["metadata"]["run-id"], "run-2")
        self.assertEqual(result["metadata"]["roadblock-password"], "[redacted]")
        self.assertEqual(result["metadata"]["accessToken"], "[redacted]")
        self.assertEqual(result["metadata"]["clientSecret"], "[redacted]")
        self.assertEqual(result["metadata"]["pull-secrets"], "[redacted]")
        self.assertEqual(result["metadata"]["api-key"], "[redacted]")
        self.assertEqual(result["metadata"]["apiKey"], "[redacted]")
        self.assertEqual(result["metadata"]["access-key"], "[redacted]")
        self.assertEqual(result["metadata"]["apikey"], "[redacted]")
        self.assertEqual(result["metadata"]["accesskey"], "[redacted]")
        self.assertEqual(result["metadata"]["secretkey"], "[redacted]")
        self.assertEqual(result["metadata"]["clientsecret"], "[redacted]")
        self.assertEqual(result["metadata"]["passwd"], "[redacted]")
        self.assertEqual(result["metadata"]["dbpassword"], "[redacted]")
        self.assertEqual(result["metadata"]["roadblockpassword"], "[redacted]")
        self.assertEqual(result["metadata"]["registrycredentials"], "[redacted]")
        self.assertEqual(result["metadata"]["DB_PASS"], "[redacted]")
        self.assertEqual(result["metadata"]["user-pass"], "[redacted]")
        self.assertEqual(
            result["metadata"]["serialized-json"], '{"password":"[redacted]"}'
        )
        self.assertEqual(
            result["metadata"]["embedded-json"],
            "--config '{\"password\":\"[redacted]\"}'",
        )
        self.assertEqual(result["metadata"]["escaped-config"], "[redacted]")
        self.assertEqual(result["metadata"]["mixed-malformed-json"], "[redacted]")
        self.assertEqual(result["metadata"]["quoted-assignment"], "[redacted]")
        self.assertEqual(result["metadata"]["escaped-assignment"], "[redacted]")
        self.assertEqual(
            result["metadata"]["escaped-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["quoted-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["ansi-c-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["single-quoted-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["command-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["command-shell-option-with-space"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["embedded-command-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["command-shell-colon-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["bracketed-option"],
            "password[0]=[redacted]",
        )
        self.assertEqual(
            result["metadata"]["backtick-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameter-expansion-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameter-expansion-colon-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["unprefixed-command-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["unprefixed-backtick-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["unprefixed-parameter-expansion"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["ansi-c-hex-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["ansi-c-octal-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["ansi-c-shell-word"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["quoted-double-shell-word"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["quoted-single-shell-word"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["command-shell-word"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["assembled-shell-assignment"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["underscore-assignment"], "_PASSWORD=[redacted]"
        )
        self.assertEqual(
            result["metadata"]["underscore-option"], "--_password [redacted]"
        )
        self.assertEqual(
            result["metadata"]["whitespace-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["whitespace-escaped-shell-option"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["escaped-shell-assignment"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["quoted-shell-assignment"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["line-continuation-assignment"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["repository-url"],
            "https://[redacted]@example.com/repository",
        )
        self.assertEqual(
            result["metadata"]["repository-url-with-at"],
            "https://[redacted]@example.com/repository",
        )
        self.assertEqual(
            result["metadata"]["escaped-unicode-malformed"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["embedded-json-with-assignment"],
            'prefix {"password":"[redacted]"} password=[redacted]',
        )
        self.assertEqual(
            result["metadata"]["nested-descriptor-name-list"],
            [{"name": ["password"]}, "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["structured-name-list-object"],
            {"name": ["password"], "value": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["descriptor-with-payload"],
            {"name": "password", "payload": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["nested-descriptor-list"],
            {"args": [{"name": "password"}], "value": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["parameters-with-value"],
            {"parameters": [{"name": "password"}], "value": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["params-with-value"],
            {"params": [{"name": "password"}], "value": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["parameters-with-payload"],
            {"parameters": [{"name": "password"}], "payload": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["header-descriptor"],
            {"authorization_header": "[redacted]", "value": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["header-descriptor-payload"],
            {"authorization_header": "[redacted]", "payload": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["escaped-descriptor-name"],
            {r"\u006eame": "password", "value": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["root-bare-header-descriptor"],
            {
                "benchmarks": [],
                "headers": [{"name": "password"}],
                "opaque": "[redacted]",
            },
        )
        self.assertEqual(
            result["metadata"]["escaped-unicode-key"][r"\u0070assword"],
            "[redacted]",
        )
        self.assertEqual(result["metadata"]["malformed-json"], "[redacted]")
        self.assertEqual(
            result["metadata"]["malformed-api-json"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["quoted-access-assignment"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["option-array"],
            ["--password", "[redacted]", "--token", "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["option-array-with-dash"],
            ["--password", "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["option-array-with-other"],
            ["--password", "[redacted]", "secret"],
        )
        self.assertEqual(
            result["metadata"]["structured-list-args"],
            {"key": "password", "args": "[redacted]"},
        )
        self.assertEqual(
            result["metadata"]["structured-list-option"],
            {"option": ["password", "[redacted]"]},
        )
        self.assertEqual(
            result["metadata"]["nested-option-object"],
            ["--password", "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["header-option"],
            ["Authorization", "[redacted]", "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["request-headers"],
            [{"header": "Authorization", "value": "[redacted]"}],
        )
        self.assertEqual(
            result["metadata"]["sequenced-header"],
            [{"header": "Authorization"}, "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["sequenced-argument"],
            [{"argument": "password"}, "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["sequenced-params"],
            [{"params": "password"}, "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["prefixed-header-sequence"],
            ["prefix", {"header": "Authorization"}, "[redacted]", "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["structured-sensitive-key-sequence"],
            {"args": [{"password": "[redacted]"}, "[redacted]"]},
        )
        self.assertEqual(
            result["metadata"]["split-structured-list"],
            [{"name": "password"}, "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["nested-split-list"],
            [["password"], "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["nested-non-leading-list"],
            [["command", "password"], "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["nested-nested-descriptor"],
            [[{"name": "password"}], "[redacted]"],
        )
        self.assertEqual(
            result["metadata"]["non-leading-sensitive-list"],
            ["command", "password", "[redacted]", "tail"],
        )
        self.assertEqual(result["metadata"]["parameters"][0]["arg"], "password")
        self.assertEqual(result["metadata"]["parameters"][0]["val"], "[redacted]")
        self.assertEqual(result["metadata"]["parameters"][1]["argument"], "password")
        self.assertEqual(result["metadata"]["parameters"][1]["value"], "[redacted]")
        self.assertEqual(result["metadata"]["parameters"][2]["key"], "password")
        self.assertEqual(result["metadata"]["parameters"][2]["value"], "[redacted]")
        self.assertEqual(result["metadata"]["parameters"][3]["option"], "password")
        self.assertEqual(result["metadata"]["parameters"][3]["value"], "[redacted]")
        self.assertEqual(result["metadata"]["parameters"][4]["flag"], "api-key")
        self.assertEqual(result["metadata"]["parameters"][4]["value"], "[redacted]")
        self.assertEqual(result["metadata"]["parameters"][5]["arg"], "pwd")
        self.assertEqual(result["metadata"]["parameters"][5]["val"], "[redacted]")
        self.assertEqual(result["metadata"]["parameters"][6]["param"], "password")
        self.assertEqual(result["metadata"]["parameters"][6]["value"], "[redacted]")
        self.assertEqual(
            result["metadata"]["parameters"][7]["argument"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][8]["values"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][9]["parameterValue"], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][10],
            [
                "prefix",
                {"authorization": "[redacted]"},
                "[redacted]",
                "[redacted]",
            ],
        )
        self.assertEqual(
            result["metadata"]["parameters"][11], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][12], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][13], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][14], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][15], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][16], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][17], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][18], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][19], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][20], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][21], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][22], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][23], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["parameters"][24], "[redacted]"
        )
        self.assertEqual(
            result["metadata"]["opts"],
            "host=example password=[redacted] api-key=[redacted] "
            "--api-key [redacted] --password [redacted] "
            "--roadblock-password [redacted] --client-secret [redacted] "
            "--roadblockPassword [redacted] --registryCredentials [redacted] "
            "--password=[redacted] --token [redacted] --access-token=[redacted] "
            "--password=[redacted] --roadblockpassword [redacted] "
            "--registrycredentials [redacted] --password [redacted]",
        )
        self.assertEqual(result["metadata"]["registries"]["public"]["push-token"], "[redacted]")
        self.assertEqual(result["metadata"]["registries"]["public"]["token-file"], "[redacted]")
        self.assertEqual(result["metadata"]["endpoints"][0]["auth"], "[redacted]")
        self.assertEqual(result["metadata"]["endpoints"][1]["opts"], "[redacted]")

    def test_redacts_jwt_and_bearer_credentials(self):
        self.assertEqual(
            self.operations._redact_metadata(
                {"arg": "jwt", "val": "do-not-return"}
            ),
            {"arg": "jwt", "val": "[redacted]"},
        )
        self.assertEqual(
            self.operations._redact_metadata("--bearer=do-not-return"),
            "--bearer=[redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata(
                {"creds": "do-not-return", "registry-creds": "do-not-return"}
            ),
            {"creds": "[redacted]", "registry-creds": "[redacted]"},
        )
        self.assertEqual(
            self.operations._redact_metadata(
                {
                    "signature": "do-not-return",
                    "hmac": "do-not-return",
                    "sig": "do-not-return",
                }
            ),
            {
                "signature": "[redacted]",
                "hmac": "[redacted]",
                "sig": "[redacted]",
            },
        )
        self.assertEqual(
            self.operations._redact_metadata("--creds user:do-not-return"),
            "--creds [redacted]",
        )
        self.assertEqual(
            self.operations.redact_log_text(
                "https://storage.example/blob?sv=2026&sig=do-not-return"
            ),
            "https://storage.example/blob?sv=2026&sig=[redacted]",
        )

    def test_get_local_run_metadata_redacts_cookie_credentials(self):
        run_directory = self.root / "run" / "cookie-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "run-id": "cookie-run",
                    "headers": {
                        "Cookie": "session=COOKIESECRET",
                        "session": "SESSIONSECRET",
                    },
                    "opts": "Cookie: session=COOKIESECRET",
                }
            ),
            encoding="utf-8",
        )

        result = self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(
            result["metadata"]["headers"],
            {"Cookie": "[redacted]", "session": "[redacted]"},
        )
        self.assertEqual(result["metadata"]["opts"], "[redacted]")

    def test_redacts_short_password_options(self):
        self.assertEqual(
            self.operations._redact_metadata("mysql -pSECRET"),
            "mysql -p[redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("mysql -p SECRET"),
            "mysql -p [redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("mysql -p=SECRET"),
            "mysql -p=[redacted]",
        )

        self.assertEqual(
            self.operations._redact_metadata(["mysql", "-p", "SECRET"]),
            ["mysql", "-p", "[redacted]"],
        )
        self.assertEqual(
            self.operations._redact_metadata("mysql -apSECRET"),
            "mysql -ap[redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("curl -auadmin:SECRET"),
            "curl -au[redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("-api-key S3CR3T"),
            "-api-key [redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("-password RANDOMVALUE"),
            "-password [redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("-auth SENTINEL"),
            "-auth [redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("-api_key S3CR3T"),
            "-api_key [redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("-apikey S3CR3T"),
            "-apikey [redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("mysql -P 3306"),
            "mysql -P 3306",
        )
        self.assertEqual(
            self.operations._redact_metadata(["mysql", "-ap", "SECRET"]),
            ["mysql", "-ap", "[redacted]"],
        )

    def test_redacts_user_password_options(self):
        self.assertEqual(
            self.operations._redact_metadata("curl -u user:SECRET"),
            "curl -u [redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("curl --user=user:SECRET"),
            "curl --user=[redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata(["curl", "-u", "user:SECRET"]),
            ["curl", "-u", "[redacted]"],
        )

    def test_redacts_structured_user_credentials(self):
        self.assertEqual(
            self.operations._redact_metadata({"user": "admin:SECRET"}),
            {"user": "admin:[redacted]"},
        )
        self.assertEqual(
            self.operations._redact_metadata(
                {"name": "user", "value": "admin:SECRET"}
            ),
            {"name": "user", "value": "admin:[redacted]"},
        )
        self.assertEqual(
            self.operations._redact_metadata('{"user":"admin:SECRET"}'),
            '{"user":"admin:[redacted]"}',
        )
        self.assertEqual(
            self.operations._redact_metadata("username=admin:SECRET"),
            "username=admin:[redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("user admin:SECRET"),
            "user admin:[redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("username admin:SECRET"),
            "username admin:[redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata("--username admin:SECRET"),
            "--username admin:[redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata(["user", "admin:SECRET"]),
            ["user", "[redacted]"],
        )
        self.assertEqual(
            self.operations._redact_metadata(["--username", "admin:SECRET"]),
            ["--username", "[redacted]"],
        )
        self.assertEqual(
            self.operations._redact_metadata('"user": "admin:SECRET"'),
            '"user": "admin:[redacted]"',
        )

    def test_redacts_short_option_descriptor_values(self):
        metadata = {
            "iterations": [
                {
                    "params": [
                        {"arg": "-p", "val": "SECRET"},
                        {"arg": "-u", "val": "admin:SECRET"},
                        {"arg": "-ap", "val": "SECRET"},
                    ]
                }
            ]
        }

        self.assertEqual(
            self.operations._redact_metadata(metadata),
            {
                "iterations": [
                    {
                        "params": [
                            {"arg": "-p", "val": "[redacted]"},
                            {"arg": "-u", "val": "[redacted]"},
                            {"arg": "-ap", "val": "[redacted]"},
                        ]
                    }
                ]
            },
        )

    def test_redacts_option_looking_sensitive_values(self):
        self.assertEqual(
            self.operations._redact_metadata("--password --SECRET"),
            "--password [redacted]",
        )

    def test_redacts_unlabelled_common_credential_tokens(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiJhbGljZSJ9."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        standalone_tokens = (
            jwt,
            "github_pat_" + "A" * 30,
            *(prefix + "A" * 36 for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
            "glpat-" + "A" * 30,
            "xoxb-" + "A" * 30,
            "xapp-" + "A" * 30,
            "AKIA" + "A" * 16,
            "ASIA" + "A" * 16,
            "AIza" + "A" * 30,
            "sk-" + "A" * 30,
            "sk-proj-" + "A" * 30,
            "sk_live_" + "A" * 30,
            "rk_test_" + "A" * 30,
        )
        for token in standalone_tokens:
            with self.subTest(token_prefix=token[:8]):
                self.assertNotIn(token, self.operations._redact_metadata(token))
                self.assertNotIn(token, self.operations.redact_log_text(token))
        keyed_token = "ghp_" + "B" * 36
        keyed_result = self.operations._redact_metadata({keyed_token: "safe value"})
        self.assertNotIn(keyed_token, json.dumps(keyed_result))
        self.assertEqual(list(keyed_result.values()), ["safe value"])
        self.assertEqual(
            self.operations._redact_metadata("--password --token"),
            "--password [redacted]",
        )
        self.assertEqual(
            self.operations._redact_metadata(["--password", "--SECRET"]),
            ["--password", "[redacted]"],
        )
        self.assertEqual(
            self.operations._redact_metadata(["--password", "--abc"]),
            ["--password", "[redacted]"],
        )
        self.assertEqual(
            self.operations._redact_metadata(["--password", "--token", "SECRET"]),
            ["--password", "[redacted]", "[redacted]"],
        )
        self.assertEqual(
            self.operations._redact_metadata(
                ["--password", "--token", "--region", "SECRET"]
            ),
            ["--password", "[redacted]", "[redacted]", "SECRET"],
        )
        self.assertEqual(
            self.operations._redact_metadata(
                "--password --token --region SECRET"
            ),
            "--password [redacted]",
        )

    def test_redacts_container_valued_sensitive_descriptors(self):
        self.assertEqual(
            self.operations._redact_metadata(
                [
                    "prefix",
                    {"authorization": ["Authorization"]},
                    "Bearer",
                    "do-not-return",
                ]
            ),
            [
                "prefix",
                {"authorization": "[redacted]"},
                "[redacted]",
                "[redacted]",
            ],
        )
        self.assertEqual(
            self.operations._redact_metadata(
                [
                    "prefix",
                    {"password": ["password"]},
                    "Bearer",
                    "do-not-return",
                ]
            ),
            [
                "prefix",
                {"password": "[redacted]"},
                "[redacted]",
                "[redacted]",
            ],
        )

    def test_redacts_root_container_descriptor_siblings(self):
        self.assertEqual(
            self.operations._redact_metadata(
                {
                    "benchmarks": [],
                    "authorization": ["Authorization"],
                    "config": "Bearer secret",
                }
            ),
            {
                "benchmarks": [],
                "authorization": "[redacted]",
                "config": "[redacted]",
            },
        )

    def test_bounds_adversarial_shell_redaction_work(self):
        value = ('"' + ("a" * 256) + "$" + ("b" * 256) + "x") * 2020
        with self.assertRaises(OperationError) as raised:
            self.operations._redact_metadata(value)

        self.assertEqual(raised.exception.code, "result_too_large")

    def test_redaction_handles_large_unmatched_quote_tokens(self):
        value = "x {" + ('"' * 10_000)

        self.assertEqual(self.operations._redact_metadata(value), value)

    def test_redaction_work_limit_does_not_mask_later_safe_metadata(self):
        metadata = {
            "first": "a" * 12_000,
            "second": "b" * 5_000,
            "later-safe": "preserve-this-value",
        }

        self.assertEqual(self.operations._redact_metadata(metadata), metadata)

    def test_redaction_work_limit_preserves_long_plain_text(self):
        value = "plain-text " * 2_000

        self.assertGreater(len(value), MAX_METADATA_REDACTION_WORK)
        self.assertEqual(self.operations._redact_metadata(value), value)

        prose = "This explains that a field name is documented here. " * 500
        self.assertGreater(len(prose), MAX_METADATA_REDACTION_WORK)
        self.assertEqual(self.operations._redact_metadata(prose), prose)

        safe_option = "plain-text " * 2_000 + "--region us-east-1"
        self.assertEqual(self.operations._redact_metadata(safe_option), safe_option)

    def test_get_local_run_metadata_rejects_long_attached_credentials(self):
        run_directory = self.root / "run" / "long-attached-credential-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps({
                    "opts": (
                        "plain-text " * 2_000
                    + "mysql -p9f81d2 curl -uadmin:9f81d2 password SECRET "
                    "DB_PASS=S3CR3T password_secret=S3CR3T "
                    "--password.foo=S3CR3T password[0]=S3CR3T"
                )
            }),
            encoding="utf-8",
        )

        with self.assertRaises(OperationError) as raised:
            self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(raised.exception.code, "result_too_large")

    def test_long_metadata_scans_past_benign_prefixes(self):
        value = (
            "plain-text " * 2_000
            + "password is a field name; password ACTUAL_SECRET "
            + "DB_PASS SECRET clientSecret SECRET username=admin:SECRET "
            + 'clientsecret SECRET {"name":"password","value":"SECRET"} '
            + '{"user":"admin:SECRET"} '
            + "'password': RANDOMVALUE pa\"ss\"word RANDOMVALUE "
            + "-token SECRET -token=SECRET"
        )

        with self.assertRaises(OperationError) as raised:
            self.operations._redact_metadata(value)

        self.assertEqual(raised.exception.code, "result_too_large")

        for suffix in ("client-secret SECRET", "DB_PASS SECRET", "password_secret SECRET"):
            with self.assertRaises(OperationError) as raised:
                self.operations._redact_metadata("x" * 17_000 + " " + suffix)
            self.assertEqual(raised.exception.code, "result_too_large")

    def test_get_local_run_metadata_rejects_excessive_nesting(self):
        run_directory = self.root / "run" / "deep-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata: dict[str, object] = {"run-id": "run-deep"}
        cursor = metadata
        for index in range(MAX_METADATA_DEPTH + 1):
            child: dict[str, object] = {f"level-{index}": {}}
            cursor.update(child)
            cursor = child[f"level-{index}"]  # type: ignore[assignment]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with self.assertRaises(OperationError) as raised:
            self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(raised.exception.code, "result_too_large")

    def test_get_local_run_metadata_redacts_root_descriptor_unknown_payload(self):
        run_directory = self.root / "run" / "root-descriptor-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "benchmarks": [],
                    "parameters": [{"name": "password"}],
                    "payload": "do-not-return",
                }
            ),
            encoding="utf-8",
        )

        result = self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(
            result["metadata"],
            {
                "benchmarks": [],
                "parameters": [{"name": "password"}],
                "payload": "[redacted]",
            },
        )

    def test_redaction_fails_closed_at_embedded_fragment_limit(self):
        value = "prefix " + "{}" * (MAX_METADATA_JSON_FRAGMENTS + 1)

        self.assertEqual(CrucibleOperations._redact_metadata(value), "[redacted]")

    def test_get_local_run_metadata_redacts_root_sensitive_key_payload(self):
        run_directory = self.root / "run" / "root-sensitive-key-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "benchmarks": [],
                    "authorization": "Authorization",
                    "payload": "do-not-return",
                    "value": "do-not-return",
                }
            ),
            encoding="utf-8",
        )

        result = self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(
            result["metadata"],
            {
                "benchmarks": [],
                "authorization": "[redacted]",
                "payload": "[redacted]",
                "value": "[redacted]",
            },
        )

    def test_get_local_run_metadata_redacts_root_sensitive_key_opaque(self):
        run_directory = self.root / "run" / "root-sensitive-opaque-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "benchmarks": [],
                    "authorization": "Authorization",
                    "opaque": "do-not-return",
                    "run-id": "root-sensitive-opaque",
                    "benchmark": "fio",
                    "samples": [],
                }
            ),
            encoding="utf-8",
        )

        result = self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(
            result["metadata"],
            {
                "benchmarks": [],
                "authorization": "[redacted]",
                "opaque": "[redacted]",
                "run-id": "root-sensitive-opaque",
                "benchmark": "fio",
                "samples": [],
            },
        )

    def test_get_local_run_metadata_maps_parser_recursion_error(self):
        run_directory = self.root / "run" / "parser-deep-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        depth = 5000
        metadata_path.write_text(
            '{"level":' * depth + "null" + "}" * depth,
            encoding="utf-8",
        )

        with self.assertRaises(OperationError) as raised:
            self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(raised.exception.code, "result_too_large")

    def test_get_local_run_metadata_maps_embedded_json_recursion_error(self):
        run_directory = self.root / "run" / "embedded-deep-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        depth = 5000
        embedded = '{"level":' * depth + "null" + "}" * depth
        metadata_path.write_text(json.dumps({"embedded": embedded}), encoding="utf-8")

        with self.assertRaises(OperationError) as raised:
            self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(raised.exception.code, "result_too_large")

    def test_load_run_metadata_maps_embedded_json_recursion_error(self):
        run_directory = self.root / "run" / "loader-embedded-deep-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        depth = 10000
        metadata_path.write_text(
            '{"level":' * depth + "null" + "}" * depth,
            encoding="utf-8",
        )

        with self.assertRaises(OperationError) as raised:
            self.operations._load_run_metadata(run_directory)

        self.assertEqual(raised.exception.code, "result_too_large")

    def test_get_local_run_metadata_maps_oversized_integer_error(self):
        run_directory = self.root / "run" / "oversized-integer-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            '{"value":' + ("9" * 5000) + "}",
            encoding="utf-8",
        )

        with self.assertRaises(OperationError) as raised:
            self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(raised.exception.code, "invalid_run")

    def test_get_local_run_metadata_redacts_compressed_artifact(self):
        run_directory = self.root / "run" / "compressed-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json.xz"
        metadata_path.parent.mkdir(parents=True)
        with lzma.open(metadata_path, "wt", encoding="utf-8") as stream:
            json.dump(
                {
                    "run-id": "run-compressed",
                    "password": "do-not-return",
                    "nested": {"accessToken": "do-not-return"},
                    "serialized-json": '{"token":"do-not-return"}',
                },
                stream,
            )

        result = self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(result["metadata"]["run-id"], "run-compressed")
        self.assertEqual(result["metadata"]["password"], "[redacted]")
        self.assertEqual(result["metadata"]["nested"]["accessToken"], "[redacted]")
        self.assertEqual(
            result["metadata"]["serialized-json"], '{"token":"[redacted]"}'
        )

    def test_get_local_run_metadata_bounds_decompressed_compressed_artifact(self):
        run_directory = self.root / "run" / "small-compressed-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json.xz"
        metadata_path.parent.mkdir(parents=True)
        with lzma.open(metadata_path, "wt", encoding="utf-8") as stream:
            json.dump({"run-id": "compressed-small"}, stream)

        decoded_size = len('{"run-id": "compressed-small"}')
        self.assertGreater(metadata_path.stat().st_size, decoded_size + 1)
        result = self.operations.get_local_run_metadata(
            run_directory, max_bytes=decoded_size + 1
        )

        self.assertEqual(result["metadata"]["run-id"], "compressed-small")

    def test_get_local_run_metadata_uses_bounded_xz_decoder(self):
        run_directory = self.root / "run" / "bounded-decoder-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json.xz"
        metadata_path.parent.mkdir(parents=True)
        with lzma.open(metadata_path, "wt", encoding="utf-8") as stream:
            json.dump({"run-id": "bounded-decoder"}, stream)

        with patch(
            "crucible_mcp.operations.lzma.LZMADecompressor",
            wraps=lzma.LZMADecompressor,
        ) as decoder:
            result = self.operations.get_local_run_metadata(run_directory)

        decoder.assert_called_once_with(
            format=lzma.FORMAT_XZ,
            memlimit=MAX_METADATA_DECOMPRESSOR_MEMORY,
        )
        self.assertEqual(result["metadata"]["run-id"], "bounded-decoder")

    def test_get_local_run_metadata_accepts_high_preset_xz(self):
        run_directory = self.root / "run" / "high-preset-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json.xz"
        metadata_path.parent.mkdir(parents=True)
        with lzma.open(metadata_path, "wt", encoding="utf-8", preset=9) as stream:
            json.dump({"run-id": "high-preset"}, stream)

        result = self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(result["metadata"]["run-id"], "high-preset")

    def test_get_local_run_metadata_rejects_expanded_redacted_response(self):
        run_directory = self.root / "run" / "expanded-metadata-run"
        metadata_path = run_directory / "run" / "rickshaw-run.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            json.dumps({"entries": [{"password": "x"} for _ in range(40000)]}),
            encoding="utf-8",
        )

        with self.assertRaises(OperationError) as raised:
            self.operations.get_local_run_metadata(run_directory)

        self.assertEqual(raised.exception.code, "result_too_large")

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
            with self.assertRaises(OperationError) as raised:
                self.operations.list_run_artifacts(run_directory)

        self.assertEqual(raised.exception.code, "artifact_traversal_failed")

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

    def test_list_run_artifacts_advances_after_iterator_errors(self):
        run_directory = self.root / "run" / "iterator-error"
        (run_directory / "run" / "iterations").mkdir(parents=True)

        class FailingIterator:
            def __next__(self):
                raise OSError("iterator failed")

            def close(self):
                return None

        real_scandir = os.scandir
        calls = 0

        def failing_scandir(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                return FailingIterator()
            return real_scandir(path)

        with patch("crucible_mcp.operations.os.scandir", side_effect=failing_scandir):
            result = self.operations.list_run_artifacts(run_directory)

        self.assertFalse(result["complete"])
        self.assertGreater(result["next_offset"], result["offset"])

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

    def test_get_run_artifact_redacts_credentials_across_lines_and_pages(self):
        run_directory = self.root / "run" / "artifact-credential-redaction"
        artifact = run_directory / "run" / "iterations" / "output.txt"
        artifact.parent.mkdir(parents=True)
        content = (
            "safe preface\n"
            "password: |\n"
            "  artifact-yaml-secret\n"
            "  artifact-yaml-continuation\n"
            "username: visible-user\n"
            "{\"token\": {\n"
            "  \"value\": \"artifact-json-secret\"\n"
            "}}\n"
            "AWS_SECRET_ACCESS_KEY=artifact-env-secret\n"
            "safe ending\n"
        )
        encoded = content.encode("utf-8")
        artifact.write_bytes(encoded)

        result = self.operations.get_run_artifact(
            run_directory, "run/iterations/output.txt"
        )

        self.assertEqual(result["next_offset"], len(encoded))
        self.assertTrue(result["complete"])
        for secret in (
            "artifact-yaml-secret",
            "artifact-yaml-continuation",
            "artifact-json-secret",
            "artifact-env-secret",
        ):
            self.assertNotIn(secret, result["text"])
        self.assertIn("safe preface", result["text"])
        self.assertIn("visible-user", result["text"])
        self.assertIn("safe ending", result["text"])

        secret_offset = encoded.index(b"artifact-json-secret") + 8
        page = self.operations.get_run_artifact(
            run_directory,
            "run/iterations/output.txt",
            offset=secret_offset,
            limit=12,
        )
        self.assertNotIn("artifact-json-secret", page["text"])
        self.assertEqual(page["next_offset"], secret_offset + 12)

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

    def test_artifact_names_do_not_expose_credential_material(self):
        run_directory = self.root / "run" / "artifact-token-name"
        token = "ghp_" + "A" * 36
        sensitive_paths = (
            f"run/tool-data/{token}.txt",
            "run/tool-data/api-key=unmarkedvalue.txt",
        )
        for relative in sensitive_paths:
            artifact_path = run_directory / relative
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text("ordinary content", encoding="utf-8")

        listed = self.operations.list_run_artifacts(run_directory)

        self.assertNotIn(token, json.dumps(listed))
        self.assertEqual(listed["artifacts"], [])
        for relative in sensitive_paths:
            with self.subTest(relative=relative):
                with self.assertRaises(OperationError) as rejected:
                    self.operations.get_run_artifact(run_directory, relative)
                self.assertEqual(rejected.exception.code, "artifact_not_retrievable")

    def test_summary_redacts_credential_shaped_top_level_keys(self):
        token = "ghp_" + "A" * 36

        result = self.operations._redact_summary({
            token: "unclassified value",
            "password=top-level-secret": "also-secret",
            "safe": "ordinary diagnostic",
        })

        serialized = json.dumps(result)
        self.assertNotIn(token, serialized)
        self.assertNotIn("top-level-secret", serialized)
        self.assertNotIn("also-secret", serialized)
        self.assertEqual(result["safe"], "ordinary diagnostic")

        paired = self.operations._redact_summary({
            "arg": "password",
            "val": "top-level-paired-secret",
            "safe": "safe sibling",
        })
        self.assertNotIn("top-level-paired-secret", json.dumps(paired))
        self.assertEqual(paired["val"], "[redacted]")
        self.assertEqual(paired["safe"], "safe sibling")

        colliding = self.operations._redact_summary({
            token: "first value",
            "[redacted]": "second value",
        })
        self.assertEqual(colliding, {
            "[redacted]": "first value",
            "[redacted] (2)": "second value",
        })

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

    def test_get_run_artifact_rejects_files_larger_than_redaction_budget(self):
        run_directory = self.root / "run" / "artifact-redaction-budget"
        artifact = run_directory / "run" / "iterations" / "large.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("safe text", encoding="utf-8")

        with patch(
            "crucible_mcp.operations.MAX_ARTIFACT_REDACTION_BYTES", 4
        ):
            with self.assertRaises(OperationError) as raised:
                self.operations.get_run_artifact(
                    run_directory, "run/iterations/large.txt"
                )
            listing = self.operations.list_run_artifacts(run_directory)

        self.assertEqual(raised.exception.code, "result_too_large")
        listed = next(item for item in listing["artifacts"] if item["name"] == "large.txt")
        self.assertFalse(listed["retrievable"])

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

    def test_nul_run_paths_are_structured_operation_errors(self):
        invalid_path = Path(f"{self.root}/run\x00invalid")
        for operation in (
            lambda: self.operations.get_local_run_summary(invalid_path),
            lambda: self.operations.get_local_run_metadata(invalid_path),
            lambda: self.operations.get_run_artifact(invalid_path, "run/result-summary.json"),
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

    def test_local_archive_discovery_omits_credential_shaped_names(self):
        archive_root = self.root / "archive"
        archive_root.mkdir()
        token = "ghp_" + "A" * 36
        (archive_root / f"{token}.tar.xz").write_bytes(b"archive")
        (archive_root / "ordinary.tar.xz").write_bytes(b"archive")

        result = self.operations.list_local_archives()

        self.assertNotIn(token, json.dumps(result))
        self.assertEqual([archive["name"] for archive in result["archives"]], [
            "ordinary.tar.xz"
        ])

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
        secret = "metric-response-secret"
        response.read.return_value = json.dumps({
            "values": {
                "": [{"begin": 1, "value": f"password={secret}", "duration": 42.5}]
            },
            "usedBreakouts": [],
            "remainingBreakouts": [],
        }).encode("utf-8")
        with patch("crucible_mcp.operations.urlopen", return_value=response) as request:
            result = self.operations.get_indexed_metric(
                run="run-1", source="fio", metric_type="IOPS", period="measurement"
            )

        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["values"][""][0]["value"], "password=[redacted]")
        self.assertEqual(result["values"][""][0]["duration"], 42.5)
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
        secret = "log-query-secret"
        sensitive_query = operations.search_logs(f"password={secret}")
        self.assertNotIn(secret, json.dumps(sensitive_query))
        self.assertEqual(sensitive_query["query"], "password=[redacted]")

    def test_logger_results_redact_credentials_and_private_key_pages(self):
        database = self.root / "sensitive-logs.db"
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
            CREATE TABLE lines (id INTEGER PRIMARY KEY, session INTEGER, stream INTEGER, timestamp, line TEXT);
            CREATE INDEX idx_lines_session_stream_id ON lines (session, stream, id);
            INSERT INTO streams VALUES (1, 'STDOUT');
            INSERT INTO sources VALUES (1, 'runner');
            INSERT INTO commands VALUES (1, 'crucible run --roadblock-passwd=command-secret');
            INSERT INTO sessions VALUES (1, 'sensitive-session', 't0', 1, 1);
            INSERT INTO lines VALUES (1, 1, 1, 1, 'remotehosts --roadblock-passwd=line-secret');
            INSERT INTO lines VALUES (2, 1, 1, 2, '-----BEGIN OPENSSH PRIVATE KEY-----');
            INSERT INTO lines VALUES (3, 1, 1, 3, 'private-key-material');
            INSERT INTO lines VALUES (4, 1, 1, 4, '-----END OPENSSH PRIVATE KEY-----');
            INSERT INTO lines VALUES (5, 1, 1, 5, '-----BEGIN OPENSSH PRIVATE KEY-----');
            INSERT INTO lines VALUES (6, 1, 1, 6, '-----END RSA PRIVATE KEY-----');
            INSERT INTO lines VALUES (7, 1, 1, 7, 'payload-after-mismatched-end');
            INSERT INTO lines VALUES (8, 1, 1, 8, '-----END OPENSSH PRIVATE KEY-----');
            INSERT INTO lines VALUES (9, 1, 1, 9, '-----BEGIN RSA PRIVATE KEY-----');
            INSERT INTO lines VALUES (10, 1, 1, 10, '-----BEGIN OPENSSH PRIVATE KEY----- -----END OPENSSH PRIVATE KEY-----');
            INSERT INTO lines VALUES (11, 1, 1, 11, 'rsa-payload-after-nested-markers');
            INSERT INTO sessions VALUES (2, 'large-history-session', 't1', 1, 1);
            INSERT INTO lines VALUES (12, 2, 1, 12, 'old line one');
            INSERT INTO lines VALUES (13, 2, 1, 13, 'old line two');
            INSERT INTO lines VALUES (14, 2, 1, 14, 'old line three');
            INSERT INTO lines VALUES (15, 2, 1, 15, 'public data after an intentionally capped context');
            INSERT INTO sessions VALUES (3, 'clock-step-session', 't2', 1, 1);
            INSERT INTO lines VALUES (16, 3, 1, 10, 'ordinary line in range');
            INSERT INTO lines VALUES (17, 3, 1, 9, '-----BEGIN RSA PRIVATE KEY-----');
            INSERT INTO lines VALUES (18, 3, 1, 11, 'clock-step-key-material');
            INSERT INTO lines VALUES (19, 3, 1, 12, '-----END RSA PRIVATE KEY-----');
            INSERT INTO sessions VALUES (4, 'until-clock-step-session', 't3', 1, 1);
            INSERT INTO lines VALUES (20, 4, 1, 11, '-----BEGIN RSA PRIVATE KEY-----');
            INSERT INTO lines VALUES (21, 4, 1, 9, 'until-only-key-material');
            INSERT INTO lines VALUES (22, 4, 1, 8, '-----END RSA PRIVATE KEY-----');
            """
        )
        connection.commit()
        connection.close()

        operations = CrucibleOperations(self.root, log_db=database)
        session_list = operations.list_log_sessions()
        self.assertNotIn("command-secret", json.dumps(session_list))
        page = operations.get_log_session("sensitive-session", offset=2, limit=1)
        self.assertEqual(page["lines"][0]["line"], "[redacted private key]")
        self.assertNotIn("command-secret", json.dumps(page))
        self.assertNotIn("private-key-material", json.dumps(page))
        search = operations.search_logs("private-key", session_id="sensitive-session")
        self.assertEqual(search["matches"][0]["line"], "[redacted private key]")
        self.assertNotIn("private-key-material", json.dumps(search))
        self.assertNotIn("command-secret", json.dumps(search))

        with patch.object(
            operations,
            "_log_redaction_state_before",
            wraps=operations._log_redaction_state_before,
        ) as context_lookup:
            since_search = operations.search_logs(
                "private-key", session_id="sensitive-session", since=3
            )
        self.assertEqual(context_lookup.call_count, 1)
        self.assertEqual(since_search["matches"][0]["line"], "[redacted private key]")
        clock_step_search = operations.search_logs(
            "clock-step-key-material",
            session_id="clock-step-session",
            since=10,
        )
        self.assertEqual(
            clock_step_search["matches"][0]["line"], "[redacted private key]"
        )
        until_only_search = operations.search_logs(
            "until-only-key-material",
            session_id="until-clock-step-session",
            until=10,
        )
        self.assertEqual(
            until_only_search["matches"][0]["line"], "[redacted private key]"
        )
        mismatched_end_search = operations.search_logs(
            "payload-after",
            session_id="sensitive-session",
            since=7,
        )
        self.assertEqual(
            mismatched_end_search["matches"][0]["line"], "[redacted private key]"
        )
        self.assertNotIn("payload-after-mismatched-end", json.dumps(mismatched_end_search))
        nested_marker_search = operations.search_logs(
            "rsa-payload", session_id="sensitive-session", since=11
        )
        self.assertEqual(nested_marker_search["matches"][0]["line"], "[redacted private key]")
        self.assertNotIn("rsa-payload-after-nested-markers", json.dumps(nested_marker_search))
        with patch("crucible_mcp.operations.MAX_LOG_REDACTION_CONTEXT_LINES", 2):
            capped_history_search = operations.search_logs(
                "public data",
                session_id="large-history-session",
                since=15,
            )
        self.assertEqual(capped_history_search["matches"][0]["line"], "[redacted]")
        credential_search = operations.search_logs(
            "remotehosts", session_id="sensitive-session"
        )
        self.assertNotIn("line-secret", json.dumps(credential_search))

    def test_logger_queries_carry_sensitive_option_state_across_rows(self):
        database = self.root / "multiline-sensitive-logs.db"
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
            CREATE TABLE lines (
                id INTEGER PRIMARY KEY, session INTEGER, timestamp REAL,
                stream INTEGER, line TEXT
            );
            CREATE INDEX idx_lines_session_stream_id ON lines (session, stream, id);
            INSERT INTO sources VALUES (1, 'runner');
            INSERT INTO commands VALUES (1, 'crucible run');
            INSERT INTO sessions VALUES (1, 'multiline-secret-session', 't0', 1, 1);
            INSERT INTO streams VALUES (1, 'STDOUT');
            INSERT INTO lines VALUES (1, 1, 1, 1, 'command --token');
            INSERT INTO lines VALUES (2, 1, 2, 1, 'logger-secret-token');
            INSERT INTO lines VALUES (3, 1, 3, 1, 'safe diagnostic');
            INSERT INTO lines VALUES (4, 1, 4, 1, 'password: |');
            INSERT INTO lines VALUES (5, 1, 5, 1, '  logger-yaml-secret');
            INSERT INTO lines VALUES (6, 1, 6, 1, 'safe yaml diagnostic');
            INSERT INTO lines VALUES (7, 1, 7, 1, 'curl https://alice:\\');
            INSERT INTO lines VALUES (8, 1, 8, 1, 'url-secret@example.com');
            INSERT INTO lines VALUES (9, 1, 9, 1, 'safe after url');
            INSERT INTO sessions VALUES (2, 'inline-sensitive-json', 't1', 1, 1);
            INSERT INTO lines VALUES (10, 2, 10, 1, '{"token": {');
            INSERT INTO lines VALUES (11, 2, 11, 1, '  "value": "inline-json-secret",');
            INSERT INTO lines VALUES (12, 2, 12, 1, '  "nested": {"access": "nested-json-secret"}');
            INSERT INTO lines VALUES (13, 2, 13, 1, '}');
            INSERT INTO lines VALUES (14, 2, 14, 1, '{"safe": "safe sibling"}');
            """
        )
        connection.commit()
        connection.close()

        operations = CrucibleOperations(self.root, log_db=database)
        session = operations.get_log_session("multiline-secret-session")
        session_page = operations.get_log_session(
            "multiline-secret-session", offset=1, limit=1
        )
        search = operations.search_logs(
            "logger-secret-token", session_id="multiline-secret-session"
        )
        since_search = operations.search_logs(
            "logger-secret-token",
            session_id="multiline-secret-session",
            since=2,
        )
        yaml_session = operations.get_log_session("multiline-secret-session")
        yaml_search = operations.search_logs(
            "logger-yaml-secret", session_id="multiline-secret-session", since=5
        )
        url_search = operations.search_logs(
            "url-secret", session_id="multiline-secret-session", since=8
        )
        inline_json_session = operations.get_log_session("inline-sensitive-json")

        self.assertNotIn("logger-secret-token", json.dumps(session))
        self.assertNotIn("logger-secret-token", json.dumps(session_page))
        self.assertNotIn("logger-secret-token", search["matches"][0]["line"])
        self.assertNotIn(
            "logger-secret-token", since_search["matches"][0]["line"]
        )
        self.assertNotIn("logger-yaml-secret", json.dumps(yaml_session))
        self.assertNotIn("logger-yaml-secret", yaml_search["matches"][0]["line"])
        self.assertNotIn("url-secret", json.dumps(session))
        self.assertNotIn("url-secret", url_search["matches"][0]["line"])
        self.assertNotIn("inline-json-secret", json.dumps(inline_json_session))
        self.assertNotIn("nested-json-secret", json.dumps(inline_json_session))
        self.assertIn("safe diagnostic", json.dumps(session))
        self.assertIn("safe yaml diagnostic", json.dumps(yaml_session))
        self.assertIn("safe after url", json.dumps(session))
        self.assertIn("safe sibling", json.dumps(inline_json_session))

    def test_logger_redaction_bounds_private_key_markers_per_line(self):
        marker = "-----BEGIN RSA PRIVATE KEY-----"
        with patch(
            "crucible_mcp.operations.MAX_LOG_PRIVATE_KEY_MARKERS_PER_LINE", 1
        ):
            safe_line, state = self.operations._redact_log_line_with_context(
                f"{marker} {marker}"
            )
            self.assertEqual(safe_line, "[redacted]")
            next_line, next_state = self.operations._redact_log_line_with_context(
                "private-key-material", state
            )
        self.assertEqual(next_line, "[redacted private key]")
        self.assertIsNotNone(next_state)

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

    def test_validate_run_does_not_echo_rejected_values(self):
        secret = "validation-secret-should-not-be-echoed"

        result = self.operations.validate_run({"benchmarks": secret})

        self.assertFalse(result["valid"])
        self.assertNotIn(secret, "\n".join(result["errors"]))
        self.assertIn("benchmarks", result["errors"][0])
        self.assertIn("type", result["errors"][0])

        name_secret = "benchmark-name-secret"
        result = self.operations.validate_run({
            "benchmarks": [{"name": f"password={name_secret}"}]
        })
        self.assertNotIn(name_secret, "\n".join(result["errors"]))

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
