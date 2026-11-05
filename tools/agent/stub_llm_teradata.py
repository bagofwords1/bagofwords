#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM that drives the Teradata connector
through the real agent runtime (planner -> create_data -> codegen -> sandbox ->
read-only guard -> TeradataClient) when no funded LLM key is available.

Pair it with the fake driver (tools/teradata/README.md). Planner turns are
keyword-routed on the current <user_prompt>:
  round 1: one `create_data` call on the Teradata agent's tables
  final:   a short answer quoting the rows the tool returned, or the failure

Codegen turns return Python calling `ds_clients[<teradata client_key>]`, with
the key read from the prompt's <connection_clients> block. Each codegen prompt
is checked for the connector's Teradata guide and relative-date hint, and each
planner prompt for the primary-index metadata; when one is missing the stub
answers with code that raises, so broken prompt wiring fails visibly.

Run:  cd backend && uv run python ../tools/agent/stub_llm_teradata.py   (STUB_PORT, default 9099)
Register: uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
Env:  STUB_DUMP=/dir  write every prompt it receives (planner/codegen) there.
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
GUIDE_MARKER = "Teradata dialect differences"
DATE_HINT_MARKER = "Relative dates (Teradata)"
PI_MARKER = 'primary_index="'

# (trigger, title, tables, interpreted prompt, visualization, code body)
SCENARIOS = [
    (r"revenue by region", "Revenue by region",
     ["sales.orders", "sales.customers"],
     "Total order amount per customer region, highest first.",
     "bar_chart",
     """    df = client.execute_query(
        "LOCKING ROW FOR ACCESS "
        "SELECT c.region, SUM(CAST(o.amount AS DECIMAL(18,2))) AS revenue "
        "FROM sales.orders o JOIN sales.customers c ON o.customer_id = c.customer_id "
        "GROUP BY 1 ORDER BY 2 DESC")
    return df
"""),
    (r"latest order", "Latest order per customer",
     ["sales.orders"],
     "Each customer's most recent order.",
     "table",
     """    df = client.execute_query(
        "SELECT customer_id, order_id, order_date, amount FROM sales.orders "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC) = 1 "
        "ORDER BY customer_id")
    return df
"""),
    (r"monthly", "Monthly revenue (view)",
     ["sales.v_monthly_revenue"],
     "Revenue by month and region from the v_monthly_revenue view.",
     "table",
     """    df = client.execute_query("SELECT TOP 10 * FROM sales.v_monthly_revenue ORDER BY month_start, region")
    return df
"""),
    # The agent tries to change data. The read-only guard must refuse each one
    # before the driver sees it. The second is a Teradata short form (CT =
    # CREATE TABLE) that used to get through.
    (r"delete cancelled", "Delete cancelled orders",
     ["sales.orders"],
     "Delete orders whose status is cancelled.",
     "table",
     """    client.execute_query("DEL FROM sales.orders WHERE status = 'cancelled'")
    return client.execute_query("SELECT COUNT(*) AS n FROM sales.orders")
"""),
    (r"back ?up", "Back up orders",
     ["sales.orders"],
     "Copy sales.orders into a backup table.",
     "table",
     """    client.execute_query("CT sales.orders_backup AS (SEL * FROM sales.orders) WITH DATA")
    return client.execute_query("SELECT COUNT(*) AS n FROM sales.orders")
"""),
    # A query that never finishes: the agent's timeout must cancel it on the
    # server (the fake counts cancels).
    (r"full scan", "Full scan",
     ["sales.slow_scan"],
     "Count every row of slow_scan.",
     "table",
     """    return client.execute_query("SELECT COUNT(*) AS n FROM sales.slow_scan")
"""),
]


def _messages_text(body):
    parts, last_user = [], ""
    for m in body.get("messages", []):
        c = m.get("content")
        text = c if isinstance(c, str) else "\n".join(
            i.get("text", "") for i in (c or []) if isinstance(i, dict) and i.get("type") == "text")
        parts.append(text)
        if m.get("role") == "user":
            last_user = text
    return "\n".join(parts), last_user


def _dump(kind, text):
    d = os.environ.get("STUB_DUMP")
    if d:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{kind}-{int(time.time() * 1000)}.txt"), "w") as fh:
            fh.write(text)


def _current_turn(text, user):
    matches = list(re.finditer(r"<user_prompt>([^<]*)</user_prompt>", text))
    m = matches[-1] if matches else None
    if m:
        return m.group(1).strip(), text[m.end():]
    return user, text


