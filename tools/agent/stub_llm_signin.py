#!/usr/bin/env python3
"""Scripted OpenAI-compatible LLM for the agent-sign-in loop
(docs/feedback-loops/agent-signin-required.md).

Stands in for a real model when no funded LLM key is available. It decides
ONLY from what the planner prompt actually shows it, so it exercises the
prompt contract rather than bypassing it:

  * an <agents_needing_signin> block listing the Sales agent → it calls
    request_agent_signin for that agent (the behavior the block asks for);
  * the Sales agent's `orders` table in the schema (the user is connected)
    → create_data over it, then a short final answer;
  * neither → it says no agent it can see has sales data (what a model
    answering from its context would have to conclude).

Codegen calls (prompt mentions generate_df) get a query against the
Sales agent's orders table.

    STUB_SALES_NAME (default "Sales"), STUB_PORT (default 9099)
    cd backend && uv run python ../tools/agent/stub_llm_signin.py
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
SALES = os.environ.get("STUB_SALES_NAME", "Sales")
BLOCK = re.compile(r"<agents_needing_signin[^>]*>(.*?)</agents_needing_signin>", re.S)


def _all_text(body: dict) -> str:
    parts = [body.get("system") or ""]
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts += [i.get("text", "") for i in c if isinstance(i, dict) and i.get("type") == "text"]
    return "\n".join(parts)


def _turn_tool_results(body: dict) -> int:
    """Tool results since the latest user prompt (i.e. in THIS turn)."""
    msgs = body.get("messages", [])
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"
                     and "<user_prompt>" in json.dumps(m.get("content"))), default=-1)
    return sum(1 for m in msgs[last_user + 1:] if m.get("role") == "tool")


def _planner(text: str, body: dict):
    done = _turn_tool_results(body)
    if done:
        return None, f"Here are total sales by region for last quarter, from the {SALES} agent."
    for block in BLOCK.findall(text):
        m = re.search(r'<agent id="([^"]+)" name="' + re.escape(SALES) + r'"', block)
        if m:
            return [{"name": "request_agent_signin", "arguments": {
                "agent_ids": [m.group(1)],
                "title": f"Asking to connect {SALES}",
                "message": (f"Sales figures come from the **{SALES}** agent, which you haven't "
                            "connected yet. Connect it below, then ask again."),
            }}], None
    outside = BLOCK.sub("", text)
    ids = re.findall(r'id="([0-9a-f-]{36})"[^>]*name="' + re.escape(SALES) + r'"', outside) or \
        re.findall(r'name="' + re.escape(SALES) + r'"[^>]*id="([0-9a-f-]{36})"', outside)
    if ids and "orders" in outside:
        return [{"name": "create_data", "arguments": {
            "title": "Sales by region — last quarter",
            "user_prompt": "What were total sales by region last quarter?",
            "interpreted_prompt": "Sum of order amount by region for Q3 2025 (Jul–Sep).",
            "tables_by_source": [{"data_source_id": ids[0], "tables": ["orders"]}],
            "visualization_type": "bar_chart",
        }}], None
    return None, "None of the agents available to me has sales data, so I can't answer this."


def _codegen() -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    client = next(v for k, v in ds_clients.items() if k == '{SALES}' or k.startswith('{SALES}:'))\n"
        "    df = client.execute_query(\"SELECT region, SUM(amount) AS total_sales, COUNT(*) AS orders "
        "FROM orders WHERE order_date >= '2025-07-01' AND order_date < '2025-10-01' "
        "GROUP BY region ORDER BY total_sales DESC\")\n"
        "    return df\n"
    )


def _chunk(delta, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(p):
    return f"data: {json.dumps(p)}\n\n"


def _stream(content, tool_calls):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        for i in range(0, len(content or ""), 40):
            yield _sse(_chunk({"content": content[i:i + 40]}))
        for idx, tc in enumerate(tool_calls or []):
            yield _sse(_chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:8]}",
                                               "type": "function", "function": {"name": tc["name"], "arguments": ""}}]}))
            yield _sse(_chunk({"tool_calls": [{"index": idx, "function": {"arguments": json.dumps(tc["arguments"])}}]}))
        final = _chunk({}, finish="tool_calls" if tool_calls else "stop")
        final["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        yield _sse(final)
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _json(content):
    return JSONResponse({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
                         "created": int(time.time()), "model": "stub",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    text = _all_text(body)
    stream = bool(body.get("stream"))
    if body.get("tools"):
        calls, content = _planner(text, body)
        return _stream(content, calls) if stream else _json(content or "")
    if "generate_df" in text:
        code = _codegen()
        return _stream(code, None) if stream else _json(code)
    reply = "Sales by region"
    return _stream(reply, None) if stream else _json(reply)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
