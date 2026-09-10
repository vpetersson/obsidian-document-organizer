"""PARA ("Second Brain") layout: the archive is where scans land, and the
other three folders are respected rather than overwritten."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from scanvault.classify import DocumentMeta
from scanvault.config import load_config
from scanvault.organizer import apply as organizer_apply
from scanvault.organizer import is_managed, plan
from scanvault.vault import Vault, parse_frontmatter
from tests.helpers import StubClient

MANAGED = 'classifier: "llm"\ncssclasses:\n  - scanvault\n'


def meta(**kwargs) -> DocumentMeta:
    base = dict(
        title="Acme Invoice",
        category="Invoices",
        document_date=date(2024, 5, 2),
        tags=["scan"],
    )
    base.update(kwargs)
    return DocumentMeta(**base)


class TestScaffold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root), "vault.layout": "para"})
        self.vault = Vault(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scaffold_creates_the_four_para_folders(self):
        self.vault.scaffold()
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, ["1 Projects", "2 Areas", "3 Resources", "4 Archive"])
        index = self.root / "4 Archive/4 Archive.md"
        frontmatter, body = parse_frontmatter(index.read_text())
        self.assertTrue(frontmatter["para_index"])
        self.assertIn("cornerstone", body)

    def test_scaffold_is_idempotent_and_never_overwrites(self):
        self.vault.scaffold()
        index = self.root / "1 Projects/1 Projects.md"
        index.write_text("my own notes")
        self.assertEqual(self.vault.scaffold(), [])
        self.assertEqual(index.read_text(), "my own notes")

    def test_scaffold_dry_run_writes_nothing(self):
        planned = self.vault.scaffold(dry_run=True)
        self.assertEqual(len(planned), 4)
        self.assertFalse((self.root / "4 Archive").exists())

    def test_custom_folder_names(self):
        self.config.vault.para.archive_dir = "Archive"
        self.config.vault.para.projects_dir = "Projects"
        self.vault.scaffold()
        self.assertTrue((self.root / "Archive/Archive.md").is_file())
        self.assertEqual(
            self.vault.note_path(meta()).relative_to(self.root).parts[0], "Archive"
        )


class TestBucketRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root), "vault.layout": "para"})
        self.vault = Vault(self.config)
        self.vault.scaffold()

    def tearDown(self):
        self.tmp.cleanup()

    def write_note(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_scans_default_to_the_archive(self):
        result = self.vault.write_document(meta(), "text", pdf_path=None)
        self.assertEqual(
            result.note_path.relative_to(self.root).as_posix(),
            "4 Archive/Invoices/2024/2024-05-02 Acme Invoice.md",
        )
        frontmatter, _ = parse_frontmatter(result.note_path.read_text())
        self.assertEqual(frontmatter["para"], "archive")

    def test_each_bucket_maps_to_its_folder(self):
        for bucket, folder in (
            ("project", "1 Projects"),
            ("area", "2 Areas"),
            ("resource", "3 Resources"),
            ("archive", "4 Archive"),
        ):
            path = self.vault.note_path(meta(para=bucket))
            self.assertEqual(path.relative_to(self.root).parts[0], folder)

    def test_unknown_bucket_falls_back_to_the_archive(self):
        path = self.vault.note_path(meta(para="inbox"))
        self.assertEqual(path.relative_to(self.root).parts[0], "4 Archive")

    def test_frontmatter_promotes_a_note_out_of_the_archive(self):
        note = self.write_note(
            "4 Archive/Invoices/2024/2024-05-02 Acme Invoice.md",
            '---\ntitle: "Acme Invoice"\ndate: 2024-05-02\ncategory: "Invoices"\n'
            'tags:\n  - scan\npara: "project"\n' + MANAGED + "---\n\nbody\n",
        )
        report = plan(self.config, client=None)
        organizer_apply(report, self.config, client=None)
        moved = self.root / "1 Projects/Invoices/2024/2024-05-02 Acme Invoice.md"
        self.assertTrue(moved.is_file())
        self.assertFalse(note.exists())

    def test_a_note_moved_by_hand_is_not_dragged_back(self):
        # No `para:` key - the note's location has to be enough.
        note = self.write_note(
            "1 Projects/Invoices/2024/2024-05-02 Acme Invoice.md",
            '---\ntitle: "Acme Invoice"\ndate: 2024-05-02\ncategory: "Invoices"\n'
            "tags:\n  - scan\n" + MANAGED + "---\n\nbody\n",
        )
        report = plan(self.config, client=None)
        self.assertEqual(report.count("relocate"), 0)
        self.assertEqual(report.count("noop"), 1, "only the document counts as filed")
        action = next(a for a in report.actions if a.path == note)
        self.assertEqual(action.kind, "noop")
        self.assertEqual(action.meta.para, "project")
        organizer_apply(report, self.config, client=None)
        self.assertTrue(note.is_file())

    def test_attachment_follows_its_note_between_buckets(self):
        pdf = self.root / "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 fake")
        self.write_note(
            "4 Archive/Invoices/2024/2024-05-02 Acme Invoice.md",
            '---\ntitle: "Acme Invoice"\ndate: 2024-05-02\ncategory: "Invoices"\n'
            'tags:\n  - scan\npara: "area"\n' + MANAGED
            + 'attachment: "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf"\n---\n\nbody\n',
        )
        report = plan(self.config, client=None, ocr=False)
        organizer_apply(report, self.config, client=None)
        moved_note = self.root / "2 Areas/Invoices/2024/2024-05-02 Acme Invoice.md"
        moved_pdf = self.root / "2 Areas/_attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf"
        self.assertTrue(moved_pdf.is_file())
        self.assertFalse(pdf.exists())
        frontmatter, body = parse_frontmatter(moved_note.read_text())
        self.assertEqual(
            frontmatter["attachment"],
            "2 Areas/_attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf",
        )
        self.assertIn(f"![[{frontmatter['attachment']}]]", body)

    def test_index_notes_are_left_alone_and_not_counted_as_documents(self):
        report = plan(self.config, client=None)
        reasons = {action.reason for action in report.actions}
        self.assertEqual(reasons, {"PARA index note"})
        self.assertEqual(report.count("index"), 4)
        self.assertEqual(report.count("noop"), 0, "scaffolding is not a filed document")


class TestUnmanagedNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root), "vault.layout": "para"})
        self.vault = Vault(self.config)
        self.vault.scaffold()
        self.hand_written = self.root / "1 Projects/Kitchen renovation/Plan.md"
        self.hand_written.parent.mkdir(parents=True, exist_ok=True)
        self.hand_written.write_text("# Plan\n\nCall the plumber.\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_is_managed(self):
        self.assertFalse(is_managed({}))
        self.assertFalse(is_managed({"title": "x", "category": "Other"}))
        self.assertTrue(is_managed({"source_hash": "abc"}))
        self.assertTrue(is_managed({"classifier": "llm"}))

    def test_hand_written_notes_are_skipped_by_default(self):
        report = plan(self.config, client=StubClient({"title": "x", "category": "Other"}))
        skipped = [a for a in report.actions if a.kind == "skipped"]
        self.assertEqual([a.path for a in skipped], [self.hand_written])
        organizer_apply(report, self.config, client=None)
        self.assertTrue(self.hand_written.is_file(), "someone else's note must not move")

    def test_include_unmanaged_opts_them_in(self):
        client = StubClient(
            {"title": "Kitchen Plan", "category": "Property", "tags": ["kitchen"]}
        )
        report = plan(self.config, client=client, include_unmanaged=True)
        self.assertEqual(report.count("skipped"), 0)
        self.assertEqual(report.count("relocate"), 1)
        organizer_apply(report, self.config, client=client)
        # It stays in Projects - the bucket comes from where it already lives -
        # and the note has no date of its own, so the file's date is used.
        filed = list((self.root / "1 Projects/Property").rglob("*.md"))
        self.assertEqual(len(filed), 1)
        self.assertTrue(filed[0].name.endswith("Kitchen Plan.md"))
        self.assertNotIn("undated", filed[0].as_posix())


class TestLegacyLayoutMigration(unittest.TestCase):
    """A vault written by scanvault 0.1 must migrate with `organize --apply`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root), "vault.layout": "para"})
        note = self.root / "Documents/Invoices/2024/2024-05-02 Acme Invoice.md"
        pdf = self.root / "Attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf"
        note.parent.mkdir(parents=True)
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"%PDF-1.4 fake")
        note.write_text(
            '---\ntitle: "Acme Invoice"\ndate: 2024-05-02\ncategory: "Invoices"\n'
            'tags:\n  - scan\nclassifier: "llm"\nsource_hash: "abc123"\n'
            'attachment: "Attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf"\n---\n\n'
            "# Acme Invoice\n\n![[Attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf]]\n",
            encoding="utf-8",
        )
        self.note, self.pdf = note, pdf

    def tearDown(self):
        self.tmp.cleanup()

    def test_organize_migrates_notes_and_attachments(self):
        report = plan(self.config, client=None, ocr=False)
        self.assertEqual(report.count("relocate"), 1)
        organizer_apply(report, self.config, client=None)

        moved_note = self.root / "4 Archive/Invoices/2024/2024-05-02 Acme Invoice.md"
        moved_pdf = self.root / "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf"
        self.assertTrue(moved_note.is_file())
        self.assertTrue(moved_pdf.is_file())
        self.assertFalse(self.note.exists())
        self.assertFalse(self.pdf.exists())

        frontmatter, body = parse_frontmatter(moved_note.read_text())
        self.assertEqual(frontmatter["para"], "archive")
        self.assertEqual(frontmatter["source_hash"], "abc123", "provenance must survive")
        self.assertEqual(
            frontmatter["attachment"],
            "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice.pdf",
        )
        self.assertIn(f"![[{frontmatter['attachment']}]]", body)
        self.assertFalse((self.root / "Documents").exists(), "empty folders are pruned")


