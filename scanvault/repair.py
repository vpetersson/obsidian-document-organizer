"""Repair: find the notes whose scans "could not be found", and mend them.

A note embeds its document by path. When the document moves without the note
being told - a scan that was filed a second time by `ingest`, a file moved in
Finder, a sync that renamed something - Obsidian shows "could not be found"
where the scan should be.

`repair` walks every note and checks every document it points at. For each
link that no longer resolves it looks for where the document went:

  * a file of the same name somewhere else in the vault;
  * a note that was filed *from* a file of that name (its `source_file`), whose
    text is the same document - the scan was filed again, and that note holds
    it now.

What it does with the answer:

  * **relink** - the note's link is rewritten to where the document is now;
  * **remove** - the note is a stale copy of a note that holds the same scan,
    and has nothing in it of its own; it goes to `.scanvault/repair-trash/`,
    never straight to deletion;
  * **missing** - nothing in the vault is that document. Reported, and with
    `--drop-missing` the dead link is taken out of the note.

Then the dedupe index is rebuilt from the notes, so it stops naming notes that
are gone and starts naming the ones it lost track of.
"""

from __future__ import annotations

import difflib
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .config import Config
from .extract import DOCUMENT_SUFFIXES
from .organizer import (
    CALLOUT_BLOCK_RE,
    TEXT_BLOCK_RE,
    embedded_text,
    preserved_prose,
)
from .state import State
from .vault import FRONTMATTER_RE, Vault, _scalar, link_targets, parse_frontmatter

log = logging.getLogger(__name__)

TRASH_DIR = "repair-trash"
LINK_KEYS = ("attachment", "original")
# Below this, two notes' text is not the same document read twice.
SAME_TEXT = 0.6

WIKILINK_SUB_RE = re.compile(r"(!?\[\[)([^\]|#]+)((?:[#|][^\]]*)?\]\])")
MDLINK_SUB_RE = re.compile(r"(!?\[[^\]]*\]\()(<?)([^)>\n]+?)(>?\))")


@dataclass
class NoteInfo:
    path: Path
    frontmatter: dict[str, Any]
    body: str
    attachment: Path | None  # resolved, and only if it is on disk
    text: str

    @property
    def source_file(self) -> str:
        value = self.frontmatter.get("source_file")
        return value if isinstance(value, str) else ""


@dataclass
class Finding:
    kind: str  # relink | remove | missing | drop | failed
    note: Path
    target: str = ""  # the link as the note wrote it
    found: Path | None = None
    owner: Path | None = None  # the note that holds the document now
    reason: str = ""
    error: str = ""

    def describe(self, root: Path) -> str:
        def rel(path: Path | None) -> str:
            if path is None:
                return "-"
            try:
                return path.relative_to(root).as_posix()
            except ValueError:
                return str(path)

        if self.kind == "relink":
            return f"relink: {rel(self.note)}: {self.target} -> {rel(self.found)} ({self.reason})"
        if self.kind == "remove":
            return f"remove: {rel(self.note)} ({self.reason})"
        return f"{self.kind}: {rel(self.note)}: {self.target} ({self.reason})"


@dataclass
class IndexChanges:
    added: int = 0
    updated: int = 0
    dropped: int = 0

    def __bool__(self) -> bool:
        return bool(self.added or self.updated or self.dropped)

    def describe(self) -> str:
        return f"{self.added} added, {self.updated} updated, {self.dropped} dropped"


@dataclass
class RepairReport:
    findings: list[Finding] = field(default_factory=list)
    notes: int = 0
    links: int = 0
    index: IndexChanges = field(default_factory=IndexChanges)
    trash: Path | None = None

    def count(self, kind: str) -> int:
        return sum(1 for finding in self.findings if finding.kind == kind)


# ---- reading -----------------------------------------------------------------


def _without_text_block(body: str) -> str:
    """The body minus the OCR text, which is a document's words, not its links."""
    body = CALLOUT_BLOCK_RE.sub("", body)
    body = TEXT_BLOCK_RE.sub("", body)
    return re.sub(r"<details>.*?</details>", "", body, flags=re.DOTALL)


def document_links(frontmatter: dict[str, Any], body: str) -> list[str]:
    """Every document a note points at, as written, frontmatter first."""
    found: list[str] = []
    candidates = [frontmatter.get(key) for key in LINK_KEYS]
    candidates += link_targets(_without_text_block(body))
    for target in candidates:
        if not isinstance(target, str):
            continue
        target = unquote(target.strip())
        if Path(target).suffix.lower() not in DOCUMENT_SUFFIXES or target in found:
            continue
        if target.startswith(("http://", "https://", "obsidian://")):
            continue
        found.append(target)
    return found


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()[:2000]


