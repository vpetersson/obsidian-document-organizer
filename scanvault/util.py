"""Small helpers shared across the pipeline."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

_SLUG_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_SPACE = re.compile(r"[\s_]+")
_FS_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def slugify(value: str, max_length: int = 80) -> str:
    """Lowercase, dash-separated slug suitable for tags and file stems."""
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = _SLUG_STRIP.sub("", value)
    value = _SLUG_SPACE.sub("-", value.strip()).strip("-").lower()
    value = re.sub(r"-{2,}", "-", value)
    return value[:max_length].strip("-")


def safe_filename(value: str, max_length: int = 120, fallback: str = "untitled") -> str:
    """Keep the human-readable form but drop anything a filesystem dislikes."""
    value = unicodedata.normalize("NFC", value or "").replace("\n", " ")
    value = _FS_UNSAFE.sub("-", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" .-")
    value = value[:max_length].strip(" .-")
    return value or fallback


def unique_path(path: Path) -> Path:
    """Return `path`, or `name-2.ext`, `name-3.ext`... if it is already taken."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for counter in range(2, 1000):
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free filename near {path}")


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


_DATE_PATTERNS = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), (1, 2, 3)),
    (re.compile(r"\b(\d{4})/(\d{2})/(\d{2})\b"), (1, 2, 3)),
    (re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b"), (3, 1, 2)),
    (re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b"), (3, 2, 1)),
)


def parse_date(value: object) -> date | None:
    """Best-effort date parsing for whatever the model or a filename gives us."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for pattern, (y, m, d) in _DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return date(int(match.group(y)), int(match.group(m)), int(match.group(d)))
            except ValueError:
                continue
    return None


def truncate_words(text: str, max_chars: int) -> str:
    """Clip text at a word boundary so the model never sees a half word."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    space = cut.rfind(" ")
    if space > max_chars * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "\n[...truncated...]"
