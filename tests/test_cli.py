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
        self.assertFalse((self.vault / "4 Archive").exists())

        code, out = run(["-q", "init-vault", "--vault", str(self.vault), "--apply"])
        self.assertEqual(code, 0)
        self.assertIn("created:", out)
        self.assertTrue((self.vault / "4 Archive/4 Archive.md").is_file())

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
        for name in ("ingest", "watch", "ocr", "organize", "init-vault", "init-config"):
            options = {option for action in subparsers[name]._actions for option in action.option_strings}
            self.assertIn("--apply", options, f"{name} is missing --apply")
            self.assertIn("--dry-run", options, f"{name} is missing --dry-run")


if __name__ == "__main__":
    unittest.main()
