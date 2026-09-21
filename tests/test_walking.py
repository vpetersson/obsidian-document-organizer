"""Finding the documents in a folder, including the ones behind a link.

`Path.rglob` does not descend into a symlinked directory. A source folder
holding a link to where the scans actually live - an alias into iCloud Drive, a
network share, an external disk - looked completely empty and said nothing
about it.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scanvault.pipeline import iter_documents
from scanvault.util import walk_files


class TestWalkFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        for path in self.root.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        self.tmp.cleanup()

    def touch(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4")
        return path

    def names(self, root: Path | None = None, recursive: bool = True) -> list[str]:
        return [path.name for path in walk_files(root or self.root, recursive)]

    def test_it_descends_into_folders(self):
        self.touch("top.pdf")
        self.touch("a/b/deep.pdf")
        self.assertEqual(self.names(), ["deep.pdf", "top.pdf"])

    def test_it_descends_into_a_symlinked_folder(self):
        """The reported bug: a link to where the scans really live."""
        elsewhere = self.root / "elsewhere"
        (elsewhere / "scans").mkdir(parents=True)
        (elsewhere / "scans" / "linked.pdf").write_bytes(b"%PDF-1.4")
        source = self.root / "source"
        source.mkdir()
        os.symlink(elsewhere / "scans", source / "Scans")
        self.assertEqual(self.names(source), ["linked.pdf"])

    def test_a_symlinked_file_is_found_too(self):
        real = self.touch("real/statement.pdf")
        source = self.root / "source"
        source.mkdir()
        os.symlink(real, source / "statement.pdf")
        self.assertEqual(self.names(source), ["statement.pdf"])

    def test_a_link_pointing_at_its_own_parent_does_not_loop(self):
        source = self.root / "source"
        (source / "a").mkdir(parents=True)
        (source / "a" / "one.pdf").write_bytes(b"%PDF-1.4")
        os.symlink(source, source / "a" / "back")
        self.assertEqual(self.names(source), ["one.pdf"])

    def test_two_links_to_the_same_folder_yield_it_once(self):
        target = self.root / "target"
        target.mkdir()
        (target / "one.pdf").write_bytes(b"%PDF-1.4")
        source = self.root / "source"
        source.mkdir()
        os.symlink(target, source / "first")
        os.symlink(target, source / "second")
        self.assertEqual(self.names(source), ["one.pdf"])

    def test_a_broken_link_is_stepped_over(self):
        source = self.root / "source"
        source.mkdir()
        (source / "real.pdf").write_bytes(b"%PDF-1.4")
        os.symlink(self.root / "does-not-exist", source / "dangling.pdf")
        self.assertEqual(self.names(source), ["real.pdf"])

    def test_an_unreadable_folder_is_reported_rather_than_ignored(self):
        """"Nothing to do" and "not allowed to look" must not look the same."""
        source = self.root / "source"
        (source / "locked").mkdir(parents=True)
        (source / "readable.pdf").write_bytes(b"%PDF-1.4")
        (source / "locked").chmod(0o000)
        if os.access(source / "locked", os.R_OK):
            self.skipTest("running as a user that can read anything")
        with self.assertLogs("scanvault.util", level="WARNING") as logs:
            found = self.names(source)
        self.assertEqual(found, ["readable.pdf"])
        self.assertIn("could not read", "\n".join(logs.output))

    def test_without_recursion_only_the_top_level(self):
        self.touch("top.pdf")
        self.touch("a/deep.pdf")
        self.assertEqual(self.names(recursive=False), ["top.pdf"])

    def test_a_missing_folder_is_empty_rather_than_an_error(self):
        self.assertEqual(walk_files(self.root / "nothing-here"), [])

    def test_the_order_does_not_depend_on_the_filesystem(self):
        for name in ("z.pdf", "a.pdf", "m/n.pdf", "b/c.pdf"):
            self.touch(name)
        self.assertEqual(walk_files(self.root), sorted(walk_files(self.root)))


class TestIterDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4")
        return path

    def names(self, recursive: bool = True) -> list[str]:
        return [path.name for path in iter_documents(self.root, recursive)]

    def test_documents_behind_a_symlinked_folder_are_ingested(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "scan.pdf").write_bytes(b"%PDF-1.4")
        source = self.root / "source"
        source.mkdir()
        os.symlink(elsewhere, source / "linked")
        self.assertEqual([p.name for p in iter_documents(source)], ["scan.pdf"])

    def test_it_still_skips_hidden_folders(self):
        self.touch("visible.pdf")
        self.touch(".hidden/secret.pdf")
        self.assertEqual(self.names(), ["visible.pdf"])

    def test_it_still_skips_our_own_temporary_output(self):
        self.touch("real.pdf")
        self.touch("something.ocr.pdf")
        self.assertEqual(self.names(), ["real.pdf"])

    def test_it_still_skips_files_that_are_not_documents(self):
        self.touch("real.pdf")
        self.touch("notes.txt")
        self.assertEqual(self.names(), ["real.pdf"])

    def test_a_single_file_is_still_accepted(self):
        path = self.touch("one.pdf")
        self.assertEqual([p.name for p in iter_documents(path)], ["one.pdf"])

    def test_icloud_placeholders_are_reported_not_silently_dropped(self):
        """There are no bytes to read, but saying nothing looks like a bug."""
        self.touch("here.pdf")
        (self.root / ".statement.pdf.icloud").write_bytes(b"")
        with self.assertLogs("scanvault.pipeline", level="WARNING") as logs:
            found = self.names()
        self.assertEqual(found, ["here.pdf"])
        message = "\n".join(logs.output)
        self.assertIn("not downloaded", message)
        self.assertIn("statement.pdf", message)

    def test_an_unrelated_hidden_file_says_nothing(self):
        self.touch("here.pdf")
        (self.root / ".DS_Store").write_bytes(b"")
        (self.root / ".notes.txt.icloud").write_bytes(b"")
        with self.assertNoLogs("scanvault.pipeline", level="WARNING"):
            self.assertEqual(self.names(), ["here.pdf"])


if __name__ == "__main__":
    unittest.main()
