r"""How readable is a document's text? Scoring OCR output so bad runs can be redone.

OCR fails quietly. A scan that came out as `1|\|\/O|CE Nr. 4?3` produces a note,
a title and a category exactly like a good one does - the pipeline has no way to
know it filed gibberish, and neither does anyone reading the vault until they
search for a word that is in the document and get nothing back.

This scores the text on how much of it looks like language, so `scanvault
re-ocr` can find the documents worth reading again. Everything here is
stdlib-only string work: no dictionary to install, no model to call, nothing
that leaves the machine.

The signals, in the order they carry weight:

* **word shape** - what fraction of the letter-only tokens could be words at
  all. A token with no vowel, five consonants in a row, a letter repeated four
  times, or capitals in the middle of it is OCR noise, not a word.
* **letters per character** - good text is mostly letters. Garbled scans are
  full of `|`, `/`, `~` and stray punctuation, because that is what the shapes
  of ruined glyphs resemble.
* **common words** - whether the text contains the words every European
  language is built out of. This is the plainest test there is: if there are no
  real words in a document, it is clearly gibberish. It cuts the other way too:
  a page of running prose with not one function word on it is not prose, even
  when every token on it is shaped like a word.
* **letters read as digits** - `lnv0lce numb3r` is the signature failure of a
  scan read at too low a resolution, and digits interleaved through a word are
  nothing else. Reference codes are not this: `GB123456789` is one run of
  letters and one of digits, not the two alternating.
* **how much came off the page** - when the page count is known, a full sheet
  of A4 that yielded sixty characters was not read, whether or not those sixty
  characters happen to be words. This one only ever caps the score; a document
  that really is one line stays a document that really is one line.

No one of these is sufficient alone. A bank statement is mostly figures and
proper nouns, so it scores nothing on common words and still reads perfectly; a
page of scanner noise can stumble into a real word by accident. So the first
three are blended into a score, the last two only ever cut it back, and a
document with neither a plausible word nor a common word anywhere in it is
floored at gibberish regardless of what the rest of the signals say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .util import fold

# Scoring is linear in the length of the text, and a 400-page scan has no more
# to tell us than its first few hundred kilobytes.
MAX_SAMPLE_CHARS = 250_000

# Alphanumeric runs. `[^\W_]` is "word character but not underscore", which is
# what splits "INV-1234" into a reference and a number rather than one token.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Folding strips diacritics, so å and ä arrive here as a, ö as o, ü as u.
_VOWELS = frozenset("aeiouy")
# "aaaa", "llll" - a glyph the engine saw four times over.
_REPEATED = re.compile(r"(.)\1{3,}")
# Five consonants in a row. Swedish and German manage four ("angstskt" is not a
# word, "höstskt" is not either); five is noise in every language this sees.
_CONSONANT_RUN = re.compile(r"[bcdfghjklmnpqrstvwxz]{5,}")
# Words this long are two words the engine ran together, or a barcode.
_MAX_WORD_LENGTH = 30
# "Ltd", "GmbH", "AB", "Nr" - vowel-less, capitalised, and on half the
# paperwork there is. Longer than this without a vowel is not an abbreviation.
_MAX_ABBREVIATION = 4

# The only letters worth treating as words on their own. English "a" and "i",
# and the Nordic "å" which folds to "a". Romance "e", "o" and "y" are words too,
# but a lone letter is also the commonest shape of OCR noise, and a page of
# stray glyphs must not read as a page of Italian conjunctions.
_SINGLE_LETTER_WORDS = frozenset("ai")

# Alternating runs of letters and digits. One run of each is a reference code;
# three or more is a word the engine read some of the letters out of.
_RUN_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)
_MIN_GARBLED_RUNS = 3

# Running text this letter-dense is prose, not a table of figures - and prose
# with not one function word in it did not come off the page intact. A
# statement that is mostly numbers is exempt, which is the point of the bar.
_PROSE_LETTER_RATIO = 0.6
# ...as long as there is enough of it that a function word was to be expected.
_MIN_TOKENS_FOR_COMMON = 15
# Where such a document is capped: below the default threshold, so it is offered
# for a re-read, and no lower, because the evidence is circumstantial.
_NO_COMMON_WORDS_CAP = 0.4

# Common words are only counted from this length up. "ce", "on" and "de" are
# real words, and they are also two glyphs of noise landing next to each other;
# at three letters that stops happening by accident.
_MIN_COMMON_LENGTH = 3


def _common_words() -> frozenset[str]:
    """Function words and paperwork words, folded the way tokens are.

    Deliberately multilingual and deliberately small. The point is not to
    identify the language - it is to answer "is any of this a word", which
    every one of these settles on its own. Single letters are left out: they
    turn up in noise too easily to mean anything.
    """
    words = """
    the of and to in is that for it as was with be by on not this are or from
    at which but have an had they you were their one all we can has there been
    if more when will would who so no its my your our please thank see also
    dear sincerely yours regards attached enclosed
    date total amount payment invoice number account page reference due paid
    balance address customer details period statement

    och att det som en pa ar av for med till den har de inte om ett men var jag
    vi du kan ska fran vid eller nar alla detta sig sa efter under over samt
    enligt kronor betala faktura datum belopp summa moms sida avser galler
    kundnummer ovrigt vanliga halsningar

    und der die das den dem ist nicht mit fur von auf zu ein eine sich wir sie
    bei aus dass oder werden wurde auch nach wenn kann sind rechnung betrag
    seite sehr geehrte freundlichen grussen

    le la les de des du et un une est pour dans que qui pas sur avec au aux
    vous nous par plus ce cette sont votre notre montant facture page
    cordialement madame monsieur

    el los las del en por para con se su sus son como mas este esta fecha
    importe pagina estimado atentamente

    il lo di che per non da al della come questo fattura data importo totale
    pagina gentile cordiali saluti

    het een van op te die met voor zijn aan ook als factuur bedrag totaal
    geachte vriendelijke groet

    er til af skal belob dato beloep side venlig hilsen med vennlig

    ja on ei se etta ovat mutta kuin oli tai myos lasku laskun paiva yhteensa
    sivu

    nao dos das uma fatura valor prezado atenciosamente

    nie jest sie oraz dla przez kwota razem strona numer
    """
    return frozenset(
        fold(word) for word in words.split() if len(word) >= _MIN_COMMON_LENGTH
    )


COMMON_WORDS = _common_words()


@dataclass(frozen=True)
class TextQuality:
    """What a piece of extracted text looks like, and whether to believe it."""

    score: float
    verdict: str  # empty | gibberish | poor | good | thin
    chars: int
    words: int
    word_ratio: float
    letter_ratio: float
    common_ratio: float
    garbled_ratio: float = 0.0
    # Characters per page, when the page count was known. None when it was not.
    density: float | None = None

    @property
    def readable(self) -> bool:
        """True when the text is worth keeping as it is."""
        return self.verdict in ("good", "thin")

    def describe(self) -> str:
        """One line, for a report: `0.08 gibberish (no common words)`."""
        return f"{self.score:.2f} {self.verdict} ({self.explain()})"

    def explain(self) -> str:
        """Why this text scored the way it did, in the reader's terms."""
        if self.verdict == "empty":
            return "no text at all"
        if self.verdict == "thin":
            return f"only {self.chars} characters, too little to judge"
        reasons = []
        if not self.words:
            reasons.append("no word-shaped tokens")
        elif self.word_ratio < 0.6:
            reasons.append(f"{self.word_ratio:.0%} of tokens look like words")
        if not self.common_ratio:
            reasons.append("no common words")
        if self.letter_ratio < 0.6:
            reasons.append(f"{self.letter_ratio:.0%} of characters are letters")
        if self.garbled_ratio >= 0.05:
            reasons.append(f"{self.garbled_ratio:.0%} of tokens have digits inside words")
        if self.density is not None and self.density < 250:
            reasons.append(f"only {self.density:.0f} characters a page came off it")
        if not reasons:
            return f"{self.word_ratio:.0%} word-shaped, {self.common_ratio:.0%} common words"
        return ", ".join(reasons)


