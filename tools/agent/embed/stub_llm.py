#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM for the embedded-chat feedback loop.

One scripted behaviour, chosen so the browser shows identity end to end:

* Planner call, no tool observation yet → call the echo MCP tool
  (``query_production_orders``) through whichever surface the planner was
  given: the native ``mcp__<connection>__query_production_orders`` tool when
  present, else ``execute_mcp`` with ``STUB_MCP_CONNECTION_ID``.
* Planner call after the observation → a short markdown answer that quotes
  what the MCP server *received*: the ``custom_metadata`` BOW injected
  (``employee_id`` from the member attribute, the dynamic ``workflow_id`` /
  ``erp_user`` from the host's metadata) and the record the host page put in
  the embed context. The Playwright run asserts on that text, so the identity
  that reached the tool is visible to a person, not only in a capture file.
* Anything else (titles, follow-ups, judges) → short generic text.

Run:  cd backend && STUB_PORT=9099 uv run python ../tools/agent/embed/stub_llm.py
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
CONNECTION_ID = os.environ.get("STUB_MCP_CONNECTION_ID", "")
ECHO_TOOL = "query_production_orders"


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


def _tool_names(body: dict) -> list[str]:
    names = []
    for t in body.get("tools") or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.append(fn["name"])
    return names


def _current_turn(text: str) -> str:
    """The prompt from the current user turn onwards.

    The planner renders earlier turns (their tool results included) in the
    context sections that precede <user_prompt>, and THIS turn's observations
    after it. Anything before the last <user_prompt> is history: an observation
    there belongs to an earlier turn and must not stop this turn from calling
    the tool."""
    idx = text.rfind("<user_prompt>")
    return text[idx:] if idx >= 0 else text


def _observation(text: str) -> dict | None:
    """The echoed MCP capture, if THIS turn already has a tool observation.

    The tool output is JSON text ({"received_arguments": …, "received_headers": …});
    it may appear escaped inside another JSON string, so find the last
    ``received_arguments`` and parse outwards defensively."""
    text = _current_turn(text)
    idx = text.rfind("received_arguments")
    if idx < 0:
        return None
    window = text[max(0, idx - 2): idx + 4000]
    # Unescape once if it is nested JSON.
    for candidate in (window, window.replace('\\"', '"').replace("\\\\", "\\")):
        start = candidate.find("{", 0)
        depth = 0
        for i in range(start, len(candidate)):
            if candidate[i] == "{":
                depth += 1
            elif candidate[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start:i + 1])
                    except Exception:
                        break
    return None


def _record_from_context(text: str) -> str:
    m = re.search(r'"recordId":\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def _plan(body: dict, text: str):
    names = _tool_names(body)
    obs = _observation(text)
    if obs is None:
        native = next((n for n in names if n.startswith("mcp__") and n.endswith(ECHO_TOOL)), None)
        if native:
            return [{"name": native, "arguments": {"prompt": "orders for the signed-in employee", "company": "ERP"}}], \
                "I'll look up your production orders."
        if "execute_mcp" in names and CONNECTION_ID:
            return [{"name": "execute_mcp", "arguments": {
                "connection_id": CONNECTION_ID, "tool_name": ECHO_TOOL,
                "arguments": {"prompt": "orders for the signed-in employee", "company": "ERP"},
            }}], "I'll look up your production orders."
        return None, "I can't reach the production orders tool from here."

    args = (obs.get("received_arguments") or {})
    meta = args.get("custom_metadata") or {}
    headers = obs.get("received_headers") or {}
    employee = meta.get("employee_id") or headers.get("x-employee-id") or "unknown"
    record = _record_from_context(text)
    rows = [
        ("Employee", f"`{employee}`"),
        ("Workflow", f"`{meta.get('workflow_id', '—')}`"),
        ("ERP user", f"`{meta.get('erp_user', '—')}`"),
        ("Record in view", f"`{record or '—'}`"),
        ("Header X-Employee-Id", f"`{headers.get('x-employee-id', '—')}`"),
    ]
    table = "| Field | Value |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)
    content = (
        f"Here are the production orders scoped to **{employee}**.\n\n{table}\n\n"
        "The orders tool received exactly this identity from BOW — nothing came from the browser."
    )
    return None, content


def _chunk(delta: dict, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream(content, tool_calls):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        if content:
            for i in range(0, len(content), 24):
                yield _sse(_chunk({"content": content[i:i + 24]}))
                time.sleep(0.02)  # visible streaming for the recording
        if tool_calls:
            for idx, tc in enumerate(tool_calls):
                yield _sse(_chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                                  "function": {"name": tc["name"], "arguments": ""}}]}))
                args = json.dumps(tc["arguments"])
                for i in range(0, len(args), 80):
                    yield _sse(_chunk({"tool_calls": [{"index": idx, "function": {"arguments": args[i:i + 80]}}]}))
        final = _chunk({}, finish="tool_calls" if tool_calls else "stop")
        final["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        yield _sse(final)
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _json(content, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                  "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                                 for tc in tool_calls]
    return JSONResponse({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
                         "created": int(time.time()), "model": "stub",
                         "choices": [{"index": 0, "message": message,
                                      "finish_reason": "tool_calls" if tool_calls else "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


DUMP = os.environ.get("STUB_DUMP_FILE")  # optional: last planner prompt, for debugging the loop


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text = _all_text(body)
    stream = bool(body.get("stream"))
    if DUMP and body.get("tools"):
        with open(DUMP, "w") as fh:
            fh.write(text)
    if body.get("tools"):
        tool_calls, content = _plan(body, text)
        return _stream(content, tool_calls) if stream else _json(content, tool_calls)
    generic = "Production orders"
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
