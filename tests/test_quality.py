r"""The OCR quality score: telling words from what a bad scan leaves behind.

Every case here is text of a kind this program really sees - a Swedish letter,
a bank statement that is mostly figures, a page the engine read as `1|\|\/O|CE`.
The thresholds are only useful if they sort those the way a person would.
"""

from __future__ import annotations

import unittest

from scanvault.config import QualityConfig
from scanvault.quality import (
    COMMON_WORDS,
    looks_garbled,
    looks_like_word,
    score_text,
)

ENGLISH_INVOICE = """ACME LTD
INVOICE 2024-05-02
Invoice number INV-1234
Amount due 120.00 EUR
Payment due within 30 days of the invoice date, by bank transfer.
Acme Ltd, 12 Example Street, London. VAT GB123456789."""

SWEDISH_LETTER = """Skatteverket
Slutskattebesked for inkomstar 2023
Du har fatt tillbaka 4 210 kronor pa ditt skattekonto.
Beloppet betalas ut till det konto du har anmalt.
Har du fragor kan du ringa oss pa 0771-567 567."""

GERMAN_BILL = """Stadtwerke Muenchen GmbH
Rechnung fuer Strom und Gas
Sehr geehrte Damen und Herren, anbei finden Sie die Rechnung
fuer den Abrechnungszeitraum. Der Betrag wird eingezogen."""

# Mostly figures, barely a function word in it, and perfectly readable.
BANK_STATEMENT = """NORDEA BANK ABP
Kontoutdrag 2024-03-01 - 2024-03-31
2024-03-02  Swish  -450,00   12 340,55
2024-03-04  Kortkop ICA  -289,50   12 051,05
2024-03-25  Lon  32 100,00   44 151,05
Ingaende saldo 12 790,55  Utgaende saldo 44 151,05"""

GIBBERISH = r"""1|\|\/O|CE  Nr. 4?3
rrn  tt||  ;;  gf-  ~~  |][  x
.,, wq  ]f  |I1  cX  rw8  ''  --
##  }{  ,,,  llllll  ..  \/\/"""

# The classic low-resolution failure: letters read as the digits they resemble.
LETTERS_AS_DIGITS = """AC|\\/|E  L1D
lnv0lce numb3r  1NV-l234
Amount due  l20.OO  EUR
Payment due w1th1n 3O days of the 1nv01ce date"""


class TestGoodTextScoresGood(unittest.TestCase):
    def test_an_english_invoice(self):
        quality = score_text(ENGLISH_INVOICE)
        self.assertEqual(quality.verdict, "good")
        self.assertGreater(quality.score, 0.8)

    def test_a_swedish_letter_without_the_language_pack(self):
        # Folded on both sides, so "Förfallodatum" read as "Forfallodatum" by a
        # tesseract with no Swedish pack still scores as the words it is.
        quality = score_text(SWEDISH_LETTER)
        self.assertEqual(quality.verdict, "good")

    def test_german(self):
        self.assertEqual(score_text(GERMAN_BILL).verdict, "good")

    def test_a_statement_that_is_mostly_figures(self):
        """No common words at all, and it still has to read as good.

        This is the case a naive "does it contain stopwords" check gets wrong,
        and getting it wrong means re-OCR'ing every bank statement in a vault.
        """
        quality = score_text(BANK_STATEMENT)
        self.assertEqual(quality.common_ratio, 0.0)
        self.assertEqual(quality.verdict, "good")


class TestBadTextScoresBad(unittest.TestCase):
    def test_scanner_noise(self):
        quality = score_text(GIBBERISH)
        self.assertEqual(quality.verdict, "gibberish")
        self.assertLess(quality.score, 0.25)

    def test_no_real_words_anywhere_is_floored(self):
        quality = score_text("qq ;; ]f |I1 cX rw8 zz xx |][ ~~ }{ ,,, .. \\/\\/ ##")
        self.assertEqual(quality.common_ratio, 0.0)
        self.assertLessEqual(quality.score, 0.05)

    def test_letters_read_as_digits(self):
        quality = score_text(LETTERS_AS_DIGITS)
        self.assertGreater(quality.garbled_ratio, 0.1)
        self.assertIn(quality.verdict, ("poor", "gibberish"))

    def test_prose_with_no_function_words_in_it_is_not_prose(self):
        """The subtle failure: every token word-shaped, none of them a word.

        This is what a page read slightly too small leaves behind, and it
        passes every other test here.
        """
        quality = score_text(
            "eter easng 5210 hon Mah 2024 Saran care aunt ate 0.28 pe "
            "ol sour sue 3820 GBP payable Src debi Ap kWh Mete raeding"
        )
        self.assertEqual(quality.common_ratio, 0.0)
        self.assertGreater(quality.word_ratio, 0.9, "nearly every token is word-shaped")
        self.assertEqual(quality.verdict, "poor")

    def test_a_table_of_figures_is_exempt_from_that(self):
        """It has no function words either, and it is perfectly readable."""
        quality = score_text(BANK_STATEMENT)
        self.assertEqual(quality.common_ratio, 0.0)
        self.assertEqual(quality.verdict, "good")

    def test_empty_text(self):
        quality = score_text("")
        self.assertEqual(quality.verdict, "empty")
        self.assertEqual(quality.score, 0.0)
        self.assertFalse(quality.readable)

    def test_whitespace_only_is_empty(self):
        self.assertEqual(score_text("   \n\n \t ").verdict, "empty")


