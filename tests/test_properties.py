"""Folding the properties panel away.

Frontmatter on a filed document is bookkeeping - which classifier ran, where
the date came from, the hash. Obsidian shows all of it above every note, which
for a scanned letter is a screen of machine-readable detail before the letter.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scanvault.classify import DocumentMeta
from scanvault.config import load_config
from scanvault.vault import CSS_SNIPPET_NAME, Vault, css_snippet, parse_frontmatter


class TestCssClasses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = load_config(overrides={"vault_dir": self.tmp.name})
        self.vault = Vault(self.config)
        self.meta = DocumentMeta(
            title="Invoice INV-1234",
            category="Invoices",
            document_date=date(2024, 5, 2),
            correspondent="Acme Ltd",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def render(self) -> str:
        return self.vault.render_note(self.meta, "text", None)

    def test_a_note_carries_the_class_the_snippet_targets(self):
        frontmatter, _ = parse_frontmatter(self.render())
        self.assertEqual(frontmatter["cssclasses"], ["scanvault"])

    def frontmatter_keys(self, note: str) -> list[str]:
        block = note.split("---\n")[1]
        return [
            line.split(":", 1)[0]
            for line in block.splitlines()
            if line and not line.startswith((" ", "-"))
        ]

    def test_it_is_the_last_property(self):
        """The panel should end on the least interesting line, not open on it."""
        self.assertEqual(self.frontmatter_keys(self.render())[-1], "cssclasses")

    def test_it_stays_last_behind_the_attachment(self):
        note = self.vault.render_note(self.meta, "text", self.vault.attachment_path(self.meta))
        keys = self.frontmatter_keys(note)
        self.assertEqual(keys[-1], "cssclasses")
        self.assertIn("attachment", keys)

    def test_emptying_the_setting_writes_none(self):
        self.config.vault.cssclasses = []
        frontmatter, _ = parse_frontmatter(self.render())
        self.assertNotIn("cssclasses", frontmatter)

    def test_the_class_is_configurable(self):
        self.config.vault.cssclasses = ["paperwork", "no-properties"]
        frontmatter, _ = parse_frontmatter(self.render())
        self.assertEqual(frontmatter["cssclasses"], ["paperwork", "no-properties"])


class TestSnippet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.vault = Vault(self.config)
        self.path = self.root / ".obsidian" / "snippets" / f"{CSS_SNIPPET_NAME}.css"

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_lands_where_obsidian_looks_for_snippets(self):
        self.assertEqual(self.vault.write_css_snippet(), self.path)
        self.assertTrue(self.path.is_file())

    def test_it_targets_the_class_the_notes_carry(self):
        self.vault.write_css_snippet()
        self.assertIn(".scanvault .metadata-container", self.path.read_text())

    def test_a_preview_writes_nothing(self):
        self.assertEqual(self.vault.write_css_snippet(dry_run=True), self.path)
        self.assertFalse(self.path.exists())

    def test_an_existing_snippet_is_never_overwritten(self):
        """It is the user's file the moment it exists."""
        self.path.parent.mkdir(parents=True)
        self.path.write_text("/* mine */", encoding="utf-8")
        self.assertIsNone(self.vault.write_css_snippet())
        self.assertEqual(self.path.read_text(), "/* mine */")

    def test_no_classes_means_no_snippet(self):
        self.config.vault.cssclasses = []
        self.assertIsNone(self.vault.write_css_snippet())
        self.assertFalse(self.path.exists())

    def test_the_css_follows_the_configured_classes(self):
        self.assertIn(".paperwork .metadata-container", css_snippet(["paperwork"]))


class TestSnippetEnabled(unittest.TestCase):
    """Obsidian only loads snippets that are switched on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.vault = Vault(self.config)
        self.appearance = self.root / ".obsidian" / "appearance.json"
        self.appearance.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data: object) -> None:
        self.appearance.write_text(json.dumps(data), encoding="utf-8")

    def test_enabled(self):
        self.write({"enabledCssSnippets": ["scanvault", "other"]})
        self.assertIs(self.vault.snippet_enabled(), True)

    def test_written_but_not_switched_on(self):
        self.write({"enabledCssSnippets": ["other"]})
        self.assertIs(self.vault.snippet_enabled(), False)

    def test_no_appearance_file_is_unknown_rather_than_false(self):
        self.assertIsNone(self.vault.snippet_enabled())

    def test_unreadable_appearance_file_is_unknown(self):
        self.appearance.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.vault.snippet_enabled())


if __name__ == "__main__":
    unittest.main()
