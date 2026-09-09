"""The model is asked once: a preview and the --apply that follows share answers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scanvault.cache import CACHE_FILENAME, ClassificationCache
from scanvault.classify import classify
from scanvault.config import load_config
from scanvault.organizer import apply as organizer_apply
from scanvault.organizer import plan
from tests.helpers import StubClient

RESPONSE = {"title": "Rental Agreement", "category": "Contracts", "tags": ["rent"]}
NOTE = "---\ntitle: \"scan\"\nclassifier: \"heuristic\"\n---\n\nRENTAL AGREEMENT with the landlord, dated 2023-01-15.\n"


class TestCacheBasics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(overrides={"vault_dir": str(self.root)})

    def tearDown(self):
        self.tmp.cleanup()

    def cache(self) -> ClassificationCache:
        return ClassificationCache(self.root / ".scanvault", self.config)

    def test_round_trip_through_disk(self):
        cache = self.cache()
        self.assertIsNone(cache.get("some text"))
        cache.put("some text", RESPONSE)
        cache.save()

        reloaded = self.cache()
        self.assertEqual(reloaded.get("some text"), RESPONSE)
        self.assertTrue((self.root / ".scanvault" / CACHE_FILENAME).is_file())

    def test_a_different_model_does_not_hit(self):
        cache = self.cache()
        cache.put("some text", RESPONSE)
        cache.save()

        self.config.llm.model = "something-else:7b"
        self.assertIsNone(self.cache().get("some text"))

    def test_a_different_category_list_does_not_hit(self):
        cache = self.cache()
        cache.put("some text", RESPONSE)
        cache.save()

        self.config.categories = ["Invoices", "Other"]
        self.assertIsNone(self.cache().get("some text"))

    def test_edited_text_does_not_hit(self):
        cache = self.cache()
        cache.put("some text", RESPONSE)
        self.assertIsNone(cache.get("some other text"))

    def test_disabled_cache_never_stores_or_answers(self):
        cache = ClassificationCache(self.root / ".scanvault", self.config, enabled=False)
        cache.put("some text", RESPONSE)
        cache.save()
        self.assertIsNone(cache.get("some text"))
        self.assertFalse((self.root / ".scanvault" / CACHE_FILENAME).exists())

    def test_a_corrupt_cache_is_ignored(self):
        directory = self.root / ".scanvault"
        directory.mkdir(parents=True)
        (directory / CACHE_FILENAME).write_text("{not json")
        self.assertEqual(self.cache().entries, {})

    def test_an_older_version_is_ignored(self):
        directory = self.root / ".scanvault"
        directory.mkdir(parents=True)
        (directory / CACHE_FILENAME).write_text(json.dumps({"version": 0, "entries": {"x": {}}}))
        self.assertEqual(self.cache().entries, {})

    def test_classify_stores_and_reuses_the_answer(self):
        client = StubClient(RESPONSE)
        cache = self.cache()
        first = classify("RENTAL AGREEMENT", self.config, client, cache=cache)
        second = classify("RENTAL AGREEMENT", self.config, client, cache=cache)
        self.assertEqual(len(client.calls), 1, "the second call came from the cache")
        self.assertEqual(first.title, second.title)
        self.assertEqual(cache.hits, 1)


class TestPreviewThenApply(unittest.TestCase):
    """The complaint this was written for: a dry run must not be paid for twice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        for name in ("agreement", "lease"):
            note = self.root / "Scanned" / f"{name}.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(NOTE.replace("landlord", f"landlord of {name}"), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_apply_run_reuses_the_previews_answers(self):
        preview_client = StubClient(RESPONSE)
        plan(self.config, client=preview_client)
        self.assertEqual(len(preview_client.calls), 2, "the preview classified both notes")

        apply_client = StubClient(RESPONSE)
        report = plan(self.config, client=apply_client)
        organizer_apply(report, self.config, client=apply_client)
        self.assertEqual(len(apply_client.calls), 0, "no note was sent to the model twice")
        self.assertTrue(list((self.root / "4 Archive").rglob("*.md")))

    def test_no_cache_asks_again(self):
        first = StubClient(RESPONSE)
        plan(self.config, client=first, use_cache=False)
        second = StubClient(RESPONSE)
        plan(self.config, client=second, use_cache=False)
        self.assertEqual(len(second.calls), 2)

    def test_a_preview_writes_only_inside_the_state_folder(self):
        plan(self.config, client=StubClient(RESPONSE))
        written = {p.relative_to(self.root).parts[0] for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(written, {"Scanned", ".scanvault"})


if __name__ == "__main__":
    unittest.main()
