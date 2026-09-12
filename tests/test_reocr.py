"""`re-ocr`: finding the documents that were read as gibberish, and redoing them.

The planning tests need no OCR toolchain - scoring is pure string work, and a
PDF with a deliberately garbled text layer is a fixture we can write. The write
half is tested by standing in for `extract`, so what is exercised is the part
that can lose someone's documents: replacing a PDF, rewriting a note, and
deciding when *not* to.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scanvault.config import OcrConfig, load_config
from scanvault.extract import ExtractResult
from scanvault.organizer import embedded_text
from scanvault.quality import score_text
from scanvault import reocr
from scanvault.reocr import apply as reocr_apply
from scanvault.reocr import pass_config, plan
from scanvault.vault import parse_frontmatter
from tests.helpers import StubClient, make_scanned_pdf, make_text_pdf

HAS_EXTRACTOR = shutil.which("pdftotext") is not None
try:
    import pypdf  # type: ignore  # noqa: F401

    HAS_EXTRACTOR = True
except ImportError:
    pass

HAS_OCRMYPDF = shutil.which("ocrmypdf") is not None
HAS_RASTERISER = shutil.which("pdftoppm") is not None

# What a page read at the wrong resolution actually looks like.
GIBBERISH_LINES = [
    "1|\\|\\/O|CE Nr 4?3 rrn tt|| gf- ~~ |][ x",
    ".,, wq ]f |I1 cX rw8 '' -- ## }{ ,,, llllll .. \\/\\/",
    "|]|[ }} zz~ xq |I| f]f ;;; wqw ]][ rrn ttt |\\|",
    "%%% |[]| ~~~ cXc rw8r ,,. ;;; }{} |I1| \\/\\ xzq",
]

READABLE_LINES = [
    "NORTHERN ENERGY LIMITED",
    "Electricity bill for March 2024",
    "Meter reading 45210 kWh taken on 31 March 2024",
    "Standing charge 12.40 and a unit rate of 0.28 per kWh",
    "Total amount due 88.20 GBP, payable by direct debit on 15 April.",
    "Please quote your account number 8842119 when you contact us.",
]


def note_text_for(lines: list[str], attachment: str, extra: str = "") -> str:
    """A note as scanvault writes one, with the OCR text embedded in it."""
    quoted = "\n".join(f"> {line}" for line in lines)
    return (
        "---\n"
        'title: "Scan"\n'
        "date: 2024-03-31\n"
        'category: "Other"\n'
        "tags:\n  - scan\n"
        'classifier: "llm"\n'
        'scanvault_version: "0.21.0"\n'
        f'attachment: "{attachment}"\n'
        f"{extra}"
        "---\n\n"
        "# Scan\n\n"
        f"![[{attachment}]]\n\n"
        "> [!quote]- Extracted text\n> ```text\n"
        f"{quoted}\n> ```\n"
    )


class TestPassConfig(unittest.TestCase):
    def test_every_pass_forces_ocr(self):
        base = OcrConfig()
        self.assertFalse(base.force, "the fixture has to start from the default")
        for name in ("force", "oversample", "clean"):
            with self.subTest(pass_name=name):
                self.assertTrue(pass_config(name, base).force)

    def test_the_passes_escalate(self):
        base = OcrConfig()
        first = pass_config("force", base)
        second = pass_config("oversample", base)
        third = pass_config("clean", base)
        self.assertEqual(first.oversample, base.oversample)
        self.assertGreater(second.oversample, first.oversample)
        self.assertGreater(second.rasterize_dpi, 0)
        self.assertFalse(second.clean)
        self.assertTrue(third.clean)

    def test_a_pass_never_lowers_a_setting_the_user_raised(self):
        base = OcrConfig(oversample=900, rasterize_dpi=1200)
        escalated = pass_config("oversample", base)
        self.assertEqual(escalated.oversample, 900)
        self.assertEqual(escalated.rasterize_dpi, 1200)

    def test_an_unknown_pass_is_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            pass_config("magic", OcrConfig())
        self.assertIn("magic", str(caught.exception))


@unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext to read a text layer")
class TestPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.vault_dir)})
        self.attachments = self.vault_dir / "Archive" / "_attachments"
        self.attachments.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, name: str, lines: list[str], extra: str = "") -> tuple[Path, Path]:
        pdf = make_text_pdf(self.attachments / f"{name}.pdf", lines)
        relative = pdf.relative_to(self.vault_dir).as_posix()
        note = self.vault_dir / "Archive" / f"{name}.md"
        note.write_text(note_text_for(lines, relative, extra), encoding="utf-8")
        return note, pdf

    def test_a_garbled_document_is_a_candidate_and_a_readable_one_is_not(self):
        self.add("garbled", GIBBERISH_LINES)
        self.add("readable", READABLE_LINES)
        report = plan(self.config)

        self.assertEqual(len(report.documents), 2)
        self.assertEqual(len(report.todo), 1)
        self.assertEqual(report.todo[0].document.name, "garbled.pdf")
        self.assertEqual(report.count("good"), 1)

    def test_the_worst_documents_come_first(self):
        self.add("readable", READABLE_LINES)
        self.add("garbled", GIBBERISH_LINES)
        scores = [c.quality.score for c in plan(self.config).documents]
        self.assertEqual(scores, sorted(scores))

    def test_planning_changes_nothing(self):
        note, pdf = self.add("garbled", GIBBERISH_LINES)
        before = (note.read_text(encoding="utf-8"), pdf.read_bytes())
        plan(self.config)
        self.assertEqual((note.read_text(encoding="utf-8"), pdf.read_bytes()), before)

    def test_the_threshold_decides_what_is_a_candidate(self):
        self.add("readable", READABLE_LINES)
        self.assertEqual(len(plan(self.config).todo), 0)
        self.assertEqual(len(plan(self.config, threshold=1.0).todo), 1)

    def test_all_reads_everything_again(self):
        self.add("readable", READABLE_LINES)
        report = plan(self.config, include_all=True)
        self.assertEqual(len(report.todo), 1)
        self.assertIn("--all", report.todo[0].reason)

    def test_notes_scanvault_did_not_write_are_left_alone(self):
        pdf = make_text_pdf(self.attachments / "theirs.pdf", GIBBERISH_LINES)
        relative = pdf.relative_to(self.vault_dir).as_posix()
        note = self.vault_dir / "Archive" / "theirs.md"
        note.write_text(f"---\ntitle: \"Mine\"\n---\n\n![[{relative}]]\n", encoding="utf-8")

        self.assertEqual(len(plan(self.config).todo), 0)
        self.assertEqual(len(plan(self.config, include_unmanaged=True).todo), 1)

    def test_a_document_two_notes_point_at_is_only_scored_once(self):
        note, pdf = self.add("garbled", GIBBERISH_LINES)
        relative = pdf.relative_to(self.vault_dir).as_posix()
        second = self.vault_dir / "Archive" / "second.md"
        second.write_text(note_text_for(GIBBERISH_LINES, relative), encoding="utf-8")
        self.assertEqual(len(plan(self.config).documents), 1)

    def test_a_loose_pdf_no_note_points_at_is_scored_too(self):
        make_text_pdf(self.vault_dir / "Archive" / "loose.pdf", GIBBERISH_LINES)
        report = plan(self.config)
        self.assertEqual(len(report.todo), 1)
        self.assertIsNone(report.todo[0].note)

    def test_a_short_document_is_reported_but_never_re_ocrd(self):
        self.add("receipt", ["ICA Kvantum", "Totalt 189,50"])
        report = plan(self.config)
        self.assertEqual(report.count("thin"), 1)
        self.assertEqual(len(report.todo), 0)


@unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext to read a text layer")
class TestApply(unittest.TestCase):
    """The write half, with OCR stood in for so it runs anywhere.

    Replacing a PDF and rewriting a note are the operations that can destroy
    something. What OCR returns is the toolchain's business; what we do with it
    is ours.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault_dir = self.root / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.vault_dir)})
        self.attachments = self.vault_dir / "Archive" / "_attachments"
        self.attachments.mkdir(parents=True)
        self.pdf = make_text_pdf(self.attachments / "garbled.pdf", GIBBERISH_LINES)
        relative = self.pdf.relative_to(self.vault_dir).as_posix()
        self.note = self.vault_dir / "Archive" / "garbled.md"
        self.note.write_text(note_text_for(GIBBERISH_LINES, relative), encoding="utf-8")
        self._real_extract = reocr.extract

    def tearDown(self):
        reocr.extract = self._real_extract
        self.tmp.cleanup()

    def stub_extract(self, lines: list[str], only: str | None = None):
        """Stand in for OCR: every pass returns `lines`, or only pass `only` does."""
        calls: list[str] = []

        def fake(path: Path, settings, work_dir: Path) -> ExtractResult:
            calls.append(work_dir.name)
            if only is not None and work_dir.name != only:
                return ExtractResult("", True, "stub", path, 1)
            produced = work_dir / f"{path.stem}.ocr.pdf"
            make_text_pdf(produced, lines)
            return ExtractResult("\n".join(lines), True, "stub", produced, 1)

        reocr.extract = fake
        return calls

    def frontmatter(self) -> dict:
        data, _ = parse_frontmatter(self.note.read_text(encoding="utf-8"))
        return data

    def test_better_text_replaces_the_pdf_and_the_note(self):
        self.stub_extract(READABLE_LINES)
        report = plan(self.config)
        reocr_apply(report, self.config, client=None, reclassify=False)

        self.assertEqual(report.count("improved"), 1)
        self.assertIn("Electricity bill", self.pdf.read_bytes().decode("latin-1"))
        body = self.note.read_text(encoding="utf-8")
        self.assertIn("Electricity bill for March 2024", embedded_text(body))
        self.assertNotIn("llllll", body, "the gibberish must be gone from the note")

    def test_the_new_score_is_recorded_in_the_note(self):
        self.stub_extract(READABLE_LINES)
        reocr_apply(plan(self.config), self.config, client=None, reclassify=False)
        recorded = self.frontmatter()["ocr_quality"]
        self.assertGreaterEqual(recorded, self.config.quality.threshold)
        self.assertEqual(recorded, score_text("\n".join(READABLE_LINES), pages=1).score)

    def test_text_that_is_no_better_is_not_written_over_the_original(self):
        self.stub_extract(GIBBERISH_LINES)
        before = self.pdf.read_bytes()
        report = plan(self.config)
        reocr_apply(report, self.config, client=None, reclassify=False)

        self.assertEqual(report.count("unchanged"), 1)
        self.assertEqual(report.count("improved"), 0)
        self.assertEqual(self.pdf.read_bytes(), before, "a bad re-read must change nothing")

    def test_the_passes_escalate_until_one_works(self):
        calls = self.stub_extract(READABLE_LINES, only="oversample")
        report = plan(self.config)
        reocr_apply(report, self.config, client=None, reclassify=False)

        self.assertEqual(calls[:2], ["force", "oversample"])
        self.assertEqual(report.documents[0].winner, "oversample")

    def test_a_good_enough_pass_stops_the_escalation(self):
        calls = self.stub_extract(READABLE_LINES)
        reocr_apply(plan(self.config), self.config, client=None, reclassify=False)
        self.assertEqual(calls, ["force"], "no reason to keep trying once it reads")

    def test_reclassifying_refiles_the_document_under_its_real_category(self):
        self.stub_extract(READABLE_LINES)
        client = StubClient(
            {
                "title": "Electricity bill March 2024",
                "category": "Utilities",
                "document_date": "2024-03-31",
                "correspondent": "Northern Energy Limited",
                "summary": "Electricity for March.",
                "tags": ["electricity"],
                "confidence": 0.9,
            }
        )
        report = plan(self.config)
        reocr_apply(report, self.config, client=client, reclassify=True)

        self.assertTrue(report.documents[0].reclassified)
        self.assertFalse(self.note.exists(), "the note moved to its new category")
        moved = list((self.vault_dir / "Archive" / "Utilities").rglob("*.md"))
        self.assertEqual(len(moved), 1)
        self.assertIn("Electricity bill", moved[0].read_text(encoding="utf-8"))

    def test_reclassifying_without_a_model_still_uses_the_heuristics(self):
        """`--no-llm` is not `--no-reclassify`.

        A title derived from readable text beats one derived from gibberish
        whether the model or the keyword rules derived it, and this is what
        `organize --no-llm` does too.
        """
        self.stub_extract(READABLE_LINES)
        report = plan(self.config)
        reocr_apply(report, self.config, client=None, reclassify=True)

        self.assertTrue(report.documents[0].reclassified)
        self.assertFalse(self.note.exists())
        refiled = list(self.vault_dir.rglob("*.md"))
        self.assertEqual(len(refiled), 1)
        self.assertNotIn('title: "Scan"', refiled[0].read_text(encoding="utf-8"))

    def test_no_reclassify_keeps_the_metadata_and_the_location(self):
        self.stub_extract(READABLE_LINES)
        report = plan(self.config)
        reocr_apply(report, self.config, client=StubClient({}), reclassify=False)

        self.assertFalse(report.documents[0].reclassified)
        self.assertTrue(self.note.exists())
        self.assertEqual(self.frontmatter()["title"], "Scan")

    def test_a_failing_pass_is_reported_rather_than_raised(self):
        def explode(path, settings, work_dir):
            raise OSError("no toolchain here")

        reocr.extract = explode
        report = plan(self.config)
        reocr_apply(report, self.config, client=None, reclassify=False)
        self.assertEqual(report.count("failed"), 1)
        self.assertTrue(self.pdf.is_file(), "a failure leaves the document alone")

    def test_nothing_is_left_in_the_work_folder(self):
        self.stub_extract(READABLE_LINES)
        reocr_apply(plan(self.config), self.config, client=None, reclassify=False)
        work = self.config.state_root / "work"
        leftovers = [p for p in work.rglob("*")] if work.exists() else []
        self.assertEqual(leftovers, [], "the scratch directory has to be cleaned up")


