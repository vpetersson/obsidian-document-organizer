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

from .classify import DocumentMeta, classify, resolve_date
from .config import BUCKETS, Config
from .extract import OcrError, available_backend, extract, needs_password, pdf_text
from .llm import OllamaClient
from .pipeline import process_file
from .state import State
from .util import sha256_file, unique_path
from .vault import Vault

log = logging.getLogger(__name__)

TEXT_BLOCK_RE = re.compile(r"^## Extracted text\s*\n+```(?:text)?\n(.*?)\n```", re.DOTALL | re.MULTILINE)
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


def note_text(vault: Vault, note: Path, frontmatter: dict[str, Any], body: str, config: Config) -> str:
    """Best text already available for a note - never runs OCR.

    Planning stays read-only and cheap; OCR is a separate, reported action.
    """
    attachment = attachment_path(vault, frontmatter)
    if attachment is not None:
        text = pdf_text(attachment)
        if len(text) >= config.ocr.min_text_chars:
            return text
    match = TEXT_BLOCK_RE.search(body)
    if match:
        return match.group(1)
    return re.sub(r"^#.*$", "", body, flags=re.MULTILINE).strip()


def attachment_path(vault: Vault, frontmatter: dict[str, Any]) -> Path | None:
    """The note's attachment, if it is recorded and still on disk."""
    attachment = frontmatter.get("attachment")
    if not isinstance(attachment, str):
        return None
    path = vault.root / attachment
    return path if path.is_file() else None


def attachment_needs_ocr(vault: Vault, frontmatter: dict[str, Any], config: Config) -> bool:
    """True when the note's PDF has no text layer at all.

    The bar here is "is it searchable", not `min_text_chars` - a short receipt
    is a legitimately tiny but perfectly searchable document.
    """
    attachment = attachment_path(vault, frontmatter)
    if attachment is None or attachment.suffix.lower() != ".pdf":
        return False
    return len(pdf_text(attachment)) < config.ocr.searchable_min_chars


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
) -> OrganizeReport:
    """Work out what the vault needs, without touching anything."""
    vault = Vault(config)
    report = OrganizeReport()
    backend = "none"
    if ocr:
        try:
            backend = available_backend(config.ocr)
        except OcrError as exc:
            log.warning("%s", exc)

    for note in vault.iter_notes():
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

        if ocr and attachment_needs_ocr(vault, frontmatter, config):
            attachment = attachment_path(vault, frontmatter)
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

        text = ""
        if should_classify:
            text = note_text(vault, note, frontmatter, body, config)
            meta = classify(text, config, client, source=note)
            resolve_date(meta, attachment_path(vault, frontmatter) or note, config)
            reason = "reclassified" if reclassify else "incomplete metadata"
        else:
            meta = vault.meta_from_note(frontmatter, note.stem)
            reason = "layout drift"
        meta.para = resolve_bucket(vault, note, frontmatter, config)

        target = vault.note_path(meta)
        if target.resolve() == note.resolve():
            if should_classify:
                report.actions.append(
                    Action("rewrite", note, note, reason, meta, frontmatter, text)
                )
            else:
                report.actions.append(Action("noop", note, note, "already filed", meta))
            continue
        report.actions.append(Action("relocate", note, target, reason, meta, frontmatter, text))

    if adopt:
        known_hashes = State(config.state_root).documents
        for index, pdf in enumerate(vault.iter_loose_pdfs(), start=1):
            if index % 50 == 0:
                # A vault of several hundred PDFs takes a while to hash and
                # probe; say something rather than looking hung.
                log.info("scanned %d PDFs...", index)
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
    vault: Vault, action: Action, config: Config, client: OllamaClient | None
) -> None:
    """OCR the note's attachment in place, then refresh its metadata and text."""
    pdf = attachment_path(vault, action.frontmatter)
    if pdf is None:
        raise OcrError("the attachment recorded in this note is missing")
    result = extract(pdf, config.ocr, work_dir=config.state_root / "work")
    if result.pdf_path != pdf and result.pdf_path.exists():
        # Keep the searchable PDF: that is the point of OCR'ing an old vault.
        shutil.move(str(result.pdf_path), pdf)
    if action.classify_after:
        action.meta = classify(result.text, config, client, source=action.path)
        resolve_date(action.meta, pdf, config)
    else:
        action.meta = vault.meta_from_note(action.frontmatter, action.path.stem)
    action.meta.para = resolve_bucket(vault, action.path, action.frontmatter, config)
    action.body_text = result.text
    action.frontmatter["ocr"] = result.backend
    if result.pages:
        action.frontmatter["pages"] = result.pages


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
    text = action.body_text
    if not text and vault.config.vault.include_text:
        _, body = vault.read_note(action.path)
        match = TEXT_BLOCK_RE.search(body)
        text = match.group(1) if match else ""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        vault.render_note(meta, text, attachment, extra=preserved), encoding="utf-8"
    )


def apply(
    report: OrganizeReport,
    config: Config,
    client: OllamaClient | None = None,
    use_state: bool = True,
) -> OrganizeReport:
    """Execute a plan. Returns the same report with kinds updated to what happened."""
    vault = Vault(config)
    state = State(config.state_root) if use_state else None

    for action in report.actions:
        try:
            if action.kind in ("noop", "index", "skipped", "duplicate", "failed"):
                continue

            if action.kind == "adopt":
                bucket = vault.bucket_from_path(action.path)
                result = process_file(
                    action.path,
                    config,
                    vault,
                    client,
                    state,
                    bucket=bucket,
                    source_root=vault.root,
                )
                if result.status == "failed":
                    action.kind, action.error = "failed", result.error
                else:
                    action.target, action.meta = result.note, result.meta
                continue

            if action.kind == "ocr":
                _run_ocr(vault, action, config, client)
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

    if state is not None:
        state.save()
    return report


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
