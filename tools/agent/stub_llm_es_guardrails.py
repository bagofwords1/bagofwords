#!/usr/bin/env python3
"""Scripted OpenAI-compatible stub LLM: an agent that runs into Elasticsearch
query guardrails and adapts.

Drives a real agent run (planner -> create_data -> codegen -> execute) against
a real cluster without a paid model, so the guardrails can be verified in the
UI exactly as a user meets them:

  planner round 1: create_data "error volume across every index"
      codegen attempt 1: `"index": "*"`                  -> all-index block
      codegen attempt 2 (sees that error): nested aggs  -> aggregation limits
  planner round 2: create_data "errors per service in <INDEX> only"
      codegen: one terms aggregation                     -> succeeds
  planner round 3: plain-text answer

Config env vars:
  STUB_ES_CLIENT  ds_clients key, e.g. "Ops Logs:Prod Logs (Elastic)" (required)
  STUB_ES_DS_ID   data source id for tables_by_source (required)
  STUB_ES_INDEX   index pattern the compliant query targets (default applogs-*)
  STUB_PORT       default 9098

Run:  uv run python ../tools/agent/stub_llm_es_guardrails.py   (from backend/)
Then add an LLM provider of type "custom" with base_url http://127.0.0.1:9098/v1.
"""
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

CLIENT = os.environ.get("STUB_ES_CLIENT", "")
DS_ID = os.environ.get("STUB_ES_DS_ID", "")
INDEX = os.environ.get("STUB_ES_INDEX", "applogs-*")

ROUND1 = "Count error-level events per service across every index"
ROUND2 = f"Count error-level events per service in {INDEX} only"


def _all_text(body: dict) -> str:
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(i.get("text", "") for i in c if isinstance(i, dict) and i.get("type") == "text")
    return "\n".join(parts)


def _tool_results(body: dict) -> int:
    by_role = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
    return max(by_role, _all_text(body).count('"execution_number"'))


def _planner(body: dict):
    done = _tool_results(body)
    if done == 0:
        return [{"name": "create_data", "arguments": {
            "title": "Errors per service — all indices",
            "user_prompt": ROUND1,
            "interpreted_prompt": ROUND1 + ", grouped by service and level with latency.",
            "tables_by_source": [{"data_source_id": DS_ID, "tables": [INDEX]}],
        }}], "Scanning every index for error events."
    if done == 1:
        return [{"name": "create_data", "arguments": {
            "title": "Errors per service",
            "user_prompt": ROUND2,
            "interpreted_prompt": ROUND2 + " — a single terms aggregation.",
            "tables_by_source": [{"data_source_id": DS_ID, "tables": [INDEX]}],
        }}], "The connection blocks all-index searches and caps aggregations; retrying within its limits."
    return None, (
        f"Error events per service are in the table above (from {INDEX}). The first attempt "
        "was rejected by the connection's guardrails — all-index searches are blocked and "
        "aggregations are capped — so the query was narrowed to a single terms aggregation."
    )


def _query(body_json: str) -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    es = ds_clients[{CLIENT!r}]\n"
        f"    df = es.execute_query({body_json!r})\n"
        "    print('rows:', len(df))\n"
        "    return df\n"
    )


def _codegen(text: str) -> str:
    error_filter = {"term": {"level": "error"}}
    if ROUND2 in text:
        return _query(json.dumps({"index": INDEX, "query": error_filter,
                                  "aggs": {"service": {"terms": {"field": "service", "size": 10}}}}))
    if "is blocked on this connection" in text:
        return _query(json.dumps({"index": INDEX, "query": error_filter, "aggs": {
            "service": {"terms": {"field": "service", "size": 10}, "aggs": {
                "level": {"terms": {"field": "level", "size": 5}, "aggs": {
                    "p95": {"percentiles": {"field": "latency_ms", "percents": [95]}}}}}},
            "total": {"value_count": {"field": "status"}}}}))
    return _query(json.dumps({"index": "*", "query": error_filter,
                              "aggs": {"service": {"terms": {"field": "service", "size": 10}}}}))


def _chunk(delta: dict, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream(content, tool_calls):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        for i in range(0, len(content or ""), 40):
            yield _sse(_chunk({"content": content[i:i + 40]}))
            time.sleep(0.02)
        for idx, tc in enumerate(tool_calls or []):
            yield _sse(_chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:8]}",
                                               "type": "function",
                                               "function": {"name": tc["name"], "arguments": ""}}]}))
            args = json.dumps(tc["arguments"])
            for i in range(0, len(args), 80):
                yield _sse(_chunk({"tool_calls": [{"index": idx, "function": {"arguments": args[i:i + 80]}}]}))
        final = _chunk({}, finish="tool_calls" if tool_calls else "stop")
        final["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        yield _sse(final)
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _json(content: str):
    return JSONResponse({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
                         "created": int(time.time()), "model": "stub",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text = _all_text(body)
    stream = bool(body.get("stream"))
    if body.get("tools"):
        tool_calls, content = _planner(body)
        return _stream(content, tool_calls) if stream else _json(content)
    if "generate_df" in text:
        code = _codegen(text)
        return _stream(code, None) if stream else _json(code)
    generic = "Error volume by service"
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9098")), log_level="warning")
