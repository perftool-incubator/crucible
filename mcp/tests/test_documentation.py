import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
