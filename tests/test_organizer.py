"""Organizer: adopting loose PDFs and re-filing existing notes."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scanvault.config import load_config
from scanvault.organizer import apply as organizer_apply
from scanvault.organizer import needs_classification, plan
from scanvault.vault import parse_frontmatter
from tests.helpers import StubClient, make_text_pdf

HAS_EXTRACTOR = shutil.which("pdftotext") is not None
try:
    import pypdf  # type: ignore  # noqa: F401

    HAS_EXTRACTOR = True
except ImportError:
    pass

CONTRACT_LINES = [
    "RENTAL AGREEMENT",
    "This agreement is made on 2023-01-15 between Landlord Ltd and the tenant.",
    "The tenant hereby agrees to the terms and conditions set out below.",
    "Monthly rent is 950.00 EUR payable in advance on the first day of each month.",
]

LLM_RESPONSE = {
    "title": "Rental Agreement",
    "category": "Contracts",
    "document_date": "2023-01-15",
    "correspondent": "Landlord Ltd",
    "summary": "Rental agreement with Landlord Ltd.",
    "tags": ["rent", "lease"],
    "confidence": 0.9,
}


class TestNeedsClassification(unittest.TestCase):
    def test_detects_gaps(self):
        self.assertTrue(needs_classification({}))
        self.assertTrue(needs_classification({"title": "x", "category": "Other"}))
        self.assertTrue(
            needs_classification({"title": "x", "category": "Other", "tags": ["a"], "classifier": "heuristic"})
        )
        self.assertFalse(
            needs_classification({"title": "x", "category": "Other", "tags": ["a"], "classifier": "llm"})
        )


class TestOrganizer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.vault_dir)})
        (self.vault_dir / "Archive").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_note(self, relative: str, content: str) -> Path:
        path = self.vault_dir / "Archive" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_misfiled_note_is_relocated(self):
        note = self.write_note(
            "Unsorted/rental.md",
            '---\ntitle: "Rental Agreement"\ndate: 2023-01-15\ncategory: "Contracts"\n'
            'tags:\n  - scan\nclassifier: "llm"\n---\n\n# Rental Agreement\n\nbody\n',
        )
        report = plan(self.config, client=None)
        self.assertEqual(report.count("relocate"), 1)
        self.assertTrue(note.is_file(), "planning must not move anything")

        organizer_apply(report, self.config, client=None)
        moved = self.vault_dir / "Archive/Contracts/2023/2023-01-15 Rental Agreement.md"
        self.assertTrue(moved.is_file())
        self.assertFalse(note.exists())
        self.assertFalse(note.parent.exists(), "the emptied folder should be pruned")

    def test_correctly_filed_note_is_left_alone(self):
        note = self.write_note(
            "Contracts/2023/2023-01-15 Rental Agreement.md",
            '---\ntitle: "Rental Agreement"\ndate: 2023-01-15\ncategory: "Contracts"\n'
            'tags:\n  - scan\nclassifier: "llm"\ncssclasses:\n  - scanvault\n---\n\nbody\n',
        )
        before = note.read_text()
        report = plan(self.config, client=None)
        organizer_apply(report, self.config, client=None)
        self.assertEqual(report.count("noop"), 1)
        self.assertEqual(note.read_text(), before)

    def test_a_note_from_before_cssclasses_is_rewritten_to_gain_it(self):
        """Otherwise the properties snippet only ever applies to new documents."""
        note = self.write_note(
            "Contracts/2023/2023-01-15 Rental Agreement.md",
            '---\ntitle: "Rental Agreement"\ndate: 2023-01-15\ncategory: "Contracts"\n'
            'tags:\n  - scan\nclassifier: "llm"\n---\n\nbody\n',
        )
        report = plan(self.config, client=None)
        self.assertEqual([a.reason for a in report.actions if a.kind == "rewrite"],
                         ["missing cssclasses"])
        organizer_apply(report, self.config, client=None)
        self.assertIn("cssclasses:\n  - scanvault", note.read_text())

    def test_nothing_drifts_when_no_classes_are_configured(self):
        self.config.vault.cssclasses = []
        self.write_note(
            "Contracts/2023/2023-01-15 Rental Agreement.md",
            '---\ntitle: "Rental Agreement"\ndate: 2023-01-15\ncategory: "Contracts"\n'
            'tags:\n  - scan\nclassifier: "llm"\n---\n\nbody\n',
        )
        self.assertEqual(plan(self.config, client=None).count("noop"), 1)

    def test_prose_someone_wrote_survives_a_rewrite(self):
        """Re-filing a vault must not be a way to destroy what is in it."""
        note = self.write_note(
            "Contracts/2023/2023-01-15 Rental Agreement.md",
            '---\ntitle: "Rental Agreement"\ndate: 2023-01-15\ncategory: "Contracts"\n'
            'tags:\n  - scan\nclassifier: "llm"\n---\n\n# Rental Agreement\n\n'
            "Renewal is due in March; ask about the parking space.\n\n"
            "> [!quote]- Extracted text\n> ```text\n> TENANCY AGREEMENT\n> ```\n",
        )
        organizer_apply(plan(self.config, client=None), self.config, client=None)
        body = note.read_text()
        self.assertIn("ask about the parking space", body)
        self.assertIn("TENANCY AGREEMENT", body)

    def test_that_prose_is_not_duplicated_on_the_next_run(self):
        note = self.write_note(
            "Contracts/2023/2023-01-15 Rental Agreement.md",
            '---\ntitle: "Rental Agreement"\ndate: 2023-01-15\ncategory: "Contracts"\n'
            'tags:\n  - scan\nclassifier: "llm"\n---\n\n# Rental Agreement\n\n'
            "Renewal is due in March.\n",
        )
        for _ in range(2):
            organizer_apply(plan(self.config, client=None), self.config, client=None)
        self.assertEqual(note.read_text().count("Renewal is due in March."), 1)

    def test_a_class_added_by_hand_is_kept(self):
        note = self.write_note(
            "Contracts/2023/2023-01-15 Rental Agreement.md",
            '---\ntitle: "Rental Agreement"\ndate: 2023-01-15\ncategory: "Contracts"\n'
            'tags:\n  - scan\nclassifier: "llm"\ncssclasses:\n  - mine\n---\n\nbody\n',
        )
        report = plan(self.config, client=None)
        organizer_apply(report, self.config, client=None)
        body = note.read_text()
        self.assertIn("- mine", body)
        self.assertIn("- scanvault", body)

    @unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext")
    def test_note_with_missing_metadata_is_reclassified_from_its_attachment(self):
        pdf = make_text_pdf(self.vault_dir / "Attachments/misc/agreement.pdf", CONTRACT_LINES)
        self.write_note(
            "Unsorted/agreement.md",
            f'---\ntitle: "agreement"\nattachment: "{pdf.relative_to(self.vault_dir).as_posix()}"\n---\n\nold body\n',
        )
        client = StubClient(LLM_RESPONSE)
        report = plan(self.config, client=client)
        organizer_apply(report, self.config, client=client)

        self.assertIn("RENTAL AGREEMENT", client.calls[0][1], "the model should see the PDF text")
        moved = self.vault_dir / "Archive/Contracts/2023/2023-01-15 Landlord Ltd - Rental Agreement.md"
        self.assertTrue(moved.is_file())
        frontmatter, body = parse_frontmatter(moved.read_text())
        self.assertEqual(frontmatter["correspondent"], "Landlord Ltd")
        self.assertEqual(
            frontmatter["attachment"],
            "Archive/_attachments/Contracts/2023/2023-01-15 Landlord Ltd - Rental Agreement.pdf",
        )
        self.assertTrue((self.vault_dir / frontmatter["attachment"]).is_file())
        self.assertFalse(pdf.exists(), "the attachment should have moved with the note")
        self.assertIn("RENTAL AGREEMENT", body)

    @unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext")
    def test_loose_pdf_is_adopted(self):
        make_text_pdf(self.vault_dir / "Inbox/scan.pdf", CONTRACT_LINES)
        report = plan(self.config, client=StubClient(LLM_RESPONSE))
        self.assertEqual(report.count("adopt"), 1)
        organizer_apply(report, self.config, client=StubClient(LLM_RESPONSE))
        self.assertTrue(
            (
                self.vault_dir
                / "Archive/Contracts/2023/2023-01-15 Landlord Ltd - Rental Agreement.md"
            ).is_file()
        )

    def test_no_adopt_ignores_loose_pdfs(self):
        (self.vault_dir / "Inbox").mkdir()
        (self.vault_dir / "Inbox/scan.pdf").write_bytes(b"%PDF-1.4 fake")
        report = plan(self.config, client=None, adopt=False)
        self.assertEqual(report.count("adopt"), 0)

    def test_reclassify_rewrites_in_place(self):
        self.write_note(
            "Contracts/2023/2023-01-15 Landlord Ltd - Rental Agreement.md",
            '---\ntitle: "Rental Agreement"\ndate: 2023-01-15\ncategory: "Contracts"\n'
            'correspondent: "Landlord Ltd"\n'
            'tags:\n  - scan\nclassifier: "llm"\nsource_hash: "deadbeef"\n---\n\n'
            "## Extracted text\n\n```text\nRENTAL AGREEMENT 2023-01-15\n```\n",
        )
        client = StubClient(LLM_RESPONSE)
        report = plan(self.config, client=client, reclassify=True)
        self.assertEqual(report.count("rewrite"), 1)
        organizer_apply(report, self.config, client=client)
        note = (
            self.vault_dir
            / "Archive/Contracts/2023/2023-01-15 Landlord Ltd - Rental Agreement.md"
        )
        frontmatter, body = parse_frontmatter(note.read_text())
        self.assertEqual(frontmatter["correspondent"], "Landlord Ltd")
        self.assertEqual(frontmatter["source_hash"], "deadbeef", "provenance must survive a rewrite")
        self.assertIn("RENTAL AGREEMENT 2023-01-15", body, "the OCR text must survive a rewrite")

    def test_failures_do_not_stop_the_run(self):
        self.write_note("a.md", '---\ntitle: "A"\ncategory: "Other"\ntags:\n  - scan\nclassifier: "llm"\n---\n')
        self.write_note("b.md", '---\ntitle: "B"\ncategory: "Other"\ntags:\n  - scan\nclassifier: "llm"\n---\n')
        report = plan(self.config, client=None)
        report.actions[0].meta = None  # force an exception on the first action
        organizer_apply(report, self.config, client=None)
        self.assertEqual(report.count("failed"), 1)
        self.assertTrue((self.vault_dir / "Archive/Other/undated/undated B.md").is_file())


if __name__ == "__main__":
    unittest.main()
