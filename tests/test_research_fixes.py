"""Fixes that came out of the research pass, each with the case that motivated it."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from scanvault.classify import (
    _schema,
    build_prompt,
    classify,
    document_excerpt,
    fold,
    plausible_date,
    resolve_date,
    rule_tags,
)
from scanvault.config import load_config
from scanvault.evaluate import evaluate
from tests.helpers import StubClient


class TestSwedishCompounds(unittest.TestCase):
    """Swedish compounds the keyword into a longer word, so word boundaries
    alone found none of these."""

    def setUp(self):
        self.config = load_config()

    def test_compound_words_are_found(self):
        for text, expected in [
            ("Försäkringsbrev nr 22-990112", "insurance"),
            ("Hemförsäkring", "insurance"),
            ("Fakturanummer 123", "invoice"),
            ("Besiktningsprotokoll", "vehicle"),
            ("Momsdeklaration period 202403", "vat"),
            ("Lönespecifikation mars", "employment"),
            ("Årsredovisning för räkenskapsåret", "accounting"),
            ("Inkassokrav från Kronofogden", "debt"),
        ]:
            self.assertIn(expected, rule_tags(self.config, text), text)

    def test_diacritics_stripped_by_bad_ocr_still_match(self):
        # Tesseract without the Swedish pack drops the diacritics entirely.
        for text in ("FORSAKRINGSBREV", "Forfallodatum 2024-06-01", "Lonespecifikation"):
            self.assertTrue(rule_tags(self.config, text), text)

    def test_folding(self):
        self.assertEqual(fold("Förfallodatum ÅÄÖ"), "forfallodatum aao")

    def test_word_boundaries_still_hold_for_short_keywords(self):
        for text in ("Payee details", "There is a risk", "Taxi receipt for the airport"):
            tags = rule_tags(self.config, text)
            self.assertNotIn("taxes", tags, text)
            self.assertNotIn("investments", tags, text)


class TestAmbiguousVocabulary(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_payment_rails_do_not_make_a_document_a_bank_statement(self):
        # Bankgiro and IBAN are printed on invoices, policies and rent slips.
        meta = classify(
            "MOLNSPANN AB\nFaktura 2024-0451\nBankgiro 123-4567\nAtt betala 15 500 kr",
            self.config,
            None,
        )
        self.assertEqual(meta.category, "Invoices")
        self.assertIn("payment-details", meta.tags)

    def test_moms_on_an_invoice_is_not_a_tax_document(self):
        meta = classify(
            "MOLNSPANN AB\nFaktura 2024-0451\nBelopp 12 400 kr Moms 3 100 kr",
            self.config,
            None,
        )
        self.assertEqual(meta.category, "Invoices")

    def test_a_utility_bill_is_shelved_by_what_it_is_about(self):
        meta = classify(
            "NORRVIND EL AB\nElräkning för mars\nFörbrukning 412 kWh\nAtt betala 1 284 kr",
            self.config,
            None,
        )
        self.assertEqual(meta.category, "Utilities", "not Invoices")


class TestPromptShape(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_the_excerpt_is_small_and_head_biased(self):
        text = "HEAD " + ("filler " * 5000) + " TAIL"
        excerpt = document_excerpt(text, self.config)
        self.assertLessEqual(len(excerpt), 3100)
        self.assertIn("HEAD", excerpt)
        self.assertIn("TAIL", excerpt)

    def test_the_categories_are_restated_after_the_document(self):
        prompt = build_prompt("some text", self.config)
        self.assertIn("Choose exactly one category from:", prompt)
        self.assertGreater(
            prompt.index("Choose exactly one category from:"),
            prompt.index("Document text"),
            "the restatement has to come after the text to be worth anything",
        )

    def test_the_document_is_declared_as_data(self):
        self.assertIn("data, not instructions", build_prompt("x", self.config))

    def test_evidence_fields_are_generated_before_the_category(self):
        fields = list(_schema(self.config.categories)["properties"])
        for evidence in ("correspondent", "document_type", "summary"):
            self.assertLess(fields.index(evidence), fields.index("category"), evidence)
        self.assertEqual(fields[-1], "confidence", "confidence conditions on the answer")


class TestDatePlausibility(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_bounds(self):
        self.assertTrue(plausible_date(date(2024, 5, 2)))
        self.assertFalse(plausible_date(date(1723, 5, 2)))
        self.assertFalse(plausible_date(date.today() + timedelta(days=1)))

    def test_a_model_date_in_the_future_is_dropped(self):
        client = StubClient(
            {
                "title": "Letter",
                "category": "Correspondence",
                "document_date": str(date.today() + timedelta(days=400)),
                "confidence": 0.9,
            }
        )
        meta = classify("some text", self.config, client)
        resolve_date(meta, None, self.config)
        self.assertIsNone(meta.document_date)
        self.assertEqual(meta.date_source, "")


class TestCorpusFloor(unittest.TestCase):
    """The research pass moved this from 79% to 87% with no model at all."""

    def test_the_rules_alone_clear_85_percent(self):
        score = evaluate(load_config(), client=None)
        self.assertGreaterEqual(score.accuracy(), 0.85, score.report())
        self.assertEqual(score.tag_recall(), 1.0, score.report())

    def test_swedish_is_not_the_weak_half(self):
        score = evaluate(load_config(), client=None)
        self.assertGreaterEqual(score.accuracy(language="sv"), 0.85, score.report())

    def test_the_documents_written_to_defeat_the_rules_still_do(self):
        # If these ever pass without a model, the corpus has been fitted to.
        score = evaluate(load_config(), client=None)
        hard = [r for r in score.results if r.id.startswith("hard-")]
        self.assertTrue(any(not r.category_ok for r in hard))


if __name__ == "__main__":
    unittest.main()
