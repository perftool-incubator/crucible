import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "list-subprojects.py")
spec = importlib.util.spec_from_file_location("list_subprojects", MODULE_PATH)
list_subprojects = importlib.util.module_from_spec(spec)
spec.loader.exec_module(list_subprojects)


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "description": {"type": "string"},
        "cdm_indexed": {"type": "boolean"},
    },
    "required": ["tool", "description"],
}

BENCHMARK_SCHEMA = {
    "type": "object",
    "properties": {
        "benchmark": {"type": "string"},
        "description": {"type": "string"},
        "cdm_indexed": {"type": "boolean"},
    },
    "required": ["benchmark", "description"],
}

# A simplified stand-in for multiplex's real req-schema.json, used by
# TestListSubprojects below. multiplex is a separate subproject repo
# (subprojects/core/multiplex/) that isn't checked out in crucible-ci's
# unittest job -- unlike schema/tool-metadata.json and
# schema/benchmark-metadata.json, which live in this repo and are always
# present. TestRealMultiplexSchema further down validates against the
# actual file, but only when it's available locally.
MULTIPLEX_SCHEMA = {
    "type": "object",
    "properties": {
        "presets": {
            "type": "object",
            "patternProperties": {".*": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"arg": {"type": "string"}, "vals": {"type": "array"}},
                    "required": ["arg", "vals"],
                },
            }},
        },
        "validations": {
            "type": "object",
            "patternProperties": {".*": {
                "type": "object",
                "properties": {"args": {"type": "array", "items": {"type": "string"}}},
                "required": ["args"],
            }},
        },
    },
    "required": ["validations"],
}

REAL_MULTIPLEX_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "subprojects", "core", "multiplex", "JSON", "req-schema.json",
)


