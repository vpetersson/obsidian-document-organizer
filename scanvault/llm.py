"""Model clients with structured-output support: ollama (stdlib only) and Apple's."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from .config import LlmConfig

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """Raised when the model is unreachable or returns something unusable."""


class LlmClient(Protocol):
    """What the classifier needs from a model, whichever provider runs it."""

    config: LlmConfig

    def available(self) -> bool: ...

    def has_model(self) -> bool: ...

    def list_models(self) -> list[str]: ...

    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]: ...


def make_client(config: LlmConfig) -> LlmClient:
    """The client for `config.provider`."""
    if config.provider == "apple":
        from .apple import AppleClient

        return AppleClient(config)
    return OllamaClient(config)


def is_local_host(url: str) -> bool:
    """True when the URL points at this machine. Informational only."""
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    if hostname in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


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

    def _chat(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
        images: list[str] | None,
    ) -> str:
        message: dict[str, Any] = {"role": "user", "content": user}
        if images:
            message["images"] = images
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, message],
            "stream": False,
            "keep_alive": self.config.keep_alive,
            "format": schema if schema else "json",
            # Reasoning models otherwise spend the whole response thinking and
            # return empty content. Older ollama builds ignore the field.
            "think": False,
            "options": {
                "temperature": self.config.temperature,
                "num_ctx": self.config.num_ctx,
            },
        }
        data = self._post("/api/chat", payload)
        message_out = data.get("message") or {}
        content = message_out.get("content") or ""
        if not content.strip():
            # Some builds put a reasoning model's whole answer here instead.
            content = message_out.get("thinking") or ""
        return content

    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """One-shot chat constrained to JSON; returns the parsed object."""
        content = self._chat(system, user, schema, images)
        if not content.strip() and schema is not None:
            # A JSON-schema `format` is not understood by every model or every
            # ollama version; plain JSON mode usually is.
            log.warning(
                "%s returned an empty response for a schema request; retrying in plain JSON mode",
                self.config.model,
            )
            content = self._chat(system, user, None, images)
        if not content.strip():
            raise LlmError(
                f"{self.config.model} returned an empty response. Check that "
                f"`ollama run {self.config.model}` answers, and that the model is "
                "not a reasoning-only build."
            )
        return parse_json_object(content)


THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating code fences, prose and think blocks."""
    text = THINK_BLOCK_RE.sub("", content or "").strip()
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
        parsed = _loads_repaired(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise LlmError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


# How many slips one answer may have before it is not worth repairing.
MAX_REPAIRS = 20


def _loads_repaired(text: str) -> Any:
    """json.loads, mending the slips small models make when writing JSON by hand.

    Each one shows up as "Expecting ',' delimiter" right after a value:

    * a quote inside a string that was not escaped - `"says "pay now" by"`;
    * a bare value that is not JSON - `"amount": 512.00 GBP`, `"date": 2024-03-01`;
    * a missing comma between two members.
    """
    for _ in range(MAX_REPAIRS):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            if not exc.msg.startswith("Expecting ',' delimiter"):
                raise LlmError(f"model returned malformed JSON: {exc}{_context(text, exc.pos)}") from exc
            mended = _mend(text, exc.pos)
            if mended is None:
                raise LlmError(f"model returned malformed JSON: {exc}{_context(text, exc.pos)}") from exc
            text = mended
    raise LlmError(f"model returned malformed JSON: more than {MAX_REPAIRS} errors")


def _mend(text: str, pos: int) -> str | None:
    before = text[:pos].rstrip()
    if text[pos] == '"' and before.endswith(('"', "]", "}", "e", "l")) or (
        text[pos] == '"' and before[-1:].isdigit()
    ):
        return text[:pos] + ", " + text[pos:]
    if before.endswith('"'):
        # The string ended too early: the quote that closed it was inside it.
        quote = len(before) - 1
        return text[:quote] + '\\"' + text[quote + 1 :]
    colon = before.rfind(":")
    if colon != -1 and '"' not in before[colon:]:
        # A bare value: quote everything up to the end of the member.
        stop = colon + 1
        while stop < len(text) and text[stop] not in ",}]\n":
            stop += 1
        value = text[colon + 1 : stop].strip()
        return text[: colon + 1] + " " + json.dumps(value) + text[stop:]
    return None


def _context(text: str, pos: int) -> str:
    return f" near {text[max(0, pos - 60) : pos + 30]!r}"
