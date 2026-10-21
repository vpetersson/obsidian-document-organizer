"""AppleClient against a fake `apple_fm_sdk`, so it runs anywhere."""

from __future__ import annotations

import enum
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from scanvault.apple import AppleClient, apple_schema
from scanvault.cache import ClassificationCache
from scanvault.config import APPLE_MODEL, Config, LlmConfig, load_config
from scanvault.llm import LlmError, OllamaClient, make_client


def fake_sdk(state: dict) -> types.ModuleType:
    """The parts of apple_fm_sdk 0.2 that AppleClient touches."""
    fm = types.ModuleType("apple_fm_sdk")

    class FoundationModelsError(Exception):
        pass

    class GenerationError(FoundationModelsError):
        pass

    for name, base in (
        ("InvalidGenerationSchemaError", FoundationModelsError),
        ("ExceededContextWindowSizeError", GenerationError),
        ("GuardrailViolationError", GenerationError),
        ("UnsupportedGuideError", GenerationError),
        ("RefusalError", GenerationError),
        ("RateLimitedError", GenerationError),
    ):
        setattr(fm, name, type(name, (base,), {}))
    fm.FoundationModelsError = FoundationModelsError
    fm.GenerationError = GenerationError

    class Reason(enum.IntEnum):
        APPLE_INTELLIGENCE_NOT_ENABLED = 0
        DEVICE_NOT_ELIGIBLE = 1
        MODEL_NOT_READY = 2
        UNKNOWN = 0xFF

    class Guardrails(enum.IntEnum):
        DEFAULT = 0
        PERMISSIVE_CONTENT_TRANSFORMATIONS = 1

    fm.SystemLanguageModelUnavailableReason = Reason
    fm.SystemLanguageModelGuardrails = Guardrails

    class SystemLanguageModel:
        def __init__(self, use_case=None, guardrails=Guardrails.DEFAULT):
            state["guardrails"] = guardrails

        def is_available(self):
            reason = state.get("unavailable")
            return (reason is None, reason)

    class SamplingMode:
        def __init__(self, kind):
            self.kind = kind

        @classmethod
        def greedy(cls):
            return cls("greedy")

    class GenerationOptions:
        def __init__(self, sampling=None, temperature=None, maximum_response_tokens=None):
            self.sampling = sampling
            self.temperature = temperature

    class GeneratedContent:
        def __init__(self, content: dict):
            self.content = content

        def to_json(self) -> str:
            return json.dumps(self.content)

    class LanguageModelSession:
        def __init__(self, instructions=None, model=None, tools=None):
            self.instructions = instructions

        async def respond(self, prompt, *, json_schema=None, options=None):
            state.setdefault("requests", []).append(
                {
                    "instructions": self.instructions,
                    "prompt": prompt,
                    "json_schema": json_schema,
                    "options": options,
                }
            )
            reply = state["replies"].pop(0)
            if isinstance(reply, Exception):
                raise reply
            return GeneratedContent(reply) if isinstance(reply, dict) else reply

    fm.SystemLanguageModel = SystemLanguageModel
    fm.SamplingMode = SamplingMode
    fm.GenerationOptions = GenerationOptions
    fm.LanguageModelSession = LanguageModelSession
    return fm


class TestAppleClient(unittest.TestCase):
    def setUp(self):
        self.state: dict = {"replies": []}
        self.fm = fake_sdk(self.state)
        patcher = mock.patch.dict(sys.modules, {"apple_fm_sdk": self.fm})
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, **overrides) -> AppleClient:
        return AppleClient(LlmConfig(provider="apple", **overrides))

    def test_a_guided_response_comes_back_as_a_dict(self):
        self.state["replies"] = [{"title": "Acme Invoice", "category": "Invoices"}]
        schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}, "category": {"type": "string"}},
            "required": ["title"],
        }
        result = self.client().chat_json("system prompt", "user prompt", schema=schema)
        self.assertEqual(result, {"title": "Acme Invoice", "category": "Invoices"})
        request = self.state["requests"][0]
        self.assertEqual(request["instructions"], "system prompt")
        self.assertEqual(request["prompt"], "user prompt")
        self.assertEqual(request["json_schema"]["x-order"], ["title", "category"])
        self.assertEqual(request["options"].sampling.kind, "greedy", "temperature 0 is greedy")
        self.assertEqual(
            self.state["guardrails"],
            self.fm.SystemLanguageModelGuardrails.PERMISSIVE_CONTENT_TRANSFORMATIONS,
        )

    def test_a_nonzero_temperature_is_passed_through(self):
        self.state["replies"] = ['{"ok": true}']
        self.client(temperature=0.4).chat_json("s", "u")
        options = self.state["requests"][0]["options"]
        self.assertIsNone(options.sampling)
        self.assertEqual(options.temperature, 0.4)

    def test_an_unguided_response_is_parsed_out_of_the_text(self):
        self.state["replies"] = ['```json\n{"ok": true}\n```']
        self.assertEqual(self.client().chat_json("s", "u"), {"ok": True})
        self.assertIsNone(self.state["requests"][0]["json_schema"])

    def test_a_rejected_schema_is_retried_in_plain_json_mode(self):
        self.state["replies"] = [
            self.fm.InvalidGenerationSchemaError("no guide for that"),
            '{"title": "Recovered"}',
        ]
        result = self.client().chat_json("s", "u", schema={"type": "object", "properties": {}})
        self.assertEqual(result["title"], "Recovered")
        first, second = self.state["requests"]
        self.assertIsNotNone(first["json_schema"])
        self.assertIsNone(second["json_schema"], "the retry drops the schema")

    def test_framework_errors_become_llm_errors(self):
        cases = {
            "ExceededContextWindowSizeError": "max_chars",
            "GuardrailViolationError": "declined",
            "RefusalError": "declined",
            "RateLimitedError": "RateLimitedError",
        }
        for name, expected in cases.items():
            with self.subTest(name):
                self.state["replies"] = [getattr(self.fm, name)("nope")]
                with self.assertRaises(LlmError) as caught:
                    self.client().chat_json("s", "u")
                self.assertIn(expected, str(caught.exception))

    def test_an_empty_response_is_an_error(self):
        self.state["replies"] = ["  "]
        with self.assertRaises(LlmError):
            self.client().chat_json("s", "u")

    def test_images_are_refused(self):
        with self.assertRaises(LlmError):
            self.client().chat_json("s", "u", images=["aGVsbG8="])

    def test_availability_reasons_are_actionable(self):
        client = self.client()
        self.assertTrue(client.available())
        self.assertEqual(client.list_models(), [APPLE_MODEL])
        self.assertTrue(client.has_model(), "there is nothing to pull")
        Reason = self.fm.SystemLanguageModelUnavailableReason
        self.state["unavailable"] = Reason.APPLE_INTELLIGENCE_NOT_ENABLED
        self.assertFalse(client.available())
        self.assertIn("System Settings", client.unavailable_reason())
        with self.assertRaises(LlmError):
            client.list_models()
        self.state["unavailable"] = Reason.UNKNOWN
        self.assertIn("UNKNOWN", client.unavailable_reason())


