"""Reading the date off the document itself.

The cases here are the ones that were silently falling through to the file's
timestamp: month names in text, Swedish spellings, and every document that
prints a due date next to its own.
"""

from __future__ import annotations

import os
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from scanvault.classify import DocumentMeta, resolve_date
from scanvault.config import load_config
from scanvault.dates import date_from_text, find_dates, strong_date
from scanvault.util import file_created_date

TODAY = date(2026, 9, 10)


def read(text: str, day_first: bool = True) -> date | None:
    found = date_from_text(text, day_first, today=TODAY)
    return found[0] if found else None


class TestFormats(unittest.TestCase):
    """Every shape a date is written in, in text rather than in a filename."""

    def check(self, expected: date, *samples: str) -> None:
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(read(f"Date: {sample}"), expected)

    def test_numeric(self):
        self.check(
            date(2024, 5, 2),
            "2024-05-02", "2024/05/02", "2024.05.02", "02/05/2024", "2.5.2024",
            "02-05-2024", "2/5/24",
        )

    def test_english_month_names(self):
        self.check(
            date(2024, 5, 2),
            "2 May 2024", "2nd May 2024", "May 2, 2024", "May 2nd, 2024",
            "2 may 2024", "MAY 2 2024",
        )

    def test_abbreviated_months(self):
        self.check(date(2017, 3, 5), "5 Mar 2017", "Mar. 5, 2017", "5 March 2017")
        self.check(date(2021, 9, 8), "8 Sept 2021", "Sep 8 2021")

    def test_swedish_month_names(self):
        self.check(date(2024, 5, 2), "2 maj 2024", "den 2 maj 2024", "2 Maj 2024")
        self.check(date(2023, 10, 14), "14 oktober 2023", "14 okt 2023")
        self.check(date(2022, 1, 31), "31 januari 2022", "31 jan 2022")

    def test_swedish_diacritics_survive_a_missing_language_pack(self):
        # Tesseract without swe drops the diacritics rather than garbling them.
        self.assertEqual(read("Fakturadatum: 2024-05-02"), date(2024, 5, 2))
        self.assertEqual(read("Utfärdat den 2 maj 2024"), date(2024, 5, 2))
        self.assertEqual(read("Utfardat den 2 maj 2024"), date(2024, 5, 2))

    def test_day_first_is_configurable(self):
        self.assertEqual(read("Date: 03/04/2024"), date(2024, 4, 3))
        self.assertEqual(read("Date: 03/04/2024", day_first=False), date(2024, 3, 4))
        # 13 is not a month either way round.
        self.assertEqual(read("Date: 13/04/2024", day_first=False), date(2024, 4, 13))

    def test_a_month_and_a_year_dates_a_statement_to_the_first(self):
        self.assertEqual(read("Statement date May 2024"), date(2024, 5, 1))

    def test_nonsense_is_not_a_date(self):
        for sample in ("Account 4242 0000 1111", "Invoice 12345678", "Date: 2024-13-45"):
            with self.subTest(sample=sample):
                self.assertIsNone(read(sample))


