"""Minimal ollama client (stdlib only) with structured-output support."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .config import LlmConfig

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """Raised when ollama is unreachable or returns something unusable."""


class OllamaClient:
    def __init__(self, config: LlmConfig):
        self.config = config
        self.host = config.host.rstrip("/")

    def _post(self, path: str, payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.config.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise LlmError(f"ollama {path} returned {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LlmError(f"cannot reach ollama at {self.host}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LlmError(f"ollama {path} returned invalid JSON: {exc}") from exc

    def list_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise LlmError(f"cannot reach ollama at {self.host}: {exc}") from exc
        return [model.get("name", "") for model in data.get("models", [])]

    def available(self) -> bool:
        try:
            self.list_models()
            return True
        except LlmError:
            return False

    def has_model(self) -> bool:
        """True when the configured model is pulled (tag-insensitive match)."""
        wanted = self.config.model
        names = self.list_models()
        base = wanted.split(":", 1)[0]
        return any(name == wanted or name.split(":", 1)[0] == base for name in names)

    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """One-shot chat constrained to JSON; returns the parsed object."""
        message: dict[str, Any] = {"role": "user", "content": user}
        if images:
            message["images"] = images
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, message],
            "stream": False,
            "keep_alive": self.config.keep_alive,
            "format": schema if schema else "json",
            "options": {
                "temperature": self.config.temperature,
                "num_ctx": self.config.num_ctx,
            },
        }
        data = self._post("/api/chat", payload)
        content = (data.get("message") or {}).get("content", "")
        return parse_json_object(content)


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating code fences and leading prose."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LlmError(f"model did not return JSON: {content[:200]!r}") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"model returned malformed JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LlmError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
