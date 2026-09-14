"""ClaudeLLM against a local stub of the streaming Messages API: request shape, parsing, truncation, refusal."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import pytest

from repo2readme.llm import ClaudeLLM, RefusalError, TruncatedError
from repo2readme.schemas import ChunkSummary

GOOD = json.dumps({"files": [{"path": "a.py", "purpose": "p", "key_symbols": [], "facts": [], "is_entrypoint": False}]})


def _sse(text: str, stop: str) -> bytes:
    events = [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                              "model": "claude-opus-5", "content": [], "stop_reason": None,
                                              "stop_sequence": None,
                                              "usage": {"input_tokens": 10, "output_tokens": 1,
                                                        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


@pytest.fixture
def stub():
    state = {"reply": (GOOD, "end_turn"), "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"].append({
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(_sse(*state["reply"]))

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = anthropic.AsyncAnthropic(api_key="sk-test", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0)
    yield state, client
    server.shutdown()


KW = dict(stage="map", system=[{"type": "text", "text": "x"}], user="hi", schema=ChunkSummary, effort="medium", max_tokens=100)


async def test_request_shape_and_parse(stub):
    state, client = stub
    llm = ClaudeLLM("claude-opus-5", client=client)
    out = await llm.generate(**KW)
    assert out.files[0].path == "a.py"
    req = state["requests"][0]
    body = req["body"]
    assert req["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert body["model"] == "claude-opus-5" and body["stream"] is True
    assert body["thinking"] == {"type": "adaptive"} and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert llm.usage.stages["map"].cache_read_tokens == 5


async def test_truncation_is_distinguishable(stub):
    state, client = stub
    state["reply"] = ('{"files":[{"path":"a.py","purp', "max_tokens")
    with pytest.raises(TruncatedError):
        await ClaudeLLM("claude-opus-5", client=client).generate(**KW)


async def test_refusal(stub):
    state, client = stub
    state["reply"] = ("", "refusal")
    with pytest.raises(RefusalError):
        await ClaudeLLM("claude-opus-5", client=client).generate(**KW)


async def test_haiku_omits_thinking_effort_and_fallbacks(stub):
    state, client = stub
    await ClaudeLLM("claude-haiku-4-5", client=client).generate(**KW)
    body = state["requests"][0]["body"]
    assert "thinking" not in body and "fallbacks" not in body and "effort" not in body["output_config"]
