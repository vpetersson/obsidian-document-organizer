"""Phase 2b: write and read the Obsidian vault."""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

from . import __version__
from .classify import DocumentMeta
from .config import BUCKETS, Config
from .util import parse_date, safe_filename, unique_path

log = logging.getLogger(__name__)

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
# `![[file.pdf]]`, `[[folder/file.pdf|label]]` and `[label](folder/file.pdf)`
WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
MDLINK_RE = re.compile(r"\]\(<?([^)>\s]+)>?\)")


def link_targets(body: str) -> list[str]:
    """Every link target in a note body, wiki-style or markdown-style."""
    return WIKILINK_RE.findall(body) + MDLINK_RE.findall(body)


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


PARA_INDEX_NOTES = {
    "project": "Short-term efforts with a goal and a finish line. Move a "
    "document here by setting `para: project` in its frontmatter.",
    "area": "Ongoing responsibilities you maintain over time - health, "
    "finances, a property, a vehicle.",
    "resource": "Topics and reference material you want at hand but are not "
    "actively working on.",
    "archive": "Everything inactive, and the default home for scanned "
    "documents. This is the cornerstone of the vault: scanvault files every "
    "new scan here unless a note says otherwise.",
}


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

    def para_folder(self, bucket: str) -> str:
        """Folder name for a PARA bucket, falling back to the archive."""
        para = self.config.vault.para
        if bucket not in BUCKETS:
            log.warning("unknown para bucket %r; filing under the archive", bucket)
        return para.folder(bucket)

    def bucket_from_path(self, path: Path) -> str | None:
        """Which PARA folder a file currently sits in, if any."""
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return None
        if not relative.parts:
            return None
        head = relative.parts[0]
        for bucket, folder in self.config.vault.para.buckets().items():
            if head == folder:
                return bucket
        return None

    def _render_template(self, template: str, meta: DocumentMeta) -> Path:
        values = {k: safe_filename(v) for k, v in meta.placeholders().items()}
        values["para"] = safe_filename(self.para_folder(meta.para))
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
            "date_source": meta.date_source,
            "category": meta.category,
            "correspondent": meta.correspondent,
            "tags": meta.tags,
            "reference": meta.reference,
            "amount": meta.amount,
            "currency": meta.currency,
            "language": meta.language,
            "confidence": meta.confidence,
            "para": meta.para,
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

    def scaffold(self, dry_run: bool = False) -> list[Path]:
        """Create the PARA folders and their index notes. Never overwrites."""
        created: list[Path] = []
        for bucket, folder in self.config.vault.para.buckets().items():
            directory = self.root / folder
            index = directory / f"{folder}.md"
            if index.exists():
                continue
            created.append(index)
            if dry_run:
                continue
            directory.mkdir(parents=True, exist_ok=True)
            body = dump_frontmatter({"title": folder, "para_index": True})
            index.write_text(
                f"{body}\n\n# {folder}\n\n{PARA_INDEX_NOTES[bucket]}\n", encoding="utf-8"
            )
        return created

    # ---- reading ----------------------------------------------------------

    def iter_notes(self) -> Iterator[Path]:
        root = self.config.notes_root
        if not root.exists():
            return
        for path in sorted(root.rglob("*.md")):
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            yield path

    def _record_link(self, target: str, paths: set[Path], names: set[str]) -> None:
        target = unquote(target.strip())
        if not target.lower().endswith(".pdf"):
            return
        # Obsidian's shortest-path links carry no folder, so a bare filename has
        # to count too. Matching too eagerly only means we leave a PDF alone.
        names.add(Path(target).name)
        if "/" in target:
            paths.add((self.root / target).resolve())

    def iter_loose_pdfs(self) -> Iterator[Path]:
        """PDFs in the vault that no note points at - by frontmatter or by link.

        Notes written by hand embed their PDFs with `![[...]]` rather than an
        `attachment:` key; treating those as loose would file a second copy and
        break the link.
        """
        linked_paths: set[Path] = set()
        linked_names: set[str] = set()
        for note in self.iter_notes():
            data, body = parse_frontmatter(note.read_text(encoding="utf-8", errors="replace"))
            attachment = data.get("attachment")
            if isinstance(attachment, str):
                self._record_link(attachment, linked_paths, linked_names)
            for target in link_targets(body):
                self._record_link(target, linked_paths, linked_names)

        for path in sorted(self.root.rglob("*.pdf")):
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            if path.resolve() in linked_paths or path.name in linked_names:
                continue
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
            date_source=str(data.get("date_source") or ""),
            correspondent=str(data.get("correspondent") or ""),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            reference=str(data.get("reference") or ""),
            classifier=str(data.get("classifier") or ""),
            para=str(data.get("para") or "") or self.config.vault.para.default_bucket,
        )
