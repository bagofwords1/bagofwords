#!/usr/bin/env python3
"""Deterministic OpenAI-compatible LLM for run-control localhost QA.

The scripted planner does this:
1. emits one invalid create_note call (a recoverable tool error),
2. creates a Plan with three unchecked items,
3. completes three substantive search rounds,
4. tries to finish prematurely,
5. after the harness rejects that finish, checks off the Plan,
6. returns the verified final answer.

Run from backend/: ``uv run python ../tools/agent/run_control_stub_llm.py``.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
_planner_calls = 0


def _all_text(body: dict) -> str:
    parts: list[str] = []

    def collect(value):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    # Collect the whole request rather than assuming one OpenAI request shape;
    # both Chat Completions messages and Responses-style input can reach a
    # compatible endpoint depending on the client configuration.
    collect(body)
    return "\n".join(parts)


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "run-control-stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream_response(content: str | None, tool_call: dict | None):
    def generate():
        yield _sse(_chunk({"role": "assistant"}))
        if content:
            for index in range(0, len(content), 60):
                yield _sse(_chunk({"content": content[index:index + 60]}))
        if tool_call:
            call_id = f"call_{uuid.uuid4().hex[:8]}"
            yield _sse(_chunk({"tool_calls": [{
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {"name": tool_call["name"], "arguments": ""},
            }]}))
            arguments = json.dumps(tool_call["arguments"])
            for index in range(0, len(arguments), 80):
                yield _sse(_chunk({"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": arguments[index:index + 80]},
                }]}))
        finish = "tool_calls" if tool_call else "stop"
        final = _chunk({}, finish=finish)
        final["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        yield _sse(final)
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


def _json_response(content: str) -> JSONResponse:
    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "run-control-stub",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    })


def _planner_step(call_number: int, text: str) -> tuple[str | None, dict | None]:
    if call_number == 0:
        # Missing required content: proves a validation error no longer ends
        # the run. The next planner iteration creates the note correctly.
        return "Testing recovery from a malformed note call.", {
            "name": "create_note",
            "arguments": {"title": "Plan"},
        }
    if call_number == 1:
        return "Creating the durable task checklist.", {
            "name": "create_note",
            "arguments": {
                "title": "Plan",
                "content": (
                    "- [ ] inspect the first angle\n"
                    "- [ ] inspect the second angle\n"
                    "- [ ] reconcile evidence before the final answer"
                ),
            },
        }
    if call_number in {2, 3, 4}:
        angle = call_number - 1
        return f"Inspecting angle {angle}.", {
            "name": "search_agents",
            "arguments": {
                "query": [f"run-control-angle-{angle}"],
                "limit": 5,
                "title": f"Inspecting angle {angle}",
            },
        }
    if call_number == 5:
        return "Premature answer: this must be rejected while the Plan is unchecked.", None
    if call_number == 6:
        match = re.search(
            r'<note\s+id="([^"]+)"\s+title="Plan"\s*>',
            text,
        ) or re.search(
            r"note_id\D+([0-9a-f]{8}-[0-9a-f-]{27,})",
            text,
            flags=re.IGNORECASE,
        )
        note_id = match.group(1) if match else "missing-note-id"
        return "Reconciling the checklist after the rejected completion.", {
            "name": "edit_note",
            "arguments": {
                "note_id": note_id,
                "content": (
                    "- [x] inspect the first angle\n"
                    "- [x] inspect the second angle\n"
                    "- [x] reconcile evidence before the final answer"
                ),
            },
        }
    return "Verified completion after checklist reconciliation.", None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    global _planner_calls
    body = await request.json()
    stream = bool(body.get("stream"))
    if body.get("tools"):
        call_number = _planner_calls
        _planner_calls += 1
        content, tool_call = _planner_step(call_number, _all_text(body))
        if stream:
            return _stream_response(content, tool_call)
        return _json_response(content or "")
    generic = "Run-control auxiliary response."
    return _stream_response(generic, None) if stream else _json_response(generic)


@app.post("/reset")
async def reset():
    global _planner_calls
    _planner_calls = 0
    return {"ok": True}


@app.get("/stats")
async def stats():
    return {"planner_calls": _planner_calls}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "gpt-5.4", "object": "model"}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("STUB_PORT", "9099")),
        log_level="warning",
    )
