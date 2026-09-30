import json
import re
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft201909Validator
from crucible_mcp.documentation import DocumentationCatalog


class TestDocumentationCatalog(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def test_large_allowlisted_document_is_available_as_bounded_chunks(self):
        content = b"0123456789abcdef" * 3 + b"needle"
        (self.docs / "how-run-files-work.md").write_bytes(content)
        catalog = DocumentationCatalog(self.root, max_document_bytes=16)

        resources = catalog.list_resources()
        self.assertTrue(resources)
        self.assertTrue(all(resource["size"] <= 16 for resource in resources))
        self.assertTrue(all("/chunk/" in resource["uri"] for resource in resources))

        assembled = b"".join(
            catalog.read_resource(resource["uri"])["text"].encode("utf-8")
            for resource in resources
        )
        self.assertEqual(assembled, content)
        with self.assertRaises(ValueError):
            catalog.read_resource("crucible://docs/run-files")

    def test_search_reads_bounded_chunks(self):
        content = b"prefix " * 4 + b"needle " + b"suffix " * 4
        (self.docs / "how-run-files-work.md").write_bytes(content)
        catalog = DocumentationCatalog(self.root, max_document_bytes=16)

        results = catalog.search("needle")
        self.assertEqual(len(results), 1)
        self.assertIn("/chunk/", results[0]["uri"])

    def test_agentic_perf_workflow_is_allowlisted(self):
        (self.docs / "mcp-agentic-perf-workflow.md").write_text(
            "# Agentic-perf MCP Workflow\n", encoding="utf-8"
        )
        catalog = DocumentationCatalog(self.root)

        resources = catalog.list_resources()

        self.assertIn(
            "crucible://docs/agentic-perf-workflow",
            {resource["uri"] for resource in resources},
        )

    def test_multibyte_character_stays_within_chunk_limit(self):
        content = b"0123456789abcde" + "€".encode("utf-8") + b"tail"
        (self.docs / "how-run-files-work.md").write_bytes(content)
        catalog = DocumentationCatalog(self.root, max_document_bytes=16)

        resources = catalog.list_resources()
        self.assertTrue(all(resource["size"] <= 16 for resource in resources))
        assembled = b"".join(
            catalog.read_resource(resource["uri"])["text"].encode("utf-8")
            for resource in resources
        )
        self.assertEqual(assembled, content)

    def test_rejects_limit_smaller_than_maximum_utf8_character(self):
        with self.assertRaises(ValueError):
            DocumentationCatalog(self.root, max_document_bytes=1)

    def test_rejects_unbounded_search_queries(self):
        catalog = DocumentationCatalog(self.root)
        with self.assertRaises(ValueError):
            catalog.search(" ".join(f"term{index}" for index in range(65)))
        with self.assertRaises(ValueError):
            catalog.search("x" * 4097)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _json_example(path: Path, heading: str) -> dict:
    document = path.read_text(encoding="utf-8")
    section = document.split(heading, 1)[1]
    match = re.search(r"```json\s+(.*?)\s+```", section, re.DOTALL)
    if match is None:
        raise AssertionError(f"no JSON example found after {heading!r} in {path}")
    return json.loads(match.group(1))


class TestDocumentedEndpointExamples(unittest.TestCase):
    def test_examples_match_installed_endpoint_schemas(self):
        schema_root = (
            REPOSITORY_ROOT / "subprojects" / "core" / "rickshaw" / "schema"
        )
        if not schema_root.is_dir():
            self.skipTest("active Rickshaw endpoint schemas are not installed")

        examples = (
            (
                REPOSITORY_ROOT / "docs" / "how-run-files-work.md",
                "### Remotehosts endpoint",
                "remotehosts",
                False,
            ),
            (
                REPOSITORY_ROOT / "docs" / "how-run-files-work.md",
                "### Kubernetes endpoint",
                "kube",
                False,
            ),
            (
                REPOSITORY_ROOT / "docs" / "how-endpoints-work.md",
                "### Remotehosts example",
                "remotehosts",
                True,
            ),
            (
                REPOSITORY_ROOT / "docs" / "how-endpoints-work.md",
                "### Kubernetes example",
                "kube",
                True,
            ),
        )
        for doc_path, heading, endpoint_type, wrapped in examples:
            with self.subTest(document=doc_path.name, endpoint=endpoint_type):
                example = _json_example(doc_path, heading)
                endpoint = example["endpoints"][0] if wrapped else example
                schema = json.loads(
                    (schema_root / f"{endpoint_type}.json").read_text(
                        encoding="utf-8"
                    )
                )
                errors = list(Draft201909Validator(schema).iter_errors(endpoint))
                self.assertEqual(
                    errors,
                    [],
                    f"{heading} is invalid: "
                    + "; ".join(error.message for error in errors),
                )


if __name__ == "__main__":
    unittest.main()
