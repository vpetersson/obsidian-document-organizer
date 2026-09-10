"""A ledger of the tags a vault actually uses.

Without one, every document is tagged from scratch and the vocabulary drifts:
`mortgage` and `mortgages`, `invoice` and `invoices`, `council-tax` and
`counciltax`. Each is a reasonable answer on its own and the set of them is
useless - searching `mortgage` finds four fifths of your mortgage paperwork.

So before a tag is written, it is matched against the ones already in use. The
matching is in two steps, cheap first:

  * a *fingerprint* - diacritics folded, words singularised and sorted - which
    catches the endings and the word order. `invoices` and `invoice` are the
    same tag; so are `tax-council` and `council-tax`.
  * failing that, string similarity against the tags already in the ledger.

Both are deliberately conservative. Two tags whose digits differ are never
merged, because `year-2023` and `year-2024` are 89% similar and completely
different, and a tag from the configured rules or category list is never merged
away - that vocabulary is the fixed part.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .util import fold, slugify

log = logging.getLogger(__name__)

LEDGER_FILE = "tags.json"
# Words too short or too odd to singularise by dropping an "s".
_KEEP_TRAILING_S = ("ss", "us", "is", "as")
# Endings that take "-es" rather than "-s": tax/taxes, church/churches. Only
# these, because "houses" loses just the "s" and the plain rule below has it.
_ES_PLURAL = ("xes", "zes", "ches", "shes")


def _singular(word: str) -> str:
    """A crude English singular, applied only where it is safe.

    Swedish plurals (-ar, -er, -or) are left alone: "faktura" and "fakturor"
    would need a real stemmer, and guessing wrong merges two real tags.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(_ES_PLURAL):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(_KEEP_TRAILING_S):
        return word[:-1]
    return word


def fingerprint(tag: str) -> str:
    """What two tags have to share to be the same tag.

    `Mortgages`, `mortgage` and `mortgage` with a stray accent all reduce to
    the same string; `year-2023` and `year-2024` do not.
    """
    words = [word for word in fold(tag).replace("_", "-").split("-") if word]
    return " ".join(sorted(_singular(word) for word in words))


def _digits(tag: str) -> str:
    return "".join(char for char in tag if char.isdigit())


@dataclass
class Group:
    """One tag and the variants of it that have been seen."""

    canonical: str
    counts: dict[str, int] = field(default_factory=dict)
    protected: bool = False

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def variants(self) -> list[str]:
        """The spellings folded into this tag, most used first."""
        others = [tag for tag in self.counts if tag != self.canonical]
        return sorted(others, key=lambda tag: (-self.counts[tag], tag))

    def elect(self) -> None:
        """Pick the spelling to keep: the one most documents already use.

        A protected tag - one the rules or the category list name - keeps its
        spelling however rare it is, because that vocabulary is the fixed part.
        """
        if self.protected:
            return
        if not self.counts:
            return
        self.canonical = min(self.counts, key=lambda tag: (-self.counts[tag], len(tag), tag))


