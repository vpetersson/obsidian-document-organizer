"""Phase 2b: write and read the Obsidian vault."""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .classify import DocumentMeta
from .config import Config
from .util import parse_date, safe_filename, unique_path

log = logging.getLogger(__name__)

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def dump_frontmatter(data: dict[str, Any]) -> str:
    """Emit the small YAML subset we use: scalars and flat string lists."""
    lines = ["---"]
    for key, value in data.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        elif isinstance(value, (date, datetime)):
            lines.append(f"{key}: {value.isoformat()}")
        else:
            lines.append(f"{key}: {_quote(str(value))}")
    lines.append("---")
    return "\n".join(lines)


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    return raw


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter, body). Missing or unreadable frontmatter -> ({}, content)."""
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("  - ", "- ")) and current_key:
            data.setdefault(current_key, [])
            if isinstance(data[current_key], list):
                data[current_key].append(_scalar(line.split("- ", 1)[1]))
            continue
        if ":" in line and not line.startswith(" "):
            key, _, raw = line.partition(":")
            key = key.strip()
            current_key = key
            data[key] = [] if not raw.strip() else _scalar(raw)
    return data, content[match.end() :]


@dataclass
class WriteResult:
    note_path: Path
    attachment_path: Path | None
    created: bool = True


class Vault:
    def __init__(self, config: Config):
        if config.vault_dir is None:
            raise ValueError("vault_dir is required")
        self.config = config
        self.root = config.vault_dir

    # ---- layout -----------------------------------------------------------

    def _render_template(self, template: str, meta: DocumentMeta) -> Path:
        values = {k: safe_filename(v) for k, v in meta.placeholders().items()}
        try:
            rendered = template.format(**values)
        except KeyError as exc:
            raise ValueError(f"unknown placeholder {exc} in template {template!r}") from exc
        parts = [safe_filename(part) for part in rendered.split("/") if part.strip()]
        if not parts:
            parts = ["Other", "untitled"]
        return Path(*parts)

    def note_path(self, meta: DocumentMeta) -> Path:
        rel = self._render_template(self.config.vault.note_path_template, meta)
        return self.config.notes_root / rel.with_suffix(".md")

    def attachment_path(self, meta: DocumentMeta, suffix: str = ".pdf") -> Path:
        rel = self._render_template(self.config.vault.attachment_path_template, meta)
        return self.config.attachments_root / rel.with_suffix(suffix)

    def wikilink(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # ---- writing ----------------------------------------------------------

    def render_note(
        self,
        meta: DocumentMeta,
        text: str,
        attachment: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        frontmatter: dict[str, Any] = {
            "title": meta.title,
            "date": meta.document_date,  # emitted unquoted so Obsidian sees a date
            "category": meta.category,
            "correspondent": meta.correspondent,
            "tags": meta.tags,
            "reference": meta.reference,
            "amount": meta.amount,
            "currency": meta.currency,
            "language": meta.language,
            "confidence": meta.confidence,
            "classifier": meta.classifier,
            "scanvault_version": __version__,
            "processed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        frontmatter.update(extra or {})
        if attachment is not None:
            frontmatter["attachment"] = self.wikilink(attachment)

        body = [dump_frontmatter(frontmatter), "", f"# {meta.title}", ""]
        if meta.summary:
            body += [meta.summary, ""]
        if attachment is not None:
            body += [f"![[{self.wikilink(attachment)}]]", ""]
        if self.config.vault.include_text and text.strip():
            clipped = text.strip()[: self.config.vault.max_text_chars]
            body += ["## Extracted text", "", "```text", clipped, "```", ""]
        return "\n".join(body)

    def write_document(
        self,
        meta: DocumentMeta,
        text: str,
        pdf_path: Path | None,
        source_path: Path | None = None,
        extra: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> WriteResult:
        note = unique_path(self.note_path(meta))
        attachment = unique_path(self.attachment_path(meta)) if pdf_path else None
        if dry_run:
            return WriteResult(note, attachment)

        if attachment is not None and pdf_path is not None:
            attachment.parent.mkdir(parents=True, exist_ok=True)
            action = self.config.vault.source_action
            same_file = source_path is not None and pdf_path.resolve() == source_path.resolve()
            if action == "move" and same_file:
                shutil.move(str(pdf_path), attachment)
            else:
                shutil.copyfile(pdf_path, attachment)
                if action == "move" and source_path is not None and source_path.exists():
                    source_path.unlink()
                if not same_file and pdf_path.exists() and pdf_path.parent != attachment.parent:
                    pdf_path.unlink(missing_ok=True)  # drop the temporary OCR output

        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(self.render_note(meta, text, attachment, extra), encoding="utf-8")
        return WriteResult(note, attachment)

    # ---- reading ----------------------------------------------------------

    def iter_notes(self) -> Iterator[Path]:
        root = self.config.notes_root
        if not root.exists():
            return
        for path in sorted(root.rglob("*.md")):
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            yield path

    def iter_loose_pdfs(self) -> Iterator[Path]:
        """PDFs sitting in the vault that no note points at yet."""
        linked = set()
        for note in self.iter_notes():
            data, _ = parse_frontmatter(note.read_text(encoding="utf-8", errors="replace"))
            attachment = data.get("attachment")
            if isinstance(attachment, str):
                linked.add((self.root / attachment).resolve())
        for path in sorted(self.root.rglob("*.pdf")):
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            if path.resolve() not in linked:
                yield path

    def read_note(self, path: Path) -> tuple[dict[str, Any], str]:
        return parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))

    def meta_from_note(self, data: dict[str, Any], fallback_title: str) -> DocumentMeta:
        tags = data.get("tags")
        return DocumentMeta(
            title=str(data.get("title") or fallback_title),
            category=str(data.get("category") or "Other"),
            summary="",
            document_date=parse_date(data.get("date")),
            correspondent=str(data.get("correspondent") or ""),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            reference=str(data.get("reference") or ""),
            classifier=str(data.get("classifier") or ""),
        )
