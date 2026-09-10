"""Images are documents too: converted to searchable PDFs, then filed."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scanvault.config import load_config
from scanvault.extract import OcrError, extract, is_image
from scanvault.organizer import apply as organizer_apply
from scanvault.organizer import plan
from scanvault.pipeline import ingest, iter_documents
from scanvault.vault import Vault, parse_frontmatter
from tests.helpers import StubClient, make_scan_image, make_text_pdf

HAS_RASTERISER = shutil.which("pdftoppm") is not None
HAS_BACKEND = shutil.which("ocrmypdf") is not None or shutil.which("tesseract") is not None

LINES = [
    "EXAMPLE BANK PLC",
    "STATEMENT 2021-02-07",
    "Account ending 4242",
    "Interest paid 12.40 GBP",
]

RESPONSE = {
    "title": "Annual statement",
    "category": "Banking",
    "correspondent": "Example Bank",
    "document_date": "2021-02-07",
    "tags": ["statement"],
}


class TestRecognisingImages(unittest.TestCase):
    def test_suffixes(self):
        for name in ("a.jpg", "b.JPEG", "c.png", "d.tiff", "e.heic", "f.webp"):
            self.assertTrue(is_image(Path(name)), name)
        for name in ("a.pdf", "b.md", "c.txt"):
            self.assertFalse(is_image(Path(name)), name)


@unittest.skipUnless(HAS_RASTERISER, "needs pdftoppm to build an image fixture")
class TestImagesAreFiled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "inbox"
        self.vault = self.root / "vault"
        self.image = make_scan_image(self.source / "IMG_4021.jpg", LINES)
        self.config = load_config(
            overrides={"source_dir": str(self.source), "vault_dir": str(self.vault)}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_image_is_picked_up_as_a_document(self):
        self.assertEqual([p.name for p in iter_documents(self.source)], ["IMG_4021.jpg"])

    @unittest.skipUnless(HAS_BACKEND, "needs an OCR backend")
    def test_extract_turns_it_into_a_searchable_pdf(self):
        result = extract(self.image, self.config.ocr, work_dir=self.root / "work")
        self.assertTrue(result.ocr_performed)
        self.assertEqual(result.pdf_path.suffix, ".pdf")
        self.assertIn("EXAMPLE", result.text.upper())
        self.assertIn("BANK", result.text.upper())

    @unittest.skipUnless(HAS_BACKEND, "needs an OCR backend")
    def test_ingest_files_the_pdf_and_keeps_the_photo(self):
        report = ingest(self.config, client=StubClient(RESPONSE))
        self.assertEqual(report.count("ingested"), 1, report.results)

        note = self.vault / "Archive/Banking/2021/2021-02-07 Example Bank - Annual statement.md"
        pdf = self.vault / "Archive/_attachments/Banking/2021/2021-02-07 Example Bank - Annual statement.pdf"
        jpg = pdf.with_suffix(".jpg")
        self.assertTrue(note.is_file())
        self.assertTrue(pdf.is_file(), "the searchable PDF is what gets filed")
        self.assertTrue(jpg.is_file(), "the original photo is kept beside it")
        self.assertFalse(self.image.exists(), "the inbox copy was moved")

        frontmatter, body = parse_frontmatter(note.read_text())
        self.assertEqual(frontmatter["source_file"], "IMG_4021.jpg")
        self.assertTrue(frontmatter["attachment"].endswith(".pdf"))
        self.assertTrue(frontmatter["original"].endswith(".jpg"))
        self.assertIn("EXAMPLE", body.upper())

    @unittest.skipUnless(HAS_BACKEND, "needs an OCR backend")
    def test_the_photo_can_be_dropped_instead(self):
        self.config.vault.keep_original_image = False
        ingest(self.config, client=StubClient(RESPONSE))
        images = [p for p in self.vault.rglob("*.jpg")]
        self.assertEqual(images, [])

    @unittest.skipUnless(HAS_BACKEND, "needs an OCR backend")
    def test_a_kept_original_is_not_adopted_as_a_second_document(self):
        ingest(self.config, client=StubClient(RESPONSE))
        report = plan(self.config, client=StubClient(RESPONSE))
        self.assertEqual(report.count("adopt"), 0, "the kept photo belongs to its note")


@unittest.skipUnless(HAS_RASTERISER, "needs pdftoppm to build an image fixture")
class TestImagesInAVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.vault)})
        self.image = make_scan_image(self.vault / "Inbox/IMG_4022.png", LINES, fmt="png")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_plan_says_what_will_happen_to_it(self):
        action = next(a for a in plan(self.config, client=None).actions if a.kind == "adopt")
        self.assertIn("png image", action.reason)
        self.assertIn("searchable PDF", action.reason)
        self.assertTrue(action.needs_ocr)

    @unittest.skipUnless(HAS_BACKEND, "needs an OCR backend")
    def test_applying_converts_and_files_it(self):
        client = StubClient(RESPONSE)
        organizer_apply(plan(self.config, client=client), self.config, client=client)
        filed = self.vault / "Archive/_attachments/Banking/2021/2021-02-07 Example Bank - Annual statement.pdf"
        self.assertTrue(filed.is_file())
        self.assertTrue(filed.with_suffix(".png").is_file())
        self.assertFalse(self.image.exists())
        self.assertEqual(list(self.vault.rglob("*.ocr.pdf")), [], "no temp files left behind")

    def test_a_linked_image_is_left_alone(self):
        note = self.vault / "3 Resources/Appliance.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("# Appliance\n\n![[Inbox/IMG_4022.png]]\n")
        self.assertEqual(list(Vault(self.config).iter_loose_documents()), [])


class TestUnreadableImages(unittest.TestCase):
    def test_a_missing_backend_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            image.write_bytes(b"not really a jpeg")
            config = load_config(overrides={"ocr.backend": "none"})
            with self.assertRaises(OcrError) as caught:
                extract(image, config.ocr, work_dir=Path(tmp))
            self.assertIn("no OCR backend", str(caught.exception))

    def test_a_pdf_is_still_treated_as_a_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Several ordinary lines: one very long line runs off the page and
            # poppler, unlike pypdf, drops what falls outside it.
            pdf = make_text_pdf(
                Path(tmp) / "doc.pdf",
                [
                    "ACME LTD",
                    "INVOICE 2024-05-02",
                    "Invoice number INV-1234",
                    "Payment due within 30 days of the invoice date, by bank transfer.",
                    "Acme Ltd, 12 Example Street, London. VAT GB123456789.",
                    "Thank you for your business; payment references the invoice number.",
                ],
            )
            config = load_config()
            result = extract(pdf, config.ocr)
            self.assertEqual(result.backend, "text-layer")


if __name__ == "__main__":
    unittest.main()
