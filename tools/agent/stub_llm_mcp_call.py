#!/usr/bin/env python3
"""Scripted OpenAI-compatible stub LLM: call one MCP tool if it is offered.

Planner requests (body has `tools`):
  * no tool result yet: if a tool named ``mcp__<conn>__<STUB_MCP_TOOL>`` is in
    the request's tool list, call it with STUB_MCP_ARGS; otherwise answer
    "MCP TOOL NOT OFFERED".
  * after a tool result: answer "MCP RESULT: <first 300 chars of it>".
Every planner request's tool names are appended to STUB_LOG (JSON lines), so
the loop can show exactly what the run offered.

    cd backend && STUB_PORT=9311 uv run python ../tools/agent/stub_llm_mcp_call.py

Register it with setup_stub_llm.py --stub-url http://127.0.0.1:9311/v1.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

TOOL = os.environ.get("STUB_MCP_TOOL", "cost_report")
ARGS = json.loads(os.environ.get("STUB_MCP_ARGS", '{"days": 30}'))
LOG = os.environ.get("STUB_LOG", "/tmp/bow-agent/stub_llm_mcp_call.jsonl")

app = FastAPI()


def _chunk(delta: dict, finish=None) -> str:
    return "data: " + json.dumps({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
        "created": int(time.time()), "model": "stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **({"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}} if finish else {}),
    }) + "\n\n"


def _reply(content: str | None, call: dict | None, stream: bool):
    if not stream:
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content or ""},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })

    def gen():
        yield _chunk({"role": "assistant"})
        if content:
            yield _chunk({"content": content})
        if call:
            yield _chunk({"tool_calls": [{"index": 0, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                          "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}]})
        yield _chunk({}, finish="tool_calls" if call else "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))
    if not body.get("tools"):
        return _reply("ok", None, stream)

    names = [(t.get("function") or {}).get("name") or t.get("name") for t in body["tools"]]
    tool_results = [m for m in body.get("messages", []) if m.get("role") == "tool"]
    with open(LOG, "a") as f:
        f.write(json.dumps({"tools": names, "tool_results": len(tool_results)}) + "\n")

    if tool_results:
        c = tool_results[-1].get("content")
        text = c if isinstance(c, str) else json.dumps(c)
        return _reply(f"MCP RESULT: {text[:300]}", None, stream)
    target = next((n for n in names if n and n.startswith("mcp__") and n.endswith(f"__{TOOL}")), None)
    if not target:
        return _reply("MCP TOOL NOT OFFERED", None, stream)
    return _reply(None, {"name": target, "arguments": ARGS}, stream)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9311")), log_level="warning")
