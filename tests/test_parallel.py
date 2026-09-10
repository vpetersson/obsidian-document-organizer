"""Classification fans out; everything that writes stays on one thread."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from scanvault.cache import ClassificationCache
from scanvault.config import load_config
from scanvault.organizer import apply as organizer_apply
from scanvault.organizer import plan
from scanvault.parallel import MAX_WORKERS, Progress, parallel_map, resolve_workers
from scanvault.pipeline import ingest
from tests.helpers import StubClient, make_text_pdf

RESPONSE = {"title": "Statement", "category": "Banking", "confidence": 0.9}
LINES = [
    "EXAMPLE BANK PLC",
    "Statement 2024-05-02",
    "Account 55-0192-33",
    "Closing balance 1,204.00 GBP",
    "Interest paid 12.40 GBP over the statement period.",
]


class SlowClient(StubClient):
    """A model that takes its time, and counts how many calls overlap."""

    def __init__(self, response: dict, delay: float = 0.15):
        super().__init__(response)
        self.delay = delay
        self.concurrent = 0
        self.peak = 0
        self._lock = threading.Lock()

    def chat_json(self, system: str, user: str, schema=None, images=None) -> dict:
        with self._lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        try:
            time.sleep(self.delay)
            return super().chat_json(system, user, schema, images)
        finally:
            with self._lock:
                self.concurrent -= 1


class TestWorkerPool(unittest.TestCase):
    def test_results_keep_their_order(self):
        out = parallel_map(lambda i: i * 2, range(20), workers=8)
        self.assertEqual(out, [i * 2 for i in range(20)])

    def test_work_actually_overlaps(self):
        started = time.monotonic()
        parallel_map(lambda _: time.sleep(0.1), range(8), workers=4)
        self.assertLess(time.monotonic() - started, 0.6, "8 x 100ms should not take 800ms")

    def test_one_failure_does_not_sink_the_rest(self):
        def work(i: int) -> str:
            if i == 2:
                raise ValueError("no")
            return f"ok-{i}"

        out = parallel_map(work, range(4), workers=4, on_error=lambda i, exc: f"failed-{i}")
        self.assertEqual(out, ["ok-0", "ok-1", "failed-2", "ok-3"])

    def test_without_a_handler_the_error_surfaces(self):
        with self.assertRaises(ValueError):
            parallel_map(lambda _: (_ for _ in ()).throw(ValueError("no")), [1], workers=2)

    def test_worker_counts_are_sane(self):
        self.assertEqual(resolve_workers(0), 1)
        self.assertEqual(resolve_workers(-3), 1)
        self.assertEqual(resolve_workers(4), 4)
        self.assertEqual(resolve_workers(1000), MAX_WORKERS)

    def test_progress_counts_once_per_item(self):
        progress = Progress(3)
        for name in ("a", "b", "c"):
            progress.start(name)
        self.assertEqual(progress.done, 3)

    def test_the_cache_survives_being_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(Path(tmp) / ".scanvault", load_config())
            parallel_map(lambda i: cache.put(f"text {i}", {"title": str(i)}), range(50), workers=8)
            self.assertEqual(len(cache.entries), 50)


class TestOrganizeInParallel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        for index in range(6):
            note = self.root / "Scanned" / f"note {index}.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(
                f'---\ntitle: "scan {index}"\ncategory: "Other"\nclassifier: "llm"\n---\n\n'
                f"Statement number {index} from the bank, dated 2024-05-02.\n",
                encoding="utf-8",
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_notes_are_classified_concurrently(self):
        client = SlowClient(RESPONSE)
        self.config.llm.workers = 4
        started = time.monotonic()
        report = plan(self.config, client=client)
        elapsed = time.monotonic() - started

        self.assertEqual(len(client.calls), 6)
        self.assertGreater(client.peak, 1, "the whole point")
        self.assertLess(elapsed, 6 * client.delay, "slower than sequential would be a bug")
        self.assertEqual(len(report.actions), 6)

    def test_one_worker_is_still_correct(self):
        client = SlowClient(RESPONSE, delay=0.01)
        self.config.llm.workers = 1
        report = plan(self.config, client=client)
        self.assertEqual(client.peak, 1)
        self.assertEqual(len(report.actions), 6)

    def test_the_plan_is_the_same_either_way(self):
        one = plan(self.config, client=StubClient(RESPONSE), use_cache=False)
        self.config.llm.workers = 8
        many = plan(self.config, client=StubClient(RESPONSE), use_cache=False)
        self.assertEqual(
            [(a.kind, a.path.name) for a in one.actions],
            [(a.kind, a.path.name) for a in many.actions],
        )


class TestIngestInParallel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "inbox"
        self.config = load_config(
            overrides={"source_dir": str(self.source), "vault_dir": str(self.root / "vault")}
        )
        for index in range(5):
            make_text_pdf(self.source / f"scan_{index}.pdf", [f"Document {index}"] + LINES)

    def tearDown(self):
        self.tmp.cleanup()

    def test_documents_are_read_concurrently_and_filed_in_order(self):
        client = SlowClient(RESPONSE)
        self.config.llm.workers = 4
        report = ingest(self.config, client=client)
        self.assertEqual(report.count("ingested"), 5, report.results)
        self.assertGreater(client.peak, 1)
        self.assertEqual(
            [result.source.name for result in report.results],
            [f"scan_{index}.pdf" for index in range(5)],
        )

    def test_identical_documents_are_still_deduplicated(self):
        # Every worker checks the index before the first copy has been written,
        # so the duplicate has to be caught again at filing time.
        for index in range(3):
            make_text_pdf(self.source / f"copy_{index}.pdf", LINES)
        self.config.llm.workers = 4
        report = ingest(self.config, client=StubClient(RESPONSE))
        self.assertEqual(report.count("duplicate"), 2, "two of the three copies")
        # Five distinct scans plus one of the three identical copies.
        attachments = list((self.root / "vault" / "Archive" / "_attachments").rglob("*.pdf"))
        self.assertEqual(len(attachments), 6)
        self.assertEqual(
            list((self.root / "vault").rglob("*.ocr.pdf")),
            [],
            "a document we decided not to file should not leave its OCR behind",
        )


class TestAdoptInParallel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        for index in range(4):
            make_text_pdf(self.root / "Inbox" / f"scan_{index}.pdf", [f"Document {index}"] + LINES)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unfiled_documents_are_read_concurrently(self):
        client = SlowClient(RESPONSE)
        self.config.llm.workers = 4
        report = plan(self.config, client=client)
        self.assertEqual(report.count("adopt"), 4)

        client.calls.clear()
        client.peak = 0
        organizer_apply(report, self.config, client=client)
        self.assertGreater(client.peak, 1, "the adopts should overlap too")
        self.assertEqual(len(list(self.root.rglob("*.md"))), 4)


if __name__ == "__main__":
    unittest.main()


class TestDocumentsFlowThroughIndividually(unittest.TestCase):
    """Not "classify everything, then file everything" - each document goes
    read, classify, written on its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "inbox"
        self.config = load_config(
            overrides={"source_dir": str(self.source), "vault_dir": str(self.root / "vault")}
        )
        self.config.llm.workers = 4
        # The first document is the slow one; if filing waited for the whole
        # batch, nothing would be written until it finished.
        for index in range(6):
            make_text_pdf(self.source / f"scan_{index}.pdf", [f"Document {index}"] + LINES)

    def tearDown(self):
        self.tmp.cleanup()

    def _record_writes(self):
        from scanvault.vault import Vault

        written: list[str] = []
        lock = threading.Lock()
        original = Vault.write_document

        def spy(vault_self, meta, text, *args, **kwargs):
            result = original(vault_self, meta, text, *args, **kwargs)
            with lock:
                written.append(kwargs.get("source_path", args[1] if len(args) > 1 else None))
            return result

        return written, spy, original

    def test_later_documents_are_filed_before_a_slow_first_one(self):
        from unittest.mock import patch

        from scanvault.vault import Vault

        class UnevenClient(SlowClient):
            def chat_json(self, system: str, user: str, schema=None, images=None) -> dict:
                # "Document 0" is in the excerpt of the first document only.
                self.delay = 0.45 if "Document 0" in user else 0.02
                return super().chat_json(system, user, schema, images)

        written, spy, _ = self._record_writes()
        with patch.object(Vault, "write_document", spy):
            report = ingest(self.config, client=UnevenClient(RESPONSE))

        self.assertEqual(report.count("ingested"), 6, report.results)
        names = [Path(str(path)).name for path in written if path]
        self.assertEqual(len(names), 6)
        self.assertNotEqual(
            names[0],
            "scan_0.pdf",
            "the slow document should not have held up the ones behind it",
        )

    def test_the_results_are_still_in_input_order(self):
        report = ingest(self.config, client=StubClient(RESPONSE))
        self.assertEqual(
            [result.source.name for result in report.results],
            [f"scan_{index}.pdf" for index in range(6)],
        )

    def test_a_crash_half_way_leaves_the_finished_documents_filed(self):
        class Exploding(StubClient):
            def __init__(self, response):
                super().__init__(response)
                self.seen = 0
                self._lock = threading.Lock()

            def chat_json(self, system: str, user: str, schema=None, images=None) -> dict:
                with self._lock:
                    self.seen += 1
                    if self.seen == 4:
                        raise RuntimeError("model fell over")
                return super().chat_json(system, user, schema, images)

        self.config.llm.fallback_to_heuristics = False
        report = ingest(self.config, client=Exploding(RESPONSE))
        filed = list((self.root / "vault").rglob("*.md"))
        self.assertGreaterEqual(len(filed), 4, "work done before the failure survives")
        self.assertEqual(report.count("ingested") + report.count("failed"), 6)


