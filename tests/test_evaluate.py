"""The corpus is how we tell an improvement from an opinion."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scanvault.config import load_config
from scanvault.evaluate import evaluate, load_corpus
from tests.helpers import StubClient

# Rules and heuristics alone, no model. Raise this when the floor rises; it is
# here to catch a change that quietly makes classification worse.
OFFLINE_FLOOR = 0.80


class TestCorpus(unittest.TestCase):
    def test_it_covers_both_languages_and_both_contexts(self):
        corpus = load_corpus()
        languages = {document["language"] for document in corpus}
        contexts = {document["context"] for document in corpus}
        self.assertEqual(languages, {"en", "sv"})
        self.assertEqual(contexts, {"personal", "business"})
        self.assertGreaterEqual(len(corpus), 30)

    def test_every_document_is_labelled(self):
        for document in load_corpus():
            self.assertTrue(document["id"])
            self.assertTrue(document["text"].strip())
            self.assertTrue(document["category"])


class TestOfflineScore(unittest.TestCase):
    def setUp(self):
        self.score = evaluate(load_config(), client=None)

    def test_the_floor_holds(self):
        self.assertGreaterEqual(self.score.accuracy(), OFFLINE_FLOOR, self.score.report())

    def test_both_languages_clear_the_floor(self):
        for language in ("en", "sv"):
            self.assertGreaterEqual(
                self.score.accuracy(language=language), OFFLINE_FLOOR, language
            )

    def test_business_documents_clear_the_floor(self):
        self.assertGreaterEqual(self.score.accuracy(context="business"), OFFLINE_FLOOR)

    def test_the_expected_tags_are_all_found(self):
        self.assertEqual(self.score.tag_recall(), 1.0, self.score.report())

    def test_the_report_names_what_it_missed(self):
        report = self.score.report()
        self.assertIn("category", report)
        for result in self.score.failures():
            self.assertIn(result.id, report)


class TestHarness(unittest.TestCase):
    def test_a_model_that_answers_perfectly_scores_perfectly(self):
        corpus = [
            {
                "id": "one",
                "language": "en",
                "context": "personal",
                "category": "Invoices",
                "tags": ["invoices"],
                "text": "A document about nothing in particular.",
            }
        ]
        client = StubClient({"title": "Something", "category": "Invoices", "confidence": 0.9})
        score = evaluate(load_config(), client, corpus)
        self.assertEqual(score.accuracy(), 1.0)
        self.assertEqual(score.failures(), [])

    def test_a_wrong_answer_is_reported(self):
        corpus = [
            {
                "id": "two",
                "language": "sv",
                "context": "business",
                "category": "Taxes",
                "tags": ["taxes"],
                "text": "Ett dokument om ingenting särskilt.",
            }
        ]
        client = StubClient({"title": "Nagot", "category": "Manuals", "confidence": 0.9})
        score = evaluate(load_config(), client, corpus)
        self.assertEqual(score.accuracy(), 0.0)
        self.assertIn("two", score.report())

    def test_a_corpus_can_come_from_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corpus.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "id": "custom",
                            "language": "sv",
                            "context": "personal",
                            "category": ["Receipts"],
                            "tags": [],
                            "text": "KVITTO\nTotalt 154 kr betalt med kort",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            score = evaluate(load_config(), None, load_corpus(path))
            self.assertEqual(len(score.results), 1)


if __name__ == "__main__":
    unittest.main()
