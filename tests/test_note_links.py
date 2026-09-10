"""Notes that embed their scans rather than recording an `attachment:`.

A hand-written Obsidian note holds its pages as `![[Scan Page 196.jpg]]`. That
is markup naming a file, not a word of the document - reading it as text is how
a wikilink ends up as a note's title, in its filename, and quoted back inside
the extracted-text block where it neither renders nor links.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scanvault.classify import heuristic
from scanvault.config import load_config
from scanvault.organizer import (
    apply as organizer_apply,
    documents_need_ocr,
    note_documents,
    note_text,
    plan,
    strip_markup,
)
from scanvault.util import clean_title
from scanvault.vault import Vault, parse_frontmatter
from tests.helpers import make_scan_image

HAS_RASTERISER = shutil.which("pdftoppm") is not None
HAS_BACKEND = shutil.which("ocrmypdf") is not None or shutil.which("tesseract") is not None

BODY = "# Bank letter\n\n![[Scan Page 196.jpg]]\n![[Scan Page 197.jpg]]\n"
NOTE = (
    "---\ntitle: Bank letter\ncategory: Banking\ntags: [scan]\nclassifier: heuristic\n---\n\n"
    + BODY
)


class TestStripMarkup(unittest.TestCase):
    def test_embeds_are_not_text(self):
        self.assertEqual(strip_markup("# Title\n\n![[Scan Page 196.jpg]]\n"), "")

    def test_markdown_images_are_not_text_either(self):
        self.assertEqual(strip_markup("![alt](scans/page1.png)"), "")

    def test_prose_around_an_embed_survives(self):
        self.assertEqual(
            strip_markup("# T\n\nThe bank wrote to us.\n\n![[page.jpg]]\n"),
            "The bank wrote to us.",
        )


class TestCleanTitle(unittest.TestCase):
    def test_a_wikilink_is_never_a_title(self):
        self.assertEqual(clean_title("![[Scan Page 196.jpg]]"), "")

    def test_doubled_ocr_spaces_are_collapsed(self):
        self.assertEqual(clean_title("Statement  date  14  February"), "Statement date 14 February")


class TestHeuristicOnALinkOnlyNote(unittest.TestCase):
    def setUp(self):
        self.config = load_config(overrides={"vault_dir": "/tmp/x"})

    def test_a_link_does_not_become_the_title(self):
        meta = heuristic("![[Scan Page 196.jpg]]\n![[Scan Page 197.jpg]]", self.config, "Bank letter")
        self.assertEqual(meta.title, "Bank letter")
        self.assertEqual(meta.title_source, "filename")

    def test_a_link_does_not_become_a_date_or_a_category(self):
        meta = heuristic("![[Scan Page 196.jpg]]", self.config, "Bank letter")
        self.assertIsNone(meta.document_date)


class TestResolveLink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "Archive" / "Scans").mkdir(parents=True)
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.vault = Vault(self.config)
        self.note = self.root / "Archive" / "Notes" / "letter.md"
        self.note.parent.mkdir(parents=True, exist_ok=True)
        self.note.write_text(NOTE, encoding="utf-8")
        self.scan = self.root / "Archive" / "Scans" / "Scan Page 196.jpg"
        self.scan.write_bytes(b"\xff\xd8\xff\xe0fake")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_bare_name_is_found_anywhere_in_the_vault(self):
        """Obsidian's default link style carries no folder."""
        self.assertEqual(self.vault.resolve_link(self.note, "Scan Page 196.jpg"), self.scan)

    def test_a_vault_relative_path_resolves(self):
        self.assertEqual(
            self.vault.resolve_link(self.note, "Archive/Scans/Scan Page 196.jpg"), self.scan
        )

    def test_a_name_beside_the_note_wins(self):
        beside = self.note.parent / "Scan Page 196.jpg"
        beside.write_bytes(b"\xff\xd8\xff\xe0other")
        self.assertEqual(self.vault.resolve_link(self.note, "Scan Page 196.jpg"), beside)

    def test_an_ambiguous_name_resolves_to_nothing(self):
        """Acting on the wrong file is worse than leaving the note alone."""
        (self.root / "Archive" / "Other").mkdir(parents=True)
        (self.root / "Archive" / "Other" / "Scan Page 196.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        self.assertIsNone(self.vault.resolve_link(self.note, "Scan Page 196.jpg"))

    def test_a_missing_file_resolves_to_nothing(self):
        self.assertIsNone(self.vault.resolve_link(self.note, "Scan Page 900.jpg"))

    def test_a_url_is_not_a_file(self):
        self.assertIsNone(self.vault.resolve_link(self.note, "https://example.com/x.pdf"))

    def test_the_notes_documents_are_what_it_embeds(self):
        self.assertEqual(note_documents(self.vault, self.note, {}, BODY), [self.scan])

    def test_a_note_of_embeds_carries_no_text(self):
        self.assertEqual(note_text(self.vault, self.note, {}, BODY, self.config), "")

    def test_an_embedded_image_has_to_be_read(self):
        self.assertTrue(documents_need_ocr(self.vault, self.note, {}, BODY, self.config))


class TestRewritingKeepsTheEmbeds(unittest.TestCase):
    """Rewriting replaces the body, so anything it embedded has to be put back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "Archive" / "Scans").mkdir(parents=True)
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        for page in (196, 197):
            (self.root / "Archive" / "Scans" / f"Scan Page {page}.jpg").write_bytes(b"\xff\xd8\xff")
        self.note = self.root / "Archive" / "Scans" / "Bank letter.md"
        self.note.write_text(NOTE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def organize(self) -> str:
        report = plan(self.config, client=None, reclassify=True, ocr=False, include_unmanaged=True)
        organizer_apply(report, self.config, client=None)
        notes = [path for path in self.root.rglob("*.md") if path.name != "Archive.md"]
        self.assertEqual(len(notes), 1, notes)
        return notes[0].read_text(encoding="utf-8")

    def test_the_note_keeps_both_scans(self):
        body = self.organize()
        self.assertIn("![[Archive/Scans/Scan Page 196.jpg]]", body)
        self.assertIn("![[Archive/Scans/Scan Page 197.jpg]]", body)

    def test_the_embeds_are_rewritten_as_vault_relative_links(self):
        """A bare name breaks once the note moves out from beside the scan."""
        self.assertNotIn("![[Scan Page 196.jpg]]", self.organize())

    def test_the_embeds_are_not_quoted_into_the_extracted_text(self):
        body = self.organize()
        head, _, quoted = body.partition("Extracted text")
        self.assertNotIn("Scan Page 196.jpg", quoted)

    def test_the_wikilink_never_reaches_the_title_or_the_filename(self):
        self.organize()
        for path in self.root.rglob("*.md"):
            self.assertNotIn("[[", path.name)
        frontmatter, _ = parse_frontmatter(
            [p for p in self.root.rglob("*.md") if p.name != "Archive.md"][0].read_text()
        )
        self.assertNotIn("[[", str(frontmatter.get("title")))


@unittest.skipUnless(HAS_RASTERISER and HAS_BACKEND, "needs pdftoppm and an OCR backend")
class TestOcrOfEmbeddedImages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "Archive" / "Scans").mkdir(parents=True)
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.pages = []
        for page, lines in (
            (196, ["ACME BANK PLC", "Statement date 14 February 2024", "Balance 1204.55 GBP"]),
            (197, ["Page 2 of 2", "Account 55-0192-33"]),
        ):
            self.pages.append(
                make_scan_image(self.root / "Archive/Scans" / f"Scan Page {page}.png", lines, fmt="png")
            )
        self.note = self.root / "Archive" / "Scans" / "Bank letter.md"
        self.note.write_text(
            "---\ntitle: Bank letter\ncategory: Banking\ntags: [scan]\nclassifier: heuristic\n---\n\n"
            "# Bank letter\n\n![[Scan Page 196.png]]\n![[Scan Page 197.png]]\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_reports_the_images_rather_than_calling_the_note_filed(self):
        report = plan(self.config, client=None, reclassify=True, include_unmanaged=True)
        actions = [a for a in report.actions if a.path == self.note]
        self.assertEqual([a.kind for a in actions], ["ocr"])
        self.assertIn("Scan Page 196.png", actions[0].reason)

    def test_every_embedded_page_is_read(self):
        report = plan(self.config, client=None, reclassify=True, include_unmanaged=True)
        organizer_apply(report, self.config, client=None)
        note = [p for p in self.root.rglob("*.md") if p.name != "Archive.md"][0]
        body = note.read_text(encoding="utf-8")
        self.assertIn("ACME", body)
        self.assertIn("Account", body, "the second page was not read")

    def test_the_images_are_left_where_the_note_points(self):
        """Replacing a .png with a PDF would break the link the user wrote."""
        report = plan(self.config, client=None, reclassify=True, include_unmanaged=True)
        organizer_apply(report, self.config, client=None)
        for page in self.pages:
            self.assertTrue(page.is_file(), page)
            self.assertNotEqual(page.read_bytes()[:4], b"%PDF")


if __name__ == "__main__":
    unittest.main()
