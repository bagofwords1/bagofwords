"""Scripted OpenAI-compatible LLM: first planner turn calls search_agents with
STUB_QUERY, subsequent turns answer in text. Logs every request."""
import json, os, sys, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
QUERY = json.loads(os.environ.get("STUB_QUERY", '["*sales*order*detail*"]'))
LOG = open(sys.argv[1], "a")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "stub-model", "object": "model"}]})
    def _json(self, obj):
        b = json.dumps(obj).encode(); self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = body.get("messages", []); tools = [t["function"]["name"] for t in body.get("tools", [])]
        has_result = any(m.get("role") == "tool" for m in msgs)
        call = "search_agents" in tools and not has_result
        LOG.write(json.dumps({"t": time.time(), "stream": body.get("stream"), "tools": len(tools), "call": call}) + "\n"); LOG.flush()
        text = "Search finished." if tools else "Stub title"
        if not body.get("stream"):
            msg = {"role": "assistant", "content": text}
            if call:
                msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                       "function": {"name": "search_agents", "arguments": json.dumps({"query": QUERY})}}]}
            return self._json({"id": "x", "object": "chat.completion", "model": "stub-model", "choices": [
                {"index": 0, "message": msg, "finish_reason": "tool_calls" if call else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        def ev(delta, fin=None, usage=None):
            c = {"id": "x", "object": "chat.completion.chunk", "model": "stub-model",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
            if usage: c["usage"] = usage
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode()); self.wfile.flush()
        if call:
            ev({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                "function": {"name": "search_agents", "arguments": json.dumps({"query": QUERY})}}]})
            ev({}, "tool_calls")
        else:
            ev({"role": "assistant", "content": text}); ev({}, "stop")
        ev({}, None, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
ThreadingHTTPServer(("127.0.0.1", 8799), H).serve_forever()
