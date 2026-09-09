"""Phase 2a: turn document text into structured metadata via ollama/Qwen."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .cache import ClassificationCache
from .config import Config
from .extract import pdf_creation_date
from .llm import LlmError, OllamaClient
from .util import (
    clean_document_name,
    date_from_filename,
    file_created_date,
    parse_date,
    slugify,
    truncate_words,
)

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a meticulous document archivist. You are given the raw OCR text of a "
    "single scanned document. Extract factual metadata only from that text. Never "
    "invent a value: if something is not stated, leave it null or empty. Answer "
    "with a single JSON object and nothing else."
)


def _schema(categories: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "category": {"type": "string", "enum": categories},
            "document_date": {"type": "string"},
            "correspondent": {"type": "string"},
            "summary": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "subjects": {"type": "array", "items": {"type": "string"}},
            "language": {"type": "string"},
            "reference": {"type": "string"},
            "amount": {"type": "string"},
            "currency": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["title", "category", "summary", "tags"],
    }


def build_prompt(
    text: str, config: Config, filename: str | None = None, folder: str | None = None
) -> str:
    categories = ", ".join(config.categories)
    parts = [
        "Classify the document below.",
        "",
        "Rules:",
        f"- category MUST be exactly one of: {categories}.",
        "- title: a short human title, no dates, no file extension (max 12 words).",
        "- document_date: the date the document itself carries (issue/statement/"
        "letter date), as YYYY-MM-DD. Not today's date. Null if absent.",
        "- correspondent: the organisation or person the document is from.",
        "- summary: 1-3 sentences on what this document is and why it matters.",
        "- tags: 4-8 lowercase keywords, dashed, no '#'. Tag what the document IS "
        "(mortgage-statement, council-tax, payslip, insurance-policy, tax-return), "
        "what it is ABOUT (a topic like mortgage, pension, car, renovation), and the "
        "kind of sender (bank, tax-authority, local-government, utility, insurer). "
        "Do not tag the date, the file format, or the word 'document'.",
        "- subjects: the specific things this document concerns, as they appear in "
        "the text - a property address, a vehicle registration, an account holder, a "
        "policy or account number. These become tags, so someone can search for the "
        "property and find everything about it. Empty list if the document is not "
        "about a specific thing.",
        "- reference: invoice/account/case number if present.",
        "- amount + currency: the document's headline total, if it has one.",
        "- confidence: 0.0-1.0, how sure you are about category and title.",
        f"- Write title and summary in {config.language_hint}.",
        "",
    ]
    if filename:
        parts.append(f"Original filename: {filename}")
    if folder:
        # Where someone filed it by hand is a strong hint about what it is.
        parts.append(f"Folder it was found in: {folder}")
    parts += ["Document text:", '"""', truncate_words(text, config.llm.max_chars), '"""']
    return "\n".join(parts)


@dataclass
class DocumentMeta:
    title: str
    category: str
    summary: str = ""
    document_date: date | None = None
    correspondent: str = ""
    tags: list[str] = field(default_factory=list)
    # What the document is about: a property, a vehicle, an account holder.
    subjects: list[str] = field(default_factory=list)
    language: str = ""
    reference: str = ""
    amount: str = ""
    currency: str = ""
    confidence: float | None = None
    classifier: str = "llm"  # "llm" | "heuristic"
    # Where document_date came from: document | filename | pdf-metadata |
    # file-created. Empty when the document is undated.
    date_source: str = ""
    # Where the title came from: model | text | filename. Worth recording,
    # because a title lifted from a scanner's filename is not a title.
    title_source: str = "model"
    # PARA bucket: project | area | resource | archive. Scans default to the
    # archive; a human moves a note elsewhere by editing its frontmatter.
    para: str = "archive"

    @property
    def year(self) -> str:
        return f"{self.document_date.year}" if self.document_date else "undated"

    @property
    def month(self) -> str:
        return f"{self.document_date.month:02d}" if self.document_date else "00"

    @property
    def date_str(self) -> str:
        return self.document_date.isoformat() if self.document_date else "undated"

    @property
    def document_name(self) -> str:
        """The document's name from its metadata: "Acme Ltd - Invoice INV-1234"."""
        title = self.title.strip()
        correspondent = self.correspondent.strip()
        if not correspondent:
            return title
        if correspondent.lower() in title.lower():
            return title
        return f"{correspondent} - {title}" if title else correspondent

    def placeholders(self) -> dict[str, str]:
        return {
            "name": self.document_name,
            "category": self.category,
            "year": self.year,
            "month": self.month,
            "date": self.date_str,
            "title": self.title,
            "slug": slugify(self.title),
            "correspondent": self.correspondent or "unknown",
            "para": self.para,
        }


