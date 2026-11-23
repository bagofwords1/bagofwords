#!/usr/bin/env python3
"""Scripted OpenAI-compatible LLM: a training-mode run whose live chain mixes
groupable calls with deliverables, to check that deliverables never fold into
a live tool group on /reports/:id.

  round 1: inspect_data + create_data            -> one live group
  round 2: create_instruction                    -> its own row
  round 3: edit_instruction (the new instruction)-> its own row
  round 4: create_artifact with authored code    -> its own row
  final:   held STUB_HOLD_S seconds, then a short answer, so the run stays
           live long enough to screenshot the chain

Reuses run_fold_stub_llm.py's streaming and codegen (same "Music Store" demo
source). Run the planner in training mode (prompt.mode = "training"): the
instruction tools exist only there.

    cd backend && STUB_DS_ID=<music store id> uv run python ../tools/agent/deliverable_groups_stub_llm.py
    cd backend && uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1

Env: STUB_DS_ID (required), STUB_PORT (default 9099), STUB_HOLD_S (default 90).
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_fold_stub_llm as base  # noqa: E402

from fastapi import Request  # noqa: E402

app = base.app
HOLD_S = float(os.environ.get("STUB_HOLD_S", "90"))
TABLES = base.TABLES
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _turn(body: dict):
    """Tool names called and tool results seen so far. The planner appends a
    context `user` message after the tool results, so count the whole
    conversation: every scenario runs in a fresh report."""
    names, results = [], []
    for m in body.get("messages", []):
        for tc in m.get("tool_calls") or []:
            names.append(tc.get("function", {}).get("name", ""))
        if m.get("role") == "tool":
            c = m.get("content")
            results.append(c if isinstance(c, str) else json.dumps(c))
    return names, results


def _artifact_code(viz_id: str) -> str:
    return (
        '<script type="text/babel">\n'
        "function App() {\n"
        f"  const viz = vizById('{viz_id}');\n"
        "  const rows = (viz && viz.rows) || [];\n"
        "  return (\n"
        "    <div style={{ padding: 24 }}>\n"
        "      <h1>Album sales</h1>\n"
        "      <table><tbody>{rows.map((r, i) => (\n"
        "        <tr key={i}>{Object.values(r).map((v, j) => <td key={j}>{String(v)}</td>)}</tr>\n"
        "      ))}</tbody></table>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"
        "</script>"
    )


def _planner(body: dict):
    names, results = _turn(body)
    joined = "\n".join(results)
    if "create_data" not in names:
        return [
            {"name": "inspect_data", "arguments": {
                "user_prompt": "Check the row count and date range in Invoice", "tables_by_source": TABLES}},
            {"name": "create_data", "arguments": {
                "title": "Revenue by country",
                "user_prompt": "Revenue by billing country",
                "interpreted_prompt": "Sum Invoice.Total grouped by BillingCountry, highest first.",
                "tables_by_source": TABLES, "visualization_type": "table"}},
        ], "Checking the invoices and building the revenue table."
    if "create_instruction" not in names:
        return [{"name": "create_instruction", "arguments": {
            "title": "Revenue means invoice totals",
            "text": "Revenue is the sum of Invoice.Total. Group by BillingCountry for revenue by country.",
            "category": "general", "confidence": 0.9, "load_mode": "intelligent",
            "destination": "agents", "data_source_ids": [base.DS_ID], "table_names": ["Invoice"]}}], None
    if "edit_instruction" not in names:
        # The observation lists the new instruction under "artifacts".
        ids = re.findall(r'"type"\s*:\s*"instruction"\s*,\s*"id"\s*:\s*"(' + UUID_RE + ')"', joined)
        return [{"name": "edit_instruction", "arguments": {
            "instruction_id": ids[-1] if ids else "missing",
            "old_text": "Group by BillingCountry for revenue by country.",
            "text": "Group by BillingCountry for revenue by country, highest first."}}], None
    if "create_artifact" not in names:
        viz = re.findall(r'"created_visualization_ids"\s*:\s*\[\s*"(' + UUID_RE + ')"', joined)
        return [{"name": "create_artifact", "arguments": {
            "prompt": "Album sales: one table of revenue by country.",
            "mode": "page", "title": "Album sales",
            "visualization_ids": viz[:1],
            "code": _artifact_code(viz[0] if viz else "")}}], None
    return None, "## Done\n\nRevenue by country is in the **Album sales** dashboard."


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text = base._all_text(body)
    stream = bool(body.get("stream"))
    if body.get("tools"):
        calls, content = _planner(body)
        if not calls:
            await asyncio.sleep(HOLD_S)
        return base._stream(content, calls) if stream else base._json(content or "")
    if "generate_df" in text:
        code = base._codegen(text)
        return base._stream(code, None) if stream else base._json(code)
    return base._stream("Album sales", None) if stream else base._json("Album sales")


# Our route must win over the base module's identical path.
app.router.routes.sort(key=lambda r: 0 if getattr(r, "endpoint", None) is chat_completions else 1)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")))
