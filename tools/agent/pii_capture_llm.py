#!/usr/bin/env python3
"""OpenAI-compatible mock LLM that records every prompt it receives.

Used by the per-provider PII exemption loop
(docs/feedback-loops/pii-provider-exemption.md). One process plays several
providers: the first path segment names the provider, so register providers in
BoW with base URLs such as

    http://127.0.0.1:9199/trusted/v1
    http://127.0.0.1:9199/public/v1

Every chat-completions request is appended to PII_CAPTURE_FILE as one JSON line
``{"provider": "<segment>", "text": "<all prompt text>", "ts": ...}`` and gets a
short plain-text answer (streamed or not), so a planner turn ends after one
call. When the user's request contains reversible placeholders
(``<EMAIL_3fa9c21d>``), the answer repeats them. ``GET /captures`` returns the recorded lines; ``DELETE /captures`` clears
them.

Env: PII_CAPTURE_PORT (default 9199), PII_CAPTURE_FILE
(default /tmp/bow-agent/pii_captures.jsonl).

Run (from backend/, for deps):  uv run python ../tools/agent/pii_capture_llm.py
"""
import json
import os
import re
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

PORT = int(os.environ.get("PII_CAPTURE_PORT", "9199"))
CAPTURE_FILE = os.environ.get("PII_CAPTURE_FILE", "/tmp/bow-agent/pii_captures.jsonl")
ANSWER = "Noted. I can see the customer's contact details in your message."
# Reversible placeholders (the ``pseudonymize`` action), e.g. <EMAIL_3fa9c21d>.
TOKEN_RE = re.compile(r"<[A-Z][A-Z0-9_]*_[0-9a-f]{8,12}>")


def _answer(body: dict) -> str:
    """Echo the placeholders found in the user's own request, so a loop can
    show them coming back as real values in the UI."""
    text = "\n".join(_text_of(m.get("content")) for m in body.get("messages") or [] if m.get("role") == "user")
    asks = re.findall(r"<user_prompt>(.*?)</user_prompt>", text, re.S)
    tokens = list(dict.fromkeys(TOKEN_RE.findall(asks[-1] if asks else text)))
    if not tokens:
        return ANSWER
    return "Noted. I will follow up with " + " and ".join(tokens[:4]) + "."

app = FastAPI()


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text") or part.get("content") or ""
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text") or part.get("content") or "", str)
        )
    return ""


def _record(provider: str, body: dict) -> None:
    text = "\n".join(_text_of(m.get("content")) for m in body.get("messages") or [])
    os.makedirs(os.path.dirname(CAPTURE_FILE), exist_ok=True)
    with open(CAPTURE_FILE, "a") as fh:
        fh.write(json.dumps({"provider": provider, "model": body.get("model"), "text": text, "ts": time.time()}) + "\n")


def _chunk(cid: str, model: str, delta=None, finish=None, usage=None) -> str:
    """One SSE chunk; with ``usage`` it is the trailing usage-only chunk."""
    payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
    if usage is not None:
        payload["choices"] = []
        payload["usage"] = usage
    else:
        payload["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    return f"data: {json.dumps(payload)}\n\n"


@app.post("/{provider}/v1/chat/completions")
async def chat(provider: str, request: Request):
    body = await request.json()
    _record(provider, body)
    model = body.get("model") or "mock"
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    usage = {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22}
    answer = _answer(body)
    if body.get("stream"):
        def gen():
            yield _chunk(cid, model, {"role": "assistant", "content": ""})
            yield _chunk(cid, model, {"content": answer})
            yield _chunk(cid, model, finish="stop")
            yield _chunk(cid, model, usage=usage)
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": usage,
    })


@app.get("/{provider}/v1/models")
async def models(provider: str):
    return {"object": "list", "data": [{"id": "mock", "object": "model"}]}


@app.get("/captures")
async def captures():
    if not os.path.exists(CAPTURE_FILE):
        return []
    with open(CAPTURE_FILE) as fh:
        return [json.loads(line) for line in fh if line.strip()]


@app.delete("/captures")
async def clear_captures():
    if os.path.exists(CAPTURE_FILE):
        os.remove(CAPTURE_FILE)
    return {"cleared": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