def same_text(a: str, b: str) -> float:
    a, b = _normalise(a), _normalise(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def own_writing(info: NoteInfo, owner: NoteInfo) -> str:
    """What is in this note and not in the note that holds its scan now.

    The summary is not recorded anywhere but the body, so there is no telling
    it apart from a line someone added - except that a summary of the same
    document is in the other note too. Anything that is not counts as theirs.
    """
    theirs = _normalise_all(owner.body)
    paragraphs = re.split(r"\n\s*\n", preserved_prose(info.body))
    return "\n\n".join(
        paragraph.strip()
        for paragraph in paragraphs
        if paragraph.strip() and _normalise_all(paragraph) not in theirs
    )


def _normalise_all(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _documents_by_name(vault: Vault) -> dict[str, list[Path]]:
    by_name: dict[str, list[Path]] = {}
    for path in sorted(vault.root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in DOCUMENT_SUFFIXES:
            continue
        if any(part.startswith(".") for part in path.relative_to(vault.root).parts):
            continue
        by_name.setdefault(path.name, []).append(path)
    return by_name


def _read_notes(vault: Vault) -> list[NoteInfo]:
    notes: list[NoteInfo] = []
    for path in vault.iter_notes():
        try:
            frontmatter, body = vault.read_note(path)
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)
            continue
        if frontmatter.get("para_index"):
            continue
        attachment = None
        value = frontmatter.get("attachment")
        if isinstance(value, str):
            candidate = vault.root / unquote(value.strip())
            attachment = candidate if candidate.is_file() else None
        notes.append(NoteInfo(path, frontmatter, body, attachment, embedded_text(body)))
    return notes


# ---- planning ----------------------------------------------------------------


def _resolves(vault: Vault, note: Path, target: str, by_name: dict[str, list[Path]]) -> bool:
    if vault.resolve_link(note, target) is not None:
        return True
    # A bare name shared by several files is ambiguous to us, but Obsidian
    # picks one of them and shows it - that link is not broken.
    return "/" not in target and len(by_name.get(Path(target).name, [])) > 1


def _recover(
    info: NoteInfo,
    target: str,
    notes: list[NoteInfo],
    by_name: dict[str, list[Path]],
) -> tuple[Path | None, NoteInfo | None, str]:
    """Where the document a broken link meant has gone: (file, owning note, how)."""
    name = Path(target).name
    owners_by_file = {other.attachment: other for other in notes if other.attachment}

    same_name = by_name.get(name, [])
    if len(same_name) == 1:
        found = same_name[0]
        owner = owners_by_file.get(found)
        return found, owner if owner is not info else None, "the same file, somewhere else"

    # Filed again: a note made from a file of this name now holds it, under the
    # name scanvault gave it.
    owners = [
        other
        for other in notes
        if other is not info and other.attachment is not None and other.source_file == name
    ]
    named = owners
    if info.text:
        # Scanners count from 1 again; "Scan 52.pdf" names a different document
        # every few hundred pages. The text is what says it is this one.
        scored = [(same_text(info.text, other.text), other) for other in owners]
        matching = [(score, other) for score, other in scored if score >= SAME_TEXT or not other.text]
        owners = [max(matching, key=lambda pair: pair[0])[1]] if matching else []
    if named and not owners:
        names = ", ".join(other.path.name for other in named)
        return None, None, f"filed again from a {name} ({names}), but the text does not match"
    if len(owners) == 1:
        owner = owners[0]
        return owner.attachment, owner, f"filed again as {owner.path.name}"
    if len(owners) > 1:
        return None, None, f"{len(owners)} notes were filed from a {name}; cannot tell which"
    return None, None, "nothing in the vault is this document"


def plan(config: Config, drop_missing: bool = False) -> RepairReport:
    """Check every link in the vault and decide what each broken one needs."""
    vault = Vault(config)
    report = RepairReport()
    notes = _read_notes(vault)
    by_name = _documents_by_name(vault)
    report.notes = len(notes)
    removed: set[Path] = set()

    for info in notes:
        links = document_links(info.frontmatter, info.body)
        report.links += len(links)
        broken = [target for target in links if not _resolves(vault, info.path, target, by_name)]
        if not broken:
            continue

        recovered = [(target, *_recover(info, target, notes, by_name)) for target in broken]
        owners = {owner.path: owner for _, found, owner, _ in recovered if owner is not None}
        healthy = len(links) - len(broken)
        sole = next(iter(owners.values())) if len(owners) == 1 else None
        if (
            sole is not None
            and all(found is not None and owner is not None for _, found, owner, _ in recovered)
            and healthy == 0
            and sole.path not in removed
            and not own_writing(info, sole)
        ):
            # Every scan it had is in one other note now, and nothing in this
            # one is not in that one too: it is the stale half of a duplicate.
            owner = sole.path
            report.findings.append(
                Finding(
                    "remove",
                    info.path,
                    ", ".join(broken),
                    owner=owner,
                    reason=f"its scan was filed again as {owner.relative_to(vault.root).as_posix()}",
                )
            )
            removed.add(info.path)
            continue

        for target, found, owner, how in recovered:
            if found is not None:
                if owner is not None:
                    how += "; this note is a duplicate of it, with writing of its own to keep"
                report.findings.append(
                    Finding("relink", info.path, target, found, owner.path if owner else None, how)
                )
            else:
                kind = "drop" if drop_missing else "missing"
                report.findings.append(Finding(kind, info.path, target, reason=how))

    report.index = rebuild_index(
        vault, State(config.state_root), [n for n in notes if n.path not in removed], dry_run=True
    )
    return report


# ---- applying ----------------------------------------------------------------


def _matches(written: str, target: str) -> bool:
    return unquote(written.strip()) == target


def retarget(content: str, target: str, replacement: str | None) -> str:
    """Point every link to `target` at `replacement` - or remove it, given None."""
    frontmatter = FRONTMATTER_RE.match(content)
    head, body = (content[: frontmatter.end()], content[frontmatter.end() :]) if frontmatter else ("", content)

    def front(line: str) -> str | None:
        key, sep, raw = line.partition(":")
        if sep and key.strip() in LINK_KEYS and not line.startswith(" "):
            value = _scalar(raw)
            if isinstance(value, str) and _matches(value, target):
                if replacement is None:
                    return None
                escaped = replacement.replace("\\", "\\\\").replace('"', '\\"')
                return f'{key}: "{escaped}"'
        return line

    if head:
        lines = [front(line) for line in head.split("\n")]
        head = "\n".join(line for line in lines if line is not None)

    def wiki(match: re.Match[str]) -> str:
        if not _matches(match.group(2), target):
            return match.group(0)
        if replacement is None:
            return ""
        return f"{match.group(1)}{replacement}{match.group(3)}"

    def markdown(match: re.Match[str]) -> str:
        if not _matches(match.group(3), target):
            return match.group(0)
        if replacement is None:
            return ""
        open_, close = match.group(2), match.group(4)
        if not open_ and " " in replacement:
            open_, close = "<", ">)"
        return f"{match.group(1)}{open_}{replacement}{close}"

    new_body = WIKILINK_SUB_RE.sub(wiki, body)
    new_body = MDLINK_SUB_RE.sub(markdown, new_body)
    if replacement is None:
        # An embed on a line of its own leaves a blank line behind; fold it.
        new_body = re.sub(r"\n{3,}", "\n\n", new_body)
    return head + new_body


def apply(report: RepairReport, config: Config) -> RepairReport:
    """Make the changes `plan` found. Returns the same report, updated."""
    vault = Vault(config)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trash = config.state_root / TRASH_DIR / stamp

    by_note: dict[Path, list[Finding]] = {}
    for finding in report.findings:
        by_note.setdefault(finding.note, []).append(finding)

    for note, findings in by_note.items():
        try:
            if any(finding.kind == "remove" for finding in findings):
                destination = trash / note.relative_to(vault.root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(note), destination)
                report.trash = trash
                continue
            content = note.read_text(encoding="utf-8", errors="replace")
            changed = content
            for finding in findings:
                if finding.kind == "relink" and finding.found is not None:
                    changed = retarget(changed, finding.target, vault.wikilink(finding.found))
                elif finding.kind == "drop":
                    changed = retarget(changed, finding.target, None)
            if changed != content:
                note.write_text(changed, encoding="utf-8")
        except Exception as exc:  # keep mending the rest of the vault
            log.exception("failed to repair %s", note)
            for finding in findings:
                finding.kind, finding.error = "failed", f"{type(exc).__name__}: {exc}"

    state = State(config.state_root)
    report.index = rebuild_index(vault, state, _read_notes(vault))
    state.save()
    return report


# ---- the index ---------------------------------------------------------------


def rebuild_index(
    vault: Vault, state: State, notes: list[NoteInfo], dry_run: bool = False
) -> IndexChanges:
    """Bring `.scanvault/index.json` back in line with the notes on disk.

    An entry is the source hash of a filed document and where it went. It goes
    stale when a note moves or is deleted, and a stale entry is worse than none:
    it is what makes `ingest` say "already filed" about a document that is not
    in the vault any more.
    """
    changes = IndexChanges()
    wanted: dict[str, dict[str, Any]] = {}
    for info in notes:
        digest = info.frontmatter.get("source_hash")
        if not isinstance(digest, str) or not digest:
            continue
        wanted[digest] = {
            "note": info.path.relative_to(vault.root).as_posix(),
            "attachment": (
                info.attachment.relative_to(vault.root).as_posix() if info.attachment else None
            ),
            "source": info.frontmatter.get("source_file"),
            "title": str(info.frontmatter.get("title") or ""),
            "category": str(info.frontmatter.get("category") or ""),
        }

    for digest, entry in list(state.documents.items()):
        if digest in wanted:
            continue
        note = entry.get("note")
        if isinstance(note, str) and (vault.root / note).is_file():
            continue  # a note we could not read the hash of; leave it be
        changes.dropped += 1
        if not dry_run:
            state.forget(digest)

    for digest, fields in wanted.items():
        current = state.get(digest)
        if current is None:
            changes.added += 1
        elif any(current.get(key) != fields[key] for key in ("note", "attachment")):
            changes.updated += 1
        else:
            continue
        if not dry_run:
            merged = {**(current or {}), **fields}
            merged.pop("processed_at", None)
            state.record(digest, **merged)
    return changes
