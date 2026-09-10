"""Phase 1 + 2 for new scans: source folder in, Obsidian vault out."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .cache import ClassificationCache
from .parallel import Progress, pipeline as run_pipeline, resolve_workers
from .classify import DocumentMeta, classify, resolve_date
from .config import Config
from .extract import DOCUMENT_SUFFIXES, ExtractResult, OcrError, extract, is_image
from .llm import OllamaClient
from .state import State
from .tags import TagLedger, ledger_for, report_folded
from .util import sha256_file, slugify
from .vault import Vault

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = DOCUMENT_SUFFIXES


@dataclass
class ProcessResult:
    source: Path
    status: str  # ingested | duplicate | failed | skipped
    note: Path | None = None
    attachment: Path | None = None
    meta: DocumentMeta | None = None
    ocr_backend: str = ""
    error: str = ""


@dataclass
class Report:
    results: list[ProcessResult] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for result in self.results if result.status == status)

    def __len__(self) -> int:
        return len(self.results)

    @property
    def failures(self) -> list[ProcessResult]:
        return [result for result in self.results if result.status == "failed"]


def iter_documents(source: Path, recursive: bool = True) -> Iterator[Path]:
    if source.is_file():
        yield source
        return
    walker = source.rglob("*") if recursive else source.glob("*")
    for path in sorted(walker):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if any(part.startswith(".") for part in path.relative_to(source).parts):
            continue
        if path.name.endswith(".ocr.pdf"):
            continue
        yield path


def source_folder(path: Path, root: Path | None) -> str:
    """The document's folder relative to `root`, e.g. "Work receipts"."""
    if root is None:
        return ""
    try:
        relative = path.parent.resolve().relative_to(root.resolve())
    except ValueError:
        return ""
    return "" if relative == Path(".") else relative.as_posix()


def is_stable(path: Path, settle_seconds: float = 2.0) -> bool:
    """True when the file stopped growing - a scanner may still be writing it."""
    try:
        first = path.stat().st_size
        time.sleep(settle_seconds)
        return first == path.stat().st_size and first > 0
    except OSError:
        return False


@dataclass
class Prepared:
    """Everything decided about a document before anything is written.

    Splitting the read-and-think half from the write half is what lets the slow
    half run on several workers while the vault is still only ever written by
    one thread.
    """

    path: Path
    digest: str
    extracted: ExtractResult
    meta: DocumentMeta
    extra: dict[str, Any]
    result: ProcessResult | None = None  # set when there is nothing to file


def prepare_document(
    path: Path,
    config: Config,
    client: OllamaClient | None,
    state: State | None = None,
    bucket: str | None = None,
    source_root: Path | None = None,
    cache: ClassificationCache | None = None,
    ledger: TagLedger | None = None,
) -> Prepared:
    """Hash, OCR and classify one document. Touches nothing in the vault."""
    digest = sha256_file(path)
    if state is not None:
        known = state.get(digest)
        if known:
            log.info("skipping %s: already filed as %s", path.name, known.get("note"))
            return _nothing_to_file(
                path,
                digest,
                ProcessResult(path, "duplicate", note=Path(str(known.get("note") or ""))),
            )

    try:
        extracted = extract(path, config.ocr, work_dir=config.state_root / "work")
    except OcrError as exc:
        log.error("OCR failed for %s: %s", path.name, exc)
        return _nothing_to_file(path, digest, ProcessResult(path, "failed", error=str(exc)))
    except Exception as exc:  # pragma: no cover - unexpected backend crash
        log.exception("unexpected failure on %s", path.name)
        return _nothing_to_file(
            path, digest, ProcessResult(path, "failed", error=f"{type(exc).__name__}: {exc}")
        )

    meta = classify(extracted.text, config, client, source=path, cache=cache, ledger=ledger)
    resolve_date(meta, path, config, extracted.text)
    meta.para = bucket or config.vault.para.default_bucket
    folder = source_folder(path, source_root)
    if folder and config.vault.tag_source_folder:
        for part in folder.split("/"):
            tag = slugify(part)
            if tag and tag not in meta.tags:
                meta.tags.append(tag)
    extra = {
        "source_file": path.name,
        "source_folder": folder,
        "source_hash": digest,
        "ocr": extracted.backend,
        "pages": extracted.pages,
    }
    return Prepared(path, digest, extracted, meta, extra)


def _nothing_to_file(path: Path, digest: str, result: ProcessResult) -> Prepared:
    empty = ExtractResult("", False, "none", path, None)
    return Prepared(path, digest, empty, DocumentMeta("", ""), {}, result)


