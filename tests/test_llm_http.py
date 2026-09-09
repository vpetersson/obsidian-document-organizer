"""OllamaClient against a fake ollama HTTP server."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from scanvault.config import LlmConfig
from scanvault.llm import LlmError, OllamaClient

STATE: dict[str, object] = {}


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the server
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": "qwen3.5:9b"}, {"name": "llama3:8b"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length))
        STATE["request"] = request
        STATE.setdefault("requests", []).append(request)
        if STATE.get("fail"):
            self._send(500, {"error": "model runner crashed"})
            return
        replies = STATE.get("replies")
        message = replies.pop(0) if replies else {"content": STATE["content"]}
        self._send(200, {"message": {"role": "assistant", **message}})


class TestOllamaClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def client(self, model: str = "qwen3.5:9b") -> OllamaClient:
        return OllamaClient(LlmConfig(host=self.host, model=model, timeout=10))

    def setUp(self):
        STATE.clear()
        STATE["content"] = '{"title": "Acme Invoice", "category": "Invoices"}'

    def test_available_and_model_lookup(self):
        client = self.client()
        self.assertTrue(client.available())
        self.assertTrue(client.has_model())
        self.assertTrue(self.client("qwen3.5").has_model(), "tag-less names should match")
        self.assertFalse(self.client("mistral:7b").has_model())

    def test_chat_json_sends_the_expected_payload(self):
        result = self.client().chat_json("system prompt", "user prompt", schema={"type": "object"})
        self.assertEqual(result["title"], "Acme Invoice")
        request = STATE["request"]
        self.assertEqual(request["model"], "qwen3.5:9b")
        self.assertFalse(request["stream"])
        self.assertEqual(request["format"], {"type": "object"})
        self.assertEqual(request["options"]["temperature"], 0.0)
        self.assertEqual(request["messages"][0]["content"], "system prompt")
        self.assertEqual(request["messages"][1]["content"], "user prompt")

    def test_http_error_becomes_llm_error(self):
        STATE["fail"] = True
        with self.assertRaises(LlmError) as caught:
            self.client().chat_json("s", "u")
        self.assertIn("500", str(caught.exception))

    def test_an_empty_schema_response_is_retried_in_plain_json_mode(self):
        STATE["replies"] = [{"content": ""}, {"content": '{"title": "Recovered"}'}]
        result = self.client().chat_json("s", "u", schema={"type": "object"})
        self.assertEqual(result["title"], "Recovered")
        requests = STATE["requests"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["format"], {"type": "object"})
        self.assertEqual(requests[1]["format"], "json", "the retry drops the schema")

    def test_an_empty_response_twice_is_an_error_that_names_the_model(self):
        STATE["replies"] = [{"content": ""}, {"content": "   "}]
        with self.assertRaises(LlmError) as caught:
            self.client().chat_json("s", "u", schema={"type": "object"})
        self.assertIn("qwen3.5:9b", str(caught.exception))
        self.assertIn("empty response", str(caught.exception))

    def test_a_reasoning_models_thinking_field_is_used_when_content_is_empty(self):
        STATE["replies"] = [{"content": "", "thinking": 'so: {"title": "From thinking"}'}]
        result = self.client().chat_json("s", "u", schema={"type": "object"})
        self.assertEqual(result["title"], "From thinking")
        self.assertEqual(len(STATE["requests"]), 1, "no retry was needed")

    def test_think_blocks_in_the_content_are_ignored(self):
        STATE["content"] = '<think>Maybe {"title": "wrong"}?</think>\n{"title": "Right"}'
        self.assertEqual(self.client().chat_json("s", "u")["title"], "Right")

    def test_thinking_is_switched_off_in_the_request(self):
        self.client().chat_json("s", "u")
        self.assertIs(STATE["request"]["think"], False)

    def test_unreachable_host(self):
        client = OllamaClient(LlmConfig(host="http://127.0.0.1:1", timeout=2))
        self.assertFalse(client.available())
        with self.assertRaises(LlmError):
            client.chat_json("s", "u")


if __name__ == "__main__":
    unittest.main()


class TestHostScope(unittest.TestCase):
    """Your own machine and your own network are fine; the internet is not."""

    def scope(self, url: str, resolved: list[str] | None = None) -> str:
        from scanvault.llm import host_scope

        return host_scope(url, resolver=lambda _: resolved or [])

    def test_this_machine(self):
        for url in ("http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434"):
            self.assertEqual(self.scope(url), "machine", url)

    def test_a_box_on_the_lan(self):
        for url in (
            "http://192.168.1.50:11434",
            "http://10.0.0.5:11434",
            "http://172.20.1.4:11434",
            "http://[fd00::1]:11434",
            "http://169.254.10.10:11434",
        ):
            self.assertEqual(self.scope(url), "network", url)

    def test_lan_hostnames_need_no_lookup(self):
        for url in ("http://ollama.local:11434", "http://nas.lan:11434", "http://box.internal"):
            self.assertEqual(self.scope(url), "network", url)

    def test_a_hostname_is_judged_by_what_it_resolves_to(self):
        self.assertEqual(self.scope("http://ollama.example.com", ["192.168.1.50"]), "network")
        self.assertEqual(self.scope("http://ollama.example.com", ["93.184.216.34"]), "public")

    def test_a_name_that_does_not_resolve_is_treated_as_public(self):
        self.assertEqual(self.scope("http://nowhere.example", []), "public")

    def test_a_mix_of_public_and_private_answers_is_public(self):
        self.assertEqual(self.scope("http://split.example", ["192.168.1.5", "8.8.8.8"]), "public")

    def test_public_addresses(self):
        self.assertEqual(self.scope("http://8.8.8.8:11434"), "public")


class TestPublicHostsNeedPermission(unittest.TestCase):
    def test_a_lan_host_is_allowed_without_asking(self):
        client = OllamaClient(LlmConfig(host="http://192.168.1.50:11434"))
        self.assertEqual(client.host, "http://192.168.1.50:11434")

    def test_localhost_is_allowed_without_asking(self):
        self.assertEqual(
            OllamaClient(LlmConfig(host="http://localhost:11434")).host, "http://localhost:11434"
        )

    def test_a_public_host_is_refused_by_default(self):
        with self.assertRaises(LlmError) as caught:
            OllamaClient(LlmConfig(host="http://8.8.8.8:11434"))
        message = str(caught.exception)
        self.assertIn("public internet", message)
        self.assertIn("allow_public_host", message)

    def test_a_public_host_works_when_asked_for(self):
        client = OllamaClient(LlmConfig(host="http://8.8.8.8:11434", allow_public_host=True))
        self.assertEqual(client.host, "http://8.8.8.8:11434")
