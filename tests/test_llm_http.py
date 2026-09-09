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


class TestHostIsInformationOnly(unittest.TestCase):
    """Where ollama runs is the user's choice; we only report it."""

    def test_local_hosts_are_recognised(self):
        from scanvault.llm import is_local_host

        for url in ("http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434"):
            self.assertTrue(is_local_host(url), url)
        for url in ("http://192.168.1.50:11434", "http://ollama.example.com:11434"):
            self.assertFalse(is_local_host(url), url)

    def test_any_host_is_accepted(self):
        for host in (
            "http://localhost:11434",
            "http://192.168.1.50:11434",
            "http://ollama.example.com:11434",
        ):
            self.assertEqual(OllamaClient(LlmConfig(host=host)).host, host)