def _teradata_source_id(text):
    for m in re.finditer(r'<data_source\b[^>]*>', text):
        window = text[m.start(): m.start() + 6000].split("</data_source>")[0]
        if 'type="teradata"' in window or "teradata" in m.group(0).lower():
            idm = re.search(r'\bid="([0-9a-f-]{36})"', m.group(0))
            if idm:
                return idm.group(1)
    m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[^\n]{0,200}teradata', text, re.I)
    return m.group(1) if m else None


def _scenario(user):
    return next((s for s in SCENARIOS if re.search(s[0], user, re.I)), None)


def _plan(body):
    text, user = _messages_text(body)
    _dump("planner", text)
    user, tail = _current_turn(text, user)
    scenario = _scenario(user)
    if scenario is None:
        return None, "I can answer: revenue by region, latest order per customer, monthly revenue."
    observations = [(st or "success", blob) for st, blob in
                    re.findall(r'^(?:\[([a-z_]+)\]\s*)?(\{"summary".*)$', tail, re.M)]
    if not observations:
        ds = _teradata_source_id(text)
        if not ds:
            return None, "STUB: no Teradata agent in this report's scope."
        if PI_MARKER not in text:
            return None, "STUB: primary_index metadata missing from the planner schema context."
        _, title, tables, interpreted, viz, _code = scenario
        args = {"title": title, "interpreted_prompt": interpreted,
                "tables_by_source": [{"data_source_id": ds, "tables": tables}],
                "visualization_type": viz}
        return [{"name": "create_data", "arguments": args}], f"Querying Teradata: {title.lower()}."
    status, blob = observations[-1]
    try:
        obs = json.loads(blob)
    except ValueError:
        obs = {"summary": blob[:300]}
    summary = obs.get("summary", "")
    if status != "success":
        return None, f"TERADATA_RESULT failed ({status}): {summary}"
    return None, f"TERADATA_RESULT {scenario[1]}:\n\n{_preview_table(obs)}"


def _preview_table(obs):
    prev = obs.get("data_preview") or {}
    cols = [c.get("field") for c in prev.get("columns") or [] if c.get("field")]
    rows = prev.get("rows") or []
    if not cols or not rows:
        return obs.get("summary", "")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows[:12]:
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(lines)


def _raise(msg):
    return f"def generate_df(ds_clients, excel_files):\n    raise RuntimeError({msg!r})\n"


def _codegen(text):
    _dump("codegen", text)
    if GUIDE_MARKER not in text:
        return _raise("STUB: Teradata query guide missing from the codegen prompt")
    if DATE_HINT_MARKER not in text:
        return _raise("STUB: Teradata relative-date hint missing from the codegen prompt")
    m = re.search(r"client_key:[ \t]*([^\n]+?)[ \t]*\ndescription:[ \t]*Teradata Vantage database", text)
    if not m:
        return _raise("STUB: no Teradata client_key in <connection_clients>")
    key = m.group(1)
    # The planner's interpreted prompt is passed down verbatim; match on it,
    # never on the whole prompt (table names like v_monthly_revenue would match).
    scenario = next((sc for sc in SCENARIOS if sc[3] in text), None)
    if scenario is None:
        return _raise("STUB: no known scenario in the codegen prompt")
    body = scenario[5]
    return ("def generate_df(ds_clients, excel_files):\n"
            f"    client = ds_clients[{key!r}]\n" + body)


def _chunk(delta, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": "stub", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


def _stream(content, tool_calls):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        if content:
            for i in range(0, len(content), 60):
                yield _sse(_chunk({"content": content[i:i + 60]}))
        for idx, tc in enumerate(tool_calls or []):
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
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                              "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])}} for t in tool_calls]
    return JSONResponse({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
                         "model": "stub", "choices": [{"index": 0, "message": msg,
                                                       "finish_reason": "tool_calls" if tool_calls else "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))
    if body.get("tools"):
        tool_calls, content = _plan(body)
        return _stream(content, tool_calls) if stream else _json(content, tool_calls)
    text, _ = _messages_text(body)
    if "generate_df" in text:
        code = _codegen(text)
        return _stream(code, None) if stream else _json(code)
    _dump("other", text)
    generic = "Teradata sales" if re.search(r"title", text, re.I) else "OK."
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