class TestListSubprojects(unittest.TestCase):

    def setUp(self):
        self.crucible_home = tempfile.mkdtemp()
        list_subprojects.CRUCIBLE_HOME = self.crucible_home

        schema_dir = os.path.join(self.crucible_home, "schema")
        os.makedirs(schema_dir)
        with open(os.path.join(schema_dir, "tool-metadata.json"), "w") as f:
            json.dump(TOOL_SCHEMA, f)
        with open(os.path.join(schema_dir, "benchmark-metadata.json"), "w") as f:
            json.dump(BENCHMARK_SCHEMA, f)

        multiplex_schema_dir = os.path.join(self.crucible_home, "subprojects", "core", "multiplex", "JSON")
        os.makedirs(multiplex_schema_dir)
        with open(os.path.join(multiplex_schema_dir, "req-schema.json"), "w") as f:
            json.dump(MULTIPLEX_SCHEMA, f)
        self.multiplex_schema = MULTIPLEX_SCHEMA

        self.tools_dir = os.path.join(self.crucible_home, "subprojects", "tools")
        self.benchmarks_dir = os.path.join(self.crucible_home, "subprojects", "benchmarks")
        os.makedirs(self.tools_dir)
        os.makedirs(self.benchmarks_dir)

        self.tool_config = list_subprojects.KIND_CONFIG["tool"]
        self.benchmark_config = list_subprojects.KIND_CONFIG["benchmark"]

    def tearDown(self):
        shutil.rmtree(self.crucible_home)

    def make_subproject(self, base_dir, name, rickshaw_field, rickshaw=True, metadata=None, multiplex=None):
        subproject_dir = os.path.join(base_dir, name)
        os.makedirs(subproject_dir, exist_ok=True)
        if rickshaw:
            with open(os.path.join(subproject_dir, "rickshaw.json"), "w") as f:
                json.dump({rickshaw_field: name}, f)
        if metadata is not None:
            config = self.tool_config if base_dir == self.tools_dir else self.benchmark_config
            with open(os.path.join(subproject_dir, config["metadata_filename"]), "w") as f:
                if isinstance(metadata, str):
                    f.write(metadata)
                else:
                    json.dump(metadata, f)
        if multiplex is not None:
            with open(os.path.join(subproject_dir, "multiplex.json"), "w") as f:
                if isinstance(multiplex, str):
                    f.write(multiplex)
                else:
                    json.dump(multiplex, f)
        return subproject_dir

    def test_build_entry_no_rickshaw_json_returns_none(self):
        subproject_dir = os.path.join(self.tools_dir, "broken")
        os.makedirs(subproject_dir)
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        self.assertIsNone(list_subprojects.build_entry(subproject_dir, self.tool_config, schema))

    def test_build_entry_non_dict_rickshaw_json_falls_back(self):
        subproject_dir = os.path.join(self.tools_dir, "listrickshaw")
        os.makedirs(subproject_dir)
        with open(os.path.join(subproject_dir, "rickshaw.json"), "w") as f:
            json.dump(["not", "an", "object"], f)
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema)
        self.assertIsNone(entry)
        self.assertIn("expected a JSON object", stderr.getvalue())

    def test_build_entry_non_dict_multiplex_json_falls_back(self):
        subproject_dir = self.make_subproject(self.tools_dir, "listmultiplex", "tool")
        with open(os.path.join(subproject_dir, "multiplex.json"), "w") as f:
            json.dump(["not", "an", "object"], f)
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema)
        self.assertIsNotNone(entry)
        self.assertIsNone(entry["params"])
        self.assertIn("expected a JSON object", stderr.getvalue())

    def test_one_bad_subproject_does_not_break_the_whole_listing(self):
        # a single malformed multiplex.json (valid JSON, wrong shape) must not
        # prevent collect_entries() from returning every other subproject
        self.make_subproject(self.tools_dir, "goodtool", "tool")
        bad_dir = self.make_subproject(self.tools_dir, "badtool", "tool")
        with open(os.path.join(bad_dir, "multiplex.json"), "w") as f:
            json.dump(["not", "an", "object"], f)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entries = list_subprojects.collect_entries(self.tool_config, None)
        names = sorted(e["name"] for e in entries)
        self.assertEqual(names, ["badtool", "goodtool"])

    def test_build_entry_no_metadata_file_falls_back(self):
        subproject_dir = self.make_subproject(self.tools_dir, "notool", "tool")
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema)
        self.assertEqual(entry["name"], "notool")
        self.assertFalse(entry["has_metadata"])
        self.assertIsNone(entry["description"])
        self.assertEqual(entry["subtools"], [])
        self.assertIsNone(entry["cdm_indexed"])
        self.assertIsNone(entry["params"])

    def test_build_entry_valid_metadata(self):
        subproject_dir = self.make_subproject(
            self.tools_dir, "mytool", "tool",
            metadata={"tool": "mytool", "description": "does things", "cdm_indexed": True},
        )
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema)
        self.assertTrue(entry["has_metadata"])
        self.assertEqual(entry["description"], "does things")
        self.assertTrue(entry["cdm_indexed"])

    def test_build_entry_invalid_json_falls_back(self):
        subproject_dir = self.make_subproject(
            self.tools_dir, "badjson", "tool",
            metadata="{not valid json",
        )
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema)
        self.assertFalse(entry["has_metadata"])
        self.assertIn("not valid JSON", stderr.getvalue())

    def test_build_entry_schema_validation_failure_falls_back(self):
        subproject_dir = self.make_subproject(
            self.tools_dir, "badschema", "tool",
            # missing required 'description' field
            metadata={"tool": "badschema", "cdm_indexed": True},
        )
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema)
        self.assertFalse(entry["has_metadata"])
        self.assertIn("failed schema validation", stderr.getvalue())

    def test_build_entry_multiplex_json_included(self):
        subproject_dir = self.make_subproject(
            self.tools_dir, "withparams", "tool",
            multiplex={"presets": {"defaults": []}, "validations": {}},
        )
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema)
        self.assertEqual(entry["params"], {"presets": {"defaults": []}, "validations": {}})

    def test_build_entry_invalid_multiplex_json_ignored(self):
        subproject_dir = self.make_subproject(
            self.tools_dir, "badmultiplex", "tool",
            multiplex="{not valid json",
        )
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema)
        self.assertIsNone(entry["params"])
        self.assertIn("could not parse", stderr.getvalue())

    def test_build_entry_valid_multiplex_json_passes_schema_validation(self):
        subproject_dir = self.make_subproject(
            self.tools_dir, "schemavalid", "tool",
            multiplex={
                "presets": {"defaults": [{"arg": "interval", "vals": ["3"]}]},
                "validations": {"positive_integer": {"args": ["interval"], "vals": "^[1-9][0-9]*$"}},
            },
        )
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema, self.multiplex_schema)
        self.assertIsNotNone(entry["params"])
        self.assertEqual(entry["params_count"], 1)

    def test_build_entry_malformed_multiplex_shape_rejected_by_schema(self):
        # 'validations' mapped to a list instead of an object -- valid JSON,
        # passes the plain isinstance(dict) check, but violates req-schema.json.
        # This is the exact shape that used to crash count_params() outright
        # (AttributeError: 'list' object has no attribute ...) before schema
        # validation was added to reject it at parse time instead.
        subproject_dir = self.make_subproject(
            self.tools_dir, "schemainvalid", "tool",
            multiplex={"validations": ["not", "a", "dict"]},
        )
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema, self.multiplex_schema)
        self.assertIsNone(entry["params"])
        self.assertIsNone(entry["params_count"])
        self.assertIn("failed schema validation", stderr.getvalue())
        # confirm the whole listing survives -- table rendering must not crash either
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            list_subprojects.print_table([entry], self.tool_config, terminal_width=100)
        self.assertIn("-", stdout.getvalue().splitlines()[2].split())

    def test_collect_entries_name_filter(self):
        self.make_subproject(self.tools_dir, "toolone", "tool")
        self.make_subproject(self.tools_dir, "tooltwo", "tool")
        entries = list_subprojects.collect_entries(self.tool_config, "toolone")
        self.assertEqual([e["name"] for e in entries], ["toolone"])

    def test_collect_entries_benchmark_kind_uses_sub_benchmarks_field(self):
        self.make_subproject(
            self.benchmarks_dir, "mybench", "benchmark",
            metadata={"benchmark": "mybench", "description": "d", "cdm_indexed": False},
        )
        entries = list_subprojects.collect_entries(self.benchmark_config, None)
        self.assertEqual(len(entries), 1)
        self.assertIn("sub-benchmarks", entries[0])
        self.assertNotIn("subtools", entries[0])

    def test_print_table_collapses_embedded_newline(self):
        entries = [{
            "name": "mytool",
            "has_metadata": True,
            "description": "Line one\nLine two continues here",
            "subtools": [],
            "cdm_indexed": False,
            "cdm_sources": [],
            "output_files": [],
            "params": None,
        }]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            list_subprojects.print_table(entries, self.tool_config, terminal_width=100)
        lines = stdout.getvalue().splitlines()
        # header + separator + exactly one data row -- the embedded newline
        # must not have produced an extra line
        self.assertEqual(len(lines), 3)
        self.assertIn("Line one Line two continues here", lines[2])

    def test_print_table_wraps_long_description_at_terminal_width(self):
        entries = [{
            "name": "mytool",
            "has_metadata": True,
            "description": "one two three four five six seven eight nine ten",
            "subtools": [],
            "cdm_indexed": False,
            "cdm_sources": [],
            "output_files": [],
            "params": None,
        }]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            list_subprojects.print_table(entries, self.tool_config, terminal_width=40)
        lines = stdout.getvalue().splitlines()
        # narrow terminal forces the description across multiple lines,
        # each one indented under the Description column with blank cells
        # for Name/Subtools/Metrics/Params
        self.assertGreater(len(lines), 3)
        self.assertTrue(lines[2].startswith("mytool"))
        self.assertFalse(lines[3].startswith("mytool"))

    def test_print_table_metrics_column_counts_cdm_types(self):
        entries = [{
            "name": "mytool",
            "has_metadata": True,
            "description": "does things",
            "subtools": [],
            "cdm_indexed": True,
            "cdm_sources": [{"source": "s", "types": ["a", "b", "c"]}],
            "output_files": [],
            "params": None,
        }]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            list_subprojects.print_table(entries, self.tool_config, terminal_width=100)
        lines = stdout.getvalue().splitlines()
        self.assertIn("3", lines[2].split())

    def test_print_table_metrics_column_sums_across_subtools(self):
        entries = [{
            "name": "mytool",
            "has_metadata": True,
            "description": "does things",
            "subtools": [
                {"name": "a", "cdm_indexed": True, "cdm_sources": [{"source": "a", "types": ["x", "y"]}]},
                {"name": "b", "cdm_indexed": True, "cdm_sources": [{"source": "b", "types": ["z"]}]},
                {"name": "c", "cdm_indexed": False, "cdm_sources": []},
            ],
            "cdm_indexed": None,
            "cdm_sources": [],
            "output_files": [],
            "params": None,
        }]
        self.assertEqual(list_subprojects.count_metrics(entries[0], "subtools"), 3)

    def test_print_table_no_metadata_shows_dashes(self):
        entries = [{
            "name": "mytool",
            "has_metadata": False,
            "description": None,
            "subtools": [],
            "cdm_indexed": None,
            "cdm_sources": [],
            "output_files": [],
            "params": None,
        }]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            list_subprojects.print_table(entries, self.tool_config, terminal_width=100)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[2].split(), ["mytool", "-", "-", "-", "-"])

    def test_count_metrics_no_metadata_returns_none(self):
        entry = {"has_metadata": False, "subtools": [], "cdm_indexed": None, "cdm_sources": []}
        self.assertIsNone(list_subprojects.count_metrics(entry, "subtools"))

    def test_count_metrics_metadata_but_not_indexed_returns_zero(self):
        entry = {"has_metadata": True, "subtools": [], "cdm_indexed": False, "cdm_sources": []}
        self.assertEqual(list_subprojects.count_metrics(entry, "subtools"), 0)

    def test_count_params_no_multiplex_returns_none(self):
        self.assertIsNone(list_subprojects.count_params({"params": None}))

    def test_count_params_counts_distinct_args_across_presets_and_validations(self):
        entry = {"params": {
            "presets": {"defaults": [{"arg": "a", "vals": ["1"]}], "essentials": [{"arg": "b", "vals": ["2"]}]},
            "validations": {"rule": {"args": ["a", "c"], "vals": ".+"}},
        }}
        # a, b, c -- 'a' appears in both a preset and a validation but counts once
        self.assertEqual(list_subprojects.count_params(entry), 3)

    def test_build_entry_includes_metrics_and_params_counts(self):
        subproject_dir = self.make_subproject(
            self.tools_dir, "counted", "tool",
            metadata={"tool": "counted", "description": "d", "cdm_indexed": True,
                      "cdm_sources": [{"source": "s", "types": ["a", "b"]}]},
            multiplex={"presets": {"defaults": [{"arg": "x", "vals": ["1"]}]}, "validations": {}},
        )
        schema = json.load(open(os.path.join(self.crucible_home, "schema", "tool-metadata.json")))
        entry = list_subprojects.build_entry(subproject_dir, self.tool_config, schema)
        self.assertEqual(entry["metrics_count"], 2)
        self.assertEqual(entry["params_count"], 1)


