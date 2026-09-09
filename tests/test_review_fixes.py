"""Regressions found in the code review."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scanvault.cache import ClassificationCache
from scanvault.classify import _match_category, classify, document_excerpt
from scanvault.config import load_config
from scanvault.organizer import plan
from scanvault.util import parse_date
from tests.helpers import StubClient

# No tags, so the note counts as incomplete and every run wants to classify it.
NOTE = (
    '---\ntitle: "scan"\ncategory: "Other"\nclassifier: "llm"\n---\n\n'
    "A letter about a mortgage from the bank, dated 2024-05-02.\n"
)


class TestReclassifyIgnoresTheCache(unittest.TestCase):
    """--reclassify asked the cache, got the old answer, and changed nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        note = self.root / "Scanned/scan.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(NOTE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_normal_run_reuses_the_cached_answer(self):
        first = StubClient({"title": "First", "category": "Banking"})
        plan(self.config, client=first)
        self.assertEqual(len(first.calls), 1, "the first run does ask")
        second = StubClient({"title": "Second", "category": "Banking"})
        plan(self.config, client=second)
        self.assertEqual(len(second.calls), 0)

    def test_reclassify_asks_again(self):
        plan(self.config, client=StubClient({"title": "First", "category": "Banking"}))
        second = StubClient({"title": "Second", "category": "Banking"})
        report = plan(self.config, client=second, reclassify=True)
        self.assertEqual(len(second.calls), 1, "the whole point of --reclassify")
        action = next(a for a in report.actions if a.meta)
        self.assertEqual(action.meta.title, "Second")

    def test_the_fresh_answer_is_then_cached(self):
        plan(self.config, client=StubClient({"title": "First", "category": "Banking"}))
        plan(self.config, client=StubClient({"title": "Second", "category": "Banking"}), reclassify=True)
        third = StubClient({"title": "Third", "category": "Banking"})
        report = plan(self.config, client=third)
        self.assertEqual(len(third.calls), 0)
        action = next(a for a in report.actions if a.meta)
        self.assertEqual(action.meta.title, "Second", "the reclassified answer replaced the old one")

    def test_the_cache_can_be_told_not_to_answer(self):
        cache = ClassificationCache(self.root / ".scanvault", self.config, read=False)
        cache.put("text", {"title": "x"})
        self.assertIsNone(cache.get("text"))
        cache.save()
        self.assertIsNotNone(
            ClassificationCache(self.root / ".scanvault", self.config).get("text"),
            "writing still happened",
        )


class TestCategoryMatching(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_close_variants_land_where_they_should(self):
        for said, expected in [
            ("Banking", "Banking"),
            ("Bank", "Banking"),
            ("bank statement", "Banking"),
            ("invoice", "Invoices"),
            ("Utility bills", "Utilities"),
            ("Tax", "Taxes"),
            ("Student loan", "Loans"),
            ("medical records", "Medical"),
            ("governmental", "Government"),
        ]:
            self.assertEqual(_match_category(said, self.config), expected, said)

    def test_nonsense_falls_back_instead_of_matching_a_substring(self):
        # "Motherhood" used to match "Other" by substring, which was luck.
        for said in ("Motherhood", "Nonsense", "", None, 42):
            self.assertEqual(_match_category(said, self.config), "Other", repr(said))


class TestAmbiguousDates(unittest.TestCase):
    def test_a_day_past_the_twelfth_is_no_longer_dropped(self):
        self.assertEqual(str(parse_date("13/04/2024")), "2024-04-13")

    def test_the_reading_order_is_the_callers_choice(self):
        self.assertEqual(str(parse_date("03/04/2024")), "2024-04-03")
        self.assertEqual(str(parse_date("03/04/2024", day_first=False)), "2024-03-04")

    def test_an_impossible_first_reading_falls_back(self):
        self.assertEqual(str(parse_date("12/25/2024")), "2024-12-25")

    def test_iso_is_never_ambiguous(self):
        self.assertEqual(str(parse_date("2024-05-02")), "2024-05-02")
        self.assertEqual(str(parse_date("2024-05-02", day_first=False)), "2024-05-02")


class TestPromptBudget(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_a_short_document_is_sent_whole(self):
        self.assertEqual(document_excerpt("short text", self.config), "short text")

    def test_a_long_document_is_sampled_from_both_ends(self):
        text = "HEAD " + ("filler " * 20000) + " TAIL"
        excerpt = document_excerpt(text, self.config)
        self.assertIn("HEAD", excerpt)
        self.assertIn("TAIL", excerpt, "totals and signatures live at the bottom")
        self.assertIn("[...]", excerpt)
        self.assertLess(len(excerpt), len(text))

    def test_the_excerpt_respects_a_small_context_window(self):
        self.config.llm.num_ctx = 2048
        excerpt = document_excerpt("x" * 50000, self.config)
        self.assertLessEqual(len(excerpt), int(2048 * 3.5) - 4000 + 20)


class TestReviewTag(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_a_heuristic_result_asks_to_be_looked_at(self):
        meta = classify("ACME LTD INVOICE\nAmount due", self.config, None)
        self.assertIn("needs-review", meta.tags)

    def test_a_low_confidence_answer_asks_to_be_looked_at(self):
        client = StubClient({"title": "Maybe", "category": "Other", "confidence": 0.2})
        self.assertIn("needs-review", classify("text", self.config, client).tags)

    def test_a_confident_answer_does_not(self):
        client = StubClient({"title": "Sure", "category": "Banking", "confidence": 0.9})
        self.assertNotIn("needs-review", classify("text", self.config, client).tags)


class TestTagRulesMerge(unittest.TestCase):
    def test_extra_rules_add_to_the_built_in_table(self):
        config = load_config()
        config.tags.extra_rules = {"boat": ["mooring"], "taxes": ["kommunalskatt"]}
        merged = config.tags.all_rules()
        self.assertIn("boat", merged)
        self.assertIn("government", merged, "the built-ins are still there")
        self.assertIn("kommunalskatt", merged["taxes"])
        self.assertIn("hmrc", merged["taxes"], "and the built-in keywords survive")


if __name__ == "__main__":
    unittest.main()
