"""Picking documents out of a Desktop: screenshots by name, and `--only`."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from scanvault.classify import DocumentMeta, filename_title, resolve_date
from scanvault.config import SourceConfig, load_config
from scanvault.pipeline import ingest, iter_documents
from scanvault.select import (
    SELECTORS,
    describe,
    is_screenshot,
    keeper,
    parse_selectors,
    read_screenshot_name,
    screenshot_names,
)
from scanvault.pipeline import tag_from_file
from scanvault.tags import TagLedger
from scanvault.vault import Vault
from tests.helpers import make_scan_image, make_text_pdf

HAS_RASTERISER = shutil.which("pdftoppm") is not None
HAS_BACKEND = shutil.which("ocrmypdf") is not None or shutil.which("tesseract") is not None


class TestScreenshotNames(unittest.TestCase):
    def taken(self, name: str) -> datetime | None:
        found = read_screenshot_name(name)
        return found.taken_at if found else None

    def test_the_modern_macos_name(self):
        self.assertEqual(
            self.taken("Screenshot 2024-05-02 at 14.23.07.png"),
            datetime(2024, 5, 2, 14, 23, 7),
        )

    def test_the_name_macos_wrote_before_mojave(self):
        self.assertEqual(
            self.taken("Screen Shot 2019-03-01 at 4.15.22 PM.png"),
            datetime(2019, 3, 1, 16, 15, 22),
        )

    def test_midnight_and_noon_read_the_right_way_round(self):
        self.assertEqual(
            self.taken("Screenshot 2024-05-02 at 12.01.00 AM.png").hour, 0
        )
        self.assertEqual(
            self.taken("Screenshot 2024-05-02 at 12.01.00 PM.png").hour, 12
        )

    def test_an_afternoon_marker_written_in_front_of_the_clock(self):
        # Korean, Chinese and Japanese put theirs before the time, where the
        # separator would otherwise swallow it and lose half the day.
        for name, hour in (
            ("스크린샷 2024-05-02 오후 2.23.07.png", 14),
            ("스크린샷 2024-05-02 오전 2.23.07.png", 2),
            ("截屏2024-05-02 下午2.23.07.png", 14),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.taken(name).hour, hour, name)

    def test_the_word_for_at_is_not_mistaken_for_a_meridiem(self):
        self.assertEqual(self.taken("Bildschirmfoto 2024-05-02 um 9.41.12.png").hour, 9)

    def test_the_localised_names_apple_ships(self):
        for name in (
            "Skärmavbild 2024-05-02 kl. 14.23.07.png",
            "Bildschirmfoto 2024-05-02 um 14.23.07.png",
            "Capture d’écran 2024-05-02 à 14.23.07.png",
            "Captura de pantalla 2024-05-02 a las 14.23.07.png",
            "Снимок экрана 2024-05-02 в 14.23.07.png",
            "スクリーンショット 2024-05-02 14.23.07.png",
            "截屏2024-05-02 14.23.07.png",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.taken(name), datetime(2024, 5, 2, 14, 23, 7), name
                )

    def test_an_ascii_apostrophe_reads_the_same_as_the_typographic_one(self):
        self.assertEqual(
            self.taken("Capture d'écran 2024-05-02 à 14.23.07.png"),
            datetime(2024, 5, 2, 14, 23, 7),
        )

    def test_a_second_is_optional(self):
        self.assertEqual(
            self.taken("Screenshot 2024-05-02 at 14.23.png"),
            datetime(2024, 5, 2, 14, 23),
        )

    def test_what_macos_appends_to_a_duplicate_is_ignored(self):
        self.assertEqual(
            self.taken("Screenshot 2024-05-02 at 14.23.07 (2).png"),
            datetime(2024, 5, 2, 14, 23, 7),
        )

    def test_a_name_without_a_stamp_is_not_a_screenshot(self):
        for name in (
            "Screenshot.png",
            "Screenshot of the boiler.png",
            "Screenshot 2024-05-02.png",  # a date, but no time
            "invoice 2024-05-02 14.23.pdf",  # a stamp, but not a capture
            "Screenshot 2024-05-02 report 14.30.png",  # a word, not "at"
        ):
            with self.subTest(name=name):
                self.assertIsNone(self.taken(name), name)

    def test_an_impossible_stamp_is_not_a_date(self):
        self.assertIsNone(self.taken("Screenshot 2024-13-40 at 14.23.07.png"))
        self.assertIsNone(self.taken("Screenshot 2024-05-02 at 25.99.07.png"))

    def test_a_name_someone_wrote_their_own_words_into_is_kept(self):
        found = read_screenshot_name("Screenshot 2024-05-02 at 14.23.07 boiler warranty.png")
        self.assertIsNotNone(found)
        self.assertEqual(found.rest, "boiler warranty")

    def test_a_plain_capture_carries_nothing_beyond_the_stamp(self):
        found = read_screenshot_name("Screenshot 2024-05-02 at 14.23.07.png")
        self.assertEqual(found.rest, "")

    def test_the_config_can_add_a_capture_tool_of_its_own(self):
        source = SourceConfig(screenshot_names=["Grab"])
        names = screenshot_names(source)
        self.assertIsNone(read_screenshot_name("Grab 2024-05-02 at 14.23.07.png"))
        self.assertIsNotNone(read_screenshot_name("Grab 2024-05-02 at 14.23.07.png", names))


class TestSelectors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.desktop = Path(self.tmp.name)
        for name in (
            "Screenshot 2024-05-02 at 14.23.07.png",
            "Screen Shot 2019-03-01 at 4.15.22 PM.png",
            "invoice.pdf",
            "company-logo.png",
            "holiday.jpg",
        ):
            (self.desktop / name).write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def found(self, *include: str) -> set[str]:
        keep = keeper(SourceConfig(include=list(include)))
        return {path.name for path in iter_documents(self.desktop, keep=keep)}

    def test_all_is_everything_scanvault_can_read(self):
        self.assertEqual(len(self.found("all")), 5)

    def test_screenshots_and_pdfs_leaves_the_logo_and_the_holiday_photo(self):
        self.assertEqual(
            self.found("screenshots", "pdfs"),
            {
                "Screenshot 2024-05-02 at 14.23.07.png",
                "Screen Shot 2019-03-01 at 4.15.22 PM.png",
                "invoice.pdf",
            },
        )

    def test_pdfs_alone(self):
        self.assertEqual(self.found("pdfs"), {"invoice.pdf"})

    def test_images_includes_screenshots(self):
        self.assertEqual(len(self.found("images")), 4)

    def test_a_typo_in_the_config_is_refused_rather_than_guessed_at(self):
        # Silently widening this would empty a Desktop into the vault, and
        # silently narrowing it looks like a folder with nothing in it.
        for include in (["nonsense"], ["pdfs", "documents"], []):
            with self.subTest(include=include), self.assertRaises(ValueError):
                keeper(SourceConfig(include=include))

    def test_the_config_reads_the_same_values_the_flag_does(self):
        self.assertEqual(self.found("PDFs"), {"invoice.pdf"})

    def test_naming_a_file_outright_files_it_whatever_the_filter_says(self):
        keep = keeper(SourceConfig(include=["screenshots"]))
        logo = self.desktop / "company-logo.png"
        self.assertEqual(list(iter_documents(logo, keep=keep)), [logo])

    def test_parse_selectors_rejects_what_is_not_one(self):
        self.assertEqual(parse_selectors("screenshots, pdfs"), ["screenshots", "pdfs"])
        self.assertEqual(parse_selectors("PDFs"), ["pdfs"])
        with self.assertRaises(ValueError):
            parse_selectors("screenshots,documents")

    def test_a_flag_that_names_nothing_is_refused(self):
        # Reading `--only ","` as "everything" would widen the filter, which is
        # the one direction a typo must never go.
        for value in (",", "  ", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_selectors(value)

    def test_every_selector_parses(self):
        self.assertEqual(parse_selectors(",".join(SELECTORS)), list(SELECTORS))

    def test_describe_names_what_was_asked_for(self):
        self.assertEqual(describe(["all"]), "documents")
        self.assertEqual(describe(["screenshots", "pdfs"]), "screenshots, pdfs")

    def test_is_screenshot_reads_the_name_not_the_suffix(self):
        self.assertTrue(is_screenshot(self.desktop / "Screenshot 2024-05-02 at 14.23.07.png"))
        self.assertFalse(is_screenshot(self.desktop / "company-logo.png"))


class TestScreenshotDating(unittest.TestCase):
    """A screenshot is dated by the clock that took it, not by the picture."""

    def setUp(self):
        self.config = load_config()

    def resolved(self, name: str, meta: DocumentMeta) -> DocumentMeta:
        return resolve_date(meta, Path(name), self.config, "Invoice date: 2019-03-04")

    def test_the_capture_time_beats_a_date_the_model_read_off_the_picture(self):
        meta = DocumentMeta("Invoice", "Invoices", document_date=date(2019, 3, 4))
        self.resolved("Screenshot 2024-05-02 at 14.23.07.png", meta)
        self.assertEqual(meta.document_date, date(2024, 5, 2))
        self.assertEqual(meta.date_source, "screenshot")

    def test_an_ordinary_scan_is_still_dated_by_the_document(self):
        meta = DocumentMeta("Invoice", "Invoices", document_date=date(2019, 3, 4))
        self.resolved("scan_001.pdf", meta)
        self.assertEqual(meta.document_date, date(2019, 3, 4))
        self.assertEqual(meta.date_source, "document")

    def test_turning_it_off_puts_the_document_back_in_charge(self):
        self.config.dates.screenshot_capture_time = False
        meta = DocumentMeta("Invoice", "Invoices", document_date=date(2019, 3, 4))
        self.resolved("Screenshot 2024-05-02 at 14.23.07.png", meta)
        self.assertEqual(meta.document_date, date(2019, 3, 4))

    def test_a_capture_stamped_in_the_future_is_not_a_date(self):
        meta = DocumentMeta("Invoice", "Invoices")
        self.resolved("Screenshot 2999-05-02 at 14.23.07.png", meta)
        self.assertNotEqual(meta.date_source, "screenshot")

    def test_the_year_tag_follows_the_date_the_note_ends_up_with(self):
        meta = DocumentMeta(
            "Invoice",
            "Invoices",
            document_date=date(2019, 3, 4),
            tags=["scan", "year-2019", "receipt"],
        )
        self.resolved("Screenshot 2024-05-02 at 14.23.07.png", meta)
        # In place, so the tag list keeps the order it was written in.
        self.assertEqual(meta.tags, ["scan", "year-2024", "receipt"])

    def test_an_undated_document_that_gains_a_date_gains_the_tag(self):
        meta = DocumentMeta("Invoice", "Invoices", tags=["scan"])
        self.resolved("Screenshot 2024-05-02 at 14.23.07.png", meta)
        self.assertEqual(meta.tags, ["scan", "year-2024"])

    def test_a_date_that_does_not_move_leaves_the_tags_alone(self):
        meta = DocumentMeta(
            "Invoice", "Invoices", document_date=date(2019, 3, 4), tags=["year-2019"]
        )
        self.resolved("scan_001.pdf", meta)
        self.assertEqual(meta.tags, ["year-2019"])

    def test_the_tag_is_left_alone_when_year_tags_are_switched_off(self):
        self.config.tags.year_tag = False
        meta = DocumentMeta("Invoice", "Invoices", document_date=date(2019, 3, 4), tags=["scan"])
        self.resolved("Screenshot 2024-05-02 at 14.23.07.png", meta)
        self.assertEqual(meta.tags, ["scan"])


class TestScreenshotTitle(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_a_capture_with_nothing_in_its_name_keeps_the_moment_it_was_taken(self):
        self.assertEqual(
            filename_title(Path("Screenshot 2024-05-02 at 14.23.07.png"), self.config),
            "Screenshot 2024-05-02 14.23.07",
        )

    def test_two_captures_a_second_apart_do_not_get_the_same_title(self):
        first = filename_title(Path("Screenshot 2024-05-02 at 14.23.07.png"), self.config)
        second = filename_title(Path("Screenshot 2024-05-02 at 14.23.08.png"), self.config)
        self.assertNotEqual(first, second)

    def test_words_someone_wrote_into_the_name_win_over_the_stamp(self):
        self.assertEqual(
            filename_title(
                Path("Screenshot 2024-05-02 at 14.23.07 boiler warranty.png"), self.config
            ),
            "boiler warranty",
        )

    def test_an_ordinary_scan_is_named_the_way_it_always_was(self):
        self.assertEqual(
            filename_title(Path("SwiftScan Feb 7, 2021 11.45 AM.pdf"), self.config),
            "Untitled document",
        )
        self.assertEqual(
            filename_title(Path("Boiler service 2019-04-02 - 1.pdf"), self.config),
            "Boiler service",
        )


class TestTaggingFromTheFile(unittest.TestCase):
    """A tag the filename decided still goes through the vault's vocabulary."""

    def setUp(self):
        self.meta = DocumentMeta("Receipt", "Receipts", tags=["scan"])

    def test_it_folds_into_the_spelling_the_vault_already_uses(self):
        ledger = TagLedger()
        ledger.record(["screenshots"] * 5)
        tag_from_file(self.meta, "screenshot", ledger)
        self.assertEqual(self.meta.tags, ["scan", "screenshots"])

    def test_a_vault_that_has_never_seen_it_learns_it(self):
        ledger = TagLedger()
        tag_from_file(self.meta, "screenshot", ledger)
        self.assertEqual(self.meta.tags, ["scan", "screenshot"])
        self.assertIn("screenshot", {group.canonical for group in ledger.groups})

    def test_it_is_added_once(self):
        for _ in range(3):
            tag_from_file(self.meta, "screenshot", None)
        self.assertEqual(self.meta.tags, ["scan", "screenshot"])

    def test_an_empty_name_adds_nothing(self):
        tag_from_file(self.meta, "", None)
        self.assertEqual(self.meta.tags, ["scan"])


