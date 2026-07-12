#!/usr/bin/env python
"""Seed a report whose assistant turn contains connection-tool calls carrying
model-authored `title`s, so the real UI (MCPTool.vue / file-tool components)
renders the new human-readable titles.

Registers an org/admin/report through the real API, then writes the completion
graph directly (Completion → AgentExecution → ToolExecution → CompletionBlock)
— a last-resort raw seed (see tests/AGENTS.md rule 5) because the normal path
to a completion requires a live LLM, which this demo deliberately avoids: the
point is to exercise the *rendering* of `arguments_json.title`, not the model.

Prints the report URL + login so a Playwright run can screenshot it.

Usage:
    TEST_DATABASE_URL="sqlite:///db/agent.db" .venv/bin/python scripts/seed_tool_title_demo.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("SEED_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("SEED_PASSWORD", "Password123!")

# Each tuple: (tool_name, arguments_json, result_json, result_summary, icon)
# arguments_json carries the model-authored `title` — the whole point.
DEMO_CALLS = [
    (
        "search_mcps",
        {"query": "notion", "title": "Finding available Notion tools"},
        {"total_count": 2, "tools": [
            {"name": "notion_search", "description": "Search Notion pages"},
            {"name": "notion_get_page", "description": "Fetch a Notion page"},
        ]},
        "Found 2 tools",
    ),
    (
        "execute_mcp",
        {"connection_id": "conn_notion", "tool_name": "notion_search",
         "arguments": {"query": "churned customers"},
         "title": "Searching Notion for churned customers"},
        {"success": True, "connection_name": "Notion",
         "preview": [{"id": "pg_churn", "title": "Churn Playbook"}]},
        "Executed 'notion_search'",
    ),
    (
        "execute_mcp",
        {"connection_id": "conn_notion", "tool_name": "notion_get_page",
         "arguments": {"page_id": "pg_churn"},
         "title": "Reading the Churn Playbook page"},
        {"success": True, "connection_name": "Notion",
         "preview": "Customers with no login in 30 days are considered at risk…"},
        "Executed 'notion_get_page'",
    ),
    (
        "web_fetch",
        {"url": "https://example.com/pricing", "title": "Reading the pricing page"},
        {"success": True, "status_code": 200, "final_url": "https://example.com/pricing",
         "content": "Pricing: $20 per seat / month."},
        "Fetched pricing page",
    ),
]


def api_setup() -> dict:
    """Register admin + org + a report through the real API; return ids/token."""
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        # First run creates the admin; later runs (sign-up now disabled) just log in.
        c.post("/api/auth/register", json={"name": "Demo Admin", "email": EMAIL, "password": PASSWORD})
        login = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD})
        if login.status_code != 200:
            sys.exit(f"login failed: {login.status_code} {login.text}")
        tok = login.json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        orgs = c.get("/api/organizations", headers=h).json()
        if orgs:
            org_id = orgs[0]["id"]
        else:
            org_id = c.post("/api/organizations", headers=h, json={"name": "Demo Org"}).json()["id"]
        h["X-Organization-Id"] = org_id
        rep = c.post("/api/reports", headers=h, json={"title": "Connection tool titles demo"})
        if rep.status_code not in (200, 201):
            sys.exit(f"create report failed: {rep.status_code} {rep.text}")
        report_id = rep.json()["id"]
        return {"token": tok, "org_id": org_id, "report_id": report_id, "email": EMAIL, "password": PASSWORD}


async def seed_blocks(ctx: dict) -> None:
    os.environ.setdefault("TESTING", "true")
    # Register the ORM models the server loads. Blanket-import every model module
    # so relationship() names (ApiKey, CompletionFeedback, …) resolve — but skip
    # dormant models whose relationships reference classes that don't exist in
    # this build (they'd break global mapper configuration and the server itself
    # never imports them).
    import importlib
    import pkgutil
    import app.models as _models_pkg
    _SKIP = {"application"}
    for _m in pkgutil.iter_modules(_models_pkg.__path__):
        if _m.name in _SKIP:
            continue
        importlib.import_module(f"app.models.{_m.name}")
    from app.dependencies import async_session_maker
    from app.models.completion import Completion
    from app.models.agent_execution import AgentExecution
    from app.models.tool_execution import ToolExecution
    from app.models.completion_block import CompletionBlock

    now = datetime.utcnow()
    async with async_session_maker() as db:
        # 1) user message
        user_c = Completion(
            prompt={"content": "Pull our churn context from Notion and note the seat price."},
            completion={"content": ""}, status="success", model="claude-haiku-4-5",
            turn_index=0, message_type="human_message", role="user",
            report_id=ctx["report_id"], user_id=None, created_at=now,
        )
        db.add(user_c)
        await db.flush()

        # 2) assistant (system) message that owns the blocks
        sys_c = Completion(
            prompt={"content": ""},
            completion={"content": "Here's what I found across Notion and the pricing page."},
            status="success", model="claude-haiku-4-5", turn_index=1,
            message_type="ai_completion", role="system", report_id=ctx["report_id"],
            created_at=now + timedelta(seconds=1),
        )
        db.add(sys_c)
        await db.flush()

        ae = AgentExecution(
            completion_id=sys_c.id, organization_id=ctx["org_id"], report_id=ctx["report_id"],
            status="completed", started_at=now, completed_at=now + timedelta(seconds=6),
            latest_seq=len(DEMO_CALLS),
        )
        db.add(ae)
        await db.flush()

        # BEFORE mode: strip the model-authored title so the UI falls back to
        # its old mechanical label (connection name / raw tool). Used to capture
        # the before/after comparison.
        no_titles = os.environ.get("SEED_NO_TITLES") == "1"
        # RUNNING mode: leave the last call in a running state so the shimmer +
        # right-side spinner can be captured.
        running = os.environ.get("SEED_RUNNING") == "1"

        for i, (tool_name, args, result, summary) in enumerate(DEMO_CALLS):
            if no_titles:
                args = {k: v for k, v in args.items() if k != "title"}
            is_last = i == len(DEMO_CALLS) - 1
            te_running = running and is_last
            te = ToolExecution(
                agent_execution_id=ae.id, tool_name=tool_name, tool_action=None,
                arguments_json=args,
                status="running" if te_running else "success",
                success=not te_running,
                started_at=now + timedelta(seconds=i),
                completed_at=None if te_running else now + timedelta(seconds=i + 1),
                duration_ms=None if te_running else 850.0,
                result_summary=None if te_running else summary,
                result_json=None if te_running else result,
            )
            db.add(te)
            await db.flush()

            block = CompletionBlock(
                completion_id=sys_c.id, agent_execution_id=ae.id, source_type="tool",
                tool_execution_id=te.id, block_index=(i + 1) * 100, loop_index=i,
                title=args.get("title") or tool_name,
                status="in_progress" if te_running else "completed", icon="🔧",
                content=None, started_at=te.started_at,
                completed_at=None if te_running else te.completed_at,
                duration_ms=None if te_running else 850.0,
            )
            db.add(block)

        # a trailing final answer block for realism
        final_block = CompletionBlock(
            completion_id=sys_c.id, agent_execution_id=ae.id, source_type="final",
            block_index=(len(DEMO_CALLS) + 1) * 100, loop_index=len(DEMO_CALLS),
            title="Answer", status="completed", icon="✅",
            content="At-risk = no login in 30 days (per the Churn Playbook). Seat price is $20/mo.",
            started_at=now, completed_at=now + timedelta(seconds=6),
        )
        db.add(final_block)
        await db.commit()


def main() -> int:
    ctx = api_setup()
    asyncio.run(seed_blocks(ctx))
    print(json.dumps({
        "report_url": f"http://localhost:3000/reports/{ctx['report_id']}",
        "report_id": ctx["report_id"],
        "email": ctx["email"],
        "password": ctx["password"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
