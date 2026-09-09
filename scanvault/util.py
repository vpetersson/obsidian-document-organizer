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


MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
_SEP = r"[\s._,-]+"
# Digits are separated by punctuation, not word boundaries: "_" is a word
# character, so "602334_03_Jul_2025" needs an explicit "not a digit" look-around.
_D = r"(?<!\d)"
# "Mar 5, 2017", "March 5 2017"
_MONTH_FIRST = re.compile(
    rf"({_MONTH_NAMES})[a-z]*{_SEP}{_D}(\d{{1,2}})(?:st|nd|rd|th)?{_SEP}(\d{{4}})(?!\d)", re.I
)
# "5 Mar 2017", "03_Jul_2025"
_DAY_FIRST = re.compile(
    rf"{_D}(\d{{1,2}})(?!\d){_SEP}({_MONTH_NAMES})[a-z]*{_SEP}(\d{{4}})(?!\d)", re.I
)
# "2016-09-08", "2016_09_08"
_ISO_LOOSE = re.compile(r"(?<!\d)(\d{4})[._-](\d{2})[._-](\d{2})(?!\d)")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def date_from_filename(name: str) -> date | None:
    """Pull a date out of a filename, e.g. "receipt Mar 5, 2017.pdf".

    Only patterns carrying a four-digit year are accepted, so an account number
    or a house number cannot masquerade as a date.
    """
    stem = Path(name).stem
    match = _ISO_LOOSE.search(stem)
    if match:
        found = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if found:
            return found
    match = _MONTH_FIRST.search(stem)
    if match:
        found = _safe_date(
            int(match.group(3)), MONTHS[match.group(1).lower()[:3]], int(match.group(2))
        )
        if found:
            return found
    match = _DAY_FIRST.search(stem)
    if match:
        found = _safe_date(int(match.group(3)), MONTHS[match.group(2).lower()[:3]], int(match.group(1)))
        if found:
            return found
    return parse_date(stem)


def file_created_date(path: Path) -> date | None:
    """The file's creation date where the platform records one, else mtime."""
    try:
        stat = path.stat()
    except OSError:
        return None
    stamp = getattr(stat, "st_birthtime", None) or stat.st_mtime
    try:
        return datetime.fromtimestamp(stamp).date()
    except (OverflowError, OSError, ValueError):
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