def looks_like_word(token: str) -> bool:
    """True when `token` has the shape of a word in some language.

    Shape only: this cannot tell a real word from a plausible non-word, and it
    is not meant to. It tells `Förfallodatum` from `F0rfa||0d4tum`, which is the
    difference that matters when deciding whether a scan needs reading again.
    """
    folded = fold(token)
    if not folded.isalpha() or len(folded) > _MAX_WORD_LENGTH:
        return False
    if len(folded) == 1:
        return folded in _SINGLE_LETTER_WORDS
    if not _VOWELS & set(folded):
        # No vowel is the strongest single sign of noise - but "Ltd", "GmbH",
        # "AB" and "Nr" are on half the paperwork there is, and every one of
        # them starts with a capital. A vowel-less *lowercase* token is noise.
        return len(folded) <= _MAX_ABBREVIATION and token[:1].isupper()
    if _REPEATED.search(folded) or _CONSONANT_RUN.search(folded):
        return False
    # `lNVOlCE` - the engine read some letters as capitals and some as not.
    # An initial capital is normal and so is an acronym, so only capitals
    # *inside* an otherwise lowercase word count against it.
    if len(token) > 2 and not token.isupper():
        if sum(1 for char in token[1:] if char.isupper()) > 1:
            return False
    return True


