"""Real OCR backends against a real image-only PDF. Skipped when not installed."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scanvault.config import load_config
from scanvault.extract import available_backend, extract, pdf_text
from scanvault.pipeline import ingest
from scanvault.vault import parse_frontmatter
from tests.helpers import StubClient, make_scanned_pdf

HAS_RASTERISER = shutil.which("pdftoppm") is not None
HAS_TESSERACT = shutil.which("tesseract") is not None and shutil.which("pdfunite") is not None
HAS_OCRMYPDF = shutil.which("ocrmypdf") is not None

SCAN_LINES = [
    "ACME LTD",
    "INVOICE 2024-05-02",
    "Invoice number INV-1234",
    "Amount due 120.00 EUR",
]


@unittest.skipUnless(HAS_RASTERISER, "needs pdftoppm to build an image-only PDF")
class TestOcrBackends(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scan = make_scanned_pdf(self.root / "inbox" / "scan_001.pdf", SCAN_LINES)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_fixture_really_has_no_text_layer(self):
        self.assertEqual(pdf_text(self.scan), "")

    def _assert_ocr(self, backend: str):
        config = load_config(overrides={"ocr.backend": backend})
        result = extract(self.scan, config.ocr, work_dir=self.root / "work")
        self.assertTrue(result.ocr_performed)
        self.assertEqual(result.backend, backend)
        self.assertIn("INVOICE", result.text.upper())
        self.assertIn("INV-1234", result.text.replace(" ", ""))
        self.assertNotEqual(result.pdf_path, self.scan)
        self.assertIn("INVOICE", pdf_text(result.pdf_path).upper(), "the output PDF must be searchable")

    @unittest.skipUnless(HAS_TESSERACT, "needs tesseract + pdfunite")
    def test_tesseract_backend(self):
        self._assert_ocr("tesseract")

    @unittest.skipUnless(HAS_OCRMYPDF, "needs ocrmypdf")
    def test_ocrmypdf_backend(self):
        self._assert_ocr("ocrmypdf")

    @unittest.skipUnless(HAS_OCRMYPDF, "needs ocrmypdf")
    def test_auto_prefers_ocrmypdf(self):
        self.assertEqual(available_backend(load_config().ocr), "ocrmypdf")

    @unittest.skipUnless(HAS_TESSERACT or HAS_OCRMYPDF, "needs an OCR backend")
    def test_ingest_of_a_scanned_pdf_end_to_end(self):
        vault_dir = self.root / "vault"
        config = load_config(
            overrides={"source_dir": str(self.scan.parent), "vault_dir": str(vault_dir)}
        )
        client = StubClient(
            {
                "title": "Acme Invoice INV-1234",
                "category": "Invoices",
                "document_date": "2024-05-02",
                "correspondent": "Acme Ltd",
                "summary": "Invoice from Acme.",
                "tags": ["invoice"],
            }
        )
        report = ingest(config, client=client)
        self.assertEqual(report.count("ingested"), 1, report.results)

        note = vault_dir / "4 Archive/Invoices/2024/2024-05-02 Acme Invoice INV-1234.md"
        attachment = vault_dir / "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice INV-1234.pdf"
        self.assertTrue(note.is_file())
        frontmatter, body = parse_frontmatter(note.read_text())
        self.assertIn(frontmatter["ocr"], ("ocrmypdf", "tesseract"))
        self.assertIn("ACME", body.upper(), "the OCR text should be embedded in the note")
        self.assertIn("INVOICE", pdf_text(attachment).upper(), "the filed PDF should be searchable")
        self.assertEqual(list(vault_dir.rglob("*.ocr.pdf")), [], "no temp OCR files left behind")

    def test_backend_none_reports_a_useful_error(self):
        config = load_config(overrides={"ocr.backend": "none"})
        with self.assertRaises(Exception) as caught:
            extract(self.scan, config.ocr)
        self.assertIn("no OCR backend", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