@unittest.skipUnless(HAS_EXTRACTOR, "needs pypdf or pdftotext to read a text layer")
class TestCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        attachments = self.vault / "Archive" / "_attachments"
        attachments.mkdir(parents=True)
        for index in range(3):
            pdf = make_text_pdf(attachments / f"garbled{index}.pdf", GIBBERISH_LINES)
            relative = pdf.relative_to(self.vault).as_posix()
            note = self.vault / "Archive" / f"garbled{index}.md"
            note.write_text(note_text_for(GIBBERISH_LINES, relative), encoding="utf-8")
        make_text_pdf(attachments / "fine.pdf", READABLE_LINES)
        fine = self.vault / "Archive" / "fine.md"
        fine.write_text(
            note_text_for(READABLE_LINES, "Archive/_attachments/fine.pdf"), encoding="utf-8"
        )
        self.before = {
            path: path.read_bytes() for path in self.vault.rglob("*") if path.is_file()
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_reocr(self, *extra: str) -> tuple[int, str]:
        import contextlib
        import io

        from scanvault.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["-q", "re-ocr", "--vault", str(self.vault), "--no-llm", *extra])
        return code, out.getvalue()

    def test_preview_is_the_default_and_changes_nothing(self):
        code, out = self.run_reocr()
        self.assertEqual(code, 0)
        self.assertIn("3 to re-OCR", out)
        self.assertIn("Nothing was changed", out)
        self.assertEqual(
            {path: path.read_bytes() for path in self.vault.rglob("*") if path.is_file()},
            self.before,
        )

    def test_readable_documents_are_counted_not_listed(self):
        _, out = self.run_reocr()
        self.assertNotIn("fine.pdf", out)
        self.assertIn("1 good", out)

    def test_verbose_lists_them_and_explains_the_passes(self):
        _, out = self.run_reocr("-v")
        self.assertIn("fine.pdf", out)
        self.assertIn("passes:", out)

    def test_limit_defers_the_rest(self):
        _, out = self.run_reocr("--limit", "1")
        self.assertIn("1 to re-OCR", out)
        self.assertIn("2 deferred", out)
        self.assertIn("beyond --limit 1", out)


