"""Notes whose scans "could not be found", how they got that way, and `repair`.

The way it happened: a hand-written note embeds `![[Scan 52.pdf]]`, `organize`
files it and writes the link vault-relative, and then `ingest` is pointed at a
folder inside the vault. It moved the scan into `_attachments/` as a new
document with a note of its own - and the first note was left pointing at a
file that was no longer there.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import scanvault.pipeline as pipeline
from scanvault.cli import main
from scanvault.config import load_config
from scanvault.organizer import apply as organize_apply, plan as organize_plan
from scanvault.repair import apply as repair_apply, plan as repair_plan, retarget
from scanvault.state import State
from scanvault.vault import Vault, link_targets
from tests.helpers import make_text_pdf

COUNCIL = ["Example City Council", "Council Tax Bill 2025/26", "Date 01/03/2025"] * 30
GAS = ["Example Energy", "Invoice for gas supplied", "Date 01/04/2025"] * 30


def broken_links(vault: Vault) -> list[tuple[str, str]]:
    found = []
    for note in vault.iter_notes():
        for target in link_targets(note.read_text(encoding="utf-8")):
            if target.endswith(".pdf") and vault.resolve_link(note, target) is None:
                found.append((note.name, target))
    return found


class VaultCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        make_text_pdf(self.root / "Archive" / "Scan 52.pdf", COUNCIL)
        make_text_pdf(self.root / "Archive" / "Scan 53.pdf", GAS)
        (self.root / "Archive" / "Council tax.md").write_text("# Council tax\n\n![[Scan 52.pdf]]\n")
        (self.root / "Archive" / "Gas.md").write_text(
            "# Gas\n\n![[Scan 53.pdf]]\n\nCalled them about this on 3 April.\n"
        )
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.vault = Vault(self.config)
        organize_apply(
            organize_plan(self.config, None, include_unmanaged=True, ocr=False), self.config
        )
        self.inbox = load_config(
            overrides={"vault_dir": str(self.root), "source_dir": str(self.root / "Archive")}
        )

    def break_the_vault(self):
        """What ingest did before it learned to leave embedded scans alone."""
        guard = pipeline._without_embedded
        pipeline._without_embedded = lambda paths, vault: (paths, [])
        try:
            pipeline.ingest(self.inbox, None, None)
        finally:
            pipeline._without_embedded = guard
        self.assertEqual(len(broken_links(self.vault)), 2)


class TestIngestLeavesEmbeddedScansAlone(VaultCase):
    def test_a_scan_a_note_embeds_is_not_filed_again(self):
        report = pipeline.ingest(self.inbox, None, None)
        self.assertEqual(report.count("skipped"), 2)
        self.assertEqual(report.count("ingested"), 0)
        self.assertTrue((self.root / "Archive" / "Scan 52.pdf").is_file())
        self.assertEqual(broken_links(self.vault), [])

    def test_a_loose_scan_in_the_vault_is_still_filed(self):
        make_text_pdf(self.root / "Archive" / "Scan 54.pdf", ["Water bill"] * 40)
        report = pipeline.ingest(self.inbox, None, None)
        self.assertEqual(report.count("ingested"), 1)
        self.assertFalse((self.root / "Archive" / "Scan 54.pdf").exists())

    def test_a_scan_outside_the_vault_with_an_embedded_name_is_filed(self):
        # Scanners reuse names; only files inside the vault belong to its notes.
        inbox = Path(self.tmp.name) / "inbox"
        make_text_pdf(inbox / "Scan 52.pdf", ["A different letter"] * 40)
        config = load_config(overrides={"vault_dir": str(self.root), "source_dir": str(inbox)})
        self.assertEqual(pipeline.ingest(config, None, None).count("ingested"), 1)


class TestRepair(VaultCase):
    def test_a_healthy_vault_has_nothing_to_repair(self):
        report = repair_plan(self.config)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.links, 2)

    def test_the_stale_copy_is_removed_and_the_one_with_writing_relinked(self):
        self.break_the_vault()
        report = repair_plan(self.config)
        kinds = {finding.note.name.split(" ", 1)[1]: finding.kind for finding in report.findings}
        self.assertEqual(kinds["Council Tax Bill 2025-26.md"], "remove")
        gas = next(f for f in report.findings if f.kind == "relink")
        self.assertEqual(gas.target, "Archive/Scan 53.pdf")

        repair_apply(report, self.config)
        self.assertEqual(broken_links(self.vault), [])
        trashed = list((self.root / ".scanvault" / "repair-trash").rglob("*.md"))
        self.assertEqual(len(trashed), 1)
        self.assertIn("Council Tax", trashed[0].name)
        gas_note = gas.note.read_text(encoding="utf-8")
        self.assertIn("Called them about this on 3 April.", gas_note)
        self.assertIn("![[Archive/_attachments/", gas_note)
        self.assertEqual(repair_plan(self.config).findings, [])

    def test_preview_changes_nothing(self):
        self.break_the_vault()
        before = sorted(p for p in self.root.rglob("*") if p.is_file())
        repair_plan(self.config)
        self.assertEqual(sorted(p for p in self.root.rglob("*") if p.is_file()), before)

    def test_a_document_moved_by_hand_is_found_by_name(self):
        moved = self.root / "Elsewhere" / "Scan 52.pdf"
        moved.parent.mkdir()
        (self.root / "Archive" / "Scan 52.pdf").rename(moved)
        report = repair_plan(self.config)
        self.assertEqual([f.kind for f in report.findings], ["relink"])
        repair_apply(report, self.config)
        self.assertEqual(broken_links(self.vault), [])

    def test_a_scan_with_the_same_name_but_other_text_is_not_taken(self):
        (self.root / "Archive" / "Scan 52.pdf").unlink()
        inbox = Path(self.tmp.name) / "inbox"
        make_text_pdf(inbox / "Scan 52.pdf", ["Dentist appointment reminder"] * 40)
        config = load_config(overrides={"vault_dir": str(self.root), "source_dir": str(inbox)})
        pipeline.ingest(config, None, None)
        finding = repair_plan(self.config).findings[0]
        self.assertEqual(finding.kind, "missing")
        self.assertIn("does not match", finding.reason)

    def test_a_missing_document_is_reported_then_dropped_on_request(self):
        (self.root / "Archive" / "Scan 52.pdf").unlink()
        self.assertEqual([f.kind for f in repair_plan(self.config).findings], ["missing"])
        report = repair_plan(self.config, drop_missing=True)
        self.assertEqual([f.kind for f in report.findings], ["drop"])
        repair_apply(report, self.config)
        note = report.findings[0].note.read_text(encoding="utf-8")
        self.assertNotIn("Scan 52.pdf", note.split("Extracted text")[0])
        self.assertIn("# Council Tax", note)

    def test_the_index_is_rebuilt_from_the_notes(self):
        self.break_the_vault()
        state = State(self.config.state_root)
        entry = next(iter(state.documents.values()))
        note = self.root / entry["note"]
        moved = note.with_name("moved by hand.md")
        note.rename(moved)
        state.documents["deadbeef"] = {"note": "Archive/gone.md"}
        state._dirty = True
        state.save()

        report = repair_plan(self.config)
        self.assertEqual((report.index.updated, report.index.dropped), (1, 1))
        repair_apply(report, self.config)
        data = json.loads((self.config.state_root / "index.json").read_text())["documents"]
        self.assertNotIn("deadbeef", data)
        self.assertIn(moved.relative_to(self.root).as_posix(), [e["note"] for e in data.values()])


class TestRetarget(unittest.TestCase):
    NOTE = (
        '---\ntitle: "X"\nattachment: "old/a b.pdf"\n---\n\n# X\n\n'
        "![[old/a b.pdf]]\n\nsee [the scan](<old/a b.pdf>) and [[old/a b.pdf|here]]\n"
        "![[other.pdf]]\n"
    )

    def test_every_form_of_the_link_is_rewritten(self):
        out = retarget(self.NOTE, "old/a b.pdf", "new/c.pdf")
        self.assertNotIn("old/a b.pdf", out)
        self.assertIn('attachment: "new/c.pdf"', out)
        self.assertIn("![[new/c.pdf]]", out)
        self.assertIn("[the scan](<new/c.pdf>)", out)
        self.assertIn("[[new/c.pdf|here]]", out)
        self.assertIn("![[other.pdf]]", out)

    def test_dropping_a_link_leaves_the_rest(self):
        out = retarget(self.NOTE, "old/a b.pdf", None)
        self.assertNotIn("a b.pdf", out)
        self.assertIn("![[other.pdf]]", out)
        self.assertIn('title: "X"', out)


class TestRepairCli(VaultCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["-q", "repair", "--vault", str(self.root), *args])
        return code, out.getvalue()

    def test_preview_then_apply(self):
        self.break_the_vault()
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("[dry-run] remove:", out)
        self.assertIn("Re-run with --apply", out)
        self.assertEqual(len(broken_links(self.vault)), 2)
        code, out = self.run_cli("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(broken_links(self.vault), [])
        code, out = self.run_cli()
        self.assertIn("every note's documents are where it says they are", out)

    def test_missing_documents_fail_the_check(self):
        (self.root / "Archive" / "Scan 52.pdf").unlink()
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("--drop-missing", out)


if __name__ == "__main__":
    unittest.main()
