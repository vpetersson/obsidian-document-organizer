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
        STATE["request"] = json.loads(self.rfile.read(length))
        if STATE.get("fail"):
            self._send(500, {"error": "model runner crashed"})
            return
        self._send(200, {"message": {"role": "assistant", "content": STATE["content"]}})


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

    def test_unreachable_host(self):
        client = OllamaClient(LlmConfig(host="http://127.0.0.1:1", timeout=2))
        self.assertFalse(client.available())
        with self.assertRaises(LlmError):
            client.chat_json("s", "u")


if __name__ == "__main__":
    unittest.main()
