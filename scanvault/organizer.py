"""Organizer: bring an existing Obsidian vault in line with the current layout.

Two jobs:
  * adopt loose PDFs that live in the vault but have no note yet;
  * re-read existing notes, (re)classify them and move them to where the
    configured templates say they belong.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cache import ClassificationCache
from .parallel import Progress, parallel_map, pipeline as run_pipeline, resolve_workers
from .classify import DocumentMeta, classify, resolve_date
from .config import BUCKETS, Config
from .extract import OcrError, available_backend, extract, is_image, needs_password, pdf_text
from .llm import OllamaClient
from .pipeline import (
    Prepared,
    ProcessResult,
    _nothing_to_file,
    file_document,
    prepare_document,
)
from .state import State
from .util import sha256_file, unique_path
from .vault import Vault

log = logging.getLogger(__name__)

# The plain heading form, and the HTML one, both hold a bare fenced block.
TEXT_BLOCK_RE = re.compile(
    r"^(?:## Extracted text|<summary>Extracted text</summary>)\s*\n+```(?:text)?\n(.*?)\n```",
    re.DOTALL | re.MULTILINE,
)
# The callout form quotes every line, fences included.
CALLOUT_BLOCK_RE = re.compile(
    r"^> \[!\w+\][-+]? Extracted text\s*\n> ```(?:text)?\n(.*?)\n> ```",
    re.DOTALL | re.MULTILINE,
)


def text_style(body: str) -> str | None:
    """Which shape a note's extracted-text section was written in."""
    if CALLOUT_BLOCK_RE.search(body):
        return "callout"
    if "<summary>Extracted text</summary>" in body:
        return "details"
    if TEXT_BLOCK_RE.search(body):
        return "plain"
    return None


def embedded_text(body: str) -> str:
    """The OCR text a note carries, whichever way it was written."""
    match = CALLOUT_BLOCK_RE.search(body)
    if match:
        return "\n".join(
            line[2:] if line.startswith("> ") else line.lstrip(">")
            for line in match.group(1).splitlines()
        )
    match = TEXT_BLOCK_RE.search(body)
    return match.group(1) if match else ""
REQUIRED_KEYS = ("title", "category", "tags")


@dataclass
class Action:
    kind: str  # adopt | relocate | rewrite | noop | failed
    path: Path
    target: Path | None = None
    reason: str = ""
    meta: DocumentMeta | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body_text: str = ""
    error: str = ""
    # Set when the note's PDF has no text layer and must be OCR'd first.
    needs_ocr: bool = False
    classify_after: bool = False

    def describe(self, root: Path) -> str:
        def rel(p: Path | None) -> str:
            if p is None:
                return "-"
            try:
                return p.relative_to(root).as_posix()
            except ValueError:
                return str(p)

        moved = self.target is not None and self.target != self.path
        if moved and self.kind in ("relocate", "adopt", "ocr"):
            return f"{self.kind}: {rel(self.path)} -> {rel(self.target)} ({self.reason})"
        return f"{self.kind}: {rel(self.path)} ({self.reason})"


@dataclass
class OrganizeReport:
    actions: list[Action] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for action in self.actions if action.kind == kind)


# `![[Scan Page 196.jpg]]` is markup naming a file, not a word of the document.
# Reading it as text is how a wikilink ends up as a note's title.
LINK_MARKUP_RE = re.compile(r"!?\[\[[^\]]*\]\]|!?\[[^\]]*\]\([^)]*\)")


def strip_markup(body: str) -> str:
    """A note's body with its headings and links removed.

    What is left is prose the note actually carries. A note that is nothing but
    embedded scans has none, and saying so truthfully sends it to OCR instead of
    classifying it on the spelling of its own links.
    """
    without_headings = re.sub(r"^#.*$", "", body, flags=re.MULTILINE)
    return LINK_MARKUP_RE.sub(" ", without_headings).strip()


def note_documents(
    vault: Vault, note: Path, frontmatter: dict[str, Any], body: str
) -> list[Path]:
    """The documents a note points at: its attachment, else whatever it embeds.

    A hand-written note has no `attachment:` key - it embeds its scans with
    `![[...]]`, and those are just as much the note's documents.
    """
    attachment = attachment_path(vault, frontmatter)
    if attachment is not None:
        return [attachment]
    return vault.linked_documents(note, body)


