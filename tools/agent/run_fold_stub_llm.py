#!/usr/bin/env python3
"""Scripted OpenAI-compatible LLM for the run-activity fold on /reports/:id.

Drives one deterministic run against the demo "Music Store" source so the
report page shows every shape the fold has to handle, with no API key:

  round 1: a progress message + three inspections of the SAME table
           (describe_tables, inspect_data x2)  -> one "Checked the Invoice
           table" step with three checks
  round 2: a progress message + create_data producing a TABLE (deliverable)
  round 3: create_data producing a single NUMBER (not a deliverable)
  final:   a formatted markdown conclusion (heading, list, bold, table)

Codegen prompts get code matching the step being built; anything else
(titles, follow-ups, judges) gets a short plain answer.

    cd backend && STUB_DS_ID=<music store id> uv run python ../tools/agent/run_fold_stub_llm.py
    cd backend && uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1

Env: STUB_DS_ID (required), STUB_DS_NAME (default "Music Store"),
STUB_PORT (default 9099), STUB_DELAY_S (per-response delay, default 0.8).
"""
import asyncio
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

DS_ID = os.environ["STUB_DS_ID"]
DS_NAME = os.environ.get("STUB_DS_NAME", "Music Store")
DELAY_S = float(os.environ.get("STUB_DELAY_S", "0.8"))
TABLES = [{"data_source_id": DS_ID, "tables": ["Invoice"]}]

FINAL_ANSWER = """## Revenue by country

Total revenue across all invoices is **$2,328.60**.

- The **USA** leads with $523.06, followed by **Canada** at $303.96.
- The top five countries account for a little over half of all revenue.

| Country | Revenue |
|---|---|
| USA | $523.06 |
| Canada | $303.96 |
| France | $195.10 |

The full breakdown is in the *Revenue by country* table below.
"""


def _all_text(body: dict) -> str:
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(i.get("text", "") for i in c if isinstance(i, dict) and i.get("type") == "text")
    return "\n".join(parts)


def _planner(body: dict):
    obs = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
    if obs < 3:
        return [
            {"name": "describe_tables", "arguments": {"query": ["Invoice"]}},
            {"name": "inspect_data", "arguments": {
                "user_prompt": "Check the row count and date range in Invoice",
                "tables_by_source": TABLES}},
            {"name": "inspect_data", "arguments": {
                "user_prompt": "Check for missing totals in Invoice",
                "tables_by_source": TABLES}},
        ], "I'll start by checking the Invoice table."
    if obs < 4:
        return [{"name": "create_data", "arguments": {
            "title": "Revenue by country",
            "user_prompt": "Revenue by billing country",
            "interpreted_prompt": "Sum Invoice.Total grouped by BillingCountry, highest first.",
            "tables_by_source": TABLES,
            "visualization_type": "table"}}], "The data looks clean. Building the revenue breakdown now."
    if obs < 5:
        return [{"name": "create_data", "arguments": {
            "title": "Total revenue",
            "user_prompt": "Grand total of invoice revenue",
            "interpreted_prompt": "Sum Invoice.Total across all invoices as one number.",
            "tables_by_source": TABLES,
            "visualization_type": "table"}}], None
    return None, FINAL_ANSWER


def _codegen(text: str) -> str:
    q = "SELECT COUNT(*) AS invoices, MIN(InvoiceDate) AS first_invoice, MAX(InvoiceDate) AS last_invoice FROM Invoice"
    if "Grand total" in text or "as one number" in text:
        q = "SELECT ROUND(SUM(Total), 2) AS total_revenue FROM Invoice"
    elif "grouped by BillingCountry" in text:
        q = ("SELECT BillingCountry AS country, ROUND(SUM(Total), 2) AS revenue, COUNT(*) AS invoices "
             "FROM Invoice GROUP BY BillingCountry ORDER BY revenue DESC")
    elif "missing totals" in text:
        q = "SELECT COUNT(*) AS missing_totals FROM Invoice WHERE Total IS NULL"
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    df = ds_clients['{DS_NAME}'].execute_query(\"{q}\")\n"
        "    print(df.to_string(index=False))\n"
        "    return df\n"
    )


def _chunk(delta: dict, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream(content, tool_calls):
    async def gen():
        await asyncio.sleep(DELAY_S)
        yield _sse(_chunk({"role": "assistant"}))
        if content:
            for i in range(0, len(content), 24):
                yield _sse(_chunk({"content": content[i:i + 24]}))
                await asyncio.sleep(0.03)
        for idx, tc in enumerate(tool_calls or []):
            yield _sse(_chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:8]}",
                                               "type": "function",
                                               "function": {"name": tc["name"], "arguments": ""}}]}))
            yield _sse(_chunk({"tool_calls": [{"index": idx,
                                               "function": {"arguments": json.dumps(tc["arguments"])}}]}))
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
        calls, content = _planner(body)
        return _stream(content, calls) if stream else _json(content or "")
    if "generate_df" in text:
        code = _codegen(text)
        return _stream(code, None) if stream else _json(code)
    generic = "Revenue by country"
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