REAL_SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema")


class TestRealToolMetadataSchema(unittest.TestCase):
    """Validates the actual shipped schema/tool-metadata.json -- the synthetic
    TOOL_SCHEMA stub used above doesn't exercise oneOf/if-then-else at all."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REAL_SCHEMA_DIR, "tool-metadata.json")) as f:
            cls.schema = json.load(f)

    def base(self, **overrides):
        doc = {
            "rickshaw-tool-metadata": {"schema": {"version": "2026.08.11"}},
            "tool": "mytool",
            "description": "does things",
        }
        doc.update(overrides)
        return doc

    def assertValid(self, doc):
        list_subprojects.validate(instance=doc, schema=self.schema)

    def assertInvalid(self, doc):
        with self.assertRaises(list_subprojects.ValidationError):
            list_subprojects.validate(instance=doc, schema=self.schema)

    def test_subtools_shape_is_valid(self):
        self.assertValid(self.base(subtools=[
            {"name": "a", "description": "d", "cdm_indexed": True, "cdm_sources": [{"source": "a", "types": ["t"]}]},
        ]))

    def test_flat_shape_cdm_indexed_true_with_sources_is_valid(self):
        self.assertValid(self.base(cdm_indexed=True, cdm_sources=[{"source": "s", "types": ["t"]}]))

    def test_flat_shape_cdm_indexed_false_is_valid(self):
        self.assertValid(self.base(cdm_indexed=False))

    def test_both_subtools_and_cdm_indexed_is_invalid(self):
        self.assertInvalid(self.base(
            subtools=[{"name": "a", "description": "d", "cdm_indexed": False}],
            cdm_indexed=True,
        ))

    def test_neither_subtools_nor_cdm_indexed_is_invalid(self):
        self.assertInvalid(self.base())

    def test_cdm_indexed_true_without_sources_is_invalid(self):
        self.assertInvalid(self.base(cdm_indexed=True))

    def test_cdm_indexed_false_with_sources_is_invalid(self):
        self.assertInvalid(self.base(cdm_indexed=False, cdm_sources=[{"source": "s", "types": ["t"]}]))

    def test_subtool_cdm_indexed_true_without_sources_is_invalid(self):
        self.assertInvalid(self.base(subtools=[{"name": "a", "description": "d", "cdm_indexed": True}]))


class TestRealBenchmarkMetadataSchema(unittest.TestCase):
    """Same coverage as above, for schema/benchmark-metadata.json."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REAL_SCHEMA_DIR, "benchmark-metadata.json")) as f:
            cls.schema = json.load(f)

    def base(self, **overrides):
        doc = {
            "rickshaw-benchmark-metadata": {"schema": {"version": "2026.08.11"}},
            "benchmark": "mybench",
            "description": "does things",
        }
        doc.update(overrides)
        return doc

    def assertValid(self, doc):
        list_subprojects.validate(instance=doc, schema=self.schema)

    def assertInvalid(self, doc):
        with self.assertRaises(list_subprojects.ValidationError):
            list_subprojects.validate(instance=doc, schema=self.schema)

    def test_sub_benchmarks_shape_is_valid(self):
        self.assertValid(self.base(**{"sub-benchmarks": [
            {"name": "a", "description": "d", "cdm_indexed": True, "cdm_sources": [{"source": "a", "types": ["t"]}]},
        ]}))

    def test_flat_shape_cdm_indexed_true_with_sources_is_valid(self):
        self.assertValid(self.base(cdm_indexed=True, cdm_sources=[{"source": "s", "types": ["t"]}]))

    def test_flat_shape_cdm_indexed_false_is_valid(self):
        self.assertValid(self.base(cdm_indexed=False))

    def test_both_sub_benchmarks_and_cdm_indexed_is_invalid(self):
        self.assertInvalid(self.base(**{
            "sub-benchmarks": [{"name": "a", "description": "d", "cdm_indexed": False}],
            "cdm_indexed": True,
        }))

    def test_neither_sub_benchmarks_nor_cdm_indexed_is_invalid(self):
        self.assertInvalid(self.base())

    def test_cdm_indexed_true_without_sources_is_invalid(self):
        self.assertInvalid(self.base(cdm_indexed=True))

    def test_cdm_indexed_false_with_sources_is_invalid(self):
        self.assertInvalid(self.base(cdm_indexed=False, cdm_sources=[{"source": "s", "types": ["t"]}]))

    def test_sub_benchmark_cdm_indexed_true_without_sources_is_invalid(self):
        self.assertInvalid(self.base(**{"sub-benchmarks": [{"name": "a", "description": "d", "cdm_indexed": True}]}))


