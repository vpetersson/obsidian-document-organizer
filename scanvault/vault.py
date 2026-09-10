"""Phase 2b: write and read the Obsidian vault."""

from __future__ import annotations

import json
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
from .extract import DOCUMENT_SUFFIXES
from .util import parse_date, safe_filename, unique_path

log = logging.getLogger(__name__)

CSS_SNIPPET_NAME = "scanvault"


def css_snippet(classes: list[str]) -> str:
    """The CSS that folds the properties panel shut on scanvault's notes.

    The selectors describe Obsidian's DOM, which is Obsidian's to change - this
    is a starting point to edit, not something scanvault keeps in step.
    """
    container = ", ".join(f".{name} .metadata-container" for name in classes)
    collapsed = ", ".join(f".{name} .metadata-container .metadata-properties" for name in classes)
    expanded = ",\n".join(
        f".{name} .metadata-container:hover .metadata-properties,\n"
        f".{name} .metadata-container:focus-within .metadata-properties"
        for name in classes
    )
    return f"""/* Written by `scanvault init-vault`. Yours to edit; never overwritten.

   Frontmatter on a filed document is bookkeeping - which classifier ran, where
   the date came from, the hash that stops it being filed twice. Obsidian shows
   all of it above every note. This folds it shut on the notes scanvault wrote,
   leaving the "Properties" header there to hover or focus for the rest.

   To hide it outright instead, replace both rules with:
       {container} {{ display: none; }}

   Enable this in Settings -> Appearance -> CSS snippets. */

{collapsed} {{
  display: none;
}}

{expanded} {{
  display: block;
}}
"""


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


FLAT_INDEX_NOTE = (
    "Scanned documents are filed here by category and year. The note beside "
    "each PDF carries its metadata; the tags are what you search."
)

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


EXTRACTED_HEADING = "Extracted text"


