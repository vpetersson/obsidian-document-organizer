"""Filenames come from the document's metadata, not from what the scanner called it."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from scanvault.classify import DocumentMeta, classify
from scanvault.config import load_config
from scanvault.util import clean_document_name
from scanvault.vault import Vault
from tests.helpers import StubClient


class TestScannerNames(unittest.TestCase):
    def check(self, stem: str, expected: str):
        self.assertEqual(clean_document_name(stem), expected, stem)

    def test_pure_scanner_output_leaves_nothing(self):
        for stem in (
            "SwiftScan Feb 7, 2021 11.45 AM",
            "Scanbot Mar 5, 2017 11.57 AM - 1",
            "Scanned Documents 3",
            "Scan 10",
            "IMG_20240502_120301",
            "Untitled",
        ):
            self.check(stem, "")

    def test_a_name_someone_chose_survives(self):
        self.check("car insurance renewal", "car insurance renewal")
        self.check("Boiler service 2019-04-02 - 1", "Boiler service")
        self.check("tax return 2021 copy", "tax return 2021")

    def test_underscores_become_spaces(self):
        self.check("kitchen_quote_final", "kitchen quote final")


class TestDocumentName(unittest.TestCase):
    def meta(self, **kwargs) -> DocumentMeta:
        base = dict(title="Invoice INV-1234", category="Invoices", correspondent="Acme Ltd")
        base.update(kwargs)
        return DocumentMeta(**base)

    def test_correspondent_and_title(self):
        self.assertEqual(self.meta().document_name, "Acme Ltd - Invoice INV-1234")

    def test_no_correspondent(self):
        self.assertEqual(self.meta(correspondent="").document_name, "Invoice INV-1234")

    def test_a_correspondent_already_in_the_title_is_not_repeated(self):
        meta = self.meta(title="Acme Ltd annual statement")
        self.assertEqual(meta.document_name, "Acme Ltd annual statement")

    def test_correspondent_alone(self):
        self.assertEqual(self.meta(title="").document_name, "Acme Ltd")


class TestFilesOnDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.vault = Vault(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_pdf_is_named_from_the_metadata(self):
        meta = DocumentMeta(
            title="Annual statement",
            category="Banking",
            correspondent="Example Bank",
            document_date=date(2021, 2, 7),
        )
        pdf = Path(self.tmp.name) / "SwiftScan Feb 7, 2021 11.45 AM.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        result = self.vault.write_document(meta, "text", pdf_path=pdf, source_path=pdf)

        self.assertEqual(
            result.note_path.relative_to(self.root).as_posix(),
            "4 Archive/Banking/2021/2021-02-07 Example Bank - Annual statement.md",
        )
        self.assertEqual(
            result.attachment_path.name, "2021-02-07 Example Bank - Annual statement.pdf"
        )
        self.assertFalse(pdf.exists(), "the scanner's name does not survive")


class TestTitleSource(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_a_model_title_is_marked_as_such(self):
        client = StubClient({"title": "Annual statement", "category": "Banking"})
        meta = classify("some text", self.config, client, source=Path("Scan 10.pdf"))
        self.assertEqual(meta.title, "Annual statement")
        self.assertEqual(meta.title_source, "model")

    def test_a_scanner_filename_is_not_used_as_a_title(self):
        client = StubClient({"title": "", "category": "Banking"})
        meta = classify("some text", self.config, client, source=Path("Scan 10.pdf"))
        self.assertEqual(meta.title, "Untitled document")
        self.assertEqual(meta.title_source, "filename")

    def test_a_meaningful_filename_is_kept_when_the_model_gives_nothing(self):
        client = StubClient({"title": "", "category": "Banking"})
        meta = classify("some text", self.config, client, source=Path("car insurance renewal.pdf"))
        self.assertEqual(meta.title, "car insurance renewal")
        self.assertEqual(meta.title_source, "filename")

    def test_heuristics_take_the_title_from_the_text(self):
        meta = classify("ACME LTD INVOICE\nAmount due", self.config, None, source=Path("Scan 3.pdf"))
        self.assertEqual(meta.title_source, "text")


if __name__ == "__main__":
    unittest.main()