def looks_garbled(token: str) -> bool:
    """True when digits run *through* a token rather than after it.

    `lnv0lce`, `numb3r`, `w1th1n` - the engine read some letters as the digits
    they resemble. `GB123456789`, `P60` and `2024b` are not that: a code is
    letters then digits, or digits then letters, and stops there.
    """
    if token.isalpha() or token.isdigit():
        return False
    return len(_RUN_RE.findall(token)) >= _MIN_GARBLED_RUNS


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_text(
    text: str, config: object | None = None, pages: int | None = None
) -> TextQuality:
    """Score how readable `text` is, from 0.0 (gibberish) to 1.0 (clean prose).

    `config` is a `QualityConfig`; the defaults are used when it is omitted, so
    this is callable from anywhere that has text and no configuration to hand.
    `pages` is the document's page count when the caller knows it, which is what
    turns "these are words" into "these are all the words that were on the page".
    """
    from .config import QualityConfig

    settings = config if isinstance(config, QualityConfig) else QualityConfig()
    sample = (text or "").strip()[:MAX_SAMPLE_CHARS]
    if not sample:
        return TextQuality(0.0, "empty", 0, 0, 0.0, 0.0, 0.0)

    dense = [char for char in sample if not char.isspace()]
    letters = sum(1 for char in dense if char.isalpha())
    letter_ratio = letters / len(dense) if dense else 0.0

    tokens = _TOKEN_RE.findall(sample)
    # Plain numbers and reference codes are neither words nor noise: a bank
    # statement is mostly figures and a good scan of one must not be punished
    # for it. Tokens with digits *through* them are a different matter.
    garbled = [token for token in tokens if looks_garbled(token)]
    alphabetic = [token for token in tokens if token.isalpha()]
    words = [token for token in alphabetic if looks_like_word(token)]
    common = [
        token
        for token in tokens
        if len(token) >= _MIN_COMMON_LENGTH and fold(token) in COMMON_WORDS
    ]

    countable = [token for token in tokens if len(token) > 1]
    # Garbled tokens join the denominator: they are failed words, not figures.
    candidates = len(alphabetic) + len(garbled)
    word_ratio = len(words) / candidates if candidates else 0.0
    common_ratio = len(common) / len(countable) if countable else 0.0
    garbled_ratio = len(garbled) / len(tokens) if tokens else 0.0

    # Below ~0.30 letters per character the page is punctuation and bars; at
    # 0.75 it reads like prose. Common words top out at 12%: ordinary text runs
    # far higher, but a form or a table legitimately does not.
    letter_quality = _clamp((letter_ratio - 0.30) / 0.45)
    common_signal = _clamp(common_ratio / 0.12)
    score = 0.50 * word_ratio + 0.25 * letter_quality + 0.25 * common_signal
    # A page where one token in six has digits inside its words is a bad read of
    # a legible page, whatever the surviving words say. Capped, because a stray
    # reference code should cost a document almost nothing.
    score *= 1 - min(2 * garbled_ratio, 0.6)

    density: float | None = None
    if pages and pages > 0 and len(sample) < MAX_SAMPLE_CHARS:
        # Only a cap, never a boost: a page can be legitimately sparse, but a
        # sheet that gave up a line and a half was not read.
        density = len(sample) / pages
        filled = _clamp(density / settings.chars_per_page)
        score = min(score, 0.25 + 0.75 * filled)

    if (
        common_ratio == 0
        and letter_ratio >= _PROSE_LETTER_RATIO
        and len(countable) >= _MIN_TOKENS_FOR_COMMON
    ):
        # Every token word-shaped and not one of them a word anybody uses:
        # "eter easng 5210 hon at Mah 2024" is what a page read too small
        # leaves behind, and it passes every other test here.
        score = min(score, _NO_COMMON_WORDS_CAP)

    if word_ratio < 0.1 and not common:
        # The plainest rule of the lot: no real words means gibberish, whatever
        # the character mix happens to look like.
        score = min(score, 0.05)

    if len(sample) < settings.min_sample_chars:
        # A receipt's whole text layer is two lines. There is not enough of it
        # to call it good or bad, and re-OCR'ing every short document in a
        # vault on the strength of a coin flip is worse than leaving them.
        verdict = "thin"
    elif score < settings.gibberish_below:
        verdict = "gibberish"
    elif score < settings.threshold:
        verdict = "poor"
    else:
        verdict = "good"

    return TextQuality(
        score=round(score, 3),
        verdict=verdict,
        chars=len(sample),
        words=len(words),
        word_ratio=round(word_ratio, 3),
        letter_ratio=round(letter_ratio, 3),
        common_ratio=round(common_ratio, 3),
        garbled_ratio=round(garbled_ratio, 3),
        density=round(density, 1) if density is not None else None,
    )
