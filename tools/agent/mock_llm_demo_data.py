#!/usr/bin/env python3
"""Scripted OpenAI-compatible LLM for the demo-data feedback loop (no API key).

Serves ``POST /v1/chat/completions`` (stream and non-stream) so a
``custom`` provider pointed at it drives the real agent loop end to end:

* Planner turn (tools offered, no tool result yet) -> one native tool call to
  ``create_demo_dataset`` with a scripted spec picked by keyword from the user
  prompt (hr / finance / sales / monitoring).
* Planner turn after a ``create_demo_dataset`` result -> a short final answer
  (or, when the user rejected the card, a revised spec with the extra table).
* Generator prompts ("You write one Python function...") -> pandas/numpy code
  built from the table's column listing (types, PK/FK, nullability), with a
  small delay so progress spinners are visible in screenshots.
* Anything else (titles, follow-ups, judges) -> a short plain answer.

Deterministic; for UI evidence and regression loops only. Real-model quality
is checked separately against a live provider.

    cd backend && uv run python ../tools/agent/mock_llm_demo_data.py --port 8765
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

GEN_DELAY_S = 1.2

SPECS = {
    "hr": {
        "name": "Demo – People Analytics", "domain": "HR analytics for a 400-person software company", "icon": "👥",
        "description": "Fictional software company with departments, employees, hiring and terminations, and salary history.",
        "date_range_start": "2023-01-01", "date_range_end": "2025-06-30",
        "tables": [
            {"name": "departments", "description": "One row per department", "row_count": 8, "columns": [
                {"name": "department_id", "type": "integer", "primary_key": True, "description": "Department id"},
                {"name": "name", "type": "text", "description": "Department name"},
                {"name": "cost_center", "type": "text", "description": "Finance cost center code"}]},
            {"name": "employees", "description": "One row per employee, current and former", "row_count": 460, "columns": [
                {"name": "employee_id", "type": "integer", "primary_key": True, "description": "Employee id"},
                {"name": "department_id", "type": "integer", "references": "departments.department_id", "description": "Home department"},
                {"name": "full_name", "type": "text", "description": "Fictional full name"},
                {"name": "level", "type": "text", "description": "Career level IC1-IC5 / M1-M3"},
                {"name": "hire_date", "type": "date", "description": "First working day"},
                {"name": "termination_date", "type": "date", "nullable": True, "description": "Last day; NULL while employed"},
                {"name": "salary_usd", "type": "real", "description": "Current annual base salary"}]},
            {"name": "salary_changes", "description": "One row per compensation change", "row_count": 900, "columns": [
                {"name": "change_id", "type": "integer", "primary_key": True, "description": "Change id"},
                {"name": "employee_id", "type": "integer", "references": "employees.employee_id", "description": "Employee"},
                {"name": "effective_date", "type": "date", "description": "Date the change took effect"},
                {"name": "reason", "type": "text", "description": "promotion / merit / market adjustment"},
                {"name": "new_salary_usd", "type": "real", "description": "Salary after the change"}]},
        ],
        "realism_notes": ["Engineering ~40% of headcount", "Attrition spikes after the March review cycle"],
        "agents": [
            {"name": "People Analytics", "icon": "👥", "description": "Headcount, hiring and attrition trends",
             "tables": ["employees", "departments"],
             "conversation_starters": ["What is monthly attrition by department?", "How has headcount grown since 2023?"],
             "instructions": ["Attrition rate = terminations in period / average headcount in period."]},
            {"name": "Compensation", "icon": "💵", "description": "Salary bands, raises and promotion velocity",
             "tables": ["salary_changes", "employees", "departments"],
             "conversation_starters": ["Average raise by reason last year?"],
             "instructions": ["Merit increases happen in March; promotions can happen any month."]},
        ],
    },
    "finance": {
        "name": "Demo – E-commerce Finance", "domain": "finance for an e-commerce retailer", "icon": "🛒",
        "description": "Fictional online retailer: customers, orders, payments, refunds, chargebacks and payouts.",
        "date_range_start": "2024-07-01", "date_range_end": "2026-06-30",
        "tables": [
            {"name": "customers", "description": "One row per customer", "row_count": 1500, "columns": [
                {"name": "customer_id", "type": "integer", "primary_key": True, "description": "Customer id"},
                {"name": "country", "type": "text", "description": "ISO country"},
                {"name": "signup_date", "type": "date", "description": "Account creation date"}]},
            {"name": "orders", "description": "One row per order", "row_count": 9000, "columns": [
                {"name": "order_id", "type": "integer", "primary_key": True, "description": "Order id"},
                {"name": "customer_id", "type": "integer", "references": "customers.customer_id", "description": "Buyer"},
                {"name": "ordered_at", "type": "datetime", "description": "Order timestamp"},
                {"name": "status", "type": "text", "description": "delivered / shipped / cancelled"},
                {"name": "gross_amount", "type": "real", "description": "Order total incl. tax (USD)"}]},
            {"name": "payments", "description": "One row per payment attempt", "row_count": 9300, "columns": [
                {"name": "payment_id", "type": "integer", "primary_key": True, "description": "Payment id"},
                {"name": "order_id", "type": "integer", "references": "orders.order_id", "description": "Order paid"},
                {"name": "method", "type": "text", "description": "card / paypal / bnpl"},
                {"name": "amount", "type": "real", "description": "Captured amount USD"},
                {"name": "paid_at", "type": "datetime", "description": "Capture timestamp"}]},
            {"name": "refunds", "description": "One row per refund", "row_count": 420, "columns": [
                {"name": "refund_id", "type": "integer", "primary_key": True, "description": "Refund id"},
                {"name": "order_id", "type": "integer", "references": "orders.order_id", "description": "Refunded order"},
                {"name": "amount", "type": "real", "description": "Refunded amount USD"},
                {"name": "refunded_at", "type": "datetime", "description": "Refund timestamp"}]},
        ],
        "realism_notes": ["Q4 revenue +40%", "Refunds only on delivered orders, ~4%"],
        "agents": [
            {"name": "Revenue & Refunds", "icon": "💰", "description": "GMV, net revenue and refund rates",
             "tables": ["orders", "refunds", "customers"],
             "conversation_starters": ["Net revenue by month?", "Refund rate by country?"],
             "instructions": ["Net revenue = sum(orders.gross_amount) - sum(refunds.amount)."]},
            {"name": "Payments", "icon": "💳", "description": "Payment mix and capture timing",
             "tables": ["payments", "orders"],
             "conversation_starters": ["Share of BNPL payments over time?"],
             "instructions": ["Payment mix is measured on captured amount, not count."]},
        ],
    },
}
SPECS["sales"] = SPECS["finance"]
SPECS["monitoring"] = SPECS["hr"]

FINAL_TEXT = ("Your demo dataset is ready and the agents are attached to this session. "
              "Try one of the starter questions, or ask me to review the instructions or create evals.")


def pick_spec(text: str) -> dict:
    t = (text or "").lower()
    rules = [("finance", r"\b(finance|commerce|payments?)\b"), ("hr", r"\b(hr|people|headcount)\b"),
             ("sales", r"\bsales\b"), ("monitoring", r"\b(monitoring|logs?)\b")]
    for key, rx in rules:
        if re.search(rx, t):
            return json.loads(json.dumps(SPECS[key]))
    return json.loads(json.dumps(SPECS["finance"]))


# ---------------------------------------------------------------- generator code

COL_RE = re.compile(r"^\s*-\s+(\w+)\s+(\w+)(.*)$")


def gen_code(prompt: str) -> str:
    m = re.search(r"TABLE TO GENERATE: (\w+)", prompt)
    table = m.group(1) if m else "t"
    cols = []
    in_cols = False
    for line in prompt.splitlines():
        if line.startswith("Columns, in order"):
            in_cols = True
            continue
        if in_cols:
            mm = COL_RE.match(line)
            if not mm:
                if line.strip() == "":
                    break
                continue
            name, typ, rest = mm.groups()
            ref = re.search(r"-> (\w+)\.(\w+)", rest)
            cols.append({"name": name, "type": typ, "pk": "PRIMARY KEY" in rest,
                         "ref": ref.groups() if ref else None, "nullable": "NULLABLE" in rest})
    body = ["def generate(n, rng, tables, start, end):", "    span = int((end - start).total_seconds())", "    out = {}"]
    for c in cols:
        nm, typ = c["name"], c["type"]
        if c["pk"]:
            expr = "np.arange(1, n + 1)" if typ == "integer" else f"[f'{table[:3].upper()}-{{i:05d}}' for i in range(1, n + 1)]"
        elif c["ref"] and c["ref"][0] != table:
            rt, rc = c["ref"]
            expr = f"rng.choice(tables['{rt}']['{rc}'].to_numpy(), size=n)"
        elif c["ref"]:
            expr = "np.where(rng.random(n) < 0.2, np.nan, rng.integers(1, max(2, n // 10), size=n)).astype(float)"
        elif typ in ("date", "datetime"):
            expr = "start + pd.to_timedelta(np.sort(rng.integers(0, span, size=n)), unit='s')"
        elif typ == "integer":
            expr = "rng.poisson(20, size=n)"
        elif typ == "real":
            expr = "np.round(rng.lognormal(4.0, 0.6, size=n), 2)"
        elif typ == "boolean":
            expr = "rng.random(n) < 0.15"
        elif typ == "json":
            expr = "[{'k': int(x)} for x in rng.integers(0, 9, size=n)]"
        else:
            vocab = {"status": "['delivered','shipped','cancelled']", "method": "['card','paypal','bnpl']",
                     "level": "['IC1','IC2','IC3','IC4','IC5','M1','M2']", "reason": "['merit','promotion','market adjustment']",
                     "country": "['US','GB','DE','FR','CA','AU']"}.get(nm, f"['{nm}_' + s for s in 'abcdefgh']")
            if nm in ("name", "full_name"):
                expr = ("[f'{a} {b}' for a, b in zip(rng.choice(['Avery','Jordan','Riley','Sam','Noa','Maya','Eli','Lior'], size=n), "
                        "rng.choice(['Levi','Cohen','Park','Silva','Novak','Ito','Haddad','Berg'], size=n))]")
            else:
                expr = f"rng.choice({vocab}, size=n)"
        if c["nullable"] and not c["pk"] and not c["ref"]:
            body.append(f"    v = pd.Series({expr})")
            body.append(f"    out['{nm}'] = v.where(rng.random(n) > 0.85, None)")
        else:
            body.append(f"    out['{nm}'] = {expr}")
    body.append("    return pd.DataFrame(out)")
    return "```python\n" + "\n".join(body) + "\n```"


# ---------------------------------------------------------------- planner turns

def planner_reply(messages: list) -> dict:
    """Return {'text': str} or {'tool': name, 'args': dict}."""
    last_tool = None
    for m in messages:
        if m.get("role") == "tool":
            last_tool = m
    user_text = " ".join(
        (m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content")))
        for m in messages if m.get("role") == "user"
    )
    if last_tool is not None:
        content = last_tool.get("content")
        content = content if isinstance(content, str) else json.dumps(content)
        if "rejected" in content and "feedback" in content.lower():
            spec = pick_spec(user_text)
            spec["tables"].append({"name": "budgets", "description": "Monthly plan per department/category",
                                   "row_count": 48, "columns": [
                                       {"name": "budget_id", "type": "integer", "primary_key": True},
                                       {"name": "month", "type": "date"},
                                       {"name": "planned_amount", "type": "real"}]})
            return {"tool": "create_demo_dataset", "args": spec}
        if '"created"' in content or "Created demo connection" in content:
            return {"text": FINAL_TEXT}
        return {"text": "The demo dataset was not created — see the card for why."}
    return {"tool": "create_demo_dataset", "args": pick_spec(user_text)}


def sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


app = FastAPI()


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock-demo", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    messages = body.get("messages") or []
    model = body.get("model") or "mock-demo"
    stream = bool(body.get("stream"))
    tools = body.get("tools") or []
    all_text = "\n".join(m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content") or "")
                         for m in messages)

    if "You write one Python function" in all_text:
        await asyncio.sleep(GEN_DELAY_S)
        reply = {"text": gen_code(all_text)}
    elif tools and any((t.get("function") or {}).get("name") == "create_demo_dataset" for t in tools):
        await asyncio.sleep(0.6)
        reply = planner_reply(messages)
    else:
        reply = {"text": "Demo dataset"}

    cid = f"chatcmpl-{uuid.uuid4().hex[:10]}"
    created = int(time.time())
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    if not stream:
        msg = {"role": "assistant", "content": reply.get("text")}
        if "tool" in reply:
            msg = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                "function": {"name": reply["tool"], "arguments": json.dumps(reply["args"])}}]}
        return JSONResponse({"id": cid, "object": "chat.completion", "created": created, "model": model,
                             "choices": [{"index": 0, "message": msg,
                                          "finish_reason": "tool_calls" if "tool" in reply else "stop"}],
                             "usage": usage})

    async def gen():
        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
        if "tool" in reply:
            args = json.dumps(reply["args"])
            yield sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Designing the dataset."}, "finish_reason": None}]})
            yield sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                                                                     "function": {"name": reply["tool"], "arguments": ""}}]}, "finish_reason": None}]})
            for i in range(0, len(args), 400):
                yield sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 400]}}]}, "finish_reason": None}]})
                await asyncio.sleep(0.02)
            yield sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            text = reply["text"]
            for i in range(0, len(text), 40):
                yield sse({**base, "choices": [{"index": 0, "delta": {"content": text[i:i + 40]}, "finish_reason": None}]})
                await asyncio.sleep(0.01)
            yield sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        yield sse({**base, "choices": [], "usage": usage})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--gen-delay", type=float, default=GEN_DELAY_S, help="seconds per generator reply")
    a = ap.parse_args()
    GEN_DELAY_S = a.gen_delay
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