class TestLabels(unittest.TestCase):
    """Which of the dates on the page is the document's own."""

    def test_the_invoice_date_beats_the_due_date(self):
        text = "Fakturadatum: 2024-05-02\nFörfallodatum: 2024-06-01"
        self.assertEqual(read(text), date(2024, 5, 2))

    def test_the_due_date_alone_dates_nothing(self):
        for text in (
            "Amount due 2024-06-01",
            "Payment due by 1 June 2024",
            "Sista betalningsdag 2024-06-01",
            "Valid until 2024-06-01",
            "Date of birth: 1979-03-04",
            "Personnummer 19790304-1234",
        ):
            with self.subTest(text=text):
                self.assertIsNone(read(text), text)

    def test_a_labelled_date_beats_an_unlabelled_one_further_up(self):
        text = "Reference 2019-04-02 job number\n\nInvoice date: 2024-05-02"
        self.assertEqual(read(text), date(2024, 5, 2))

    def test_an_unlabelled_date_near_the_top_still_counts(self):
        text = "ACME LTD\n17 High Street\n\n2 May 2024\n\nDear Sir,\n"
        self.assertEqual(read(text), date(2024, 5, 2))

    def test_the_earliest_unlabelled_date_wins(self):
        text = "2 May 2024\n\nsome body text\n\nour reference 9 September 2024"
        self.assertEqual(read(text), date(2024, 5, 2))

    def test_a_period_is_not_a_date(self):
        text = "Coverage: 1 Jan 2024 - 31 Dec 2024\nDatum 2024-02-15"
        self.assertEqual(read(text), date(2024, 2, 15))

    def test_a_period_with_no_other_date_leaves_it_undated(self):
        self.assertIsNone(read("Perioden 2024-01-01 till 2024-12-31"))

    def test_a_word_ending_in_a_label_is_not_a_label(self):
        # "Validated" ends in "dated" and must not make this a labelled date.
        candidates = find_dates("Validated 2024-05-02", today=TODAY)
        self.assertEqual([candidate.label for candidate in candidates], [""])

    def test_a_future_date_is_not_this_document(self):
        self.assertIsNone(read("Date: 2030-01-01"))

    def test_the_invoice_date_beats_the_day_it_was_printed(self):
        text = "Utskriftsdatum 2024-09-09\nFakturadatum 2024-05-02"
        self.assertEqual(read(text), date(2024, 5, 2))

    def test_strong_date_only_answers_for_an_unambiguous_label(self):
        self.assertEqual(strong_date("Fakturadatum 2024-05-02", today=TODAY), date(2024, 5, 2))
        self.assertIsNone(strong_date("2024-05-02 dear sir", today=TODAY))


class TestResolveDate(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.config = load_config(overrides={"vault_dir": self.tmp.name})

    def tearDown(self):
        self.tmp.cleanup()

    def meta(self, value: date | None = None) -> DocumentMeta:
        return DocumentMeta(title="t", category="Other", document_date=value)

    def test_the_text_is_read_before_the_files_timestamps(self):
        pdf = Path(self.tmp.name) / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        meta = resolve_date(self.meta(), pdf, self.config, "Fakturadatum: 2024-05-02")
        self.assertEqual(meta.document_date, date(2024, 5, 2))
        self.assertEqual(meta.date_source, "text")

    def test_the_model_still_wins_when_its_date_is_in_the_document(self):
        meta = resolve_date(self.meta(date(2024, 5, 2)), None, self.config, "dated 2 May 2024")
        self.assertEqual(meta.document_date, date(2024, 5, 2))
        self.assertEqual(meta.date_source, "document")

    def test_a_date_the_model_invented_yields_to_a_labelled_one(self):
        meta = resolve_date(
            self.meta(date(2019, 1, 1)), None, self.config, "Invoice date: 2024-05-02"
        )
        self.assertEqual(meta.document_date, date(2024, 5, 2))

    def test_an_unlabelled_text_date_does_not_overrule_the_model(self):
        meta = resolve_date(
            self.meta(date(2019, 1, 1)), None, self.config, "some text 2024-05-02 more"
        )
        self.assertEqual(meta.document_date, date(2019, 1, 1))

    def test_the_filename_is_only_tried_when_the_text_has_nothing(self):
        pdf = Path(self.tmp.name) / "receipt 2016-09-08.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        meta = resolve_date(self.meta(), pdf, self.config, "no dates here at all")
        self.assertEqual(meta.document_date, date(2016, 9, 8))
        self.assertEqual(meta.date_source, "filename")

    def test_the_fallback_order_is_honoured(self):
        """Put the filename first and it beats the text, as configured."""
        pdf = Path(self.tmp.name) / "receipt 2016-09-08.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        self.config.dates.fallbacks = ["filename", "text"]
        meta = resolve_date(self.meta(), pdf, self.config, "Fakturadatum: 2024-05-02")
        self.assertEqual(meta.date_source, "filename")

    def test_dropping_text_from_the_fallbacks_turns_the_reader_off(self):
        self.config.dates.fallbacks = ["filename"]
        meta = resolve_date(self.meta(), None, self.config, "Fakturadatum: 2024-05-02")
        self.assertIsNone(meta.document_date)


class TestFileTimestamps(unittest.TestCase):
    def test_a_copied_file_is_dated_by_the_older_timestamp(self):
        """A vault copy has today's birth time and the original's mtime."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.pdf"
            path.write_bytes(b"%PDF-1.4")
            old = time.time() - 400 * 24 * 3600
            os.utime(path, (old, old))
            expected = date.today() - timedelta(days=400)
            self.assertEqual(file_created_date(path), expected)


if __name__ == "__main__":
    unittest.main()
