"""Dedupe index kept inside the vault, so re-runs are cheap and idempotent."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"


class State:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / INDEX_FILENAME
        self.documents: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.path, exc)
            return
        documents = data.get("documents")
        if isinstance(documents, dict):
            self.documents = documents

    def save(self) -> None:
        if not self._dirty:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "documents": self.documents}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False

    def get(self, digest: str) -> dict[str, Any] | None:
        return self.documents.get(digest)

    def record(self, digest: str, **fields: Any) -> None:
        entry = dict(fields)
        entry["processed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.documents[digest] = entry
        self._dirty = True

    def forget(self, digest: str) -> None:
        if self.documents.pop(digest, None) is not None:
            self._dirty = True

    def prune(self, vault_root: Path) -> int:
        """Drop entries whose note has been deleted from the vault."""
        removed = 0
        for digest, entry in list(self.documents.items()):
            note = entry.get("note")
            if isinstance(note, str) and not (vault_root / note).exists():
                del self.documents[digest]
                removed += 1
        self._dirty = self._dirty or bool(removed)
        return removed
