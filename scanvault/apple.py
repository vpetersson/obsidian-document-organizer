"""Apple Intelligence's on-device model, through the Foundation Models framework.

Apple's `apple_fm_sdk` package is a binding to the framework, so this only
works on a Mac with Apple silicon, macOS 26 or later and Apple Intelligence
switched on. It is imported lazily: everywhere else the provider reports itself
unavailable and the run falls back to heuristics like an unreachable ollama.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import threading
from typing import Any

from .config import APPLE_MODEL, LlmConfig
from .llm import LlmError, parse_json_object

log = logging.getLogger(__name__)

# The on-device model answers one request at a time: four sent together take
# about four times as long as one. Queue them here rather than inside the
# framework, so a request's timeout only starts once it is actually running,
# while the workers carry on with OCR in parallel.
MODEL_LOCK = threading.Lock()

SDK_MODULE = "apple_fm_sdk"

# SystemLanguageModelUnavailableReason, by name, in words a user can act on.
UNAVAILABLE_REASONS = {
    "APPLE_INTELLIGENCE_NOT_ENABLED": "Apple Intelligence is switched off "
    "(System Settings -> Apple Intelligence & Siri)",
    "DEVICE_NOT_ELIGIBLE": "this Mac cannot run Apple Intelligence",
    "MODEL_NOT_READY": "the model is still downloading; try again later",
}


def apple_schema(schema: dict[str, Any], name: str = "Response") -> dict[str, Any]:
    """Shape a plain JSON Schema the way the framework's own schemas look.

    The framework decodes the schemas Swift's `@Generable` produces: named
    objects, a closed property set, and an explicit property order. Generation
    follows that order, which is what the classifier's field order relies on.
    """
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return {**schema, "items": apple_schema(schema["items"], f"{name}Item")}
    if schema.get("type") != "object":
        return dict(schema)
    properties = {
        key: apple_schema(value, f"{name}{key.title().replace('_', '')}")
        for key, value in (schema.get("properties") or {}).items()
    }
    return {
        "title": name,
        **schema,
        "properties": properties,
        "additionalProperties": False,
        "x-order": list(properties),
    }


class AppleClient:
    """Same surface as OllamaClient, backed by the on-device model."""

    def __init__(self, config: LlmConfig):
        self.config = config
        self._fm: Any = None

    def _sdk(self) -> Any:
        if self._fm is None:
            try:
                self._fm = importlib.import_module(SDK_MODULE)
            except ImportError as exc:
                raise LlmError(
                    f"{SDK_MODULE} is not installed; on a Mac run: uv sync --extra apple"
                ) from exc
            except OSError as exc:  # the native library refused to load
                raise LlmError(f"{SDK_MODULE} could not load the framework: {exc}") from exc
        return self._fm

    def _model(self) -> Any:
        fm = self._sdk()
        # Scanned paperwork is medical letters, debts, court papers. The
        # permissive guardrails are the ones Apple provides for working on the
        # user's own content rather than generating new content - but they
        # only cover text responses; see chat_json.
        return fm.SystemLanguageModel(
            guardrails=fm.SystemLanguageModelGuardrails.PERMISSIVE_CONTENT_TRANSFORMATIONS
        )

    def unavailable_reason(self) -> str | None:
        """None when the model can answer, otherwise why it cannot."""
        try:
            ready, reason = self._model().is_available()
        except LlmError as exc:
            return str(exc)
        except Exception as exc:  # anything the binding raises before a request
            return f"the Foundation Models framework failed: {exc}"
        if ready:
            return None
        name = getattr(reason, "name", str(reason))
        return UNAVAILABLE_REASONS.get(name, f"the on-device model is unavailable ({name})")

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def list_models(self) -> list[str]:
        reason = self.unavailable_reason()
        if reason is not None:
            raise LlmError(reason)
        return [APPLE_MODEL]

    def has_model(self) -> bool:
        # There is nothing to pull: the model ships with the OS.
        return True

    def _options(self) -> Any:
        fm = self._sdk()
        if self.config.temperature <= 0:
            return fm.GenerationOptions(sampling=fm.SamplingMode.greedy())
        return fm.GenerationOptions(temperature=self.config.temperature)

    def _respond(self, system: str, user: str, schema: dict[str, Any] | None) -> Any:
        fm = self._sdk()

        async def run() -> Any:
            # A session keeps a transcript; one per request keeps every
            # document independent of the ones before it.
            session = fm.LanguageModelSession(instructions=system, model=self._model())
            if schema is None:
                return await session.respond(user, options=self._options())
            return await session.respond(
                user, json_schema=apple_schema(schema), options=self._options()
            )

        try:
            with MODEL_LOCK:
                return asyncio.run(asyncio.wait_for(run(), timeout=self.config.timeout))
        except TimeoutError as exc:
            raise LlmError(f"the on-device model did not answer in {self.config.timeout}s") from exc

    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """One-shot request constrained to JSON; returns the parsed object."""
        if images:
            raise LlmError("the apple provider does not take images")
        fm = self._sdk()
        try:
            return self._chat(fm, system, user, schema)
        except fm.ExceededContextWindowSizeError as exc:
            raise LlmError(
                "the document does not fit the on-device model's context window; "
                "lower llm.max_chars"
            ) from exc
        except (fm.GuardrailViolationError, fm.RefusalError) as exc:
            raise LlmError(f"the on-device model declined the document: {exc}") from exc
        except fm.FoundationModelsError as exc:
            raise LlmError(f"the on-device model failed: {type(exc).__name__}: {exc}") from exc

    def _chat(
        self, fm: Any, system: str, user: str, schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        # Text first, with the schema spelled out in the instructions. The
        # permissive guardrails only apply to text responses; guided generation
        # runs under the default ones, which refused 20 of the 39 documents in
        # the eval corpus - ordinary bills and payslips - as "likely unsafe".
        instructions = system
        if schema is not None:
            instructions += (
                "\n\nThe JSON object follows this JSON Schema; leave out or null "
                f"anything the document does not state:\n{json.dumps(schema)}"
            )
        try:
            return _as_object(self._respond(instructions, user, None))
        except LlmError as exc:
            if schema is None:
                raise
            # Guided generation cannot produce malformed JSON, so it is the
            # fallback for an answer that did not parse.
            log.warning("the on-device model did not answer in JSON (%s); retrying guided", exc)
        try:
            return _as_object(self._respond(system, user, schema))
        except (fm.InvalidGenerationSchemaError, fm.UnsupportedGuideError) as exc:
            raise LlmError(f"the on-device model rejected the schema: {exc}") from exc


def _as_object(result: Any) -> dict[str, Any]:
    """A guided response is GeneratedContent; an unguided one is text."""
    if isinstance(result, str):
        if not result.strip():
            raise LlmError("the on-device model returned an empty response")
        return parse_json_object(result)
    to_json = getattr(result, "to_json", None)
    if to_json is None:
        raise LlmError(f"unexpected response from the on-device model: {type(result).__name__}")
    try:
        parsed = json.loads(to_json())
    except json.JSONDecodeError as exc:
        raise LlmError(f"the on-device model returned malformed JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LlmError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