class TestWithoutTheSdk(unittest.TestCase):
    def test_a_missing_sdk_is_unavailable_not_a_crash(self):
        with mock.patch.dict(sys.modules, {"apple_fm_sdk": None}):
            client = AppleClient(LlmConfig(provider="apple"))
            self.assertFalse(client.available())
            self.assertIn("uv sync --extra apple", client.unavailable_reason())
            with self.assertRaises(LlmError):
                client.chat_json("s", "u")


class TestAppleSchema(unittest.TestCase):
    def test_objects_are_named_closed_and_ordered(self):
        schema = apple_schema(
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "category": {"type": "string", "enum": ["Invoices", "Other"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"],
            }
        )
        self.assertEqual(schema["title"], "Response")
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["x-order"], ["summary", "category", "tags"])
        self.assertEqual(schema["required"], ["summary"])
        self.assertEqual(schema["properties"]["category"]["enum"], ["Invoices", "Other"])
        self.assertEqual(schema["properties"]["tags"]["items"], {"type": "string"})

    def test_nested_objects_get_their_own_names(self):
        schema = apple_schema(
            {
                "type": "object",
                "properties": {
                    "line_items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"amount": {"type": "number"}}},
                    }
                },
            }
        )
        item = schema["properties"]["line_items"]["items"]
        self.assertEqual(item["title"], "ResponseLineItemsItem")
        self.assertEqual(item["x-order"], ["amount"])

    def test_the_classifier_schema_converts(self):
        from scanvault.classify import _schema

        schema = apple_schema(_schema(Config().categories))
        self.assertEqual(schema["x-order"][:3], ["correspondent", "document_type", "summary"])
        json.dumps(schema)  # the SDK serialises it


class TestProviderSelection(unittest.TestCase):
    def test_the_provider_picks_the_client(self):
        self.assertIsInstance(make_client(LlmConfig()), OllamaClient)
        self.assertIsInstance(make_client(LlmConfig(provider="apple")), AppleClient)

    def test_an_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            load_config(None, {"llm.provider": "openai"})

    def test_the_cache_keys_on_the_model_that_answers(self):
        config = load_config(None, {"llm.provider": "apple"})
        self.assertEqual(config.llm.model_name, APPLE_MODEL)
        cache = ClassificationCache(Path("/nonexistent"), config, enabled=False)
        self.assertEqual(cache.model, APPLE_MODEL)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.state: dict = {"replies": []}
        self.fm = fake_sdk(self.state)
        patcher = mock.patch.dict(sys.modules, {"apple_fm_sdk": self.fm})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv: str) -> str:
        import contextlib
        import io

        from scanvault.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main(list(argv))
        return out.getvalue()

    def test_doctor_checks_the_on_device_model(self):
        self.state["replies"] = [{"ok": True}]
        out = self.run_cli("doctor", "--provider", "apple", "--model", "ignored")
        self.assertIn("this machine (Apple Intelligence, on-device)", out)
        self.assertIn(f"model       : {APPLE_MODEL} OK", out)
        self.assertNotIn("ollama      :", out)

    def test_doctor_says_why_the_model_is_unavailable(self):
        Reason = self.fm.SystemLanguageModelUnavailableReason
        self.state["unavailable"] = Reason.MODEL_NOT_READY
        out = self.run_cli("doctor", "--provider", "apple")
        self.assertIn("UNAVAILABLE", out)
        self.assertIn("still downloading", out)

    def test_an_unavailable_model_falls_back_to_heuristics(self):
        import argparse

        from scanvault.cli import _client

        self.state["unavailable"] = self.fm.SystemLanguageModelUnavailableReason.UNKNOWN
        config = load_config(None, {"llm.provider": "apple"})
        with self.assertLogs("scanvault", "WARNING"):
            self.assertIsNone(_client(argparse.Namespace(), config))
        config.llm.fallback_to_heuristics = False
        with self.assertRaises(SystemExit):
            _client(argparse.Namespace(), config)


if __name__ == "__main__":
    unittest.main()