def extracted_text_block(text: str, style: str = "callout") -> list[str]:
    """The OCR text, folded away unless asked for.

    A page of OCR beneath every note buries the summary and the attachment, so
    the default is a callout Obsidian renders collapsed - the `-` after the type
    is what folds it. The text is still in the file, so search still finds it.
    """
    if style == "plain":
        return [f"## {EXTRACTED_HEADING}", "", "```text", text, "```", ""]
    if style == "details":
        return [
            "<details>",
            f"<summary>{EXTRACTED_HEADING}</summary>",
            "",
            "```text",
            text,
            "```",
            "",
            "</details>",
            "",
        ]
    if style != "callout":
        log.warning("unknown extracted_text_style %r; using a callout", style)
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
    return [f"> [!quote]- {EXTRACTED_HEADING}", "> ```text", quoted, "> ```", ""]


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
        if self.config.vault.layout != "para":
            return None
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

    def root_folder(self, meta: DocumentMeta) -> str:
        """The folder everything else hangs from.

        One folder under the flat layout; the document's PARA folder when the
        vault is organised that way.
        """
        if self.config.vault.layout == "para":
            return self.para_folder(meta.para)
        return self.config.vault.documents_dir

    def _render_template(self, template: str, meta: DocumentMeta) -> Path:
        values = {k: safe_filename(v) for k, v in meta.placeholders().items()}
        root = safe_filename(self.root_folder(meta)) if self.root_folder(meta) else ""
        # `para` is the old name for this placeholder; existing configs still work.
        values["root"] = values["para"] = root
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
        embeds: list[Path] | None = None,
        prose: str = "",
    ) -> str:
        """Render a note.

        Rewriting a note replaces its body, so anything that was not ours has to
        be put back: `embeds` are files it pointed at, `prose` is whatever
        someone wrote in it. Losing either would make re-filing a vault a way to
        destroy what is in it.
        """
        frontmatter: dict[str, Any] = {
            "title": meta.title,
            "date": meta.document_date,  # emitted unquoted so Obsidian sees a date
            "date_source": meta.date_source,
            "title_source": meta.title_source,
            "category": meta.category,
            "correspondent": meta.correspondent,
            "tags": meta.tags,
            "document_type": meta.document_type,
            "context": meta.context,
            "subjects": meta.subjects,
            "reference": meta.reference,
            "amount": meta.amount,
            "currency": meta.currency,
            "language": meta.language,
            "confidence": meta.confidence,
            "para": meta.para,
            "classifier": meta.classifier,
            "scanvault_version": __version__,
            "processed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Obsidian turns these into CSS classes on the note, which is what
            # lets a snippet fold this whole block away. Last, so the panel
            # ends with the least interesting line rather than starting on it.
            "cssclasses": list(self.config.vault.cssclasses),
        }
        frontmatter.pop("cssclasses")
        extra = dict(extra or {})
        # A caller that knows the note's existing classes hands them over here;
        # they are the user's, and adding ours must not drop theirs.
        classes = extra.pop("cssclasses", None) or list(self.config.vault.cssclasses)
        frontmatter.update(extra)
        if attachment is not None:
            frontmatter["attachment"] = self.wikilink(attachment)
        # Last, after the extras, so the panel ends on the line worth reading
        # least rather than opening on it.
        frontmatter["cssclasses"] = classes

        body = [dump_frontmatter(frontmatter), "", f"# {meta.title}", ""]
        if meta.summary:
            body += [meta.summary, ""]
        if prose.strip():
            body += [prose.strip(), ""]
        if attachment is not None:
            body += [f"![[{self.wikilink(attachment)}]]", ""]
        for embed in embeds or []:
            if attachment is not None and embed == attachment:
                continue
            # Written as a vault-relative link rather than the bare name the
            # note used, so it survives the note being moved.
            body += [f"![[{self.wikilink(embed)}]]", ""]
        if self.config.vault.include_text and text.strip():
            clipped = text.strip()[: self.config.vault.max_text_chars]
            body += extracted_text_block(clipped, self.config.vault.extracted_text_style)
        return "\n".join(body)

    def write_document(
        self,
        meta: DocumentMeta,
        text: str,
        pdf_path: Path | None,
        source_path: Path | None = None,
        extra: dict[str, Any] | None = None,
        dry_run: bool = False,
        original_path: Path | None = None,
    ) -> WriteResult:
        """Write the note and its PDF.

        `original_path` is the file the PDF was made from when that was not a
        PDF itself - an image - and it is kept beside the attachment unless
        `keep_original_image` says otherwise.
        """
        note = unique_path(self.note_path(meta))
        attachment = unique_path(self.attachment_path(meta)) if pdf_path else None
        if dry_run:
            return WriteResult(note, attachment)
        extra = dict(extra or {})

        if attachment is not None and pdf_path is not None:
            attachment.parent.mkdir(parents=True, exist_ok=True)
            action = self.config.vault.source_action
            same_file = source_path is not None and pdf_path.resolve() == source_path.resolve()
            keep_original = (
                original_path is not None
                and original_path.exists()
                and self.config.vault.keep_original_image
            )
            if action == "move" and same_file:
                shutil.move(str(pdf_path), attachment)
            else:
                shutil.copyfile(pdf_path, attachment)
                if action == "move" and source_path is not None and source_path.exists():
                    if not keep_original or source_path != original_path:
                        source_path.unlink()
                if not same_file and pdf_path.exists() and pdf_path.parent != attachment.parent:
                    pdf_path.unlink(missing_ok=True)  # drop the temporary OCR output

            if keep_original and original_path is not None:
                # The searchable PDF is what gets filed, but the photo or scan
                # it came from is the actual original, so it stays with it.
                kept = unique_path(attachment.with_suffix(original_path.suffix.lower()))
                if action == "move":
                    shutil.move(str(original_path), kept)
                else:
                    shutil.copyfile(original_path, kept)
                extra["original"] = self.wikilink(kept)

        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(self.render_note(meta, text, attachment, extra), encoding="utf-8")
        return WriteResult(note, attachment)

    def write_css_snippet(self, dry_run: bool = False) -> Path | None:
        """Write the snippet that folds the properties panel away.

        Frontmatter is bookkeeping - which classifier ran, where the date came
        from, the hash. Obsidian shows all of it above every note, which for a
        scanned letter is a screen of machine-readable detail before the letter.
        Obsidian's own setting for this (Editor -> Properties in document) is
        global, so this targets our notes only, through `cssclasses`.

        Never overwrites: the file is the user's once it exists.
        """
        classes = self.config.vault.cssclasses
        if not classes:
            return None
        path = self.root / ".obsidian" / "snippets" / f"{CSS_SNIPPET_NAME}.css"
        if path.exists():
            return None
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(css_snippet(classes), encoding="utf-8")
        return path

    def snippet_enabled(self) -> bool | None:
        """Whether Obsidian has the snippet switched on. None if it cannot tell."""
        appearance = self.root / ".obsidian" / "appearance.json"
        try:
            data = json.loads(appearance.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        enabled = data.get("enabledCssSnippets")
        if not isinstance(enabled, list):
            return None
        return CSS_SNIPPET_NAME in enabled

    def scaffold(self, dry_run: bool = False) -> list[Path]:
        """Create the folders documents will land in. Never overwrites."""
        if self.config.vault.layout != "para":
            return self._scaffold_flat(dry_run)
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

    def _scaffold_flat(self, dry_run: bool = False) -> list[Path]:
        folder = self.config.vault.documents_dir
        if not folder:
            return []
        index = self.root / folder / f"{folder}.md"
        if index.exists():
            return []
        if not dry_run:
            index.parent.mkdir(parents=True, exist_ok=True)
            body = dump_frontmatter({"title": folder, "para_index": True})
            index.write_text(
                f"{body}\n\n# {folder}\n\n{FLAT_INDEX_NOTE}\n", encoding="utf-8"
            )
        return [index]

    # ---- reading ----------------------------------------------------------

    def iter_notes(self) -> Iterator[Path]:
        root = self.config.notes_root
        if not root.exists():
            return
        for path in sorted(root.rglob("*.md")):
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            yield path

    def resolve_link(self, note: Path, target: str) -> Path | None:
        """The file a link points at, the way Obsidian resolves one.

        Obsidian's default is a "shortest path" link: `![[Scan Page 196.jpg]]`
        carries no folder and is resolved by searching the vault. So a bare name
        is looked for beside the note first, then anywhere in the vault, before
        being given up on.
        """
        target = unquote(target.strip())
        if not target or target.startswith(("http://", "https://", "obsidian://")):
            return None
        candidates = [note.parent / target, self.root / target]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        if "/" in target:
            return None
        name = Path(target).name
        matches = [
            path
            for path in self.root.rglob(name)
            if path.is_file() and not any(part.startswith(".") for part in path.parts)
        ]
        # Ambiguous is as good as missing: acting on the wrong file is worse
        # than leaving the note alone.
        return matches[0] if len(matches) == 1 else None

    def linked_documents(self, note: Path, body: str) -> list[Path]:
        """The PDFs and images a note embeds, in the order it embeds them."""
        found: list[Path] = []
        for target in link_targets(body):
            if Path(unquote(target.strip())).suffix.lower() not in DOCUMENT_SUFFIXES:
                continue
            path = self.resolve_link(note, target)
            if path is not None and path not in found:
                found.append(path)
        return found

    def _record_link(self, target: str, paths: set[Path], names: set[str]) -> None:
        target = unquote(target.strip())
        if Path(target).suffix.lower() not in DOCUMENT_SUFFIXES:
            return
        # Obsidian's shortest-path links carry no folder, so a bare filename has
        # to count too. Matching too eagerly only means we leave a PDF alone.
        names.add(Path(target).name)
        if "/" in target:
            paths.add((self.root / target).resolve())

    def iter_loose_documents(self) -> Iterator[Path]:
        """Documents in the vault that no note points at - by frontmatter or link.

        Notes written by hand embed their PDFs with `![[...]]` rather than an
        `attachment:` key; treating those as loose would file a second copy and
        break the link.
        """
        linked_paths: set[Path] = set()
        linked_names: set[str] = set()
        for note in self.iter_notes():
            data, body = parse_frontmatter(note.read_text(encoding="utf-8", errors="replace"))
            for key in ("attachment", "original"):
                value = data.get(key)
                if isinstance(value, str):
                    self._record_link(value, linked_paths, linked_names)
            for target in link_targets(body):
                self._record_link(target, linked_paths, linked_names)

        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in DOCUMENT_SUFFIXES:
                continue
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
            title_source=str(data.get("title_source") or "model"),
            correspondent=str(data.get("correspondent") or ""),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            context=str(data.get("context") or ""),
            document_type=str(data.get("document_type") or ""),
            subjects=[
                str(item) for item in (data.get("subjects") or []) if isinstance(item, str)
            ],
            reference=str(data.get("reference") or ""),
            classifier=str(data.get("classifier") or ""),
            para=str(data.get("para") or "") or self.config.vault.para.default_bucket,
        )