class TagLedger:
    """The vault's tag vocabulary, and the decision to reuse or extend it."""

    def __init__(self, cutoff: float = 0.88) -> None:
        self.cutoff = cutoff
        self._groups: dict[str, Group] = {}
        self._lock = threading.Lock()
        # Tags folded into another this run, for the report at the end.
        self.folded: dict[str, str] = {}

    # ---- building ---------------------------------------------------------

    def reserve(self, tags: Iterable[str]) -> None:
        """Declare the fixed vocabulary: rule tags, categories, base tags.

        Reserved before anything is counted, so a rule tag owns its
        fingerprint rather than losing it to whatever the model said first.
        """
        for tag in tags:
            slug = slugify(tag, max_length=40)
            if not slug:
                continue
            key = fingerprint(slug)
            group = self._groups.get(key)
            if group is None:
                self._groups[key] = Group(slug, protected=True)
            else:
                group.canonical = slug
                group.protected = True

    def observe(self, tags: Iterable[str], count: int = 1) -> None:
        """Record tags already in use, without folding them.

        Seeding is a census: `mortgage` used forty times and `mortgages` once
        both go in, and the election then decides which spelling wins.
        """
        for tag in tags:
            slug = slugify(str(tag).lstrip("#"), max_length=40)
            if not slug:
                continue
            key = fingerprint(slug)
            group = self._groups.setdefault(key, Group(slug))
            group.counts[slug] = group.counts.get(slug, 0) + count
            group.elect()

    def consolidate(self) -> None:
        """Fold the near-duplicates already in the vault into each other.

        Seeding is a census, so `council-tax` and `counciltax` both go in as
        themselves. This is the pass that decides they are one tag - biggest
        first, so the spelling most documents already use is the one that
        absorbs the rest, and protected tags absorb but are never absorbed.
        """
        order = sorted(
            self._groups.values(),
            key=lambda g: (not g.protected, -g.total, g.canonical),
        )
        absorbed: set[str] = set()
        for group in order:
            if fingerprint(group.canonical) in absorbed:
                continue
            for other in order:
                key = fingerprint(other.canonical)
                if other is group or other.protected or key in absorbed:
                    continue
                if not self._alike(group.canonical, other.canonical):
                    continue
                for tag, count in other.counts.items():
                    group.counts[tag] = group.counts.get(tag, 0) + count
                absorbed.add(key)
        for key in absorbed:
            self._groups.pop(key, None)
        for group in self._groups.values():
            # The election runs on the merged counts, not the partial ones.
            group.elect()

    def _alike(self, left: str, right: str) -> bool:
        if _digits(left) != _digits(right):
            return False
        return SequenceMatcher(None, fold(left), fold(right)).ratio() > self.cutoff

    # ---- using ------------------------------------------------------------

    def canonical(self, tag: str) -> str:
        """The tag to actually write: an existing one where one fits.

        Returns the input (registered as new) when the vault has nothing like
        it - a vocabulary that cannot grow is no more useful than one that
        grows without limit.
        """
        slug = slugify(str(tag).lstrip("#"), max_length=40)
        if not slug:
            return ""
        with self._lock:
            key = fingerprint(slug)
            group = self._groups.get(key)
            if group is None:
                group = self._match(slug)
            if group is None:
                self._groups[key] = Group(slug)
                return slug
            if group.canonical != slug:
                self.folded[slug] = group.canonical
            return group.canonical

    def _match(self, slug: str) -> Group | None:
        """The nearest tag already in the ledger, if one is near enough.

        The digit guard is the important half: "year-2023" and "year-2024" are
        89% alike and nothing to do with each other, so anything numbered is
        only ever itself.
        """
        best: Group | None = None
        best_ratio = self.cutoff
        folded = fold(slug)
        for group in self._groups.values():
            if not self._alike(slug, group.canonical):
                continue
            ratio = SequenceMatcher(None, folded, fold(group.canonical)).ratio()
            if ratio > best_ratio:
                best, best_ratio = group, ratio
        return best

    def apply(self, tags: Iterable[str]) -> list[str]:
        """Canonicalise a document's tags, keeping order and dropping repeats."""
        out: list[str] = []
        for tag in tags:
            resolved = self.canonical(tag)
            if resolved and resolved not in out:
                out.append(resolved)
        return out

    def record(self, tags: Iterable[str]) -> None:
        """Count tags as written, so the next document sees them in use."""
        with self._lock:
            for tag in tags:
                key = fingerprint(tag)
                group = self._groups.setdefault(key, Group(tag))
                group.counts[tag] = group.counts.get(tag, 0) + 1

    # ---- reporting and persistence ---------------------------------------

    @property
    def groups(self) -> list[Group]:
        """Every tag, most used first."""
        return sorted(self._groups.values(), key=lambda g: (-g.total, g.canonical))

    def duplicates(self) -> list[Group]:
        """Tags that have more than one spelling in the vault."""
        return [group for group in self.groups if group.variants]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "tags": {
                group.canonical: {
                    "count": group.total,
                    "variants": group.variants,
                }
                for group in self.groups
                if group.counts
            },
        }

    def save(self, state_root: Path) -> None:
        path = state_root / LEDGER_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - a read-only vault
            log.warning("could not write the tag ledger: %s", exc)

    def load(self, state_root: Path) -> None:
        """Seed from the last run. The vault itself is still the truth."""
        path = state_root / LEDGER_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for tag, entry in (data.get("tags") or {}).items():
            count = entry.get("count", 1) if isinstance(entry, dict) else 1
            self.observe([tag], count=max(1, int(count)))


def report_folded(ledger: TagLedger, limit: int = 10) -> None:
    """Say which tags were reused rather than minted. Silence means none were."""
    folded = ledger.folded
    if not folded:
        return
    log.info(
        "reused %d existing tag%s instead of making new ones",
        len(folded),
        "" if len(folded) == 1 else "s",
    )
    for source, target in sorted(folded.items())[:limit]:
        log.debug("  %s -> %s", source, target)
    if len(folded) > limit:
        log.debug("  ... and %d more", len(folded) - limit)


def ledger_for(config: Any, vault: Any = None) -> TagLedger:
    """A ledger seeded with the fixed vocabulary and the vault's own tags."""
    ledger = TagLedger(cutoff=config.tags.merge_cutoff)
    ledger.reserve(config.tags.base)
    ledger.reserve(config.tags.all_rules().keys())
    ledger.reserve(slugify(category) for category in config.categories)
    if vault is not None:
        for tags in vault.iter_note_tags():
            ledger.observe(tags)
    ledger.consolidate()
    return ledger
