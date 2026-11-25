#!/usr/bin/env python3
"""OpenAI-compatible stub LLM for the connectionless-agent loop.

Planner calls (request carries `tools`) answer in plain text, quoting the
<agent> entries the planner was shown, so the chat transcript proves what
reached the model. Everything else gets short text. Every request body is
appended to $STUB_LOG (JSON lines).
"""
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("STUB_LOG", "/tmp/echo_stub_llm.jsonl")
PORT = int(os.environ.get("STUB_PORT", "9099"))


def _text_of(messages):
    out = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(p.get("text", "") for p in c if isinstance(p, dict))
    return "\n".join(out)


def _planner_answer(body):
    text = _text_of(body.get("messages")) + "\n" + json.dumps(body.get("system") or "")
    agents = re.findall(r'<(?:agent|data_source) name="([^"]+)" id="[^"]+"[^>]*>(.*?)</(?:agent|data_source)>', text, re.S)
    lines = []
    for name, inner in agents:
        desc = re.search(r"<description>(.*?)</description>", inner, re.S)
        no_data = "<no_data>" in inner
        tables = re.findall(r'<item name="([^"]+)"', inner)
        bits = []
        if desc:
            bits.append(f'description: "{desc.group(1).strip()}"')
        bits.append("no data connection" if no_data else f"{len(tables)} tables")
        lines.append(f"- **{name}**: " + "; ".join(bits))
    if not lines:
        return "I was not shown any agent."
    return "Agents I was given for this question:\n" + "\n".join(lines)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "stub", "object": "model"}]}).encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with open(LOG, "a") as f:
            f.write(json.dumps(body) + "\n")
        answer = _planner_answer(body) if body.get("tools") else "Connectionless agent check"
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            base = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "stub"}
            for piece in [answer]:
                chunk = {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            end = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            self.wfile.write(f"data: {json.dumps(end)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        resp = {"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": "stub",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
