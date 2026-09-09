"""Where a document's date comes from, and in what order."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from scanvault.classify import DocumentMeta, resolve_date
from scanvault.config import load_config
from scanvault.extract import pdf_creation_date
from scanvault.util import date_from_filename, file_created_date
from tests.helpers import make_text_pdf

HAS_PYPDF = importlib.util.find_spec("pypdf") is not None


class TestFilenameDates(unittest.TestCase):
    def check(self, name: str, expected: date | None):
        self.assertEqual(date_from_filename(name), expected, name)

    def test_iso_forms(self):
        self.check("receipt 2016-09-08.pdf", date(2016, 9, 8))
        self.check("scan 2016_09_08.pdf", date(2016, 9, 8))
        self.check("2019.11.19 letter.pdf", date(2019, 11, 19))

    def test_month_names(self):
        self.check("receipt Mar 5, 2017 11.57 AM.pdf", date(2017, 3, 5))
        self.check("letter March 5 2017.pdf", date(2017, 3, 5))
        self.check("notes 1 May 2020.pdf", date(2020, 5, 1))
        self.check("statement_602334_03_Jul_2025.pdf", date(2025, 7, 3))
        self.check("scan Sept 15, 2017.pdf", date(2017, 9, 15))

    def test_things_that_are_not_dates(self):
        self.check("account 82936904.pdf", None)
        self.check("Scan 10.pdf", None)
        self.check("conference April.pdf", None)
        self.check("statement 2025-13-45.pdf", None)
        self.check("invoice 12345678.pdf", None)


class TestDateResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config()

    def tearDown(self):
        self.tmp.cleanup()

    def meta(self, document_date: date | None = None) -> DocumentMeta:
        return DocumentMeta(title="Letter", category="Correspondence", document_date=document_date)

    def test_a_date_in_the_document_always_wins(self):
        pdf = make_text_pdf(self.root / "letter 2020-01-01.pdf", ["hello"])
        meta = resolve_date(self.meta(date(2017, 3, 5)), pdf, self.config)
        self.assertEqual(meta.document_date, date(2017, 3, 5))
        self.assertEqual(meta.date_source, "document")

    def test_filename_is_the_first_fallback(self):
        pdf = make_text_pdf(self.root / "letter Mar 5, 2017.pdf", ["hello"])
        meta = resolve_date(self.meta(), pdf, self.config)
        self.assertEqual(meta.document_date, date(2017, 3, 5))
        self.assertEqual(meta.date_source, "filename")

    def test_file_date_is_the_last_resort(self):
        pdf = make_text_pdf(self.root / "Scan 10.pdf", ["hello"])
        stamp = time.mktime(date(2015, 6, 1).timetuple())
        os.utime(pdf, (stamp, stamp))
        meta = resolve_date(self.meta(), pdf, self.config)
        self.assertEqual(meta.date_source, "file-created")
        self.assertIsNotNone(meta.document_date)

    def test_fallbacks_can_be_turned_off(self):
        pdf = make_text_pdf(self.root / "letter Mar 5, 2017.pdf", ["hello"])
        self.config.dates.fallbacks = []
        meta = resolve_date(self.meta(), pdf, self.config)
        self.assertIsNone(meta.document_date)
        self.assertEqual(meta.date_source, "")
        self.assertEqual(meta.year, "undated")

    def test_order_is_configurable(self):
        pdf = make_text_pdf(self.root / "letter Mar 5, 2017.pdf", ["hello"])
        stamp = time.mktime(date(2015, 6, 1).timetuple())
        os.utime(pdf, (stamp, stamp))
        self.config.dates.fallbacks = ["file-created", "filename"]
        meta = resolve_date(self.meta(), pdf, self.config)
        self.assertEqual(meta.date_source, "file-created")

    def test_an_unknown_fallback_is_skipped_not_fatal(self):
        pdf = make_text_pdf(self.root / "letter Mar 5, 2017.pdf", ["hello"])
        self.config.dates.fallbacks = ["nonsense", "filename"]
        meta = resolve_date(self.meta(), pdf, self.config)
        self.assertEqual(meta.date_source, "filename")

    @unittest.skipUnless(HAS_PYPDF, "needs pypdf to write PDF metadata")
    def test_pdf_metadata_is_used_before_the_file_date(self):
        from pypdf import PdfWriter

        source = make_text_pdf(self.root / "Scan 11.pdf", ["hello"])
        writer = PdfWriter(clone_from=str(source))
        writer.add_metadata({"/CreationDate": "D:20180417120000Z"})
        target = self.root / "Scan 12.pdf"
        with open(target, "wb") as handle:
            writer.write(handle)

        self.assertEqual(pdf_creation_date(target), date(2018, 4, 17))
        meta = resolve_date(self.meta(), target, self.config)
        self.assertEqual(meta.document_date, date(2018, 4, 17))
        self.assertEqual(meta.date_source, "pdf-metadata")

    def test_file_created_date_handles_a_missing_file(self):
        self.assertIsNone(file_created_date(self.root / "gone.pdf"))


if __name__ == "__main__":
    unittest.main()
