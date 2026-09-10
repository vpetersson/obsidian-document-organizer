"""Cache of model answers, so a preview is not paid for twice.

`organize` classifies every note it plans to touch. Without a cache, the dry run
you do first and the `--apply` that follows each pay for the same model calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

CACHE_FILENAME = "classifications.json"
# Bumped when the prompt or the response handling changes shape.
CACHE_VERSION = 1


class ClassificationCache:
    """Maps a document's text to the model's answer for it.

    The key covers everything that would change the answer - the model, the
    category list, the language hint and the text itself - so switching model or
    editing a document misses rather than returning something stale.
    """

    def __init__(self, root: Path, config: Config, enabled: bool = True, read: bool = True):
        self.root = root
        self.path = root / CACHE_FILENAME
        self.enabled = enabled
        # A cache that answers is the wrong thing during --reclassify: the point
        # of that flag is to ask again. Writing stays on, so the new answers are
        # the ones the next run reuses.
        self.read = read
        self.model = config.llm.model
        self.categories = list(config.categories)
        self.language_hint = config.language_hint
        self.entries: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self._dirty = False
        # Workers share one cache; every mutation is read-modify-write.
        self._lock = threading.Lock()
        if enabled:
            self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("ignoring unreadable classification cache %s: %s", self.path, exc)
            return
        if data.get("version") != CACHE_VERSION:
            log.info("classification cache is from an older version; starting fresh")
            return
        entries = data.get("entries")
        if isinstance(entries, dict):
            self.entries = entries

    def save(self) -> None:
        if not self.enabled or not self._dirty:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"version": CACHE_VERSION, "entries": self.entries}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False

    def key(self, text: str) -> str:
        digest = hashlib.sha256()
        for part in (
            str(CACHE_VERSION),
            self.model,
            self.language_hint,
            "|".join(self.categories),
            text,
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def get(self, text: str) -> dict[str, Any] | None:
        if not self.enabled or not self.read:
            return None
        with self._lock:
            entry = self.entries.get(self.key(text))
        response = entry.get("response") if isinstance(entry, dict) else None
        with self._lock:
            if isinstance(response, dict):
                self.hits += 1
                return response
            self.misses += 1
        return response if isinstance(response, dict) else None

    def put(self, text: str, response: dict[str, Any]) -> None:
        if not self.enabled:
            return
        entry = {
            "response": response,
            "model": self.model,
            "cached_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with self._lock:
            self.entries[self.key(text)] = entry
            self._dirty = True

    def summary(self) -> str:
        return f"{self.hits} cached, {self.misses} new"