class TestOrganizeFlowsThrough(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.config = load_config(overrides={"vault_dir": str(self.root)})
        self.config.llm.workers = 4
        for index in range(4):
            make_text_pdf(self.root / "Inbox" / f"scan_{index}.pdf", [f"Document {index}"] + LINES)
        note = self.root / "Archive/Other/2024/2024-05-02 Old.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            '---\ntitle: "Old"\ndate: 2024-05-02\ncategory: "Other"\ntags:\n  - scan\n'
            'classifier: "llm"\n---\n\n# Old\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_adopting_and_relocating_both_happen(self):
        client = SlowClient(RESPONSE, delay=0.05)
        report = plan(self.config, client=client)
        organizer_apply(report, self.config, client=client)

        self.assertEqual(report.count("adopt"), 4)
        filed = sorted(p.name for p in (self.root / "Archive").rglob("*.md"))
        self.assertEqual(len(filed), 5, filed)
        self.assertEqual(list(self.root.rglob("*.ocr.pdf")), [])

    def test_the_slow_actions_overlap(self):
        client = SlowClient(RESPONSE, delay=0.2)
        report = plan(self.config, client=client)
        client.peak = 0
        started = time.monotonic()
        organizer_apply(report, self.config, client=client)
        self.assertGreater(client.peak, 1)
        self.assertLess(time.monotonic() - started, 4 * 0.2)
