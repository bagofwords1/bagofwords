#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM for concurrency verification.

Serves POST /v1/chat/completions (stream + non-stream). Behavior:

- Planner calls (request has `tools`): scripted rounds driven by how many
  tool observations already appear in the prompt —
    round 1: N parallel `inspect_data` tool_calls (one per seeded source)
    round 2: N parallel `create_data` tool_calls (when STUB_PHASES has
             "create"; skipped otherwise)
    final:   plain-text answer, finish_reason=stop
  The stub deliberately IGNORES parallel_tool_calls=False — it plays the
  role of a provider that emits parallel tool_use blocks, which is exactly
  the input the concurrent dispatch path must handle.

- Codegen calls (prompt mentions generate_df): returns a plain Python
  function querying the source referenced in the prompt (orders_<i>).

- Anything else (titles, follow-ups, judges): short generic text.

Config env vars:
  STUB_SOURCES_FILE  JSON file: [{"id": "...", "name": "sqlite_source_1",
                     "table": "orders_1"}, ...]  (required)
  STUB_PHASES        comma list of "inspect","create" (default "inspect,create")
  STUB_PORT          default 9099

Run:  uv run python ../tools/agent/stub_llm.py   (from backend/, for deps)
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

SOURCES = json.loads(open(os.environ["STUB_SOURCES_FILE"]).read()) if os.environ.get("STUB_SOURCES_FILE") else []
PHASES = [p.strip() for p in os.environ.get("STUB_PHASES", "inspect,create").split(",") if p.strip()]


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


def _observation_count(text: str) -> int:
    return text.count('"execution_number"')


def _planner_tool_calls(text: str):
    """Decide which batch of tool calls (or final answer) to emit."""
    n = len(SOURCES)
    obs = _observation_count(text)
    want_inspect = "inspect" in PHASES
    want_create = "create" in PHASES

    if want_inspect and obs < n:
        return [
            {
                "name": "inspect_data",
                "arguments": {
                    "user_prompt": f"Inspect row counts and amount stats in {s['table']}",
                    "tables_by_source": [{"data_source_id": s["id"], "tables": [s["table"]]}],
                },
            }
            for s in SOURCES
        ], "Inspecting all sources in parallel."
    if want_create and obs < (n * 2 if want_inspect else n):
        return [
            {
                "name": "create_data",
                "arguments": {
                    "title": f"Orders by region — {s['name']}",
                    "user_prompt": f"Summarize {s['table']} by region",
                    "interpreted_prompt": (
                        f"Aggregate {s['table']}: count of orders and total amount grouped by region."
                    ),
                    "tables_by_source": [{"data_source_id": s["id"], "tables": [s["table"]]}],
                },
            }
            for s in SOURCES
        ], "Creating one summary per source in parallel."
    return None, (
        f"All {n} sources processed. Each orders table was inspected and "
        "summarized by region. Parallel multi-tool dispatch verified."
    )


def _codegen_response(text: str) -> str:
    m = re.search(r"orders_(\d+)", text)
    idx = m.group(1) if m else "1"
    name = next((s["name"] for s in SOURCES if s["table"].endswith(f"_{idx}")), f"sqlite_source_{idx}")
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    df = ds_clients['{name}'].execute_query('SELECT region, COUNT(*) AS orders_count, "
        f"SUM(amount) AS total_amount FROM orders_{idx} GROUP BY region ORDER BY region')\n"
        "    print('rows:', len(df))\n"
        "    return df\n"
    )


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


def _stream_response(content: str | None, tool_calls: list | None):
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
        tool_calls, content = _planner_tool_calls(text)
        if stream:
            return _stream_response(content, tool_calls)
        return _json_response(content)

    if "generate_df" in text:
        code = _codegen_response(text)
        return _stream_response(code, None) if stream else _json_response(code)

    generic = "Multi-source concurrency probe."
    return _stream_response(generic, None) if stream else _json_response(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