@lru_cache(maxsize=8)
def _compiled_rules(rules: tuple[tuple[str, tuple[str, ...]], ...]) -> list[tuple[str, re.Pattern]]:
    """One regex per tag, matching on word boundaries.

    Substring matching turns "payee" into a tax document and "risk" into an
    investment one, so keywords have to end where words end.
    """
    compiled = []
    for tag, keywords in rules:
        if not keywords:
            continue
        alternatives = "|".join(re.escape(keyword) for keyword in sorted(keywords, key=len, reverse=True))
        # The group matters: without it the alternation would bind looser than
        # the look-arounds and only guard the first and last keyword.
        compiled.append(
            (
                tag,
                re.compile(
                    rf"(?<![a-z0-9æøåäöü])(?:{alternatives})(?![a-z0-9æøåäöü])", re.I
                ),
            )
        )
    return compiled


def rule_tags(config: Config, *haystacks: str) -> list[str]:
    """Tags the keyword rules insist on, whatever the model thought.

    A letter from a tax authority is about taxes even when the model called it
    "Correspondence", and that is exactly the search someone will run.
    """
    text = "\n".join(part for part in haystacks if part)
    frozen = tuple((tag, tuple(keywords)) for tag, keywords in config.tags.rules.items())
    return [tag for tag, pattern in _compiled_rules(frozen) if pattern.search(text)]


def _clean_tags(raw: Any, config: Config, meta_extra: list[str]) -> list[str]:
    """Assemble the tag list, keeping the deterministic ones first."""
    tags: list[str] = []
    for value in list(config.tags.base) + meta_extra + (raw if isinstance(raw, list) else []):
        if not isinstance(value, str):
            continue
        tag = slugify(value.lstrip("#"), max_length=40)
        if tag and tag not in tags:
            tags.append(tag)
    return tags[: max(len(config.tags.base) + len(meta_extra), config.tags.max_tags)]


def derived_tags(meta: DocumentMeta, config: Config, text: str = "") -> list[str]:
    """Everything we can tag without asking the model: category, year, sender,
    what the document is about, and whatever the keyword rules match."""
    derived = [slugify(meta.category)]
    if config.tags.year_tag and meta.document_date:
        derived.append(f"year-{meta.document_date.year}")
    if config.tags.correspondent_tag and meta.correspondent:
        derived.append(slugify(meta.correspondent, max_length=40))
    if config.tags.subject_tags:
        derived.extend(slugify(subject, max_length=40) for subject in meta.subjects[:3])
    derived.extend(rule_tags(config, meta.title, meta.correspondent, meta.summary, text[:4000]))
    return [tag for tag in derived if tag]


def _match_category(value: Any, config: Config) -> str:
    if isinstance(value, str):
        wanted = value.strip().lower()
        for category in config.categories:
            if category.lower() == wanted:
                return category
        for category in config.categories:
            if wanted and (wanted in category.lower() or category.lower() in wanted):
                return category
    return config.categories[-1] if config.categories else "Other"


def from_response(
    data: dict[str, Any], config: Config, fallback_title: str, text: str = ""
) -> DocumentMeta:
    """Normalise a raw model response into a DocumentMeta we can trust."""
    model_title = str(data.get("title") or "").strip()
    title = model_title or fallback_title
    category = _match_category(data.get("category"), config)
    correspondent = str(data.get("correspondent") or "").strip()
    confidence = data.get("confidence")
    subjects = [
        str(item).strip()[:80]
        for item in (data.get("subjects") or [])
        if isinstance(item, str) and item.strip()
    ]
    meta = DocumentMeta(
        title=re.sub(r"\s{2,}", " ", title)[:120],
        category=category,
        summary=str(data.get("summary") or "").strip(),
        document_date=parse_date(data.get("document_date")),
        correspondent=correspondent[:120],
        title_source="model" if model_title else "filename",
        subjects=subjects[:5],
        language=str(data.get("language") or "").strip(),
        reference=str(data.get("reference") or "").strip()[:80],
        amount=str(data.get("amount") or "").strip()[:40],
        currency=str(data.get("currency") or "").strip()[:8],
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
    )
    meta.tags = _clean_tags(data.get("tags"), config, derived_tags(meta, config, text))
    return meta


