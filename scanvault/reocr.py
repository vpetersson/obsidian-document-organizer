"""Find the documents whose OCR came out as gibberish, and read them again.

`organize` OCRs the documents that have *no* text. This handles the ones that
have text which is not words: a scan read at the wrong resolution, a Swedish
letter read without the Swedish language pack, a page the engine took a run at
while it was still skewed. Those file perfectly happily - note, title, category
and all - and the first sign anything is wrong is that searching the vault for a
word which is plainly on the page finds nothing.

The plan scores every document in the vault with `scanvault.quality` and changes
nothing. `--apply` reads each candidate again through a series of increasingly
aggressive passes, scores what each one produced, and keeps the best - including
the text that was already there. Some scans really are illegible, and replacing
one unreadable text layer with a differently unreadable one helps nobody.

When the text does improve, the note is rewritten around it and, unless told
otherwise, reclassified: a title and a category derived from gibberish are
gibberish too, and fixing the text without fixing them leaves the document as
hard to find as it was.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .cache import ClassificationCache
from .classify import DocumentMeta, classify, resolve_date
from .config import Config, OcrConfig
from .extract import (
    OcrError,
    available_backend,
    extract,
    is_image,
    needs_password,
    page_count,
    pdf_text,
)
from .llm import OllamaClient
from .organizer import (
    Action,
    attachment_path,
    embedded_text,
    is_managed,
    note_documents,
    resolve_bucket,
    _rewrite_note,
    _write_action,
)
from .parallel import Progress, pipeline as run_pipeline, resolve_workers
from .quality import TextQuality, score_text
from .state import State
from .tags import TagLedger, ledger_for, report_folded
from .vault import Vault

log = logging.getLogger(__name__)

# What each pass does differently, in the reader's terms. `re-ocr -v` prints
# these, because "which of these should I be running" is otherwise guesswork.
PASS_DESCRIPTIONS = {
    "force": "read the page image again, ignoring the text layer already on it",
    "oversample": "the same at 400 dpi - small type read badly at 200 often reads cleanly",
    "clean": "the same, with unpaper straightening and de-speckling the page first",
}

# The resolution the escalating passes rasterise at. High enough to give the
# engine more to work with than a 200 dpi scan does, low enough that a page
# still takes seconds rather than minutes.
ESCALATED_DPI = 400


def pass_config(name: str, base: OcrConfig) -> OcrConfig:
    """The OCR settings for one re-OCR pass.

    Every pass forces OCR: the whole point is that the text layer already there
    is wrong, so trusting it - which is what `--skip-text` does - would make the
    pass a no-op.
    """
    forced = replace(base, force=True)
    if name == "force":
        return forced
    escalated = replace(
        forced,
        oversample=max(base.oversample, ESCALATED_DPI),
        rasterize_dpi=max(base.rasterize_dpi, ESCALATED_DPI),
    )
    if name == "oversample":
        return escalated
    if name == "clean":
        return replace(escalated, clean=True)
    raise ValueError(
        f"unknown re-ocr pass {name!r}; known passes: {', '.join(PASS_DESCRIPTIONS)}"
    )


@dataclass
class Attempt:
    """One reading of a document, and what it was worth."""

    label: str
    quality: TextQuality
    text: str = ""
    pdf_path: Path | None = None
    backend: str = ""
    pages: int | None = None
    error: str = ""

    def describe(self) -> str:
        if self.error:
            # ocrmypdf's advice for a missing tool runs to a dozen lines, which
            # is a help page rather than a report line. The log has all of it.
            first = self.error.strip().splitlines()[0] if self.error.strip() else "failed"
            return f"{self.label}: did not run ({first[:120]})"
        return f"{self.label}: {self.quality.score:.2f}"


@dataclass
class Candidate:
    """A document in the vault, its text quality, and what happened to it."""

    document: Path
    quality: TextQuality
    note: Path | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)
    # plan: reocr | good | thin | skipped
    # apply: improved | unchanged | failed
    kind: str = "reocr"
    reason: str = ""
    after: TextQuality | None = None
    winner: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    meta: DocumentMeta | None = None
    text: str = ""
    reclassified: bool = False
    target: Path | None = None
    error: str = ""
    # Scratch directory holding this document's OCR output until it is consumed.
    stage: Path | None = None

    @property
    def improvement(self) -> float:
        return (self.after.score - self.quality.score) if self.after else 0.0

    def describe(self, root: Path) -> str:
        """One line: what happens to this document, how it scored, and why."""

        def rel(path: Path | None) -> str:
            if path is None:
                return "-"
            try:
                return path.relative_to(root).as_posix()
            except ValueError:
                return str(path)

        where = rel(self.document)
        if self.kind == "failed":
            return f"FAILED   {where} ({self.error})"
        if self.kind == "improved":
            detail = f"{self.quality.score:.2f} -> {self.after.score:.2f} via {self.winner}"
            if self.reclassified:
                detail += ", reclassified"
            if self.target is not None and self.target != self.note:
                detail += f", now {rel(self.target)}"
            return f"re-OCR   {where} ({detail})"
        if self.kind == "unchanged":
            best = max((a.quality.score for a in self.attempts if not a.error), default=0.0)
            return (
                f"keep     {where} ({self.quality.score:.2f}, best re-read {best:.2f}"
                " - the text already there was kept)"
            )
        marker = {"reocr": "re-OCR", "skipped": "skip", "deferred": "skip"}.get(self.kind, "keep")
        why = self.reason or self.quality.explain()
        return (
            f"{marker:8s} {where} "
            f"[{self.quality.score:.2f} {self.quality.verdict}] ({why})"
        )


@dataclass
class ReocrReport:
    documents: list[Candidate] = field(default_factory=list)
    ledger: TagLedger | None = None

    def count(self, kind: str) -> int:
        return sum(1 for candidate in self.documents if candidate.kind == kind)

    @property
    def todo(self) -> list[Candidate]:
        return [candidate for candidate in self.documents if candidate.kind == "reocr"]

    def summary(self) -> str:
        """What the whole vault scored - counted by verdict, not by what was done.

        `--all` turns readable documents into candidates, and the summary still
        has to say how many of them were readable to begin with.
        """
        order = ("gibberish", "poor", "empty", "good", "thin")
        counts = Counter(candidate.quality.verdict for candidate in self.documents)
        labels = {"thin": "too short to judge"}
        return f"{len(self.documents)} documents scored: " + ", ".join(
            f"{counts.get(name, 0)} {labels.get(name, name)}" for name in order
        )


def current_reading(
    vault: Vault, document: Path, note: Path | None, body: str
) -> tuple[str, int | None]:
    """The text a document yields right now, and its page count. Runs no OCR.

    An image has no text of its own; what a note embeds for it is the OCR
    output from when it was filed, which is exactly the text being judged. It
    has no page count either, so an image is scored on its words alone.
    """
    if document.suffix.lower() == ".pdf":
        return pdf_text(document), page_count(document)
    return (embedded_text(body) if note is not None else ""), None


def plan(
    config: Config,
    threshold: float | None = None,
    include_all: bool = False,
    include_unmanaged: bool = False,
    include_loose: bool = True,
) -> ReocrReport:
    """Score every document in the vault. Reads; changes nothing."""
    vault = Vault(config)
    settings = config.quality
    if threshold is not None:
        settings = replace(settings, threshold=threshold)
    report = ReocrReport()
    seen: set[Path] = set()

    try:
        backend = available_backend(config.ocr)
    except OcrError as exc:
        log.warning("%s", exc)
        backend = "none"
    if backend == "none":
        log.warning("no OCR backend is installed, so nothing can be read again")

    for note in vault.iter_notes():
        try:
            frontmatter, body = vault.read_note(note)
        except OSError as exc:
            log.warning("could not read %s: %s", note, exc)
            continue
        if frontmatter.get("para_index"):
            continue
        if not include_unmanaged and not is_managed(frontmatter):
            continue
        for document in note_documents(vault, note, frontmatter, body):
            key = document.resolve()
            if key in seen:
                continue
            seen.add(key)
            text, pages = current_reading(vault, document, note, body)
            report.documents.append(
                _judge(
                    document,
                    score_text(text, settings, pages),
                    backend,
                    note=note,
                    frontmatter=frontmatter,
                    include_all=include_all,
                )
            )

    if include_loose:
        for document in vault.iter_loose_documents():
            key = document.resolve()
            if key in seen or is_image(document):
                # A loose image has no text layer to judge and no note to write
                # the result into; adopting it is `organize`'s job.
                continue
            seen.add(key)
            report.documents.append(
                _judge(
                    document,
                    score_text(pdf_text(document), settings, page_count(document)),
                    backend,
                    include_all=include_all,
                )
            )

    report.documents.sort(key=lambda candidate: (candidate.quality.score, str(candidate.document)))
    log.info("%s", report.summary())
    return report


def _judge(
    document: Path,
    quality: TextQuality,
    backend: str,
    note: Path | None = None,
    frontmatter: dict[str, Any] | None = None,
    include_all: bool = False,
) -> Candidate:
    """Turn a score into a decision about one document."""
    candidate = Candidate(document, quality, note, dict(frontmatter or {}))
    if quality.readable and not include_all:
        # "good" reads fine; "thin" is too short to have an opinion about, and
        # re-OCR'ing every two-line receipt in a vault on a coin flip is worse
        # than leaving them.
        candidate.kind = "good" if quality.verdict == "good" else "thin"
        return candidate
    # --all asks for everything, but a document that reads fine is still
    # reported for what it is rather than being silently called bad.
    candidate.reason = (
        f"{quality.verdict} already, re-read because --all was given"
        if quality.readable
        else quality.explain()
    )
    if backend == "none":
        candidate.kind = "skipped"
        candidate.reason = "no OCR backend installed"
    elif document.suffix.lower() == ".pdf" and needs_password(document):
        candidate.kind = "skipped"
        candidate.reason = "password-protected (qpdf --decrypt to fix)"
    return candidate


def read_again(
    document: Path,
    config: Config,
    work_root: Path,
    before: TextQuality,
) -> tuple[list[Attempt], Attempt | None, Path | None]:
    """Run the escalating passes over one document and pick the best reading.

    Returns every attempt (so a report can say what was tried), the winner if
    one beat the text already there, and the scratch directory the winner's PDF
    is sitting in - the caller owns it and must remove it.
    """
    work_root.mkdir(parents=True, exist_ok=True)
    # mkdtemp rather than a name built from the document, because two documents
    # in different folders can share a stem and these run on several workers.
    stage = Path(tempfile.mkdtemp(dir=work_root, prefix="reocr-"))
    attempts: list[Attempt] = []
    best: Attempt | None = None

    try:
        for name in config.quality.passes:
            try:
                settings = pass_config(name, config.ocr)
            except ValueError as exc:
                log.warning("%s", exc)
                attempts.append(Attempt(name, score_text("", config.quality), error=str(exc)))
                continue
            try:
                result = extract(document, settings, work_dir=stage / name)
            except (OcrError, subprocess.TimeoutExpired, OSError) as exc:
                # A pass that cannot run is not a failure of the document:
                # `clean` needs unpaper, which plenty of machines do not have.
                log.info("%s: the %s pass did not run (%s)", document.name, name, exc)
                attempts.append(Attempt(name, score_text("", config.quality), error=str(exc)))
                continue
            quality = score_text(result.text, config.quality, result.pages)
            attempt = Attempt(
                name, quality, result.text, result.pdf_path, result.backend, result.pages
            )
            attempts.append(attempt)
            log.debug("%s: %s scored %.2f", document.name, name, quality.score)
            if best is None or quality.score > best.quality.score:
                best = attempt
            if quality.score >= config.quality.good_enough:
                # Readable is the goal, not the highest attainable score; the
                # remaining passes cost minutes a page and cannot beat "fine".
                break
    except BaseException:
        # Whatever went wrong, the scratch directory lives inside the user's
        # vault. Nothing of ours is left behind in it.
        shutil.rmtree(stage, ignore_errors=True)
        raise

    if best is None or best.quality.score < before.score + config.quality.min_gain:
        shutil.rmtree(stage, ignore_errors=True)
        return attempts, None, None
    return attempts, best, stage


def apply(
    report: ReocrReport,
    config: Config,
    client: OllamaClient | None = None,
    reclassify: bool = True,
    use_state: bool = True,
    cache: ClassificationCache | None = None,
    use_cache: bool = True,
    ledger: TagLedger | None = None,
) -> ReocrReport:
    """Read every candidate again and keep what turned out better."""
    vault = Vault(config)
    ledger = ledger or report.ledger or ledger_for(config, vault)
    report.ledger = ledger
    state = State(config.state_root) if use_state else None
    own_cache = cache is None
    if own_cache:
        # --reclassify is implied here: the whole point is that the text the
        # last answer was based on was wrong, so that answer must not be reused.
        cache = ClassificationCache(config.state_root, config, enabled=use_cache, read=False)
    work_root = config.state_root / "work"

    todo = report.todo
    log.info("reading %d documents again", len(todo))
    workers = resolve_workers(config.llm.workers)
    progress = Progress(len(todo), verb="re-OCR'ing")

    def read(candidate: Candidate) -> Candidate:
        started = progress.start(candidate.document.name)
        attempts, best, stage = read_again(
            candidate.document, config, work_root, candidate.quality
        )
        candidate.attempts = attempts
        if best is None:
            candidate.kind = "unchanged"
            failures = [attempt for attempt in attempts if attempt.error]
            if len(failures) == len(attempts) and attempts:
                candidate.kind = "failed"
                candidate.error = failures[0].error
            progress.finish(candidate.document.name, started)
            return candidate
        candidate.kind = "improved"
        candidate.after = best.quality
        candidate.winner = best.label
        candidate.text = best.text
        candidate.stage = stage
        _decide_metadata(candidate, best, vault, config, client, reclassify, cache, ledger)
        progress.finish(candidate.document.name, started)
        return candidate

    def failed(candidate: Candidate, exc: Exception) -> Candidate:
        log.exception("failed to re-OCR %s", candidate.document)
        candidate.kind = "failed"
        candidate.error = f"{type(exc).__name__}: {exc}"
        return candidate

    def write(candidate: Candidate, prepared: Candidate) -> Candidate:
        _commit(prepared, vault, config, state, cache, ledger)
        return prepared

    run_pipeline(todo, read, write, workers, on_error=failed)
    if todo:
        log.info("%s", progress.summary(workers))

    if state is not None:
        state.save()
    if own_cache and cache is not None:
        cache.save()
    report_folded(ledger)
    ledger.save(config.state_root)
    return report


def _decide_metadata(
    candidate: Candidate,
    best: Attempt,
    vault: Vault,
    config: Config,
    client: OllamaClient | None,
    reclassify: bool,
    cache: ClassificationCache | None,
    ledger: TagLedger | None,
) -> None:
    """Work out what the note should say now. Runs on a worker; writes nothing."""
    candidate.frontmatter["ocr"] = best.backend
    candidate.frontmatter["ocr_quality"] = best.quality.score
    if best.pages:
        candidate.frontmatter["pages"] = best.pages
    if candidate.note is None:
        return

    if reclassify:
        # With no client this is the keyword heuristic rather than the model,
        # which is what `organize --no-llm` does too - a title derived from
        # readable text beats one derived from gibberish either way.
        candidate.meta = classify(
            best.text, config, client, source=candidate.note, cache=cache, ledger=ledger
        )
        resolve_date(candidate.meta, candidate.document, config, best.text)
        candidate.reclassified = True
    else:
        candidate.meta = vault.meta_from_note(candidate.frontmatter, candidate.note.stem)
    candidate.meta.para = resolve_bucket(
        vault, candidate.note, candidate.frontmatter, config
    )


def _commit(
    candidate: Candidate,
    vault: Vault,
    config: Config,
    state: State | None,
    cache: ClassificationCache | None,
    ledger: TagLedger | None,
) -> None:
    """The half that touches the vault. One thread at a time, by construction."""
    if candidate.kind != "improved":
        return
    try:
        _replace_document(candidate)
        if candidate.note is None or candidate.meta is None:
            return
        action = Action(
            "ocr" if candidate.reclassified else "rewrite",
            candidate.note,
            None,
            candidate.reason,
            candidate.meta,
            candidate.frontmatter,
            candidate.text,
        )
        if not candidate.reclassified:
            # Nothing about where this document belongs has changed - only the
            # text did. Re-filing it would be `organize`'s decision to make, on
            # `organize`'s templates, and making it here would move a note
            # someone deliberately put somewhere.
            _rewrite_note(
                vault,
                action,
                attachment_path(vault, candidate.frontmatter),
                candidate.note,
                ledger,
            )
            candidate.target = candidate.note
            return
        _write_action(action, None, vault, config, state, cache, ledger)
        if action.kind == "failed":
            candidate.kind, candidate.error = "failed", action.error
        else:
            candidate.target = action.target or candidate.note
    except Exception as exc:  # keep going through the rest of the vault
        log.exception("failed to write back %s", candidate.document)
        candidate.kind, candidate.error = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        if candidate.stage is not None:
            shutil.rmtree(candidate.stage, ignore_errors=True)
            candidate.stage = None


def _replace_document(candidate: Candidate) -> None:
    """Put the newly searchable PDF where the old one was.

    An image is left alone: the note links to it by the name it has, and the
    searchable PDF made from it is a reading of the image rather than a
    replacement for it. Its text still reaches the note.
    """
    best = next(
        (attempt for attempt in candidate.attempts if attempt.label == candidate.winner), None
    )
    if best is None or best.pdf_path is None or not best.pdf_path.exists():
        return
    if candidate.document.suffix.lower() != ".pdf":
        return
    shutil.move(str(best.pdf_path), candidate.document)