@unittest.skipUnless(
    os.path.isfile(REAL_MULTIPLEX_SCHEMA_PATH),
    "multiplex subproject not checked out (expected in a full crucible install, not in crucible-ci's bare checkout)",
)
class TestRealMultiplexSchema(unittest.TestCase):
    """Validates the actual shipped subprojects/core/multiplex/JSON/req-schema.json --
    the synthetic MULTIPLEX_SCHEMA stub used above is a simplification and could drift."""

    @classmethod
    def setUpClass(cls):
        with open(REAL_MULTIPLEX_SCHEMA_PATH) as f:
            cls.schema = json.load(f)

    def assertValid(self, doc):
        list_subprojects.validate(instance=doc, schema=self.schema)

    def assertInvalid(self, doc):
        with self.assertRaises(list_subprojects.ValidationError):
            list_subprojects.validate(instance=doc, schema=self.schema)

    def test_minimal_valid_document(self):
        self.assertValid({"validations": {}})

    def test_valid_presets_and_validations(self):
        self.assertValid({
            "presets": {"defaults": [{"arg": "interval", "vals": ["3"]}]},
            "validations": {"positive_integer": {"args": ["interval"], "vals": "^[1-9][0-9]*$"}},
        })

    def test_validations_as_list_is_invalid(self):
        self.assertInvalid({"validations": ["not", "a", "dict"]})

    def test_presets_as_string_is_invalid(self):
        self.assertInvalid({"presets": "not-a-dict", "validations": {}})

    def test_missing_validations_is_invalid(self):
        self.assertInvalid({"presets": {}})


if __name__ == "__main__":
    unittest.main()
