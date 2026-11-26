#!/usr/bin/env python3
"""Scripted OpenAI-compatible LLM: a chat run that ends on result cards the
user must see after the run folds.

  round 1: create_data            (folds into the steps)
  round 2: create_scheduled_task  (deliverable: its card should be open)
  round 3: wait 10 minutes        (ends the turn; countdown + Cancel must stay)

Reuses run_fold_stub_llm.py's streaming and codegen (same "Music Store" demo
source).

    cd backend && STUB_DS_ID=<music store id> uv run python ../tools/agent/result_cards_stub_llm.py
    cd backend && uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1

Env: STUB_DS_ID (required), STUB_PORT (default 9099).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_fold_stub_llm as base  # noqa: E402

from fastapi import Request  # noqa: E402

app = base.app
TABLES = base.TABLES


def _called(body: dict) -> list[str]:
    """Tool names called so far (each scenario runs in a fresh report)."""
    return [tc.get("function", {}).get("name", "")
            for m in body.get("messages", []) for tc in (m.get("tool_calls") or [])]


def _planner(body: dict):
    names = _called(body)
    if "create_data" not in names:
        return [{"name": "create_data", "arguments": {
            "title": "Revenue by country",
            "user_prompt": "Revenue by billing country",
            "interpreted_prompt": "Sum Invoice.Total grouped by BillingCountry, highest first.",
            "tables_by_source": TABLES, "visualization_type": "table"}}], "Building the revenue table first."
    if "create_scheduled_task" not in names:
        return [{"name": "create_scheduled_task", "arguments": {
            "title": "Weekly revenue by country",
            "task_prompt": "Refresh revenue by billing country and flag any country that moved more than 10% week over week.",
            "cron_schedule": "0 8 * * 1"}}], None
    if "wait" not in names:
        return [{"name": "wait", "arguments": {
            "delay_minutes": 10,
            "reason": "Re-check revenue by country once tonight's invoice load has finished, then post the totals."}}], None
    return None, "Done."


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text = base._all_text(body)
    stream = bool(body.get("stream"))
    if body.get("tools"):
        calls, content = _planner(body)
        return base._stream(content, calls) if stream else base._json(content or "")
    if "generate_df" in text:
        code = base._codegen(text)
        return base._stream(code, None) if stream else base._json(code)
    return base._stream("Weekly revenue", None) if stream else base._json("Weekly revenue")


# Our route must win over the base module's identical path.
app.router.routes.sort(key=lambda r: 0 if getattr(r, "endpoint", None) is chat_completions else 1)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
