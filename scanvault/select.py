"""Which of the files in a folder are documents, and when a screenshot was taken.

A scanner's inbox holds nothing but scans, so `ingest` takes everything it
finds there. A Desktop is the opposite: years of screenshots, the odd PDF
someone emailed, and a pile of app icons, exported logos and wallpapers that
are not documents at all. Pointing `ingest` at one means saying which part of
it you meant, which is what `--only` and `[source] include` are for.

Screenshots also need their date read off the filename. The program that took
the picture stamps the moment into the name, and that is the only honest record
of it: copying the file resets its creation time, and a screenshot of last
year's invoice is not a document from last year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence

from .config import SourceConfig
from .extract import IMAGE_SUFFIXES


# What Apple calls a screen capture, in the languages macOS ships. Only the
# word itself and the little word between the date and the time are translated
# - the shape is always "<name> <date> <at> <time>.png" - so matching the name
# and then a date and a time covers every localisation without needing a table
# of the word for "at". "Screen Shot" is what macOS wrote before Mojave, and
# what a good half of an old Desktop is still called.
DEFAULT_SCREENSHOT_NAMES: tuple[str, ...] = (
    "Screenshot",
    "Screen Shot",
    "Skärmavbild",              # sv
    "Skærmbillede",             # da
    "Skjermbilde",              # nb
    "Näyttökuva",               # fi
    "Bildschirmfoto",           # de
    "Schermafbeelding",         # nl
    "Capture d'écran",          # fr
    "Captura de pantalla",      # es, ca
    "Captura de Tela",          # pt-BR
    "Captura de ecrã",          # pt-PT
    "Schermata",                # it
    "Zrzut ekranu",             # pl
    "Snímek obrazovky",         # cs
    "Snímka obrazovky",         # sk
    "Képernyőkép",              # hu
    "Captură de ecran",         # ro
    "Ekran Resmi",              # tr
    "Στιγμιότυπο οθόνης",       # el
    "Снимок экрана",            # ru
    "Знімок екрана",            # uk
    "צילום מסך",                # he
    "لقطة الشاشة",               # ar
    "スクリーンショット",           # ja
    "截屏",                      # zh-Hans
    "螢幕快照",                   # zh-Hant
    "스크린샷",                   # ko
    "ภาพหน้าจอ",                  # th
    # Not Apple's, but it names its files the same way and it is the capture
    # tool most likely to be the one that wrote half a Mac's Desktop.
    "CleanShot",
)

_DATE = r"(?P<year>\d{4})[-._](?P<month>\d{2})[-._](?P<day>\d{2})"
_TIME = r"(?P<hour>\d{1,2})[.:_-](?P<minute>\d{2})(?:[.:_-](?P<second>\d{2}))?"
# English writes "4.15.22 PM" after the clock; Korean, Chinese and Japanese
# write theirs in front of it, so both ends are looked at. Whole words only -
# "um" is German for "at", and the "am" in "amount" is not a time of day.
_PM = frozenset({"pm", "오후", "下午", "午後"})
_AM = frozenset({"am", "오전", "上午", "午前"})
_WORDS = re.compile(r"[\s_.,-]+")
_MARKER = "|".join(sorted(_PM | _AM, key=len, reverse=True))
# Between the date and the time sits the local word for "at" - "at", "kl.",
# "um", "à", "a las" - or, in the CJK locales, nothing at all. Up to two short
# words, each of which has to end, so neither one long word nor a whole phrase
# can pose as one: "Screenshot 2024-05-02 report 14.30" is a document someone
# named, not a capture. A meridiem may follow them without a space, because
# Chinese and Japanese write "下午2.23.07".
_JOIN = (
    r"(?P<join>[\s_.,-]*(?:[^\W\d_]{1,4}\.?[\s_.,-]+){0,2}"
    rf"(?:(?:{_MARKER})(?![^\W\d_]))?[\s_.,-]*)"
)
# macOS writes a typographic apostrophe in "Capture d’écran"; a name typed or
# copied by hand often carries the ASCII one.
_APOSTROPHE = re.compile(r"['’]")


@dataclass(frozen=True)
class Screenshot:
    """What a screen capture's filename says about it."""

    taken_at: datetime
    # Whatever the name carries beyond the stamp - someone's own "boiler
    # warranty" appended to it, say. Usually empty.
    rest: str


