#!/usr/bin/env python3
"""Deterministic OpenAI-compatible LLM for the "Stop kills the data query" loop.

The planner calls one data tool against the seeded source; its generated code
runs a query that takes STUB_SLEEP_S seconds (``SELECT pg_sleep(N)`` on
Postgres). The probe presses Stop while that query is running and watches
``pg_stat_activity`` to see whether the statement really stops.

- Planner call (request has `tools`), no tool result yet: one call to
  STUB_TOOL (``create_data`` or ``inspect_data``) targeting the data source in
  STUB_DS_FILE. Once a tool result is in the prompt: a short final answer.
- Codegen call (prompt mentions generate_df): code running the slow query on
  the first client in ``ds_clients``.
- Anything else (titles, judges, suggestions): short generic text.

Env:
  STUB_PORT     default 9099
  STUB_TOOL     create_data (default) | inspect_data — re-read per request
  STUB_SLEEP_S  query duration in seconds (default 120)
  STUB_DS_FILE  JSON {"id": "<data_source_id>", "table": "orders",
                "tool": "inspect_data"?}, re-read per request so it can be
                written after the source exists ("tool" overrides STUB_TOOL)

Run from backend/ (for deps): uv run python ../tools/agent/stop_query_stub_llm.py
"""
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
_calls = {"planner": 0, "codegen": 0}


def _source() -> dict:
    path = os.environ.get("STUB_DS_FILE", "")
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"id": "missing", "table": "orders"}


def _all_text(body: dict) -> str:
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(i.get("text", "") for i in c if isinstance(i, dict))
    return "\n".join(parts)


def _chunk(delta: dict, finish=None) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream(content: str | None, tool_call: dict | None):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        if content:
            for i in range(0, len(content), 40):
                yield _sse(_chunk({"content": content[i:i + 40]}))
        if tool_call:
            yield _sse(_chunk({"tool_calls": [{
                "index": 0, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                "function": {"name": tool_call["name"], "arguments": json.dumps(tool_call["arguments"])},
            }]}))
        final = _chunk({}, finish="tool_calls" if tool_call else "stop")
        final["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        yield _sse(final)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _json(content: str):
    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": int(time.time()), "model": "stub",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    })


def _planner(body: dict):
    if any(m.get("role") == "tool" for m in body.get("messages", [])):
        return "The query step finished.", None
    src = _source()
    tool = src.get("tool") or os.environ.get("STUB_TOOL", "create_data")
    targets = [{"data_source_id": src["id"], "tables": [src.get("table", "orders")]}]
    if tool == "inspect_data":
        args = {"user_prompt": "Profile the orders table", "tables_by_source": targets}
    else:
        args = {
            "title": "Orders by region",
            "user_prompt": "Total order amount by region",
            "interpreted_prompt": "Sum of orders.amount grouped by region.",
            "tables_by_source": targets,
        }
    return "Running the query now.", {"name": tool, "arguments": args}


def _codegen() -> str:
    sleep_s = int(float(os.environ.get("STUB_SLEEP_S", "120")))
    return (
        "def generate_df(ds_clients, excel_files):\n"
        "    key = list(ds_clients.keys())[0]\n"
        "    df = ds_clients[key].execute_query(\n"
        # The CTE runs pg_sleep exactly once (one row, cross-joined).
        f"        'WITH slow AS (SELECT pg_sleep({sleep_s})) '\n"
        "        'SELECT region, SUM(amount) AS total FROM orders, slow GROUP BY region ORDER BY region'\n"
        "    )\n"
        "    print('rows:', len(df))\n"
        "    return df\n"
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))
    if body.get("tools"):
        _calls["planner"] += 1
        content, tool_call = _planner(body)
        return _stream(content, tool_call) if stream else _json(content)
    if "generate_df" in _all_text(body):
        _calls["codegen"] += 1
        code = _codegen()
        return _stream(code, None) if stream else _json(code)
    generic = "Stop-query probe."
    return _stream(generic, None) if stream else _json(generic)


@app.get("/stats")
async def stats():
    return _calls


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "stub", "object": "model"}]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
