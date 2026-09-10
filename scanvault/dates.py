"""Finding the date a document was written, inside the document.

A letter's date is printed on it, and it is nearly always labelled: "Date",
"Datum", "Fakturadatum", "Invoice date". Everything else that looks like a date
is a distraction - the due date, a coverage period, a date of birth, the year a
form was designed - so this module does not take the first date it sees. It
collects every date in the text, scores each by the words in front of it, and
throws away the ones that are labelled as something other than the document's
own date.

Both languages are handled together, because a Swedish invoice says "Datum" and
"Faktura" and then prints the amount in English half the time anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .util import fold

# Written months in English and Swedish. "maj" and "okt" are the Swedish
# spellings that differ from the English abbreviation; the rest overlap.
MONTH_NAMES: dict[str, int] = {
    "january": 1, "januari": 1, "jan": 1,
    "february": 2, "februari": 2, "feb": 2,
    "march": 3, "mars": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5, "maj": 5,
    "june": 6, "juni": 6, "jun": 6,
    "july": 7, "juli": 7, "jul": 7,
    "august": 8, "augusti": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oktober": 10, "oct": 10, "okt": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))

# Labels that mean "this is the date the document was written". Ordered most
# specific first, and folded, so a Swedish document OCR'd without the language
# pack ("Fakturadatum" -> "Fakturadatum", "Utfärdat" -> "Utfardat") still hits.
STRONG_LABELS: tuple[str, ...] = (
    "invoice date", "date of invoice", "fakturadatum", "faktura datum",
    "statement date", "date of issue", "issue date", "issued on", "issued",
    "date of statement", "utfardat", "utfardad", "utfardandedatum",
    "dokumentdatum",
    "receipt date", "kvittodatum", "purchase date", "kopdatum", "inkopsdatum",
    "transaction date", "transaktionsdatum", "order date", "orderdatum",
    "date of this letter", "letter date", "brevdatum", "datering", "daterad",
    "utbetalningsdatum", "lonedatum", "bokforingsdatum", "reg date",
)
# Weaker, but still a label rather than a stray number.
LABELS: tuple[str, ...] = (
    "date", "datum", "dated", "den", "dag", "registrerad",
    "signed", "underskrivet",
    # A print date is the day someone hit print, which for a reprinted
    # statement is not the day it was written - so it is a label, not a
    # strong one, and "Fakturadatum" beats it.
    "printed", "printed on", "utskriftsdatum", "utskrivet", "utskriven",
)
# A date introduced by any of these is emphatically not the document's date.
REJECTED_LABELS: tuple[str, ...] = (
    "due", "due date", "payment due", "due by", "pay by", "payable by",
    "forfallodatum", "forfallodag", "sista betalningsdag", "betalas senast",
    "betalningsdag", "expiry", "expires", "expiry date", "expiration",
    "valid until", "valid to", "valid from", "valid between", "giltig till",
    "giltig fran", "giltig t o m", "galler till", "galler fran", "gallande fran",
    "date of birth", "birth date", "born", "dob", "fodelsedatum", "fodd",
    "personnummer", "renewal date", "renews", "fornyelse", "next payment",
    "next due", "next statement", "period", "perioden", "avser perioden",
    "coverage", "from", "till och med", "t o m", "fran och med", "f o m",
    "as at", "as of", "closing date", "cut-off", "deadline", "senast",
)

# How far back to look for a label. One short line, so a date on its own line
# picks up "Fakturadatum:" above it but not a paragraph three lines up.
_LABEL_WINDOW = 40
# Anything joining two dates into a range: "1 Jan 2024 - 31 Dec 2024".
_RANGE_JOIN = re.compile(r"^[\s]*(-|–|—|to|till|t\.?o\.?m\.?|until|through)[\s]*$", re.I)


@dataclass(frozen=True)
class Candidate:
    """One date found in the text, with what was written in front of it."""

    value: date
    start: int
    end: int
    label: str
    weak: bool = False

    @property
    def labelled(self) -> bool:
        return bool(self.label)


def _safe(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(raw: str) -> int:
    """Expand a two-digit year the way a receipt printer means it."""
    value = int(raw)
    if len(raw) == 4:
        return value
    return 2000 + value if value < 70 else 1900 + value


def _month(name: str) -> int | None:
    folded = fold(name).rstrip(".")
    return MONTH_NAMES.get(folded)


# 2024-05-02, 2024/05/02, 2024.05.02
_ISO = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
# 02/05/2024, 2.5.24, 02-05-2024 - which number is the day is the caller's call.
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})(?!\d)")
# 2 May 2024, 2nd May, 2024, 2 maj 2024, den 2 maj 2024
_DAY_MONTH = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th|:e)?[\s.,-]+({_MONTH})\.?[\s.,-]+(\d{{4}})(?!\d)", re.I
)
# May 2, 2024 / May 2nd 2024
_MONTH_DAY = re.compile(
    rf"({_MONTH})\.?[\s.,-]+(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?[\s.,-]+(\d{{4}})(?!\d)", re.I
)
# "May 2024" - a month with no day, which is what statements are dated by.
_MONTH_YEAR = re.compile(rf"({_MONTH})\.?[\s,]+(\d{{4}})(?!\d)", re.I)


def _label_before(text: str, start: int) -> tuple[str, bool]:
    """The label introducing the date at `start`, and whether it disqualifies it.

    Only the nearest label counts: on "Fakturadatum 2024-05-02 Förfallodatum
    2024-06-01" each date is introduced by the word directly in front of it.
    """
    window = fold(text[max(0, start - _LABEL_WINDOW) : start])
    # A blank line means the label belongs to something else.
    window = window.rsplit("\n\n", 1)[-1]
    best_key = (-1, -1)
    best_label = ""
    best_rejected = False
    for group, rejected in ((STRONG_LABELS, False), (LABELS, False), (REJECTED_LABELS, True)):
        for label in group:
            position = window.rfind(label)
            if position < 0:
                continue
            # Whole words only: "dated" must not match inside "validated", and
            # the tail has to be punctuation or space, not more letters.
            before = window[position - 1] if position else " "
            after = window[position + len(label) :]
            if before.isalnum() or re.match(r"[a-z0-9]", after):
                continue
            # Nearest to the date wins, and where two labels end together the
            # longer one is the real one: "due date" is not "date".
            key = (position + len(label), len(label))
            if key > best_key:
                best_key, best_label, best_rejected = key, label, rejected
    return best_label, best_rejected


def _find_all(text: str, day_first: bool) -> list[tuple[int, int, date, bool]]:
    """Every date in the text as (start, end, value, weak), longest match first."""
    found: list[tuple[int, int, date, bool]] = []
    taken: list[tuple[int, int]] = []

    def claim(start: int, end: int) -> bool:
        if any(start < other_end and end > other_start for other_start, other_end in taken):
            return False
        taken.append((start, end))
        return True

    def collect(pattern: re.Pattern, build, weak: bool = False) -> None:
        for match in pattern.finditer(text):
            value = build(match)
            if value is None or not claim(match.start(), match.end()):
                continue
            found.append((match.start(), match.end(), value, weak))

    collect(_ISO, lambda m: _safe(int(m[1]), int(m[2]), int(m[3])))
    collect(
        _DAY_MONTH,
        lambda m: (lambda month: _safe(_year(m[3]), month, int(m[1])) if month else None)(
            _month(m[2])
        ),
    )
    collect(
        _MONTH_DAY,
        lambda m: (lambda month: _safe(_year(m[3]), month, int(m[2])) if month else None)(
            _month(m[1])
        ),
    )

    def numeric(match: re.Match) -> date | None:
        first, second, year = int(match[1]), int(match[2]), _year(match[3])
        orders = [(first, second), (second, first)]
        if not day_first:
            orders.reverse()
        for day, month in orders:
            value = _safe(year, month, day)
            if value:
                return value
        return None

    collect(_NUMERIC, numeric)
    collect(
        _MONTH_YEAR,
        lambda m: (lambda month: _safe(_year(m[2]), month, 1) if month else None)(_month(m[1])),
        weak=True,
    )
    found.sort(key=lambda item: item[0])
    return found


def _is_range(text: str, left: tuple[int, int, date, bool], right: tuple[int, int, date, bool]) -> bool:
    """True when the two dates are the ends of a period, not two dates."""
    return bool(_RANGE_JOIN.match(text[left[1] : right[0]]))


def find_dates(text: str, day_first: bool = True, today: date | None = None) -> list[Candidate]:
    """Every plausible date in the text, with its label, in reading order.

    Dates that are one end of a range are dropped: a document covering
    "1 Jan 2024 - 31 Dec 2024" is not dated either of those days.
    """
    if not text:
        return []
    horizon = today or date.today()
    raw = _find_all(text, day_first)
    in_range = set()
    for index in range(len(raw) - 1):
        if _is_range(text, raw[index], raw[index + 1]):
            in_range.add(index)
            in_range.add(index + 1)

    candidates: list[Candidate] = []
    for index, (start, end, value, weak) in enumerate(raw):
        if index in in_range:
            continue
        if not date(1900, 1, 1) <= value <= horizon:
            continue
        label, rejected = _label_before(text, start)
        if rejected:
            continue
        candidates.append(Candidate(value, start, end, label, weak))
    return candidates


def _score(candidate: Candidate, length: int) -> tuple[int, int]:
    """Rank a candidate: labelled beats unlabelled, early beats late."""
    if candidate.label in STRONG_LABELS:
        rank = 3
    elif candidate.label:
        rank = 2
    else:
        rank = 1
    if candidate.weak:
        rank -= 1
    # Letterheads are at the top; boilerplate ("© 2019", form revision dates)
    # is at the bottom.
    return (rank, length - candidate.start)


def date_from_text(
    text: str, day_first: bool = True, today: date | None = None
) -> tuple[date, str] | None:
    """The document's own date, and the label that identified it.

    Returns None when the text carries no date we would stand behind.
    """
    candidates = find_dates(text, day_first, today)
    if not candidates:
        return None
    best = max(candidates, key=lambda candidate: _score(candidate, len(text)))
    return best.value, best.label


def strong_date(text: str, day_first: bool = True, today: date | None = None) -> date | None:
    """The date under an unambiguous label, or None.

    Used to second-guess a date the model returned that appears nowhere in the
    document it read.
    """
    for candidate in find_dates(text, day_first, today):
        if candidate.label in STRONG_LABELS and not candidate.weak:
            return candidate.value
    return None