RECEIPT = [
    "CORNER CAFE",
    "RECEIPT 2019-03-04",
    "Two coffees 6.00 GBP",
    "Thank you for your purchase. VAT receipt, card ending 4242.",
]


@unittest.skipUnless(HAS_RASTERISER and HAS_BACKEND, "needs pdftoppm and an OCR backend")
class TestArchivingADesktop(unittest.TestCase):
    """End to end: point it at a Desktop and take only what was asked for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.desktop = root / "Desktop"
        self.vault_dir = root / "vault"
        self.capture = make_scan_image(
            self.desktop / "Screenshot 2024-05-02 at 14.23.07.png", RECEIPT, fmt="png"
        )
        make_text_pdf(self.desktop / "invoice.pdf", RECEIPT)
        self.logo = make_scan_image(self.desktop / "company-logo.png", ["ACME"], fmt="png")
        self.config = load_config(
            overrides={
                "source_dir": str(self.desktop),
                "vault_dir": str(self.vault_dir),
                "source.include": ["screenshots", "pdfs"],
            }
        )
        self.report = ingest(self.config, client=None)

    def tearDown(self):
        self.tmp.cleanup()

    def notes(self) -> dict[str, dict]:
        vault = Vault(self.config)
        found = {}
        for note in vault.iter_notes():
            front, _ = vault.read_note(note)
            if not front.get("para_index"):
                found[str(front.get("source_file") or "")] = front
        return found

    def test_only_what_was_asked_for_is_filed(self):
        self.assertEqual(self.report.count("ingested"), 2, self.report.results)
        self.assertEqual(
            set(self.notes()), {"Screenshot 2024-05-02 at 14.23.07.png", "invoice.pdf"}
        )

    def test_the_logo_is_left_on_the_desktop(self):
        self.assertTrue(self.logo.is_file())

    def test_the_screenshot_is_dated_the_day_it_was_taken(self):
        front = self.notes()["Screenshot 2024-05-02 at 14.23.07.png"]
        # The receipt in the picture is dated 2019-03-04; the capture is not.
        self.assertEqual(str(front["date"]), "2024-05-02")
        self.assertEqual(front["date_source"], "screenshot")

    def test_the_screenshot_is_tagged_as_one(self):
        self.assertIn("screenshot", self.notes()["Screenshot 2024-05-02 at 14.23.07.png"]["tags"])

    def test_the_pdf_beside_it_is_dated_by_the_document(self):
        front = self.notes()["invoice.pdf"]
        self.assertEqual(str(front["date"]), "2019-03-04")
        self.assertNotIn("screenshot", front["tags"])


if __name__ == "__main__":
    unittest.main()
