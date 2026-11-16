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
  the first client in ``ds_clients`` — or, when the user's message carries
  ``SQL: <statement>``, exactly that statement (any engine: pg_sleep,
  WAITFOR DELAY, ...).
- With ``agents`` in STUB_DS_FILE (``{"warehouse": {"id": ..., "table": ...}}``)
  the agent named in the message is targeted. The final answer reports a
  query cancelled by a connection admin or refused as "connection busy".
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
import re
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


_SQL_MARK = re.compile(r"BOWSQL<<(.*?)>>BOWSQL", re.S)


def _message_text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(i.get("text", "") for i in c if isinstance(i, dict))
    return ""


def _user_request(body: dict) -> str:
    users = [m for m in body.get("messages", []) if m.get("role") == "user"]
    return _message_text(users[-1]) if users else ""


def _pick_source(text: str) -> dict:
    """``agents`` in STUB_DS_FILE ({name: {id, table}}): the one named in the message."""
    src = _source()
    agents = src.get("agents") or {}
    lowered = text.lower()
    for name, agent in agents.items():
        if name.lower() in lowered:
            return {**src, **agent}
    return src


def _planner(body: dict):
    tool_msgs = [m for m in body.get("messages", []) if m.get("role") == "tool"]
    if tool_msgs:
        result = _message_text(tool_msgs[-1])
        if "cancelled by a connection admin" in result:
            return "Your query was cancelled by an admin of the connection, so there is no result.", None
        if "Connection busy" in result:
            return ("The connection is busy right now — it is already running its maximum number of "
                    "queries. Please try again in a moment."), None
        return "Here is the result of your query.", None
    system = " ".join(_message_text(m) for m in body.get("messages", []) if m.get("role") == "system")
    if "Knowledge Harness" in system:
        return "Nothing to capture.", None
    request = _user_request(body)
    src = _pick_source(request)
    tool = src.get("tool") or os.environ.get("STUB_TOOL", "create_data")
    targets = [{"data_source_id": src["id"], "tables": [src.get("table", "orders")]}]
    # "SQL: <statement>" in the message: run exactly that statement.
    # The rendered prompt carries more context after the message: one line only.
    # The rendered prompt carries more context after the message (and closing
    # tags on the same line): keep the statement only.
    sql = request.split("SQL:", 1)[1].strip().splitlines()[0].split("<", 1)[0].strip() if "SQL:" in request else None
    marker = f" BOWSQL<<{sql}>>BOWSQL" if sql else ""
    if tool == "inspect_data":
        args = {"user_prompt": "Profile the orders table" + marker, "tables_by_source": targets}
    else:
        args = {
            "title": "Orders by region",
            "user_prompt": "Total order amount by region" + marker,
            "interpreted_prompt": "Sum of orders.amount grouped by region." + marker,
            "tables_by_source": targets,
        }
    return "Running the query now.", {"name": tool, "arguments": args}


def _codegen(text: str = "") -> str:
    found = _SQL_MARK.search(text)
    if found:
        return (
            "def generate_df(ds_clients, excel_files):\n"
            "    key = list(ds_clients.keys())[0]\n"
            f"    df = ds_clients[key].execute_query({found.group(1).strip()!r})\n"
            "    print('rows:', len(df))\n"
            "    return df\n"
        )
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
        code = _codegen(_all_text(body))
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