def note_text(vault: Vault, note: Path, frontmatter: dict[str, Any], body: str, config: Config) -> str:
    """Best text already available for a note - never runs OCR.

    Planning stays read-only and cheap; OCR is a separate, reported action.
    """
    extracted = [
        pdf_text(document)
        for document in note_documents(vault, note, frontmatter, body)
        if document.suffix.lower() == ".pdf"
    ]
    text = "\n\n".join(part for part in extracted if part)
    if len(text) >= config.ocr.min_text_chars:
        return text
    embedded = embedded_text(body)
    if embedded:
        return embedded
    return strip_markup(body)


def attachment_path(vault: Vault, frontmatter: dict[str, Any]) -> Path | None:
    """The note's attachment, if it is recorded and still on disk."""
    attachment = frontmatter.get("attachment")
    if not isinstance(attachment, str):
        return None
    path = vault.root / attachment
    return path if path.is_file() else None


def documents_need_ocr(
    vault: Vault, note: Path, frontmatter: dict[str, Any], body: str, config: Config
) -> bool:
    """True when nothing the note points at can be read without OCR.

    The bar here is "is it searchable", not `min_text_chars` - a short receipt
    is a legitimately tiny but perfectly searchable document. An embedded image
    counts: a note holding two scanned pages carries no text at all until they
    are read.
    """
    documents = note_documents(vault, note, frontmatter, body)
    if not documents:
        return False
    for document in documents:
        if document.suffix.lower() != ".pdf":
            return True  # an image: there is nothing to extract without OCR
        if len(pdf_text(document)) < config.ocr.searchable_min_chars:
            return True
    return False


def resolve_bucket(vault: Vault, note: Path, frontmatter: dict[str, Any], config: Config) -> str:
    """Where a note belongs in PARA: its frontmatter wins, then its location.

    Without the location fallback, moving a note into `1 Projects/` by hand
    would be undone by the next organize run.
    """
    declared = frontmatter.get("para")
    if isinstance(declared, str) and declared in BUCKETS:
        return declared
    return vault.bucket_from_path(note) or config.vault.para.default_bucket


# Frontmatter keys scanvault writes; their presence marks a note as ours.
MANAGED_KEYS = ("scanvault_version", "source_hash", "classifier", "attachment")


def is_managed(frontmatter: dict[str, Any]) -> bool:
    """True for notes scanvault wrote, so a Second Brain vault's own notes
    (hand-written project and area notes) are never moved around."""
    return any(frontmatter.get(key) for key in MANAGED_KEYS)


def needs_classification(frontmatter: dict[str, Any]) -> bool:
    if any(not frontmatter.get(key) for key in REQUIRED_KEYS):
        return True
    return str(frontmatter.get("classifier") or "") == "heuristic"


