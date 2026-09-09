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

from .classify import DocumentMeta, classify
from .config import BUCKETS, Config
from .extract import OcrError, extract, pdf_text
from .llm import OllamaClient
from .pipeline import process_file
from .state import State
from .util import unique_path
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

    def describe(self, root: Path) -> str:
        def rel(p: Path | None) -> str:
            if p is None:
                return "-"
            try:
                return p.relative_to(root).as_posix()
            except ValueError:
                return str(p)

        if self.kind in ("relocate", "adopt"):
            return f"{self.kind}: {rel(self.path)} -> {rel(self.target)} ({self.reason})"
        return f"{self.kind}: {rel(self.path)} ({self.reason})"


@dataclass
class OrganizeReport:
    actions: list[Action] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for action in self.actions if action.kind == kind)


def note_text(vault: Vault, note: Path, frontmatter: dict[str, Any], body: str, config: Config) -> str:
    """Best text available for a note: the attachment's, else the embedded block."""
    attachment = frontmatter.get("attachment")
    if isinstance(attachment, str):
        pdf = vault.root / attachment
        if pdf.is_file():
            text = pdf_text(pdf)
            if len(text) >= config.ocr.min_text_chars:
                return text
            try:
                return extract(pdf, config.ocr, work_dir=config.state_root / "work").text
            except OcrError as exc:
                log.warning("could not OCR %s: %s", pdf.name, exc)
    match = TEXT_BLOCK_RE.search(body)
    if match:
        return match.group(1)
    return re.sub(r"^#.*$", "", body, flags=re.MULTILINE).strip()


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
) -> OrganizeReport:
    """Work out what the vault needs, without touching anything."""
    vault = Vault(config)
    report = OrganizeReport()

    for note in vault.iter_notes():
        try:
            frontmatter, body = vault.read_note(note)
        except OSError as exc:
            report.actions.append(Action("failed", note, error=str(exc)))
            continue

        if frontmatter.get("para_index"):
            report.actions.append(Action("noop", note, note, "PARA index note"))
            continue
        if not include_unmanaged and not is_managed(frontmatter):
            report.actions.append(Action("skipped", note, note, "not a scanvault note"))
            continue

        should_classify = reclassify or needs_classification(frontmatter)
        text = ""
        if should_classify:
            text = note_text(vault, note, frontmatter, body, config)
            meta = classify(text, config, client, source=note)
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
        for pdf in vault.iter_loose_pdfs():
            report.actions.append(Action("adopt", pdf, None, "no note points at this PDF"))
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
            if action.kind in ("noop", "skipped", "failed"):
                continue

            if action.kind == "adopt":
                bucket = vault.bucket_from_path(action.path)
                result = process_file(action.path, config, vault, client, state, bucket=bucket)
                if result.status == "failed":
                    action.kind, action.error = "failed", result.error
                else:
                    action.target, action.meta = result.note, result.meta
                continue

            attachment = _move_attachment(vault, action.frontmatter, action.meta)
            destination = action.path
            if action.kind == "relocate" and action.target is not None:
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
