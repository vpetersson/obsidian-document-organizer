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


def fold(text: str) -> str:
    """Lowercase and strip diacritics.

    Tesseract without the Swedish language pack does not garble å ä ö, it drops
    the diacritics - "Förfallodatum" comes out as "Forfallodatum". Folding both
    sides means the rules still fire on a badly OCR'd Swedish document.
    """
    normalised = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in normalised if not unicodedata.combining(char))


_ISO_DATE = re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")
# 03/04/2024, 3.4.2024, 13-04-2024 - the order of the first two is a guess.
_LITTLE_ENDIAN = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(value: object, day_first: bool = True) -> date | None:
    """Best-effort date parsing for whatever the model or a filename gives us.

    `03/04/2024` is genuinely ambiguous, so the caller says which way round it
    reads; either way `13/04/2024` is the 13th, because the other reading is not
    a date at all. Getting that wrong used to mean the document had no date.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    match = _ISO_DATE.search(text)
    if match:
        found = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if found:
            return found

    match = _LITTLE_ENDIAN.search(text)
    if match:
        first, second, year = (int(match.group(index)) for index in (1, 2, 3))
        orders = [(first, second), (second, first)]
        if not day_first:
            orders.reverse()
        for day, month in orders:
            found = _safe_date(year, month, day)
            if found:
                return found
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


# Names scanner apps give their output, plus the clock they stamp on it.
_SCANNER_TOKENS = re.compile(
    r"\b(scanbot|scanpro|swiftscan|camscanner|genius\s*scan|adobe\s*scan|scanned?\s+documents?"
    r"|scan|scanned|img|image|dsc|doc|document|untitled|new)\b",
    re.I,
)
_TIME_FRAGMENT = re.compile(r"\b\d{1,2}[.:]\d{2}\s*(am|pm)?\b", re.I)
_DATE_FRAGMENT = re.compile(
    rf"\b(\d{{4}}[-._]\d{{2}}[-._]\d{{2}}|\d{{8}}|({_MONTH_NAMES})[a-z]*\s+\d{{1,2}},?\s*\d{{4}}"
    rf"|\d{{1,2}}\s+({_MONTH_NAMES})[a-z]*\s+\d{{4}})\b",
    re.I,
)
_COPY_SUFFIX = re.compile(r"[\s_-]*(\(\d+\)|-\s*\d+|copy(\s*\d+)?)$", re.I)


def clean_document_name(stem: str) -> str:
    """Strip scanner noise out of a filename so what is left can be a title.

    "SwiftScan Feb 7, 2021 11.45 AM" has nothing in it worth keeping; "Boiler
    service 2019-04-02 - 1" reduces to "Boiler service".
    """
    name = re.sub(r"[_]+", " ", stem)
    name = _DATE_FRAGMENT.sub(" ", name)
    name = _TIME_FRAGMENT.sub(" ", name)
    name = _SCANNER_TOKENS.sub(" ", name)
    name = _COPY_SUFFIX.sub("", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -–—_.,")
    # A couple of stray characters, or a bare number, is not a title.
    if len(name) < 3 or not re.search(r"[^\W\d_]{3}", name):
        return ""
    return name


def file_created_date(path: Path) -> date | None:
    """The oldest timestamp the filesystem has for this file.

    Whichever of birth time and mtime is older, because copying a document into
    a vault resets its birth time to now while carrying its mtime across - and a
    file cannot predate the document inside it. Taking the newer one is how an
    old letter ends up dated today.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    stamps = [stat.st_mtime]
    birthtime = getattr(stat, "st_birthtime", None)
    if birthtime:
        stamps.append(birthtime)
    stamp = min(stamps)
    try:
        return datetime.fromtimestamp(stamp).date()
    except (OverflowError, OSError, ValueError):
        return None


# Link markup a title must never carry: a note titled "![[Scan Page 196.jpg]]"
# is a filename with brackets in it, and Obsidian renders it as a broken embed.
_LINK_MARKUP = re.compile(r"!?\[\[[^\]]*\]\]|!?\[[^\]]*\]\([^)]*\)")


def clean_title(value: str, max_length: int = 120) -> str:
    """A title fit to put in frontmatter and in a filename.

    OCR doubles spaces, and a line lifted out of a note body can be markup
    rather than words.
    """
    without_markup = _LINK_MARKUP.sub(" ", value or "")
    collapsed = re.sub(r"\s+", " ", without_markup).strip(" -–—_.,:;")
    return collapsed[:max_length].strip()


def truncate_words(text: str, max_chars: int) -> str:
    """Clip text at a word boundary so the model never sees a half word."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    space = cut.rfind(" ")
    if space > max_chars * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "\n[...truncated...]"