def plan(
    config: Config,
    client: OllamaClient | None = None,
    reclassify: bool = False,
    adopt: bool = True,
    include_unmanaged: bool = False,
    ocr: bool = True,
    cache: ClassificationCache | None = None,
    use_cache: bool = True,
) -> OrganizeReport:
    """Work out what the vault needs, without touching anything."""
    vault = Vault(config)
    report = OrganizeReport()
    own_cache = cache is None
    if own_cache:
        # --reclassify means ask again, so the cache stops answering for this run.
        cache = ClassificationCache(
            config.state_root, config, enabled=use_cache, read=not reclassify
        )
    backend = "none"
    if ocr:
        try:
            backend = available_backend(config.ocr)
        except OcrError as exc:
            log.warning("%s", exc)

    notes = list(vault.iter_notes())
    log.info("scanning %d notes", len(notes))

    # Two passes: read every note and decide what it needs, which is cheap, then
    # send the ones that need the model out to the workers. Actions are appended
    # in note order either way, so the output does not depend on who finished
    # first.
    pending: list[tuple[Path, dict[str, Any], str, bool]] = []
    for note in notes:
        try:
            frontmatter, body = vault.read_note(note)
        except OSError as exc:
            report.actions.append(Action("failed", note, error=str(exc)))
            continue

        if frontmatter.get("para_index"):
            # Scaffolding, not a document - counting these as "already filed"
            # made an empty vault look like it held four documents.
            report.actions.append(Action("index", note, note, "PARA index note"))
            continue
        if not include_unmanaged and not is_managed(frontmatter):
            report.actions.append(Action("skipped", note, note, "not a scanvault note"))
            continue

        should_classify = reclassify or needs_classification(frontmatter)

        if ocr and documents_need_ocr(vault, note, frontmatter, body, config):
            documents = note_documents(vault, note, frontmatter, body)
            attachment = documents[0] if documents else None
            images = [path for path in documents if path.suffix.lower() != ".pdf"]
            if images:
                names = ", ".join(path.name for path in images)
                reason = f"embedded image{'s' if len(images) > 1 else ''} to read ({names})"
            else:
                reason = "attachment has no text layer"
            if attachment is not None and needs_password(attachment):
                reason = "attachment is password-protected (qpdf --decrypt to fix)"
            elif backend == "none":
                reason += " (no OCR backend installed)"
            action = Action("ocr", note, None, reason, None, frontmatter, "")
            action.needs_ocr = True
            action.classify_after = should_classify or not frontmatter.get("category")
            report.actions.append(action)
            continue

        pending.append((note, frontmatter, body, should_classify))

    to_classify = [entry for entry in pending if entry[3]]
    workers = resolve_workers(config.llm.workers) if client is not None else 1
    if to_classify:
        log.info(
            "classifying %d notes on %d worker%s",
            len(to_classify),
            workers,
            "" if workers == 1 else "s",
        )
    progress = Progress(len(to_classify))

    def classify_note(entry: tuple[Path, dict[str, Any], str, bool]) -> tuple[str, DocumentMeta]:
        note, frontmatter, body, _ = entry
        progress.start(note.name)
        text = note_text(vault, note, frontmatter, body, config)
        meta = classify(text, config, client, source=note, cache=cache)
        resolve_date(meta, attachment_path(vault, frontmatter) or note, config, text)
        return text, meta

    classified_by_note = dict(
        zip(
            (entry[0] for entry in to_classify),
            parallel_map(classify_note, to_classify, workers),
        )
    )

    for note, frontmatter, body, should_classify in pending:
        if should_classify:
            text, meta = classified_by_note[note]
            reason = "reclassified" if reclassify else "incomplete metadata"
        else:
            text = ""
            meta = vault.meta_from_note(frontmatter, note.stem)
            reason = "layout drift"
        meta.para = resolve_bucket(vault, note, frontmatter, config)

        target = vault.note_path(meta)
        if target.resolve() == note.resolve():
            if should_classify:
                report.actions.append(
                    Action("rewrite", note, note, reason, meta, frontmatter, text)
                )
                continue
            style = text_style(body)
            if style is not None and style != config.vault.extracted_text_style:
                # Only the presentation is out of date, so keep the text and
                # rewrite the note around it.
                report.actions.append(
                    Action(
                        "rewrite",
                        note,
                        note,
                        f"extracted text is {style}, not {config.vault.extracted_text_style}",
                        meta,
                        frontmatter,
                        embedded_text(body),
                    )
                )
                continue
            report.actions.append(Action("noop", note, note, "already filed", meta))
            continue
        report.actions.append(Action("relocate", note, target, reason, meta, frontmatter, text))

    classified = len(to_classify)

    if adopt:
        known_hashes = State(config.state_root).documents
        if classified:
            log.info(
                "classified %d notes; looking for documents no note points at", classified
            )
        for index, pdf in enumerate(vault.iter_loose_documents(), start=1):
            if index % 50 == 0:
                # A vault of several hundred PDFs takes a while to hash and
                # probe; say something rather than looking hung.
                log.info("scanned %d documents...", index)
            filed = known_hashes.get(sha256_file(pdf))
            if filed:
                # Same bytes as a document already in the vault: a second copy
                # would be noise, so report it and leave the file where it is.
                report.actions.append(
                    Action("duplicate", pdf, None, f"same content as {filed.get('note')}")
                )
                continue
            destination = vault.para_folder(
                vault.bucket_from_path(pdf) or config.vault.para.default_bucket
            )
            action = Action("adopt", pdf, None, "")
            if is_image(pdf):
                action.needs_ocr = True
                kind = pdf.suffix.lstrip(".").lower()
                action.reason = (
                    f"{kind} image; will be converted to a searchable PDF, classified "
                    f'and filed under "{destination}"'
                )
                if backend == "none":
                    action.reason = (
                        f"{kind} image; NO OCR BACKEND INSTALLED, so it cannot be read"
                    )
                report.actions.append(action)
                continue
            if needs_password(pdf):
                action.reason = (
                    "password-protected PDF; neither text extraction nor OCR can read "
                    "it until the password is removed (qpdf --decrypt)"
                )
                report.actions.append(action)
                continue
            if ocr and len(pdf_text(pdf)) < config.ocr.searchable_min_chars:
                action.needs_ocr = True
                action.reason = f'image-only PDF; will OCR, classify and file under "{destination}"'
                if backend == "none":
                    action.reason = (
                        f'image-only PDF; NO OCR BACKEND INSTALLED, would file under "{destination}" '
                        "with no text"
                    )
            else:
                action.reason = f'unfiled PDF; will classify and file under "{destination}"'
            report.actions.append(action)

    if own_cache and cache is not None:
        # Saved even for a dry run, so `--apply` reuses these answers.
        cache.save()
    if cache is not None and (cache.hits or cache.misses):
        log.info("classifications: %s", cache.summary())
    return report


