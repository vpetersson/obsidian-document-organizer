"""Reusing the tags a vault already has, rather than inventing near-copies.

The failure this exists to stop: `mortgage` on forty documents, `mortgages` on
one, and searching for either finds the wrong subset. The other failure - the
one that would be worse - is merging two tags that are merely similar, so most
of these tests are about what must *not* be folded.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scanvault.config import load_config
from scanvault.organizer import apply as organizer_apply, plan, tags_off_ledger
from scanvault.tags import TagLedger, fingerprint, ledger_for
from scanvault.vault import Vault

NOTE = (
    '---\ntitle: "{title}"\ndate: 2024-01-01\ncategory: "Loans"\ntags:\n{tags}'
    'classifier: "llm"\ncssclasses:\n  - scanvault\n---\n\n# {title}\n'
)


def note_text(title: str, tags: list[str]) -> str:
    return NOTE.format(title=title, tags="".join(f"  - {tag}\n" for tag in tags))


class TestFingerprint(unittest.TestCase):
    def test_plurals_collapse(self):
        self.assertEqual(fingerprint("invoices"), fingerprint("invoice"))
        self.assertEqual(fingerprint("policies"), fingerprint("policy"))

    def test_word_order_does_not_matter(self):
        self.assertEqual(fingerprint("council-tax"), fingerprint("tax-council"))

    def test_diacritics_are_folded(self):
        self.assertEqual(fingerprint("försäkring"), fingerprint("forsakring"))

    def test_a_short_word_keeps_its_s(self):
        """Dropping it would turn "gas" into "ga"."""
        self.assertNotEqual(fingerprint("gas"), fingerprint("ga"))

    def test_words_that_only_look_plural_are_left_alone(self):
        for word in ("business", "census", "analysis"):
            with self.subTest(word=word):
                self.assertEqual(fingerprint(word), word)

    def test_years_stay_apart(self):
        self.assertNotEqual(fingerprint("year-2023"), fingerprint("year-2024"))


class TestFolding(unittest.TestCase):
    def setUp(self):
        self.ledger = TagLedger()
        self.ledger.observe(
            ["mortgage"] * 30 + ["council-tax"] * 9 + ["skatteverket"] * 7 + ["hsbc"] * 20
        )
        self.ledger.consolidate()

    def fold(self, tag: str) -> str:
        return self.ledger.canonical(tag)

    def test_a_plural_reuses_the_singular(self):
        self.assertEqual(self.fold("mortgages"), "mortgage")

    def test_case_and_spacing_do_not_make_a_new_tag(self):
        self.assertEqual(self.fold("Council Tax"), "council-tax")
        self.assertEqual(self.fold("COUNCIL-TAX"), "council-tax")

    def test_a_missing_separator_is_the_same_tag(self):
        self.assertEqual(self.fold("counciltax"), "council-tax")

    def test_a_genitive_s_is_the_same_tag(self):
        self.assertEqual(self.fold("skatteverkets"), "skatteverket")

    def test_a_genuinely_new_tag_is_kept(self):
        self.assertEqual(self.fold("veterinary"), "veterinary")

    def test_a_new_tag_joins_the_vocabulary(self):
        self.fold("veterinary")
        self.assertEqual(self.fold("veterinarys"), "veterinary")

    def test_years_are_never_merged(self):
        self.ledger.observe(["year-2023"] * 20)
        self.assertEqual(self.fold("year-2024"), "year-2024")

    def test_a_longer_tag_is_not_swallowed_by_a_shorter_one(self):
        for tag in ("mortgage-statement", "hsbc-bank-plc", "council"):
            with self.subTest(tag=tag):
                self.assertEqual(self.fold(tag), tag)

    def test_related_words_are_not_the_same_word(self):
        for tag in ("medicine", "utility-bill", "banking"):
            with self.subTest(tag=tag):
                self.assertEqual(self.fold(tag), tag)

    def test_what_was_folded_is_reported(self):
        self.fold("mortgages")
        self.assertEqual(self.ledger.folded, {"mortgages": "mortgage"})

    def test_a_cutoff_of_one_leaves_only_exact_and_fingerprint_matching(self):
        strict = TagLedger(cutoff=1.0)
        strict.observe(["council-tax"] * 9)
        strict.consolidate()
        self.assertEqual(strict.canonical("counciltax"), "counciltax")
        self.assertEqual(strict.canonical("council-taxes"), "council-tax")


class TestElection(unittest.TestCase):
    def test_the_spelling_most_documents_use_wins(self):
        ledger = TagLedger()
        ledger.observe(["mortgages"] * 40)
        ledger.observe(["mortgage"] * 2)
        self.assertEqual(ledger.canonical("mortgage"), "mortgages")

    def test_a_reserved_tag_keeps_its_spelling_however_rare(self):
        """The rules and the category list are the fixed part of the vocabulary."""
        ledger = TagLedger()
        ledger.reserve(["mortgage"])
        ledger.observe(["mortgages"] * 40)
        self.assertEqual(ledger.canonical("mortgages"), "mortgage")

    def test_consolidation_keeps_the_bigger_spelling(self):
        ledger = TagLedger()
        ledger.observe(["council-tax"] * 9 + ["counciltax"] * 2)
        ledger.consolidate()
        self.assertEqual(ledger.canonical("counciltax"), "council-tax")
        group = ledger.duplicates()[0]
        self.assertEqual(group.canonical, "council-tax")
        self.assertEqual(group.variants, ["counciltax"])
        self.assertEqual(group.total, 11)


class TestSeedingFromAVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.folder = self.root / "Archive" / "Loans" / "2024"
        self.folder.mkdir(parents=True)
        self.config = load_config(overrides={"vault_dir": str(self.root)})

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, title: str, tags: list[str]) -> Path:
        path = self.folder / f"2024-01-01 {title}.md"
        path.write_text(note_text(title, tags), encoding="utf-8")
        return path

    def test_the_vaults_own_tags_are_the_vocabulary(self):
        for name in "ABC":
            self.write(f"Statement {name}", ["scan", "mortgage"])
        ledger = ledger_for(self.config, Vault(self.config))
        self.assertEqual(ledger.canonical("mortgages"), "mortgage")

    def test_hand_written_notes_count_too(self):
        """Fighting someone's own tags with near-copies is the problem."""
        (self.root / "Archive" / "reading.md").write_text(
            "---\ntags:\n  - hydroponics\n---\n\n# Reading\n", encoding="utf-8"
        )
        ledger = ledger_for(self.config, Vault(self.config))
        self.assertEqual(ledger.canonical("hydroponic"), "hydroponics")

    def test_it_survives_a_run(self):
        ledger = TagLedger()
        ledger.observe(["mortgage"] * 5)
        ledger.save(self.config.state_root)
        reloaded = TagLedger()
        reloaded.load(self.config.state_root)
        self.assertEqual(reloaded.canonical("mortgages"), "mortgage")

    def test_a_missing_ledger_file_is_not_an_error(self):
        ledger = TagLedger()
        ledger.load(self.config.state_root)
        self.assertEqual(ledger.groups, [])


