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
            list_subprojects.print_table(entries, self.tool_config)
        lines = stdout.getvalue().splitlines()
        # header + separator + exactly one data row -- the embedded newline
        # must not have produced an extra line
        self.assertEqual(len(lines), 3)
        self.assertIn("Line one Line two continues here", lines[2])


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

    def test_both_sub_benchmarks_and_cdm_indexed_is_invalid(self):
        self.assertInvalid(self.base(**{
            "sub-benchmarks": [{"name": "a", "description": "d", "cdm_indexed": False}],
            "cdm_indexed": True,
        }))

    def test_cdm_indexed_true_without_sources_is_invalid(self):
        self.assertInvalid(self.base(cdm_indexed=True))

    def test_cdm_indexed_false_with_sources_is_invalid(self):
        self.assertInvalid(self.base(cdm_indexed=False, cdm_sources=[{"source": "s", "types": ["t"]}]))


if __name__ == "__main__":
    unittest.main()
