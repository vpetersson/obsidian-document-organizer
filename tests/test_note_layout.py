"""The OCR text is in the note for search, not for reading."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from scanvault.classify import DocumentMeta
from scanvault.config import load_config
from scanvault.organizer import apply as organizer_apply
from scanvault.organizer import embedded_text, plan, text_style
from scanvault.vault import Vault, extracted_text_block

TEXT = "EXAMPLE BANK PLC\nStatement 2024-05-02\n\nSort code 00-00-00"


class TestRendering(unittest.TestCase):
    def test_the_callout_is_folded_shut(self):
        block = "\n".join(extracted_text_block(TEXT))
        self.assertTrue(block.startswith("> [!quote]- Extracted text"))
        self.assertIn("> ```text", block)
        self.assertIn("> EXAMPLE BANK PLC", block)
        self.assertIn("\n>\n", block, "blank lines stay inside the callout")

    def test_the_old_plain_form_is_still_available(self):
        block = "\n".join(extracted_text_block(TEXT, "plain"))
        self.assertTrue(block.startswith("## Extracted text"))
        self.assertIn("EXAMPLE BANK PLC", block)

    def test_details_style(self):
        block = "\n".join(extracted_text_block(TEXT, "details"))
        self.assertIn("<details>", block)
        self.assertIn("<summary>Extracted text</summary>", block)

    def test_an_unknown_style_falls_back_to_the_callout(self):
        self.assertTrue("\n".join(extracted_text_block(TEXT, "nonsense")).startswith("> [!quote]-"))

    def test_every_style_round_trips(self):
        for style in ("callout", "plain", "details"):
            body = "\n".join(extracted_text_block(TEXT, style))
            self.assertEqual(text_style(body), style, style)
            self.assertEqual(embedded_text(body).strip(), TEXT.strip(), style)


class TestNotesOnDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.vault = Vault(self.config)
        self.meta = DocumentMeta(
            title="Statement",
            category="Banking",
            document_date=date(2024, 5, 2),
            tags=["scan", "banking"],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_written_note_is_collapsed(self):
        result = self.vault.write_document(self.meta, TEXT, pdf_path=None)
        content = result.note_path.read_text()
        self.assertIn("> [!quote]- Extracted text", content)
        self.assertNotIn("## Extracted text", content)
        self.assertIn("Sort code 00-00-00", content, "the text is still searchable")

    def test_a_note_written_the_old_way_is_offered_for_rewrite(self):
        note = self.root / "Archive/Banking/2024/2024-05-02 Statement.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            '---\ntitle: "Statement"\ndate: 2024-05-02\ncategory: "Banking"\n'
            'tags:\n  - scan\nclassifier: "llm"\n---\n\n# Statement\n\n'
            "## Extracted text\n\n```text\n" + TEXT + "\n```\n",
            encoding="utf-8",
        )

        report = plan(self.config, client=None)
        action = next(a for a in report.actions if a.path == note)
        self.assertEqual(action.kind, "rewrite")
        self.assertIn("not callout", action.reason)

        organizer_apply(report, self.config, client=None)
        content = note.read_text()
        self.assertIn("> [!quote]- Extracted text", content)
        self.assertIn("Sort code 00-00-00", content, "the text survived the rewrite")

    def test_a_collapsed_note_is_left_alone(self):
        result = self.vault.write_document(self.meta, TEXT, pdf_path=None)
        report = plan(self.config, client=None)
        action = next(a for a in report.actions if a.path == result.note_path)
        self.assertEqual(action.kind, "noop")

    def test_notes_without_embedded_text_are_not_rewritten(self):
        self.config.vault.include_text = False
        result = self.vault.write_document(self.meta, TEXT, pdf_path=None)
        report = plan(self.config, client=None)
        action = next(a for a in report.actions if a.path == result.note_path)
        self.assertEqual(action.kind, "noop")


if __name__ == "__main__":
    unittest.main()
