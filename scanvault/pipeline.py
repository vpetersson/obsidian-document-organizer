"""Phase 1 + 2 for new scans: source folder in, Obsidian vault out."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .classify import DocumentMeta, classify
from .config import Config
from .extract import OcrError, extract
from .llm import OllamaClient
from .state import State
from .util import sha256_file
from .vault import Vault

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".pdf"}


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


def iter_pdfs(source: Path, recursive: bool = True) -> Iterator[Path]:
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


def is_stable(path: Path, settle_seconds: float = 2.0) -> bool:
    """True when the file stopped growing - a scanner may still be writing it."""
    try:
        first = path.stat().st_size
        time.sleep(settle_seconds)
        return first == path.stat().st_size and first > 0
    except OSError:
        return False


def process_file(
    path: Path,
    config: Config,
    vault: Vault,
    client: OllamaClient | None,
    state: State | None = None,
    dry_run: bool = False,
    bucket: str | None = None,
) -> ProcessResult:
    """OCR one PDF, classify it, and file it in the vault.

    `bucket` pins the PARA destination; without it a scan lands in the archive.
    """
    digest = sha256_file(path)
    if state is not None:
        known = state.get(digest)
        if known:
            log.info("skipping %s: already filed as %s", path.name, known.get("note"))
            return ProcessResult(path, "duplicate", note=Path(str(known.get("note") or "")))

    work_dir = config.state_root / "work"
    try:
        extracted = extract(path, config.ocr, work_dir=work_dir)
    except OcrError as exc:
        log.error("OCR failed for %s: %s", path.name, exc)
        return ProcessResult(path, "failed", error=str(exc))
    except Exception as exc:  # pragma: no cover - unexpected backend crash
        log.exception("unexpected failure on %s", path.name)
        return ProcessResult(path, "failed", error=f"{type(exc).__name__}: {exc}")

    meta = classify(extracted.text, config, client, source=path)
    meta.para = bucket or config.vault.para.default_bucket
    extra = {
        "source_file": path.name,
        "source_hash": digest,
        "ocr": extracted.backend,
        "pages": extracted.pages,
    }
    written = vault.write_document(
        meta,
        extracted.text,
        pdf_path=extracted.pdf_path,
        source_path=path,
        extra=extra,
        dry_run=dry_run,
    )
    if state is not None and not dry_run:
        state.record(
            digest,
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


def ingest(
    config: Config,
    paths: Iterable[Path] | None = None,
    client: OllamaClient | None = None,
    dry_run: bool = False,
    use_state: bool = True,
) -> Report:
    """Ingest every PDF under config.source_dir (or an explicit list of paths)."""
    if config.vault_dir is None:
        raise ValueError("vault_dir is required")
    vault = Vault(config)
    if not dry_run:
        vault.root.mkdir(parents=True, exist_ok=True)
        vault.scaffold()
    state = State(config.state_root) if use_state else None

    if paths is None:
        if config.source_dir is None:
            raise ValueError("source_dir is required")
        paths = iter_pdfs(config.source_dir)

    report = Report()
    for path in paths:
        log.info("processing %s", path)
        report.results.append(process_file(path, config, vault, client, state, dry_run))
    if state is not None and not dry_run:
        state.save()
    return report


def watch(
    config: Config,
    client: OllamaClient | None = None,
    interval: float = 20.0,
    settle_seconds: float = 2.0,
    iterations: int | None = None,
) -> Report:
    """Poll the source folder forever (or `iterations` times) and ingest new scans."""
    if config.source_dir is None:
        raise ValueError("source_dir is required")
    total = Report()
    round_number = 0
    while iterations is None or round_number < iterations:
        round_number += 1
        pending = [p for p in iter_pdfs(config.source_dir) if is_stable(p, settle_seconds)]
        if pending:
            total.results.extend(ingest(config, pending, client).results)
        if iterations is not None and round_number >= iterations:
            break
        time.sleep(interval)
    return total
