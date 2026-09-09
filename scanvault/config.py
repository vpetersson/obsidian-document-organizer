"""Configuration: defaults, TOML file, and CLI overrides."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

DEFAULT_CATEGORIES = [
    "Invoices",
    "Receipts",
    "Contracts",
    "Banking",
    "Taxes",
    "Insurance",
    "Medical",
    "Government",
    "Employment",
    "Education",
    "Property",
    "Vehicle",
    "Utilities",
    "Correspondence",
    "Manuals",
    "Personal",
    "Other",
]

CONFIG_FILENAME = "scanvault.toml"


@dataclass
class OcrConfig:
    # "auto" picks ocrmypdf, then tesseract, then gives up.
    backend: str = "auto"
    languages: str = "eng"
    # Below this many extracted characters a PDF counts as image-only.
    min_text_chars: int = 180
    # Re-OCR even when a text layer is present.
    force: bool = False
    rotate_pages: bool = True
    deskew: bool = True
    optimize: int = 1
    timeout: int = 900
    jobs: int = 0  # 0 -> let the backend decide


@dataclass
class LlmConfig:
    host: str = "http://localhost:11434"
    model: str = "qwen3.5:9b"
    # Fall back to heuristics instead of failing when ollama is unreachable.
    fallback_to_heuristics: bool = True
    temperature: float = 0.0
    num_ctx: int = 8192
    timeout: int = 300
    # How much document text the classifier gets to see.
    max_chars: int = 12000
    keep_alive: str = "5m"


@dataclass
class VaultConfig:
    notes_dir: str = "Documents"
    attachments_dir: str = "Attachments"
    # Available placeholders: category, year, month, date, title, slug, correspondent
    note_path_template: str = "{category}/{year}/{date} {title}"
    attachment_path_template: str = "{category}/{year}/{date} {title}"
    # move | copy | leave
    source_action: str = "move"
    # Extra tags added to every note.
    base_tags: list[str] = field(default_factory=lambda: ["scan"])
    max_tags: int = 8
    # Embed the OCR text in the note so Obsidian search can reach it.
    include_text: bool = True
    max_text_chars: int = 20000
    # Written under the vault; holds the dedupe index.
    state_dir: str = ".scanvault"


@dataclass
class Config:
    source_dir: Path | None = None
    vault_dir: Path | None = None
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    language_hint: str = "English"
    ocr: OcrConfig = field(default_factory=OcrConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)

    @property
    def notes_root(self) -> Path:
        return self._vault_subdir(self.vault.notes_dir)

    @property
    def attachments_root(self) -> Path:
        return self._vault_subdir(self.vault.attachments_dir)

    @property
    def state_root(self) -> Path:
        return self._vault_subdir(self.vault.state_dir)

    def _vault_subdir(self, name: str) -> Path:
        if self.vault_dir is None:
            raise ValueError("vault_dir is not set")
        return self.vault_dir / name if name else self.vault_dir


def _apply(target: Any, values: dict[str, Any], path: str) -> None:
    known = {f.name: f for f in fields(target)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown config key: {path}{key}")
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, f"{path}{key}.")
            continue
        annotation = known[key].type
        if annotation in ("Path | None", "Path") and isinstance(value, str):
            value = Path(value).expanduser()
        setattr(target, key, value)


def load_config(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Load defaults, then a TOML file (if any), then explicit overrides."""
    config = Config()
    if path is not None:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        _apply(config, data, "")
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if "." in key:
            section_name, _, leaf = key.partition(".")
            _apply(getattr(config, section_name), {leaf: value}, f"{section_name}.")
        else:
            _apply(config, {key: value}, "")
    return config


def find_config(explicit: Path | None, vault_dir: Path | None) -> Path | None:
    """Explicit path wins, then <vault>/.scanvault/scanvault.toml, then ./scanvault.toml."""
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"config file not found: {explicit}")
        return explicit
    candidates = []
    if vault_dir is not None:
        candidates.append(vault_dir / ".scanvault" / CONFIG_FILENAME)
        candidates.append(vault_dir / CONFIG_FILENAME)
    candidates.append(Path.cwd() / CONFIG_FILENAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