def file_document(
    prepared: Prepared,
    config: Config,
    vault: Vault,
    state: State | None = None,
    dry_run: bool = False,
) -> ProcessResult:
    """Write one prepared document into the vault. Single-threaded by design."""
    if prepared.result is not None:
        return prepared.result

    # Workers prepare everything before anything is filed, so two identical
    # documents can both pass the check in prepare_document. The index is only
    # written on this thread, so this is where a duplicate is really caught.
    if state is not None and prepared.digest:
        known = state.get(prepared.digest)
        if known:
            log.info(
                "skipping %s: same content as %s", prepared.path.name, known.get("note")
            )
            _discard_ocr_output(prepared, config)
            return ProcessResult(
                prepared.path, "duplicate", note=Path(str(known.get("note") or ""))
            )

    path, meta, extracted = prepared.path, prepared.meta, prepared.extracted
    written = vault.write_document(
        meta,
        extracted.text,
        pdf_path=extracted.pdf_path,
        source_path=path,
        extra=prepared.extra,
        dry_run=dry_run,
        original_path=path if is_image(path) else None,
    )
    if state is not None and not dry_run:
        state.record(
            prepared.digest,
            note=written.note_path.relative_to(vault.root).as_posix(),
            attachment=(
                written.attachment_path.relative_to(vault.root).as_posix()
                if written.attachment_path
                else None
            ),
            source=path.name,
            title=meta.title,
            category=meta.category,
        )
    return ProcessResult(
        path,
        "ingested",
        note=written.note_path,
        attachment=written.attachment_path,
        meta=meta,
        ocr_backend=extracted.backend,
    )


def _discard_ocr_output(prepared: Prepared, config: Config) -> None:
    """Drop the searchable PDF a worker made for a document we will not file."""
    produced = prepared.extracted.pdf_path
    if produced == prepared.path or not prepared.extracted.ocr_performed:
        return
    try:
        if produced.parent == config.state_root / "work":
            produced.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - best effort
        log.debug("could not remove %s: %s", produced, exc)


def process_file(
    path: Path,
    config: Config,
    vault: Vault,
    client: OllamaClient | None,
    state: State | None = None,
    dry_run: bool = False,
    bucket: str | None = None,
    source_root: Path | None = None,
    cache: ClassificationCache | None = None,
) -> ProcessResult:
    """OCR one PDF, classify it, and file it in the vault."""
    prepared = prepare_document(path, config, client, state, bucket, source_root, cache)
    return file_document(prepared, config, vault, state, dry_run)


def ingest(
    config: Config,
    paths: Iterable[Path] | None = None,
    client: OllamaClient | None = None,
    dry_run: bool = False,
    use_state: bool = True,
    cache: ClassificationCache | None = None,
    use_cache: bool = True,
    on_result: Callable[[ProcessResult], None] | None = None,
    ledger: TagLedger | None = None,
) -> Report:
    """Ingest every PDF under config.source_dir (or an explicit list of paths)."""
    if config.vault_dir is None:
        raise ValueError("vault_dir is required")
    vault = Vault(config)
    if not dry_run:
        vault.root.mkdir(parents=True, exist_ok=True)
        vault.scaffold()
        # A vault that only ever sees `ingest` should get the snippet too.
        vault.write_css_snippet()
    state = State(config.state_root) if use_state else None
    own_ledger = ledger is None
    if own_ledger:
        # Seeded from the vault, so a new document reuses the words already in
        # it instead of inventing a near-copy of each one.
        ledger = ledger_for(config, vault)
    own_cache = cache is None
    if own_cache:
        cache = ClassificationCache(config.state_root, config, enabled=use_cache)

    if paths is None:
        if config.source_dir is None:
            raise ValueError("source_dir is required")
        paths = iter_documents(config.source_dir)

    report = Report()
    paths = list(paths)
    workers = resolve_workers(config.llm.workers) if client is not None else 1
    progress = Progress(len(paths))

    def prepare(path: Path) -> Prepared:
        started = progress.start(path.name)
        prepared = prepare_document(
            path,
            config,
            client,
            state,
            source_root=config.source_dir,
            cache=cache,
            ledger=ledger,
        )
        progress.finish(path.name, started)
        return prepared

    def failed(path: Path, exc: Exception) -> Prepared:
        return _nothing_to_file(
            path, "", ProcessResult(path, "failed", error=f"{type(exc).__name__}: {exc}")
        )

    def file(path: Path, prepared: Prepared) -> ProcessResult:
        # Writing stays on the calling thread - unique filenames, the dedupe
        # index and the cache file are all shared - but it happens as soon as
        # that document is ready, not after every document has been read.
        result = file_document(prepared, config, vault, state, dry_run)
        if on_result is not None:
            # Report it now rather than at the end, so what you watch is the
            # run rather than a summary of it.
            on_result(result)
        return result

    report.results.extend(run_pipeline(paths, prepare, file, workers, on_error=failed))
    log.info("%s", progress.summary(workers))
    if state is not None and not dry_run:
        state.save()
    if own_cache and cache is not None:
        # Written even on a dry run: reusing it is the point.
        cache.save()
    if own_ledger and ledger is not None:
        report_folded(ledger)
        if not dry_run:
            ledger.save(config.state_root)
    return report


def watch(
    config: Config,
    client: OllamaClient | None = None,
    interval: float = 20.0,
    settle_seconds: float = 2.0,
    iterations: int | None = None,
    dry_run: bool = False,
) -> Report:
    """Poll the source folder forever (or `iterations` times) and ingest new scans."""
    if config.source_dir is None:
        raise ValueError("source_dir is required")
    total = Report()
    round_number = 0
    while iterations is None or round_number < iterations:
        round_number += 1
        pending = [
            p for p in iter_documents(config.source_dir) if is_stable(p, settle_seconds)
        ]
        if pending:
            total.results.extend(ingest(config, pending, client, dry_run=dry_run).results)
        if iterations is not None and round_number >= iterations:
            break
        time.sleep(interval)
    return total