class TestShortTextIsNotJudged(unittest.TestCase):
    """A receipt's whole text layer is two lines. That is not evidence."""

    def test_a_short_receipt_is_thin_not_bad(self):
        quality = score_text("ICA Kvantum\nTotalt 189,50\nMoms 25%")
        self.assertEqual(quality.verdict, "thin")
        self.assertTrue(quality.readable, "thin text is left alone, not re-OCR'd")

    def test_the_bar_is_configurable(self):
        text = "ICA Kvantum Totalt 189,50 Moms 25%"
        self.assertEqual(score_text(text, QualityConfig(min_sample_chars=5)).verdict, "good")


class TestDensity(unittest.TestCase):
    """A page that gave up a line and a half was not read, words or not."""

    def test_a_sparse_page_is_capped(self):
        text = "Invoice number and the total amount due on the date of this letter"
        loose = score_text(text)
        tight = score_text(text, pages=1)
        self.assertEqual(loose.verdict, "good")
        self.assertLess(tight.score, loose.score)
        self.assertEqual(tight.verdict, "poor")

    def test_a_full_page_is_not_capped(self):
        text = (ENGLISH_INVOICE + "\n") * 3
        self.assertEqual(score_text(text, pages=1).verdict, "good")

    def test_pages_are_optional(self):
        self.assertIsNone(score_text(ENGLISH_INVOICE).density)
        self.assertIsNone(score_text(ENGLISH_INVOICE, pages=0).density)


class TestThresholds(unittest.TestCase):
    def test_the_threshold_moves_the_verdict(self):
        strict = QualityConfig(threshold=0.99, gibberish_below=0.9)
        self.assertEqual(score_text(BANK_STATEMENT).verdict, "good")
        self.assertNotEqual(score_text(BANK_STATEMENT, strict).verdict, "good")

    def test_score_is_always_in_range(self):
        for text in (ENGLISH_INVOICE, GIBBERISH, BANK_STATEMENT, LETTERS_AS_DIGITS, "x"):
            with self.subTest(text=text[:20]):
                self.assertGreaterEqual(score_text(text).score, 0.0)
                self.assertLessEqual(score_text(text).score, 1.0)


class TestWordShape(unittest.TestCase):
    def test_words(self):
        for token in ("invoice", "Förfallodatum", "Skatteverket", "EUR", "a", "GmbH"):
            with self.subTest(token=token):
                self.assertTrue(looks_like_word(token))

    def test_not_words(self):
        for token in ("rrn", "tt", "wq", "llllll", "x", "|I1", "lNVOlCE"):
            with self.subTest(token=token):
                self.assertFalse(looks_like_word(token))

    def test_a_run_of_words_glued_together_is_not_a_word(self):
        self.assertFalse(looks_like_word("a" * 40))


class TestGarbledTokens(unittest.TestCase):
    def test_digits_running_through_a_word(self):
        for token in ("lnv0lce", "numb3r", "w1th1n", "1nv01ce"):
            with self.subTest(token=token):
                self.assertTrue(looks_garbled(token))

    def test_reference_codes_are_not_garbled(self):
        for token in ("GB123456789", "P60", "INV1234", "2024", "invoice", "1040"):
            with self.subTest(token=token):
                self.assertFalse(looks_garbled(token))


class TestCommonWords(unittest.TestCase):
    def test_the_vocabulary_is_folded_and_long_enough_to_mean_something(self):
        for word in COMMON_WORDS:
            self.assertGreaterEqual(len(word), 3, word)
            self.assertEqual(word, word.lower(), word)
            self.assertTrue(word.isascii(), f"{word} was not folded")

    def test_it_covers_the_languages_this_paperwork_arrives_in(self):
        for word in ("the", "och", "und", "les", "fecha", "fattura", "factuur"):
            self.assertIn(word, COMMON_WORDS)


if __name__ == "__main__":
    unittest.main()