@unittest.skipUnless(
    HAS_OCRMYPDF and HAS_RASTERISER and HAS_EXTRACTOR,
    "needs the real OCR toolchain",
)
class TestAgainstRealOcr(unittest.TestCase):
    """One end-to-end run against a scan that really was read badly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault_dir = self.root / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.vault_dir)})
        self.attachments = self.vault_dir / "Archive" / "_attachments"
        self.attachments.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_scan_rendered_too_small_to_read_is_recovered(self):
        # 42 dpi is far below what OCR can read; the oversampling pass upsamples
        # the page image and gets most of it back.
        scan = make_scanned_pdf(self.attachments / "scan.pdf", READABLE_LINES, dpi=42)
        relative = scan.relative_to(self.vault_dir).as_posix()
        note = self.vault_dir / "Archive" / "scan.md"
        note.write_text(note_text_for(["(nothing)"], relative), encoding="utf-8")

        report = plan(self.config)
        self.assertEqual(len(report.todo), 1, "an unreadable scan must be a candidate")
        reocr_apply(report, self.config, client=None, reclassify=False)

        candidate = report.documents[0]
        self.assertEqual(candidate.kind, "improved")
        self.assertGreater(candidate.after.score, candidate.quality.score)
        self.assertIn("ENERGY", embedded_text(note.read_text(encoding="utf-8")).upper())


if __name__ == "__main__":
    unittest.main()
