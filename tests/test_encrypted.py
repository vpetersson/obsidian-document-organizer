"""Encrypted and password-protected PDFs."""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from scanvault.config import load_config
from scanvault.extract import OcrError, extract, needs_password, pdf_text
from scanvault.organizer import plan
from tests.helpers import make_text_pdf

HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
HAS_BACKEND = shutil.which("ocrmypdf") is not None or shutil.which("tesseract") is not None

LINES = [
    "ACME LTD",
    "INVOICE 2024-05-02",
    "Invoice number INV-1234",
    "Amount due 120.00 EUR",
    "Payment due within 30 days of the invoice date, by bank transfer.",
    "Acme Ltd, 12 Example Street, London. VAT GB123456789.",
]


def encrypt(source: Path, target: Path, user_password: str, owner_password: str | None = None) -> Path:
    from pypdf import PdfWriter  # type: ignore

    writer = PdfWriter(clone_from=str(source))
    writer.encrypt(user_password, owner_password=owner_password, algorithm="AES-256")
    with open(target, "wb") as handle:
        writer.write(handle)
    return target


@unittest.skipUnless(HAS_PYPDF and HAS_CRYPTO, "needs the fast extra (pypdf[crypto])")
class TestEncryptedPdfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plain = make_text_pdf(self.root / "plain.pdf", LINES)
        # Owner password only: the file is encrypted but opens without one.
        self.owner_only = encrypt(self.plain, self.root / "owner.pdf", "", owner_password="secret")
        self.locked = encrypt(self.plain, self.root / "locked.pdf", "hunter2")

    def tearDown(self):
        self.tmp.cleanup()

    def test_owner_password_only_pdfs_are_readable(self):
        self.assertFalse(needs_password(self.owner_only))
        self.assertIn("INV-1234", pdf_text(self.owner_only))

    def test_password_protected_pdfs_are_detected(self):
        self.assertTrue(needs_password(self.locked))
        self.assertEqual(pdf_text(self.locked), "")

    def test_plain_pdfs_are_not_flagged(self):
        self.assertFalse(needs_password(self.plain))

    def test_extract_explains_how_to_fix_it(self):
        config = load_config()
        with self.assertRaises(OcrError) as caught:
            extract(self.locked, config.ocr)
        message = str(caught.exception)
        self.assertIn("password-protected", message)
        self.assertIn("qpdf --decrypt", message)

    @unittest.skipUnless(HAS_BACKEND, "needs an OCR backend")
    def test_an_encrypted_but_openable_scan_still_processes(self):
        config = load_config()
        result = extract(self.owner_only, config.ocr)
        self.assertIn("INV-1234", result.text)

    def test_the_plan_says_why_a_locked_pdf_cannot_be_filed(self):
        vault = self.root / "vault"
        (vault / "Inbox").mkdir(parents=True)
        shutil.copyfile(self.locked, vault / "Inbox/locked.pdf")
        config = load_config(overrides={"vault_dir": str(vault)})

        report = plan(config, client=None)
        action = next(a for a in report.actions if a.kind == "adopt")
        self.assertIn("password-protected", action.reason)
        self.assertIn("qpdf --decrypt", action.reason)
        self.assertFalse(action.needs_ocr, "we must not promise OCR we cannot perform")


if __name__ == "__main__":
    unittest.main()