def resolve_date(meta: DocumentMeta, source: Path | None, config: Config) -> DocumentMeta:
    """Fill in a missing date from the file itself, in the configured order.

    A date printed on the document always wins. Everything else is a guess, so
    the note records which guess it was.
    """
    if meta.document_date:
        meta.date_source = "document"
        return meta
    if source is None:
        return meta

    finders = {
        "filename": lambda path: date_from_filename(path.name),
        "pdf-metadata": pdf_creation_date,
        "file-created": file_created_date,
    }
    for name in config.dates.fallbacks:
        finder = finders.get(name)
        if finder is None:
            log.warning("unknown date fallback %r; skipping", name)
            continue
        found = finder(source)
        if found:
            meta.document_date = found
            meta.date_source = name
            log.debug("%s: date %s taken from %s", source.name, found, name)
            return meta
    return meta


HEURISTIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Invoices", ("invoice", "faktura", "rechnung", "amount due", "bill to")),
    ("Receipts", ("receipt", "kvitto", "thank you for your purchase", "subtotal")),
    ("Contracts", ("agreement", "contract", "terms and conditions", "hereby agree")),
    ("Banking", ("account statement", "bank", "iban", "sort code", "balance")),
    ("Taxes", ("tax", "vat", "hmrc", "irs", "skatteverket")),
    ("Insurance", ("insurance", "policy number", "premium", "claim")),
    ("Medical", ("patient", "diagnosis", "prescription", "clinic", "doctor")),
    ("Utilities", ("electricity", "water usage", "gas bill", "broadband", "meter reading")),
    ("Employment", ("payslip", "salary", "employment", "employer")),
    ("Government", ("ministry", "municipal", "council", "authority", "passport")),
]


def heuristic(text: str, config: Config, fallback_title: str) -> DocumentMeta:
    """Offline classifier used when ollama is unavailable."""
    lowered = text.lower()
    category = config.categories[-1] if config.categories else "Other"
    for candidate, keywords in HEURISTIC_RULES:
        if candidate in config.categories and any(word in lowered for word in keywords):
            category = candidate
            break
    first_line = next(
        (line.strip() for line in text.splitlines() if len(line.strip()) > 8), ""
    )
    title = (first_line[:80] or fallback_title).strip()
    title_source = "text" if first_line else "filename"
    meta = DocumentMeta(
        title=title,
        category=category,
        summary="",
        document_date=parse_date(text[:4000]),
        confidence=0.2,
        classifier="heuristic",
        title_source=title_source,
    )
    meta.tags = _clean_tags(["unclassified"], config, derived_tags(meta, config, text))
    return meta


def classify(
    text: str,
    config: Config,
    client: OllamaClient | None,
    source: Path | None = None,
    cache: ClassificationCache | None = None,
) -> DocumentMeta:
    """Classify one document, degrading to heuristics when configured to."""
    # A scanner's filename is noise, not a title: strip what it stamps on and
    # only keep what is left if there is something to keep.
    fallback_title = (clean_document_name(source.stem) if source else "") or "Untitled document"
    if not text.strip():
        meta = heuristic("", config, fallback_title)
        meta.tags = _clean_tags(["empty-text"], config, [])
        return meta
    if client is None:
        return heuristic(text, config, fallback_title)

    cached = cache.get(text) if cache is not None else None
    if cached is not None:
        return from_response(cached, config, fallback_title, text)

    try:
        data = client.chat_json(
            SYSTEM_PROMPT,
            build_prompt(
                text,
                config,
                source.name if source else None,
                source.parent.name if source else None,
            ),
            schema=_schema(config.categories),
        )
    except LlmError as exc:
        if not config.llm.fallback_to_heuristics:
            raise
        log.warning("classification fell back to heuristics: %s", exc)
        return heuristic(text, config, fallback_title)
    if cache is not None:
        cache.put(text, data)
    return from_response(data, config, fallback_title, text)
