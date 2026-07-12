#!/usr/bin/env python
"""Live LLM confirmation for connection-tool call titles.

Drives a real Anthropic model (Haiku) through a multi-step agentic tool-use loop
using the ACTUAL input schemas of BOW's connection tools (execute_mcp,
search_mcps, read_file, web_fetch) — the same schemas the planner ships. Each
tool call is dispatched against a small in-memory fake connection (a stand-in
Notion-like MCP server + a file source), so no external network or real
credential is needed for the tools themselves — only a Haiku-capable
ANTHROPIC_API_KEY for the model.

What it proves (the LLM-facing contract for this feature):
  Given only the real tool metadata (descriptions + JSON schemas, now carrying
  the optional `title` field), the model reliably fills `title` with a short,
  human-readable, active-voice label on every connection-tool call — the string
  the UI renders in place of the raw tool name ("Searching Notion for churned
  customers" instead of `notion_search`).

Requires ANTHROPIC_API_KEY in the environment. Never hard-code the key.

Usage:
    ANTHROPIC_API_KEY=... .venv/bin/python scripts/tool_call_titles_live_agent.py
"""
from __future__ import annotations

import json
import os
import sys

import anthropic

from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool
from app.ai.tools.implementations.search_mcps import SearchMCPsTool
from app.ai.tools.implementations.read_file import ReadFileTool
from app.ai.tools.implementations.web_fetch import WebFetchTool

MODEL = "claude-haiku-4-5-20251001"

# The real connection tools whose schemas now advertise `title`.
TOOL_CLASSES = [SearchMCPsTool, ExecuteMCPTool, ReadFileTool, WebFetchTool]
CONNECTION_TOOL_NAMES = {c().metadata.name for c in TOOL_CLASSES}

# A fake "Notion" MCP connection + a file source the model operates on.
CONNECTION_ID = "conn_notion_01"
FAKE_MCP_TOOLS = [
    {"name": "notion_search", "description": "Search Notion pages and databases by keyword.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "notion_get_page", "description": "Fetch a Notion page's content by id.",
     "input_schema": {"type": "object", "properties": {"page_id": {"type": "string"}}, "required": ["page_id"]}},
]
FAKE_PAGES = {
    "pg_churn_playbook": "Churn Playbook: customers with no login in 30d are at risk...",
    "pg_q3_revenue": "Q3 revenue was $4.2M across 318 accounts...",
}


def build_anthropic_tools():
    """Translate the real ToolMetadata (incl. the new `title` field) into the
    Anthropic tools schema — exactly what the planner would send."""
    tools = []
    for cls in TOOL_CLASSES:
        m = cls().metadata
        tools.append({"name": m.name, "description": m.description, "input_schema": m.input_schema})
    return tools


def dispatch(name: str, args: dict) -> dict:
    """Execute a tool call against the in-memory fakes; return a JSON-able result."""
    if name == "search_mcps":
        return {"success": True, "total_count": len(FAKE_MCP_TOOLS), "tools": FAKE_MCP_TOOLS}
    if name == "execute_mcp":
        called = args.get("tool_name")
        inner = args.get("arguments") or {}
        if called == "notion_search":
            return {"success": True, "connection_name": "Notion",
                    "preview": [{"id": pid, "title": pid.replace("pg_", "").replace("_", " ")}
                                for pid in FAKE_PAGES]}
        if called == "notion_get_page":
            pid = inner.get("page_id", "")
            return {"success": True, "connection_name": "Notion",
                    "preview": FAKE_PAGES.get(pid, "not found")}
        return {"success": False, "error_message": f"unknown notion tool {called}"}
    if name == "read_file":
        return {"success": True, "content_type": "text", "text": "sample file body"}
    if name == "web_fetch":
        return {"success": True, "status_code": 200, "content": "Pricing: $20/seat/mo."}
    return {"success": False, "error_message": f"unknown tool {name}"}


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — cannot run the live leg.", file=sys.stderr)
        return 2

    anth = anthropic.Anthropic()
    tools = build_anthropic_tools()

    system = (
        "You are a data analytics agent operating external connections through tools. "
        f"A Notion MCP connection is attached with connection_id '{CONNECTION_ID}'. "
        "Always discover tools with search_mcps before calling execute_mcp. "
        "Pass connection_id to every connection tool. "
        "Follow each tool's schema exactly, including any optional fields it documents. "
        "When you have gathered enough, give a short final text answer."
    )
    task = (
        "Find our Notion page about customer churn, read its content, then also fetch "
        "https://example.com/pricing to note the seat price. Use the connection tools."
    )
    messages = [{"role": "user", "content": task}]

    calls = []  # (tool_name, args)
    for _turn in range(12):
        resp = anth.messages.create(
            model=MODEL, max_tokens=1024, system=system, tools=tools, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                print(f"[assistant] {b.text.strip()[:160]}")
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            break
        results = []
        for tu in tool_uses:
            calls.append((tu.name, tu.input))
            title = tu.input.get("title") if isinstance(tu.input, dict) else None
            print(f"[tool] {tu.name}  title={title!r}  args={json.dumps(tu.input)[:90]}")
            out = dispatch(tu.name, tu.input)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})

    # ---- Verify the contract -------------------------------------------------
    conn_calls = [(n, a) for (n, a) in calls if n in CONNECTION_TOOL_NAMES]
    print("\n=== RESULT ===")
    print(f"total tool calls: {len(calls)}  connection-tool calls: {len(conn_calls)}")

    problems = []
    if len(conn_calls) < 3:
        problems.append(f"expected several connection-tool calls, got {len(conn_calls)}")

    titled = 0
    for name, args in conn_calls:
        title = (args.get("title") or "").strip() if isinstance(args, dict) else ""
        if not title:
            problems.append(f"{name}: no title set")
            continue
        titled += 1
        # A title must be human prose, not the raw tool identifier.
        if title == name or title == args.get("tool_name"):
            problems.append(f"{name}: title echoes the tool name ({title!r})")
        if len(title.split()) < 2:
            problems.append(f"{name}: title not a phrase ({title!r})")
        print(f"  ✓ {name:14s} -> {title!r}")

    # Require that the model titled the large majority of connection calls.
    if conn_calls and titled / len(conn_calls) < 0.8:
        problems.append(f"only {titled}/{len(conn_calls)} connection calls were titled")

    if problems:
        print("\nProblems:")
        for p in problems:
            print(f"  - {p}")
        print("\nLIVE E2E: FAIL")
        return 1

    print("\nLIVE E2E: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
