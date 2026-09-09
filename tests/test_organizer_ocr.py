"""The organizer must OCR documents an existing vault never had OCR'd."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scanvault.config import load_config
from scanvault.extract import pdf_text
from scanvault.organizer import apply as organizer_apply
from scanvault.organizer import attachment_needs_ocr, plan
from scanvault.vault import Vault, parse_frontmatter
from tests.helpers import StubClient, make_scanned_pdf, make_text_pdf

HAS_RASTERISER = shutil.which("pdftoppm") is not None
HAS_BACKEND = shutil.which("ocrmypdf") is not None or shutil.which("tesseract") is not None

SCAN_LINES = [
    "ACME LTD",
    "INVOICE 2024-05-02",
    "Invoice number INV-1234",
    "Amount due 120.00 EUR",
]

NOTE = (
    '---\ntitle: "Acme Invoice"\ndate: 2024-05-02\ncategory: "Invoices"\n'
    'tags:\n  - scan\nclassifier: "llm"\n'
    'attachment: "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf"\n'
    "---\n\n# Acme Invoice\n"
)


@unittest.skipUnless(HAS_RASTERISER, "needs pdftoppm to build an image-only PDF")
class TestOrganizerOcr(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.attachment = make_scanned_pdf(
            self.root / "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf",
            SCAN_LINES,
        )
        self.note = self.root / "4 Archive/Invoices/2024/2024-05-02 Acme Invoice.md"
        self.note.parent.mkdir(parents=True, exist_ok=True)
        self.note.write_text(NOTE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_detects_an_image_only_attachment(self):
        frontmatter, _ = parse_frontmatter(self.note.read_text())
        self.assertTrue(attachment_needs_ocr(Vault(self.config), frontmatter, self.config))

    def test_plan_reports_it_instead_of_calling_the_note_filed(self):
        report = plan(self.config, client=None)
        actions = [a for a in report.actions if a.path == self.note]
        self.assertEqual([a.kind for a in actions], ["ocr"])
        self.assertIn("no text layer", actions[0].reason)
        self.assertEqual(report.count("noop"), 0)

    def test_a_dry_run_does_not_touch_the_pdf(self):
        before = self.attachment.read_bytes()
        plan(self.config, client=None)
        self.assertEqual(self.attachment.read_bytes(), before)
        self.assertEqual(pdf_text(self.attachment), "")

    @unittest.skipUnless(HAS_BACKEND, "needs ocrmypdf or tesseract")
    def test_apply_ocrs_the_attachment_in_place(self):
        report = plan(self.config, client=None)
        organizer_apply(report, self.config, client=None)

        self.assertIn("INVOICE", pdf_text(self.attachment).upper(), "the vault PDF must be searchable now")
        frontmatter, body = parse_frontmatter(self.note.read_text())
        self.assertIn(frontmatter["ocr"], ("ocrmypdf", "tesseract"))
        self.assertEqual(frontmatter["pages"], 1)
        self.assertIn("INVOICE", body.upper(), "the OCR text should be embedded in the note")
        self.assertEqual(report.count("failed"), 0)
        self.assertEqual(list(self.root.rglob("*.ocr.pdf")), [], "no temp OCR files left behind")

    @unittest.skipUnless(HAS_BACKEND, "needs ocrmypdf or tesseract")
    def test_incomplete_note_is_classified_from_the_ocr_text_and_refiled(self):
        self.note.unlink()
        note = self.root / "4 Archive/Unsorted/scan.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            '---\ntitle: "scan"\n'
            'attachment: "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf"\n'
            "---\n",
            encoding="utf-8",
        )
        client = StubClient(
            {
                "title": "Acme Invoice INV-1234",
                "category": "Invoices",
                "document_date": "2024-05-02",
                "tags": ["invoice"],
            }
        )
        report = plan(self.config, client=client)
        organizer_apply(report, self.config, client=client)

        self.assertIn("INVOICE", client.calls[0][1].upper(), "the model must see the OCR text")
        moved = self.root / "4 Archive/Invoices/2024/2024-05-02 Acme Invoice INV-1234.md"
        self.assertTrue(moved.is_file())
        self.assertFalse(note.exists())
        frontmatter, _ = parse_frontmatter(moved.read_text())
        self.assertTrue((self.root / frontmatter["attachment"]).is_file())
        self.assertIn("INVOICE", pdf_text(self.root / frontmatter["attachment"]).upper())

    def test_searchable_attachments_are_not_re_ocrd(self):
        make_text_pdf(self.attachment, SCAN_LINES + [
            "Payment due within 30 days of the invoice date, by bank transfer.",
            "Acme Ltd, 12 Example Street, London. VAT GB123456789.",
        ])
        report = plan(self.config, client=None)
        self.assertEqual(report.count("ocr"), 0)
        self.assertEqual(report.count("noop"), 1)

    def test_ocr_can_be_switched_off(self):
        report = plan(self.config, client=None, ocr=False)
        self.assertEqual(report.count("ocr"), 0)
        self.assertEqual(report.count("noop"), 1)

    def test_missing_backend_is_reported_in_the_plan(self):
        self.config.ocr.backend = "none"
        report = plan(self.config, client=None)
        action = next(a for a in report.actions if a.kind == "ocr")
        self.assertIn("no OCR backend installed", action.reason)
        organizer_apply(report, self.config, client=None)
        self.assertEqual(report.count("failed"), 1, "applying must fail loudly, not silently skip")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_RASTERISER, "needs pdftoppm to build an image-only PDF")
class TestAdoptReporting(unittest.TestCase):
    """A loose PDF is where OCR usually happens, so the plan has to say so."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.loose = make_scanned_pdf(self.root / "Inbox/scan.pdf", SCAN_LINES)

    def tearDown(self):
        self.tmp.cleanup()

    def test_adopt_says_the_pdf_will_be_ocrd(self):
        report = plan(self.config, client=None)
        action = next(a for a in report.actions if a.kind == "adopt")
        self.assertTrue(action.needs_ocr)
        self.assertIn("will OCR", action.reason)

    def test_a_searchable_loose_pdf_is_not_flagged_for_ocr(self):
        make_text_pdf(
            self.loose,
            SCAN_LINES
            + [
                "Payment due within 30 days of the invoice date, by bank transfer.",
                "Acme Ltd, 12 Example Street, London. VAT GB123456789.",
            ],
        )
        action = next(a for a in plan(self.config, client=None).actions if a.kind == "adopt")
        self.assertFalse(action.needs_ocr)
        self.assertNotIn("will OCR", action.reason)


