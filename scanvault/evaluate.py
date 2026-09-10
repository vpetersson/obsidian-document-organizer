"""Score the classifier against a labelled corpus.

Without this there is no way to tell whether a prompt change helped: every
judgement about "the classifier is naive" is anecdote until the same documents
are run through it twice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .classify import classify
from .config import Config
from .llm import OllamaClient

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "corpus" / "documents.json"


@dataclass
class Result:
    id: str
    language: str
    context: str
    expected_category: list[str]
    actual_category: str
    expected_tags: list[str]
    actual_tags: list[str]
    title: str

    @property
    def category_ok(self) -> bool:
        return self.actual_category in self.expected_category

    @property
    def missing_tags(self) -> list[str]:
        return [tag for tag in self.expected_tags if tag not in self.actual_tags]


@dataclass
class Score:
    results: list[Result] = field(default_factory=list)

    def _subset(self, **criteria: str) -> list[Result]:
        return [
            result
            for result in self.results
            if all(getattr(result, key) == value for key, value in criteria.items())
        ]

    def accuracy(self, **criteria: str) -> float:
        subset = self._subset(**criteria)
        if not subset:
            return 0.0
        return sum(1 for result in subset if result.category_ok) / len(subset)

    def tag_recall(self, **criteria: str) -> float:
        subset = [r for r in self._subset(**criteria) if r.expected_tags]
        if not subset:
            return 1.0
        found = sum(len(r.expected_tags) - len(r.missing_tags) for r in subset)
        wanted = sum(len(r.expected_tags) for r in subset)
        return found / wanted if wanted else 1.0

    def failures(self) -> list[Result]:
        return [r for r in self.results if not r.category_ok or r.missing_tags]

    def report(self) -> str:
        lines = [
            f"documents      : {len(self.results)}",
            f"category        : {self.accuracy():.0%}",
            f"  english       : {self.accuracy(language='en'):.0%}",
            f"  swedish       : {self.accuracy(language='sv'):.0%}",
            f"  personal      : {self.accuracy(context='personal'):.0%}",
            f"  business      : {self.accuracy(context='business'):.0%}",
            f"expected tags   : {self.tag_recall():.0%} found",
        ]
        failures = self.failures()
        if failures:
            lines.append("")
            lines.append("misses:")
            for result in failures:
                detail = []
                if not result.category_ok:
                    detail.append(
                        f"category {result.actual_category} != "
                        f"{' or '.join(result.expected_category)}"
                    )
                if result.missing_tags:
                    detail.append(f"missing tags {', '.join(result.missing_tags)}")
                lines.append(f"  {result.id:26s} {'; '.join(detail)}")
        return "\n".join(lines)


def load_corpus(path: Path | None = None) -> list[dict[str, Any]]:
    return json.loads((path or CORPUS).read_text(encoding="utf-8"))


def evaluate(
    config: Config,
    client: OllamaClient | None = None,
    corpus: list[dict[str, Any]] | None = None,
) -> Score:
    """Classify every document in the corpus and score the answers."""
    score = Score()
    for document in corpus if corpus is not None else load_corpus():
        meta = classify(document["text"], config, client, cache=None)
        score.results.append(
            Result(
                id=document["id"],
                language=document["language"],
                context=document["context"],
                expected_category=(
                    document["category"]
                    if isinstance(document["category"], list)
                    else [document["category"]]
                ),
                actual_category=meta.category,
                expected_tags=list(document.get("tags", [])),
                actual_tags=list(meta.tags),
                title=meta.title,
            )
        )
    return score