def _move_attachment(vault: Vault, frontmatter: dict[str, Any], meta: DocumentMeta) -> Path | None:
    attachment = frontmatter.get("attachment")
    if not isinstance(attachment, str):
        return None
    current = vault.root / attachment
    if not current.is_file():
        log.warning("attachment %s is missing; leaving the link as-is", attachment)
        return None
    target = vault.attachment_path(meta, suffix=current.suffix)
    if target.resolve() == current.resolve():
        return current
    target = unique_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(current), target)
    return target


def _run_ocr(
    vault: Vault,
    action: Action,
    config: Config,
    client: OllamaClient | None,
    cache: ClassificationCache | None = None,
) -> None:
    """Read everything the note points at, and refresh its metadata from it.

    Only touches that note's own documents, so it is safe on a worker; the note
    is moved and rewritten later.
    """
    _, body = vault.read_note(action.path)
    documents = note_documents(vault, action.path, action.frontmatter, body)
    if not documents:
        raise OcrError("the attachment recorded in this note is missing")

    texts: list[str] = []
    backend = ""
    pages = 0
    for document in documents:
        result = extract(document, config.ocr, work_dir=config.state_root / "work")
        if result.pdf_path != document and result.pdf_path.exists():
            if document.suffix.lower() == ".pdf":
                # Keep the searchable PDF: that is the point of OCR'ing an old
                # vault. An image stays an image - the note links to it by name,
                # and replacing it would break the link the user wrote.
                shutil.move(str(result.pdf_path), document)
            else:
                result.pdf_path.unlink(missing_ok=True)
        if result.text.strip():
            texts.append(result.text.strip())
        backend = backend or result.backend
        pages += result.pages or 0
    text = "\n\n".join(texts)

    if action.classify_after:
        action.meta = classify(text, config, client, source=action.path, cache=cache)
        resolve_date(action.meta, documents[0], config, text)
    else:
        action.meta = vault.meta_from_note(action.frontmatter, action.path.stem)
    action.meta.para = resolve_bucket(vault, action.path, action.frontmatter, config)
    action.body_text = text
    action.frontmatter["ocr"] = backend
    if pages:
        action.frontmatter["pages"] = pages


def _rewrite_note(
    vault: Vault, action: Action, attachment: Path | None, destination: Path
) -> None:
    meta = action.meta
    assert meta is not None
    preserved = {
        key: action.frontmatter[key]
        for key in ("source_file", "source_hash", "ocr", "pages", "created")
        if key in action.frontmatter
    }
    _, body = vault.read_note(action.path)
    text = action.body_text
    if not text and vault.config.vault.include_text:
        text = embedded_text(body)
    # Scans the note embedded itself, rather than through `attachment:`. The
    # body is about to be replaced, so they have to be carried across.
    embeds = [] if attachment is not None else vault.linked_documents(action.path, body)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        vault.render_note(meta, text, attachment, extra=preserved, embeds=embeds),
        encoding="utf-8",
    )


