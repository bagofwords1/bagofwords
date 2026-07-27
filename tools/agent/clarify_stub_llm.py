#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM for the clarify multi-pick loop.

Serves POST /v1/chat/completions (stream + non-stream). Behavior:

- Planner calls (request has `tools`): first round emits ONE `clarify` tool
  call containing a multi_select question, a single-pick question, and a
  free-form question. Once the conversation contains the submitted answers
  (the "Q: ... A: ..." turn the ClarifyTool form sends), it returns a plain
  text acknowledgement echoing them, finish_reason=stop.
- Anything else (titles, follow-ups, judges): short generic text.

Run:  cd backend && uv run python ../tools/agent/clarify_stub_llm.py
Env:  STUB_PORT (default 9099)
"""
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

CLARIFY_CALL = {
    "name": "clarify",
    "arguments": {
        "questions": [
            {
                "text": "Which metrics should the dashboard include?",
                "options": ["Revenue", "Orders", "Sessions", "Conversion rate", "Other…"],
                "multi_select": True,
            },
            {
                "text": "Which date range should I use?",
                "options": ["Last 7 days", "Last 30 days", "Last 90 days"],
            },
            {
                "text": "What should the dashboard title be?",
            },
        ],
        "context": "dashboard scope is ambiguous — metrics, range and title unknown",
    },
}


def _all_text(body: dict) -> str:
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
    return "\n".join(parts)


def _answered(text: str) -> bool:
    # The submitted form re-enters the loop as "Q: <question>  \nA: <answer>".
    return "Which metrics should the dashboard include?" in text and "\nA: " in text


def _chunk(delta: dict, finish=None):
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream_response(content, tool_calls):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        if content:
            for i in range(0, len(content), 40):
                yield _sse(_chunk({"content": content[i:i + 40]}))
        if tool_calls:
            for idx, tc in enumerate(tool_calls):
                yield _sse(_chunk({"tool_calls": [{
                    "index": idx,
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": ""},
                }]}))
                args = json.dumps(tc["arguments"])
                for i in range(0, len(args), 80):
                    yield _sse(_chunk({"tool_calls": [{
                        "index": idx,
                        "function": {"arguments": args[i:i + 80]},
                    }]}))
        finish = "tool_calls" if tool_calls else "stop"
        final = _chunk({}, finish=finish)
        final["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        yield _sse(final)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _json_response(content: str):
    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "stub",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    })


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text = _all_text(body)
    stream = bool(body.get("stream"))

    if body.get("tools"):
        if _answered(text):
            content = "Got it — building the dashboard with your selections."
            tool_calls = None
        else:
            content = "I need a few details before building the dashboard."
            tool_calls = [CLARIFY_CALL]
        if stream:
            return _stream_response(content, tool_calls)
        return _json_response(content)

    generic = "Clarify multi-pick loop."
    return _stream_response(generic, None) if stream else _json_response(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