class TestLinkedPdfsAreNotLoose(unittest.TestCase):
    """PDFs a hand-written note links to must not be adopted a second time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.vault = Vault(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def add_pdf(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4 fake")
        return path

    def add_note(self, relative: str, body: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_embedded_wikilink(self):
        self.add_pdf("3 Resources/manual.pdf")
        self.add_note("3 Resources/Dishwasher.md", "# Dishwasher\n\n![[3 Resources/manual.pdf]]\n")
        self.assertEqual(list(self.vault.iter_loose_pdfs()), [])

    def test_shortest_path_wikilink_without_folders(self):
        self.add_pdf("3 Resources/manual.pdf")
        self.add_note("2 Areas/Home.md", "See [[manual.pdf|the manual]] for details.\n")
        self.assertEqual(list(self.vault.iter_loose_pdfs()), [])

    def test_markdown_link_with_encoded_spaces(self):
        self.add_pdf("3 Resources/user manual.pdf")
        self.add_note("2 Areas/Home.md", "[manual](3%20Resources/user%20manual.pdf)\n")
        self.assertEqual(list(self.vault.iter_loose_pdfs()), [])

    def test_a_genuinely_unreferenced_pdf_is_still_found(self):
        self.add_pdf("3 Resources/manual.pdf")
        self.add_note("3 Resources/Dishwasher.md", "# Dishwasher\n\n![[3 Resources/manual.pdf]]\n")
        orphan = self.add_pdf("Inbox/scan_001.pdf")
        self.assertEqual(list(self.vault.iter_loose_pdfs()), [orphan])

    def test_frontmatter_attachments_still_count(self):
        pdf = self.add_pdf("4 Archive/_attachments/x.pdf")
        self.add_note(
            "4 Archive/x.md",
            '---\ntitle: "x"\nattachment: "4 Archive/_attachments/x.pdf"\n---\n\nno body link\n',
        )
        self.assertEqual(list(self.vault.iter_loose_pdfs()), [])
        self.assertTrue(pdf.is_file())


class TestDuplicateLoosePdfs(unittest.TestCase):
    """A second copy of a document already in the vault must not be filed again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.first = self.root / "Receipts/scan.pdf"
        self.first.parent.mkdir(parents=True, exist_ok=True)
        make_text_pdf(
            self.first,
            [
                "THE COFFEE HOUSE",
                "RECEIPT 2017-03-05",
                "Total 12.40 GBP including VAT of 2.07 GBP, paid by card ending 4242.",
                "Thank you for visiting. Company number 12345678, VAT GB123456789.",
            ],
        )
        self.copy = self.root / "Receipts/scan copy.pdf"
        shutil.copyfile(self.first, self.copy)

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_copies_are_reported_as_duplicates_after_the_first_is_filed(self):
        client = StubClient({"title": "Coffee House Receipt", "category": "Receipts", "tags": ["coffee"]})
        first_plan = plan(self.config, client=client)
        self.assertEqual(first_plan.count("adopt"), 2, "nothing is filed yet, so both look adoptable")
        organizer_apply(first_plan, self.config, client=client)

        second_plan = plan(self.config, client=client)
        self.assertEqual(second_plan.count("adopt"), 0)
        self.assertEqual(second_plan.count("duplicate"), 1)
        action = next(a for a in second_plan.actions if a.kind == "duplicate")
        self.assertIn("same content as", action.reason)
        self.assertTrue(action.path.is_file(), "a duplicate is reported, never deleted")

    def test_applying_a_duplicate_action_changes_nothing(self):
        client = StubClient({"title": "Coffee House Receipt", "category": "Receipts", "tags": ["coffee"]})
        organizer_apply(plan(self.config, client=client), self.config, client=client)
        report = plan(self.config, client=client)
        before = sorted(p.name for p in self.root.rglob("*.pdf"))
        organizer_apply(report, self.config, client=client)
        self.assertEqual(sorted(p.name for p in self.root.rglob("*.pdf")), before)


@unittest.skipUnless(HAS_RASTERISER, "needs pdftoppm")
@unittest.skipUnless(HAS_BACKEND, "needs ocrmypdf or tesseract")
class TestShortDocumentsStayOcrd(unittest.TestCase):
    """A receipt's text layer is tiny; that must not read as "no text layer"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        make_scanned_pdf(
            self.root / "Receipts/scan.pdf",
            ["THE COFFEE HOUSE", "RECEIPT 2017-03-05", "Total 12.40 GBP"],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_short_receipt_is_not_re_ocrd_on_the_next_run(self):
        client = StubClient(
            {
                "title": "Coffee House Receipt",
                "category": "Receipts",
                "document_date": "2017-03-05",
                "tags": ["coffee"],
            }
        )
        organizer_apply(plan(self.config, client=client), self.config, client=client)
        attachment = next((self.root / "4 Archive/_attachments").rglob("*.pdf"))
        text = pdf_text(attachment)
        self.assertIn("COFFEE", text.upper())
        self.assertLess(len(text), 180, "the fixture must be shorter than min_text_chars")

        report = plan(self.config, client=client)
        self.assertEqual(report.count("ocr"), 0, "a searchable receipt must not be re-OCR'd")
        self.assertEqual(report.count("noop"), 1)