def apply(
    report: OrganizeReport,
    config: Config,
    client: OllamaClient | None = None,
    use_state: bool = True,
    cache: ClassificationCache | None = None,
    use_cache: bool = True,
) -> OrganizeReport:
    """Execute a plan. Returns the same report with kinds updated to what happened."""
    vault = Vault(config)
    state = State(config.state_root) if use_state else None
    own_cache = cache is None
    if own_cache:
        cache = ClassificationCache(config.state_root, config, enabled=use_cache)
    workers = resolve_workers(config.llm.workers) if client is not None else 1

    todo = [
        action
        for action in report.actions
        if action.kind not in ("noop", "index", "skipped", "duplicate", "failed")
    ]
    log.info("applying %d changes", len(todo))
    progress = Progress(len(todo), verb="reading")

    # Adopting a document and OCR'ing one both mean OCR plus a model call;
    # relocating and rewriting are file moves. Only the first kind is worth
    # sending to the workers, and each of those goes all the way through -
    # read, classify, written - without waiting for the rest.
    slow = [action for action in todo if action.kind in ("adopt", "ocr")]
    fast = [action for action in todo if action not in slow]

    def read(action: Action) -> Prepared | None:
        started = progress.start(f"{action.kind} {action.path.name}")
        if action.kind == "adopt":
            prepared = prepare_document(
                action.path,
                config,
                client,
                state,
                bucket=vault.bucket_from_path(action.path),
                source_root=vault.root,
                cache=cache,
            )
            progress.finish(action.path.name, started)
            return prepared
        _run_ocr(vault, action, config, client, cache)
        progress.finish(action.path.name, started)
        return None

    def unreadable(action: Action, exc: Exception) -> Prepared | None:
        log.exception("failed to read %s", action.path)
        action.kind, action.error = "failed", f"{type(exc).__name__}: {exc}"
        return None

    def write(action: Action, prepared: Prepared | None) -> Action:
        _write_action(action, prepared, vault, config, state, cache)
        return action

    for action in fast:
        progress.start(f"{action.kind} {action.path.name}")
        write(action, None)
    run_pipeline(slow, read, write, workers, on_error=unreadable)
    if slow:
        log.info("%s", progress.summary(workers))

    if state is not None:
        state.save()
    if own_cache and cache is not None:
        cache.save()
    return report


def _write_action(
    action: Action,
    prepared: Prepared | None,
    vault: Vault,
    config: Config,
    state: State | None,
    cache: ClassificationCache | None,
) -> None:
    """The half that touches the vault. One thread at a time, by construction."""
    try:
        if action.kind == "failed":
            return
        if action.kind == "adopt":
            if prepared is None:
                return
            result = file_document(prepared, config, vault, state)
            if result.status == "failed":
                action.kind, action.error = "failed", result.error
            else:
                action.target, action.meta = result.note, result.meta
            return

        if action.kind == "ocr":
            target = vault.note_path(action.meta)
            action.target = None if target.resolve() == action.path.resolve() else target

        attachment = _move_attachment(vault, action.frontmatter, action.meta)
        destination = action.path
        if action.target is not None and action.target.resolve() != action.path.resolve():
            destination = unique_path(action.target)
            action.target = destination
        _rewrite_note(vault, action, attachment, destination)
        if destination.resolve() != action.path.resolve():
            action.path.unlink(missing_ok=True)
            _prune_empty_dirs(action.path.parent, vault.root)
        if state is not None:
            digest = action.frontmatter.get("source_hash")
            if isinstance(digest, str) and state.get(digest):
                state.record(
                    digest,
                    note=destination.relative_to(vault.root).as_posix(),
                    attachment=(
                        attachment.relative_to(vault.root).as_posix() if attachment else None
                    ),
                    source=action.frontmatter.get("source_file"),
                    title=action.meta.title if action.meta else "",
                    category=action.meta.category if action.meta else "",
                )
    except Exception as exc:  # keep organising the rest of the vault
        log.exception("failed to organise %s", action.path)
        action.kind, action.error = "failed", f"{type(exc).__name__}: {exc}"


def _prune_empty_dirs(directory: Path, stop_at: Path) -> None:
    current = directory
    while current != stop_at and stop_at in current.parents:
        try:
            next(current.iterdir())
            return
        except StopIteration:
            current.rmdir()
            current = current.parent
        except OSError:
            return
