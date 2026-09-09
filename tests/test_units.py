"""Unit tests for the pure pieces: util, config, classify, llm parsing, state."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scanvault.classify import build_prompt, classify, from_response, heuristic
from scanvault.config import load_config
from scanvault.llm import LlmError, parse_json_object
from scanvault.state import State
from scanvault.util import parse_date, safe_filename, sha256_file, slugify, truncate_words, unique_path
from tests.helpers import StubClient


class TestUtil(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Acme Corp – Invoice #42"), "acme-corp-invoice-42")
        self.assertEqual(slugify("  MULTIPLE   spaces "), "multiple-spaces")
        self.assertEqual(slugify(""), "")

    def test_safe_filename_strips_path_separators(self):
        self.assertNotIn("/", safe_filename("2024/05 report"))
        self.assertEqual(safe_filename("..."), "untitled")
        self.assertEqual(safe_filename("con:file?"), "con-file")

    def test_parse_date_formats(self):
        self.assertEqual(parse_date("Issued 2024-05-02."), date(2024, 5, 2))
        self.assertEqual(parse_date("03/14/2024"), date(2024, 3, 14))
        self.assertEqual(parse_date("14.03.2024"), date(2024, 3, 14))
        self.assertIsNone(parse_date("no date here"))
        self.assertIsNone(parse_date(None))

    def test_parse_date_rejects_impossible(self):
        self.assertIsNone(parse_date("2024-13-45"))

    def test_truncate_words(self):
        text = "word " * 100
        clipped = truncate_words(text, 50)
        self.assertLess(len(clipped), len(text))
        self.assertIn("truncated", clipped)
        self.assertEqual(truncate_words("short", 50), "short")

    def test_unique_path_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "note.md"
            self.assertEqual(unique_path(target), target)
            target.write_text("x")
            self.assertEqual(unique_path(target).name, "note-2.md")
            self.assertEqual(len(sha256_file(target)), 64)


class TestConfig(unittest.TestCase):
    def test_overrides_and_nested_sections(self):
        config = load_config(overrides={"vault_dir": "/tmp/v", "llm.model": "qwen3.5:9b", "ocr.force": True})
        self.assertEqual(config.vault_dir, Path("/tmp/v"))
        self.assertEqual(config.llm.model, "qwen3.5:9b")
        self.assertTrue(config.ocr.force)
        self.assertEqual(config.notes_root, Path("/tmp/v"))
        self.assertEqual(config.vault.para.archive_dir, "4 Archive")

    def test_toml_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scanvault.toml"
            path.write_text('vault_dir = "/tmp/vault"\n[llm]\nmodel = "custom:7b"\n[vault]\nsource_action = "copy"\n')
            config = load_config(path)
            self.assertEqual(config.llm.model, "custom:7b")
            self.assertEqual(config.vault.source_action, "copy")

    def test_unknown_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scanvault.toml"
            path.write_text('nope = 1\n')
            with self.assertRaises(ValueError):
                load_config(path)


class TestLlmParsing(unittest.TestCase):
    def test_plain_and_fenced_json(self):
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json_object('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(parse_json_object('Sure!\n{"a": 3}\nHope that helps'), {"a": 3})

    def test_rejects_non_objects(self):
        with self.assertRaises(LlmError):
            parse_json_object("[1, 2]")
        with self.assertRaises(LlmError):
            parse_json_object("no json at all")


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_from_response_normalises(self):
        meta = from_response(
            {
                "title": "  Acme   Invoice  ",
                "category": "invoice",
                "document_date": "2024-05-02",
                "tags": ["#Acme", "Bill", "bill"],
                "confidence": 0.87,
            },
            self.config,
            "fallback",
        )
        self.assertEqual(meta.title, "Acme Invoice")
        self.assertEqual(meta.category, "Invoices")
        self.assertEqual(meta.document_date, date(2024, 5, 2))
        self.assertEqual(meta.tags, ["scan", "invoices", "year-2024", "acme", "bill"])
        self.assertEqual(meta.year, "2024")

    def test_unknown_category_falls_back_to_last(self):
        meta = from_response({"title": "x", "category": "Zebra"}, self.config, "fallback")
        self.assertEqual(meta.category, "Other")

    def test_missing_date_gives_undated_placeholders(self):
        meta = from_response({"title": "x", "category": "Other"}, self.config, "fallback")
        self.assertEqual(meta.placeholders()["date"], "undated")
        self.assertEqual(meta.year, "undated")

    def test_heuristic_picks_category_and_date(self):
        meta = heuristic("ACME LTD\nINVOICE\nAmount due 2024-05-02", self.config, "fallback")
        self.assertEqual(meta.category, "Invoices")
        self.assertEqual(meta.document_date, date(2024, 5, 2))
        self.assertEqual(meta.classifier, "heuristic")

    def test_prompt_lists_categories_and_clips_text(self):
        prompt = build_prompt("x" * 50000, self.config, "scan.pdf")
        self.assertIn("Invoices", prompt)
        self.assertIn("scan.pdf", prompt)
        self.assertLess(len(prompt), 50000)

    def test_classify_uses_client(self):
        client = StubClient({"title": "Rent Contract", "category": "Contracts", "tags": ["rent"]})
        meta = classify("some text", self.config, client)
        self.assertEqual(meta.title, "Rent Contract")
        self.assertEqual(meta.classifier, "llm")
        self.assertEqual(len(client.calls), 1)

    def test_classify_falls_back_when_llm_errors(self):
        client = StubClient(LlmError("boom"))
        meta = classify("INVOICE total due", self.config, client)
        self.assertEqual(meta.classifier, "heuristic")

    def test_classify_reraises_when_fallback_disabled(self):
        self.config.llm.fallback_to_heuristics = False
        with self.assertRaises(LlmError):
            classify("text", self.config, StubClient(LlmError("boom")))

    def test_classify_handles_empty_text(self):
        meta = classify("   ", self.config, StubClient({"title": "unused", "category": "Other"}))
        self.assertIn("empty-text", meta.tags)


class TestState(unittest.TestCase):
    def test_roundtrip_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = State(root / ".scanvault")
            state.record("abc", note="Documents/a.md", title="A")
            state.save()

            reloaded = State(root / ".scanvault")
            self.assertEqual(reloaded.get("abc")["title"], "A")
            self.assertIn("processed_at", reloaded.get("abc"))
            self.assertEqual(reloaded.prune(root), 1)
            reloaded.save()
            self.assertEqual(json.loads((root / ".scanvault" / "index.json").read_text())["documents"], {})

    def test_corrupt_index_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".scanvault"
            root.mkdir()
            (root / "index.json").write_text("{not json")
            self.assertEqual(State(root).documents, {})


if __name__ == "__main__":
    unittest.main()


class TestLoggingSetup(unittest.TestCase):
    """Third-party PDF chatter must not bury the plan output."""

    def setUp(self):
        import logging

        self.saved = {
            name: logging.getLogger(name).level for name in ("pypdf", "fontTools", "scanvault")
        }

    def tearDown(self):
        import logging

        for name, level in self.saved.items():
            logging.getLogger(name).setLevel(level)

    def test_library_loggers_are_quiet_by_default(self):
        import logging

        from scanvault.cli import _configure_logging

        _configure_logging(verbose=0, quiet=False)
        self.assertFalse(logging.getLogger("pypdf").isEnabledFor(logging.WARNING))
        self.assertFalse(logging.getLogger("fontTools").isEnabledFor(logging.WARNING))
        self.assertTrue(logging.getLogger("scanvault").isEnabledFor(logging.INFO))

    def test_double_verbose_brings_them_back(self):
        import logging

        from scanvault.cli import _configure_logging

        _configure_logging(verbose=2, quiet=False)
        self.assertTrue(logging.getLogger("pypdf").isEnabledFor(logging.WARNING))
