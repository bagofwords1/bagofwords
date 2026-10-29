#!/usr/bin/env python3
"""Scripted OpenAI-compatible stub LLM for the delete_list_rows loop.

Plays a model that, asked to delete list rows, calls ``delete_list_rows``
once, then answers in plain text with the tool's result. Lets the full stack
(planner → tool → confirmation card in the browser → delete) run without a
paid LLM. Model judgment is NOT tested here — see the live loop for that.

- Planner calls (request has ``tools``): if the conversation has no tool call
  yet → one ``delete_list_rows`` call with STUB_DELETE_ARGS; otherwise a final
  plain-text answer quoting the last tool observation.
- Anything else (titles, judges): short generic text.

Env: STUB_DELETE_ARGS (JSON object, required), STUB_PORT (default 9098).
Run (from backend/): uv run python ../tools/agent/stub_llm_delete_rows.py
See docs/feedback-loops/2026-10-21-delete-list-rows-chat-tool.md.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import stub_llm as base  # noqa: E402  (reuse the SSE / JSON response helpers)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

app = FastAPI()
DELETE_ARGS = json.loads(os.environ["STUB_DELETE_ARGS"])


def _called_already(body: dict) -> bool:
    return any(m.get("role") == "tool" or m.get("tool_calls") for m in body.get("messages", []))


def _last_observation(body: dict) -> str:
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "tool":
            c = m.get("content")
            return c if isinstance(c, str) else json.dumps(c)[:400]
    return ""


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))
    if body.get("tools"):
        names = {t.get("function", {}).get("name") for t in body["tools"]}
        if not _called_already(body):
            if "delete_list_rows" not in names:
                text = "delete_list_rows is not in my tool set."
                return base._stream_response(text, None) if stream else base._json_response(text)
            return base._stream_response("Deleting the rows you asked for.",
                                         [{"name": "delete_list_rows", "arguments": DELETE_ARGS}])
        text = "Done. " + _last_observation(body)[:300]
        return base._stream_response(text, None) if stream else base._json_response(text)
    text = "Delete list rows"
    return base._stream_response(text, None) if stream else base._json_response(text)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9098")), log_level="warning")
