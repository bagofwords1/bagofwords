#!/usr/bin/env python3
"""Scripted OpenAI-compatible LLM that streams many run shapes, for the
report page's run-activity dot (the black circle on /reports/:id).

The scenario is picked by a `[scenario:<name>]` tag in the user prompt. Every
planner response starts after a configurable silence, so each scenario has
gaps where nothing streams — exactly where the dot must stay on screen.

  direct        long silence, then a slowly streamed direct answer (no tools)
  reasoning     streamed reasoning, then a streamed answer
  text_tool     a sentence, silence, a tool call, silence, the answer
  multi_tool    three rounds of tools (inspections, then two create_data with
                slow code generation), progress text in between
  parallel      one round launching three create_data calls at once
  reason_tool   reasoning -> tool -> reasoning -> answer
  long_gap      text, a 6s silence, a tool, a 6s silence, the answer

    cd backend && STUB_DS_ID=<id> uv run python ../tools/agent/stream_indicator_stub_llm.py
    cd backend && uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1

Env: STUB_DS_ID (required), STUB_DS_NAME (default "Music Store"),
STUB_PORT (default 9099), STUB_GAP_S (planner silence, default 2.0),
STUB_CODEGEN_S (codegen silence, default 2.5).
"""
import asyncio
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

DS_ID = os.environ["STUB_DS_ID"]
DS_NAME = os.environ.get("STUB_DS_NAME", "Music Store")
GAP_S = float(os.environ.get("STUB_GAP_S", "2.0"))
CODEGEN_S = float(os.environ.get("STUB_CODEGEN_S", "2.5"))
TABLES = [{"data_source_id": DS_ID, "tables": ["Invoice"]}]

ANSWER = (
    "## Revenue by country\n\n"
    "Total revenue across all invoices is **$2,328.60**.\n\n"
    "- The **USA** leads with $523.06, followed by **Canada** at $303.96.\n"
    "- The top five countries account for a little over half of all revenue.\n"
    "- The best customer spent $49.62 across 7 invoices.\n"
)
REASONING = ("The user wants revenue by country. Invoice has BillingCountry and Total, "
             "so a grouped sum answers it. I should check the table first, then chart it.")


def _all_text(body: dict) -> str:
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(i.get("text", "") for i in c if isinstance(i, dict) and i.get("type") == "text")
    return "\n".join(parts)


def _scenario(body: dict) -> str:
    found = re.findall(r"\[scenario:([a-z_]+)\]", _all_text(body))
    return found[-1] if found else "multi_tool"


def _create(title, prompt, sql_hint, viz="table"):
    return {"name": "create_data", "arguments": {
        "title": title, "user_prompt": prompt, "interpreted_prompt": sql_hint,
        "tables_by_source": TABLES, "visualization_type": viz}}


INSPECT = {"name": "inspect_data", "arguments": {
    "user_prompt": "Check the row count and date range in Invoice", "tables_by_source": TABLES}}
DESCRIBE = {"name": "describe_tables", "arguments": {"query": ["Invoice"]}}
CHART = _create("Revenue by country", "Revenue by billing country",
                "Sum Invoice.Total grouped by BillingCountry, highest first.", "bar_chart")
TOP = _create("Top customers", "Customers ranked by total spend",
              "Sum Invoice.Total per CustomerId with the invoice count, highest first.")
TOTAL = _create("Total revenue", "Grand total of invoice revenue",
                "Sum Invoice.Total across all invoices as one number.")


def _plan(scenario: str, obs: int):
    """(reasoning, content, tool_calls, silence_s) for this planner round."""
    if scenario == "direct":
        return None, ANSWER, None, GAP_S * 2
    if scenario == "reasoning":
        return REASONING, ANSWER, None, GAP_S
    if scenario == "text_tool":
        if obs < 1:
            return None, "Let me look at the Invoice table first.", [DESCRIBE], GAP_S
        return None, ANSWER, None, GAP_S
    if scenario == "long_gap":
        if obs < 1:
            return None, "Checking the Invoice table.", [DESCRIBE], 6.0
        return None, ANSWER, None, 6.0
    if scenario == "parallel":
        if obs < 3:
            return None, "Building three results at once.", [CHART, TOP, TOTAL], GAP_S
        return None, ANSWER, None, GAP_S
    if scenario == "reason_tool":
        if obs < 1:
            return REASONING, None, [DESCRIBE], GAP_S
        if obs < 2:
            return "The table has what I need. Charting it now.", None, [CHART], GAP_S
        return "Summarising the chart.", ANSWER, None, GAP_S
    # multi_tool
    if obs < 2:
        return None, "I'll start by checking the Invoice table.", [DESCRIBE, INSPECT], GAP_S
    if obs < 3:
        return None, "The data looks clean. Charting revenue by country now.", [CHART], GAP_S
    if obs < 4:
        return None, None, [TOP], GAP_S
    return None, ANSWER, None, GAP_S


def _codegen(text: str) -> str:
    q = "SELECT COUNT(*) AS invoices, MIN(InvoiceDate) AS first_invoice, MAX(InvoiceDate) AS last_invoice FROM Invoice"
    if "as one number" in text:
        q = "SELECT ROUND(SUM(Total), 2) AS total_revenue FROM Invoice"
    elif "per CustomerId" in text:
        q = ("SELECT CustomerId AS customer_id, ROUND(SUM(Total), 2) AS spend, COUNT(*) AS invoices "
             "FROM Invoice GROUP BY CustomerId ORDER BY spend DESC")
    elif "grouped by BillingCountry" in text:
        q = ("SELECT BillingCountry AS country, ROUND(SUM(Total), 2) AS revenue, COUNT(*) AS invoices "
             "FROM Invoice GROUP BY BillingCountry ORDER BY revenue DESC")
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    df = ds_clients['{DS_NAME}'].execute_query(\"{q}\")\n"
        "    return df\n"
    )


def _chunk(delta: dict, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream(content, tool_calls=None, reasoning=None, silence=0.0, step=8, pace=0.06):
    async def gen():
        await asyncio.sleep(silence)
        yield _sse(_chunk({"role": "assistant"}))
        if reasoning:
            for i in range(0, len(reasoning), step):
                yield _sse(_chunk({"reasoning_content": reasoning[i:i + step]}))
                await asyncio.sleep(pace)
        if content:
            for i in range(0, len(content), step):
                yield _sse(_chunk({"content": content[i:i + step]}))
                await asyncio.sleep(pace)
        if tool_calls:
            # Composing the call takes a moment too.
            await asyncio.sleep(silence / 2)
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
        obs = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
        reasoning, content, calls, silence = _plan(_scenario(body), obs)
        if stream:
            return _stream(content, calls, reasoning, silence)
        await asyncio.sleep(silence)
        return _json(content or "")
    if "generate_df" in text:
        code = _codegen(text)
        if stream:
            return _stream(code, silence=CODEGEN_S, step=24, pace=0.02)
        await asyncio.sleep(CODEGEN_S)
        return _json(code)
    return _stream("Revenue by country", step=24, pace=0) if stream else _json("Revenue by country")


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
