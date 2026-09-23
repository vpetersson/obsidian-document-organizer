"""CLI contract: every writing command previews by default."""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from scanvault.cli import main
from tests.helpers import make_text_pdf

INVOICE = [
    "ACME LTD",
    "INVOICE 2024-05-02",
    "Invoice number INV-1234",
    "Amount due 120.00 EUR",
    "Payment due within 30 days of the invoice date, by bank transfer.",
    "Acme Ltd, 12 Example Street, London. VAT GB123456789.",
]

HAS_EXTRACTOR = shutil.which("pdftotext") is not None
try:
    import pypdf  # type: ignore  # noqa: F401

    HAS_EXTRACTOR = True
except ImportError:
    pass


def run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


class TestPreviewIsTheDefault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "inbox"
        self.vault = self.root / "vault"
        self.source.mkdir()
        make_text_pdf(self.source / "scan.pdf", INVOICE)

    def tearDown(self):
        self.tmp.cleanup()

    def ingest_args(self, *extra: str) -> list[str]:
        return [
            "-q",
            "ingest",
            "--source",
            str(self.source),
            "--vault",
            str(self.vault),
            "--no-llm",
            *extra,
        ]

    @unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext")
    def test_ingest_writes_nothing_without_apply(self):
        code, out = run(self.ingest_args())
        self.assertEqual(code, 0)
        self.assertIn("would file", out)
        self.assertIn("Nothing was changed", out)
        self.assertTrue((self.source / "scan.pdf").is_file())
        self.assertFalse(self.vault.exists())

    @unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext")
    def test_ingest_with_apply_files_the_document(self):
        code, out = run(self.ingest_args("--apply"))
        self.assertEqual(code, 0)
        self.assertIn("filed:", out)
        self.assertNotIn("would file", out)
        self.assertNotIn("Nothing was changed", out)
        self.assertTrue(list(self.vault.rglob("*.md")))

    @unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext")
    def test_explicit_dry_run_matches_the_default(self):
        _, default_out = run(self.ingest_args())
        _, explicit_out = run(self.ingest_args("--dry-run"))
        self.assertEqual(
            [line for line in default_out.splitlines() if "would file" in line],
            [line for line in explicit_out.splitlines() if "would file" in line],
        )

    def test_apply_and_dry_run_together_are_rejected(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(self.ingest_args("--apply", "--dry-run"))

    def test_organize_previews_by_default(self):
        self.vault.mkdir()
        code, out = run(["-q", "organize", "--vault", str(self.vault), "--no-llm"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing was changed", out)
        self.assertEqual(list(self.vault.rglob("*.md")), [])

    def test_organize_with_apply_creates_the_para_folders(self):
        self.vault.mkdir()
        code, out = run(["-q", "organize", "--vault", str(self.vault), "--no-llm", "--apply"])
        self.assertEqual(code, 0)
        self.assertNotIn("Nothing was changed", out)

    def test_init_vault_previews_by_default(self):
        code, out = run(["-q", "init-vault", "--vault", str(self.vault)])
        self.assertEqual(code, 0)
        self.assertIn("would create", out)
        self.assertFalse((self.vault / "Archive").exists())

        code, out = run(["-q", "init-vault", "--vault", str(self.vault), "--apply"])
        self.assertEqual(code, 0)
        self.assertIn("created:", out)
        self.assertTrue((self.vault / "Archive/Archive.md").is_file())

    def test_init_config_previews_by_default(self):
        target = self.root / "scanvault.toml"
        code, out = run(["-q", "init-config", "-o", str(target)])
        self.assertEqual(code, 0)
        self.assertIn("would write", out)
        self.assertFalse(target.exists())

        code, out = run(["-q", "init-config", "-o", str(target), "--apply"])
        self.assertEqual(code, 0)
        self.assertTrue(target.is_file())

    @unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext")
    def test_ocr_previews_by_default(self):
        code, out = run(["-q", "ocr", str(self.source)])
        self.assertEqual(code, 0)
        self.assertIn("would skip: scan.pdf (already searchable", out)
        self.assertIn("Nothing was changed", out)

    def test_every_writing_command_accepts_both_flags(self):
        from scanvault.cli import build_parser

        parser = build_parser()
        subparsers = next(
            action for action in parser._actions if isinstance(action.choices, dict)
        ).choices
        for name in (
            "ingest",
            "watch",
            "ocr",
            "organize",
            "re-ocr",
            "init-vault",
            "init-config",
        ):
            options = {option for action in subparsers[name]._actions for option in action.option_strings}
            self.assertIn("--apply", options, f"{name} is missing --apply")
            self.assertIn("--dry-run", options, f"{name} is missing --dry-run")


class TestPlanOutputStaysReadable(unittest.TestCase):
    """A real vault has hundreds of notes scanvault does not own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        run(["-q", "init-vault", "--vault", str(self.vault), "--apply"])
        for name in ("Holiday ideas", "Boiler service", "Bike maintenance"):
            note = self.vault / "Scanned/Private" / f"{name}.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(f"# {name}\n\nnotes I wrote myself\n")

    def tearDown(self):
        self.tmp.cleanup()

    def organize(self, *extra: str) -> str:
        return run(["organize", "--vault", str(self.vault), "--no-llm", "-q", *extra])[1]

    def test_skipped_notes_are_counted_not_listed(self):
        out = self.organize()
        self.assertNotIn("not a scanvault note", out)
        self.assertIn("3 left alone", out)
        self.assertIn("-v to list them", out)

    def test_verbose_lists_them(self):
        out = self.organize("-v")
        self.assertIn("Holiday ideas.md (not a scanvault note)", out)

    def test_para_index_notes_are_not_counted_as_filed_documents(self):
        out = self.organize()
        self.assertIn("0 already filed", out)
        self.assertNotIn("PARA index note", out)

    def test_verbose_shows_the_index_notes(self):
        self.assertIn("PARA index note", self.organize("-v"))

    def test_verbose_works_before_or_after_the_subcommand(self):
        after = run(["organize", "--vault", str(self.vault), "--no-llm", "-q", "-v"])[1]
        before = run(["-q", "-v", "organize", "--vault", str(self.vault), "--no-llm"])[1]
        self.assertIn("not a scanvault note", after)
        self.assertIn("not a scanvault note", before)


class TestOnlyFiltersTheSource(unittest.TestCase):
    """`--only` on a folder that is not an inbox."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()
        make_text_pdf(self.desktop / "invoice.pdf", INVOICE)
        (self.desktop / "Screenshot 2024-05-02 at 14.23.07.png").write_bytes(b"x")
        (self.desktop / "company-logo.png").write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, *extra: str) -> tuple[int, str]:
        """Everything the run said, stderr included.

        The junk on a Desktop is junk: a `.png` that is not an image fails OCR
        and is reported on stderr, and whether it was looked at at all is
        exactly what `--only` decides.
        """
        argv = [
            "-q",
            "ingest",
            "--source",
            str(self.desktop),
            "--vault",
            str(self.root / "vault"),
            "--no-llm",
            *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue() + err.getvalue()

    @unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext")
    def test_without_it_everything_is_offered(self):
        out = self.ingest()[1]
        self.assertIn("company-logo.png", out)

    @unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext")
    def test_it_leaves_out_what_was_not_asked_for(self):
        out = self.ingest("--only", "screenshots,pdfs")[1]
        self.assertIn("invoice.pdf", out)
        self.assertIn("Screenshot 2024-05-02 at 14.23.07.png", out)
        self.assertNotIn("company-logo.png", out)

    def test_finding_none_of_it_says_what_was_looked_for(self):
        (self.desktop / "invoice.pdf").unlink()
        (self.desktop / "Screenshot 2024-05-02 at 14.23.07.png").unlink()
        code, out = self.ingest("--only", "screenshots,pdfs")
        self.assertEqual(code, 0)
        self.assertIn("No screenshots, pdfs found", out)

    def test_a_value_that_is_not_a_kind_of_file_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self.ingest("--only", "documents")
        self.assertIn("documents", str(raised.exception))
        self.assertIn("screenshots", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