@lru_cache(maxsize=8)
def _pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(
        _APOSTROPHE.sub("['’]", re.escape(name))
        # Longest first, so "Screen Shot" is not shadowed by a shorter name
        # that happens to be a prefix of it.
        for name in sorted(names, key=len, reverse=True)
    )
    return re.compile(
        rf"(?:{alternatives})[\s_-]*{_DATE}{_JOIN}{_TIME}\s*(?P<meridiem>[ap]\.?m\.?)?",
        re.IGNORECASE,
    )


def _meridiem(match: re.Match[str]) -> str:
    """Which half of the day the time is in, from either end of it.

    Empty when neither end says, which is a 24-hour clock or a name that
    carries no marker at all.
    """
    trailing = (match.group("meridiem") or "").replace(".", "").lower()
    if trailing:
        return trailing
    for word in _WORDS.split(match.group("join") or ""):
        folded = word.lower()
        if folded in _PM:
            return "pm"
        if folded in _AM:
            return "am"
    return ""


def read_screenshot_name(
    name: str, names: tuple[str, ...] = DEFAULT_SCREENSHOT_NAMES
) -> Screenshot | None:
    """Read a screen capture's filename, or None if it is not one.

    The name has to carry both a date and a time: that pair is what makes it a
    stamp rather than a word someone typed, and a file merely called
    `Screenshot.png` says nothing about when it was taken.
    """
    stem = Path(name).stem.strip()
    match = _pattern(tuple(names)).match(stem)
    if not match:
        return None
    hour = int(match.group("hour"))
    meridiem = _meridiem(match)
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    try:
        taken_at = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            hour,
            int(match.group("minute")),
            int(match.group("second") or 0),
        )
    except ValueError:  # 2024-13-40, or 25.00 on a clock
        return None
    return Screenshot(taken_at, stem[match.end() :].strip(" _-"))


def is_screenshot(path: Path, names: tuple[str, ...] = DEFAULT_SCREENSHOT_NAMES) -> bool:
    return read_screenshot_name(path.name, names) is not None


def screenshot_names(source: SourceConfig) -> tuple[str, ...]:
    """The built-in capture names plus whatever the config adds to them."""
    return DEFAULT_SCREENSHOT_NAMES + tuple(source.screenshot_names)


# What `--only` and `[source] include` accept. "images" includes screenshots;
# "screenshots" is the subset whose filename says when it was taken.
SELECTORS = ("all", "screenshots", "pdfs", "images")


def check_selectors(wanted: Sequence[str], where: str) -> list[str]:
    """Normalise a list of selectors, refusing one that is not a kind of file.

    Loudly, because the alternative is worse in both directions: a typo that
    widens the filter empties a Desktop into the vault, and one that narrows it
    to nothing looks exactly like a folder with no documents in it.
    """
    normalised = [str(part).strip().lower() for part in wanted]
    unknown = [part for part in normalised if part not in SELECTORS]
    if unknown or not normalised:
        raise ValueError(
            (f"unknown {where} value(s): {', '.join(unknown)}" if unknown else f"{where} is empty")
            + f"; expected any of {', '.join(SELECTORS)}"
        )
    return normalised


def parse_selectors(value: str) -> list[str]:
    """Parse `--only screenshots,pdfs`, rejecting anything that is not one."""
    return check_selectors([part for part in value.split(",") if part.strip()], "--only")


def _matches(path: Path, selector: str, names: tuple[str, ...]) -> bool:
    suffix = path.suffix.lower()
    if selector == "all":
        return True
    if selector == "pdfs":
        return suffix == ".pdf"
    if selector == "images":
        return suffix in IMAGE_SUFFIXES
    return is_screenshot(path, names)


def keeper(source: SourceConfig) -> Callable[[Path], bool]:
    """A predicate for `iter_documents`: is this file one of the ones asked for?

    Raises ValueError on an `include` that names something that is not a kind
    of file, rather than guessing what was meant.
    """
    wanted = check_selectors(source.include, "[source] include")
    if "all" in wanted:
        return lambda path: True
    names = screenshot_names(source)
    return lambda path: any(_matches(path, selector, names) for selector in wanted)


def describe(include: Sequence[str]) -> str:
    """What `include` asked for, for a message about finding none of it."""
    return "documents" if not include or "all" in include else ", ".join(include)