class TestOrganizeConsolidatesAVault(unittest.TestCase):
    """The ledger has to fix the vault someone already has, not just new files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.folder = self.root / "Archive" / "Loans" / "2024"
        self.folder.mkdir(parents=True)
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        for name in "ABC":
            (self.folder / f"2024-01-01 Statement {name}.md").write_text(
                note_text(f"Statement {name}", ["scan", "mortgage"]), encoding="utf-8"
            )
        self.odd = self.folder / "2024-01-01 Statement D.md"
        self.odd.write_text(note_text("Statement D", ["scan", "mortgages"]), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_the_odd_one_out_is_rewritten(self):
        report = plan(self.config, client=None)
        rewrites = [action for action in report.actions if action.kind == "rewrite"]
        self.assertEqual([action.path for action in rewrites], [self.odd])
        self.assertIn("mortgages -> mortgage", rewrites[0].reason)
        self.assertEqual(report.count("noop"), 3)

    def test_applying_it_consolidates_the_tag(self):
        organizer_apply(plan(self.config, client=None), self.config, client=None)
        self.assertIn("- mortgage\n", self.odd.read_text())
        self.assertNotIn("mortgages", self.odd.read_text())

    def test_a_second_run_has_nothing_left_to_do(self):
        organizer_apply(plan(self.config, client=None), self.config, client=None)
        self.assertEqual(plan(self.config, client=None).count("rewrite"), 0)

    def test_a_vault_that_is_already_consistent_is_left_alone(self):
        self.odd.write_text(note_text("Statement D", ["scan", "mortgage"]), encoding="utf-8")
        self.assertEqual(plan(self.config, client=None).count("rewrite"), 0)


class TestTagsOffLedger(unittest.TestCase):
    def setUp(self):
        self.ledger = TagLedger()
        self.ledger.observe(["mortgage"] * 5)

    def test_it_names_both_spellings(self):
        drifted = tags_off_ledger({"tags": ["scan", "mortgages"]}, self.ledger)
        self.assertEqual(drifted, ["mortgages -> mortgage"])

    def test_a_consistent_note_reports_nothing(self):
        self.assertEqual(tags_off_ledger({"tags": ["mortgage"]}, self.ledger), [])

    def test_a_note_with_no_tags_reports_nothing(self):
        self.assertEqual(tags_off_ledger({}, self.ledger), [])
        self.assertEqual(tags_off_ledger({"tags": "mortgage"}, self.ledger), [])


if __name__ == "__main__":
    unittest.main()