if __name__ == "__main__":
    unittest.main()


class TestFlatLayoutIsTheDefault(unittest.TestCase):
    """PARA's other three folders stayed empty, so one folder is the default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.vault = Vault(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_documents_land_in_one_folder(self):
        self.assertEqual(self.config.vault.layout, "flat")
        self.assertEqual(
            self.vault.note_path(meta()).relative_to(self.root).as_posix(),
            "Archive/Invoices/2024/2024-05-02 Acme Invoice.md",
        )

    def test_the_scaffold_is_one_folder_too(self):
        self.vault.scaffold()
        self.assertEqual([p.name for p in self.root.iterdir()], ["Archive"])
        self.assertTrue((self.root / "Archive/Archive.md").is_file())

    def test_the_folder_is_configurable(self):
        self.config.vault.documents_dir = "Scanned"
        self.assertEqual(
            self.vault.note_path(meta()).relative_to(self.root).parts[0], "Scanned"
        )

    def test_it_can_be_the_vault_root(self):
        self.config.vault.documents_dir = ""
        self.assertEqual(
            self.vault.note_path(meta()).relative_to(self.root).as_posix(),
            "Invoices/2024/2024-05-02 Acme Invoice.md",
        )
        self.assertEqual(self.vault.scaffold(), [], "nothing to scaffold at the root")

    def test_para_buckets_are_ignored_rather_than_half_applied(self):
        self.assertEqual(
            self.vault.note_path(meta(para="project")).relative_to(self.root).parts[0],
            "Archive",
        )
        self.assertIsNone(self.vault.bucket_from_path(self.root / "1 Projects/x.md"))

    def test_the_old_placeholder_still_works(self):
        self.config.vault.note_path_template = "{para}/{category}/{date} {title}"
        self.assertEqual(
            self.vault.note_path(meta()).relative_to(self.root).as_posix(),
            "Archive/Invoices/2024-05-02 Acme Invoice.md",
        )

    def test_switching_to_para_brings_the_four_folders_back(self):
        self.config.vault.layout = "para"
        self.vault.scaffold()
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["1 Projects", "2 Areas", "3 Resources", "4 Archive"],
        )
        self.assertEqual(
            self.vault.note_path(meta()).relative_to(self.root).parts[0], "4 Archive"
        )
