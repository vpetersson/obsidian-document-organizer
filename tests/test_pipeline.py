"""End-to-end: a real PDF with a text layer goes in, a vault note comes out."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scanvault.config import load_config
from scanvault.extract import OcrError, extract, pdf_text
from scanvault.pipeline import ingest, iter_documents, process_file
from scanvault.state import State
from scanvault.vault import Vault, parse_frontmatter
from tests.helpers import StubClient, make_text_pdf


def document_notes(vault: Vault) -> list:
    """Notes for real documents, i.e. everything but the PARA index notes."""
    return [note for note in vault.iter_notes() if not vault.read_note(note)[0].get("para_index")]

HAS_PDFTOTEXT = shutil.which("pdftotext") is not None
try:  # pypdf is optional; either extractor is fine for these tests
    import pypdf  # type: ignore  # noqa: F401

    HAS_EXTRACTOR = True
except ImportError:
    HAS_EXTRACTOR = HAS_PDFTOTEXT

INVOICE_LINES = [
    "ACME LTD",
    "INVOICE 2024-05-02",
    "Invoice number: INV-1234",
    "Amount due: 120.00 EUR",
    "Thank you for your business. Payment is due within 30 days of this invoice date.",
    "Bank transfer to IBAN GB29 NWBK 6016 1331 9268 19, reference INV-1234.",
    "Acme Ltd, 12 Example Street, London, United Kingdom. VAT GB123456789.",
]

LLM_RESPONSE = {
    "title": "Acme Invoice INV-1234",
    "category": "Invoices",
    "document_date": "2024-05-02",
    "correspondent": "Acme Ltd",
    "summary": "Invoice INV-1234 from Acme Ltd for 120.00 EUR.",
    "tags": ["invoice", "acme"],
    "reference": "INV-1234",
    "amount": "120.00",
    "currency": "EUR",
    "confidence": 0.95,
}


@unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext")
class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "inbox"
        self.vault_dir = root / "vault"
        self.source.mkdir()
        self.config = load_config(
            overrides={"source_dir": str(self.source), "vault_dir": str(self.vault_dir)}
        )
        self.pdf = make_text_pdf(self.source / "scan_001.pdf", INVOICE_LINES)

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_layer_is_detected_without_ocr(self):
        result = extract(self.pdf, self.config.ocr)
        self.assertFalse(result.ocr_performed)
        self.assertEqual(result.backend, "text-layer")
        self.assertIn("INV-1234", result.text)

    def test_image_only_pdf_without_backend_raises(self):
        empty = make_text_pdf(self.source / "blank.pdf", [])
        self.config.ocr.backend = "none"
        with self.assertRaises(OcrError):
            extract(empty, self.config.ocr)

    def test_ingest_files_the_document(self):
        report = ingest(self.config, client=StubClient(LLM_RESPONSE))
        self.assertEqual(report.count("ingested"), 1)

        note = self.vault_dir / "4 Archive/Invoices/2024/2024-05-02 Acme Ltd - Acme Invoice INV-1234.md"
        attachment = self.vault_dir / "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Ltd - Acme Invoice INV-1234.pdf"
        self.assertTrue(note.is_file())
        self.assertTrue(attachment.is_file())
        self.assertFalse(self.pdf.exists(), "the original should have been moved into the vault")

        frontmatter, body = parse_frontmatter(note.read_text())
        self.assertEqual(frontmatter["category"], "Invoices")
        self.assertEqual(frontmatter["reference"], "INV-1234")
        self.assertEqual(frontmatter["source_file"], "scan_001.pdf")
        self.assertEqual(len(frontmatter["source_hash"]), 64)
        self.assertIn("INV-1234", body)
        self.assertIn("Invoice INV-1234 from Acme Ltd", body)

        # The searchable PDF is the only copy left; no temp files litter the vault.
        self.assertEqual(list(self.vault_dir.rglob("*.ocr.pdf")), [])
        self.assertIn("INV-1234", pdf_text(attachment))

    def test_reingesting_the_same_scan_is_a_noop(self):
        self.config.vault.source_action = "copy"
        ingest(self.config, client=StubClient(LLM_RESPONSE))
        report = ingest(self.config, client=StubClient(LLM_RESPONSE))
        self.assertEqual(report.count("duplicate"), 1)
        self.assertEqual(len(document_notes(Vault(self.config))), 1)

    def test_state_records_the_document(self):
        ingest(self.config, client=StubClient(LLM_RESPONSE))
        state = State(self.config.state_root)
        entry = next(iter(state.documents.values()))
        self.assertEqual(entry["category"], "Invoices")
        self.assertEqual(entry["source"], "scan_001.pdf")

    def test_dry_run_changes_nothing(self):
        report = ingest(self.config, client=StubClient(LLM_RESPONSE), dry_run=True)
        self.assertEqual(report.count("ingested"), 1)
        self.assertTrue(self.pdf.exists())
        self.assertEqual(list(self.vault_dir.rglob("*.md")), [])

    def test_without_llm_it_still_files_the_document(self):
        report = ingest(self.config, client=None)
        self.assertEqual(report.count("ingested"), 1)
        result = report.results[0]
        self.assertEqual(result.meta.category, "Invoices")
        self.assertEqual(result.meta.classifier, "heuristic")

    def test_unreadable_pdf_is_reported_not_raised(self):
        broken = self.source / "broken.pdf"
        broken.write_bytes(b"not really a pdf")
        result = process_file(broken, self.config, Vault(self.config), None)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.error)

    def test_iter_documents_skips_hidden_and_ocr_artifacts(self):
        (self.source / ".hidden").mkdir()
        make_text_pdf(self.source / ".hidden" / "x.pdf", ["x"])
        make_text_pdf(self.source / "scan_002.ocr.pdf", ["x"])
        make_text_pdf(self.source / "sub" / "scan_003.pdf", INVOICE_LINES)
        names = [p.name for p in iter_documents(self.source)]
        self.assertEqual(names, ["scan_001.pdf", "scan_003.pdf"])
        self.assertEqual([p.name for p in iter_documents(self.source, recursive=False)], ["scan_001.pdf"])


if __name__ == "__main__":
    unittest.main()
