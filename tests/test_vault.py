"""Vault layout, frontmatter and note writing."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from scanvault.classify import DocumentMeta
from scanvault.config import load_config
from scanvault.vault import Vault, dump_frontmatter, parse_frontmatter


def meta(**kwargs) -> DocumentMeta:
    base = dict(
        title="Acme Invoice",
        category="Invoices",
        summary="An invoice from Acme.",
        document_date=date(2024, 5, 2),
        correspondent="Acme Ltd",
        tags=["scan", "invoices"],
    )
    base.update(kwargs)
    return DocumentMeta(**base)


class TestFrontmatter(unittest.TestCase):
    def test_roundtrip(self):
        data = {"title": 'He said "hi"', "tags": ["a", "b"], "confidence": 0.5, "pages": 3, "skip": None}
        parsed, body = parse_frontmatter(dump_frontmatter(data) + "\n\nbody")
        self.assertEqual(parsed["title"], 'He said "hi"')
        self.assertEqual(parsed["tags"], ["a", "b"])
        self.assertEqual(parsed["confidence"], 0.5)
        self.assertEqual(parsed["pages"], 3)
        self.assertNotIn("skip", parsed)
        self.assertEqual(body.strip(), "body")

    def test_note_without_frontmatter(self):
        parsed, body = parse_frontmatter("# Just a note\n")
        self.assertEqual(parsed, {})
        self.assertEqual(body, "# Just a note\n")


class TestVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.vault = Vault(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_paths_follow_template(self):
        note = self.vault.note_path(meta())
        self.assertEqual(
            note.relative_to(self.root).as_posix(),
            "4 Archive/Invoices/2024/2024-05-02 Acme Ltd - Acme Invoice.md",
        )
        attachment = self.vault.attachment_path(meta())
        self.assertEqual(
            attachment.relative_to(self.root).as_posix(),
            "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Ltd - Acme Invoice.pdf",
        )

    def test_undated_documents_get_their_own_folder(self):
        note = self.vault.note_path(meta(document_date=None))
        self.assertIn("Invoices/undated/undated Acme Ltd - Acme Invoice", note.as_posix())

    def test_title_with_slash_does_not_escape_the_folder(self):
        note = self.vault.note_path(meta(title="2024/05 Statement"))
        self.assertEqual(note.parent.name, "2024")
        self.assertEqual(note.name, "2024-05-02 Acme Ltd - 2024-05 Statement.md")

    def test_custom_template(self):
        self.config.vault.note_path_template = "{correspondent}/{year}-{month} {slug}"
        note = self.vault.note_path(meta())
        self.assertEqual(
            note.relative_to(self.root).as_posix(), "Acme Ltd/2024-05 acme-invoice.md"
        )

    def test_unknown_placeholder_raises(self):
        self.config.vault.note_path_template = "{nope}/x"
        with self.assertRaises(ValueError):
            self.vault.note_path(meta())

    def test_write_document_creates_note_and_attachment(self):
        pdf = Path(self.tmp.name) / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        result = self.vault.write_document(
            meta(), "OCR TEXT HERE", pdf_path=pdf, source_path=pdf, extra={"source_hash": "abc"}
        )
        self.assertTrue(result.note_path.is_file())
        self.assertTrue(result.attachment_path.is_file())
        self.assertFalse(pdf.exists(), "source_action=move should consume the original")

        content = result.note_path.read_text()
        self.assertIn('title: "Acme Invoice"', content)
        self.assertIn("date: 2024-05-02", content)
        self.assertIn("  - invoices", content)
        self.assertIn('source_hash: "abc"', content)
        self.assertIn("![[4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Ltd - Acme Invoice.pdf]]", content)
        self.assertIn("OCR TEXT HERE", content)

    def test_copy_keeps_the_original(self):
        self.config.vault.source_action = "copy"
        pdf = Path(self.tmp.name) / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        self.vault.write_document(meta(), "text", pdf_path=pdf, source_path=pdf)
        self.assertTrue(pdf.exists())

    def test_dry_run_writes_nothing(self):
        pdf = Path(self.tmp.name) / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        result = self.vault.write_document(meta(), "text", pdf_path=pdf, source_path=pdf, dry_run=True)
        self.assertFalse(result.note_path.exists())
        self.assertTrue(pdf.exists())

    def test_second_document_with_same_title_does_not_overwrite(self):
        for _ in range(2):
            pdf = Path(self.tmp.name) / "scan.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            self.vault.write_document(meta(), "text", pdf_path=pdf, source_path=pdf)
        notes = sorted(p.name for p in self.vault.iter_notes())
        self.assertEqual(notes, ["2024-05-02 Acme Ltd - Acme Invoice-2.md", "2024-05-02 Acme Ltd - Acme Invoice.md"])

    def test_include_text_disabled(self):
        self.config.vault.include_text = False
        result = self.vault.write_document(meta(), "SECRET OCR", pdf_path=None)
        self.assertNotIn("SECRET OCR", result.note_path.read_text())

    def test_iter_loose_documents_skips_linked_attachments(self):
        pdf = Path(self.tmp.name) / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        self.vault.write_document(meta(), "text", pdf_path=pdf, source_path=pdf)
        loose = self.root / "Inbox" / "loose.pdf"
        loose.parent.mkdir(parents=True)
        loose.write_bytes(b"%PDF-1.4 fake")
        self.assertEqual([p.name for p in self.vault.iter_loose_documents()], ["loose.pdf"])


if __name__ == "__main__":
    unittest.main()
