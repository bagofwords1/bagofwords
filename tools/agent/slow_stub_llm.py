#!/usr/bin/env python3
"""Slow-streaming OpenAI-compatible stub LLM for SSE reconnect verification.

Planner calls (request carries `tools`) stream a long plain-text final answer
token-by-token with a configurable delay, so a completion stays in_progress
for STUB_DURATION_S seconds — long enough to refresh the page / kill the
connection mid-stream and observe reconnect behavior.

Every other call (titles, judges, suggestions) returns instantly.

Env:
  STUB_PORT        default 9099
  STUB_DURATION_S  target planner stream duration (default 60)
"""
import json
import os
import time
import uuid
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

DURATION_S = float(os.environ.get("STUB_DURATION_S", "60"))

WORDS = (
    "Here is the streaming reliability report. "
    + " ".join(f"Progress marker {i:03d} — the agent is still working and streaming tokens." for i in range(1, 201))
    + " All done: the full answer streamed to the end."
).split(" ")


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


def _slow_stream():
    delay = DURATION_S / max(len(WORDS), 1)

    async def gen():
        yield _sse(_chunk({"role": "assistant"}))
        for w in WORDS:
            yield _sse(_chunk({"content": w + " "}))
            await asyncio.sleep(delay)
        final = _chunk({}, finish="stop")
        final["usage"] = {"prompt_tokens": 100, "completion_tokens": 500, "total_tokens": 600}
        yield _sse(final)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _fast(content: str, stream: bool):
    if not stream:
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "stub",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    async def gen():
        yield _sse(_chunk({"role": "assistant"}))
        yield _sse(_chunk({"content": content}))
        final = _chunk({}, finish="stop")
        final["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        yield _sse(final)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))
    if body.get("tools"):
        return _slow_stream() if stream else _fast("Non-stream planner answer.", False)
    return _fast("Stub auxiliary answer.", stream)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
