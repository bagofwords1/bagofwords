"""Sandbox feedback loop: agent publish_status -> clarify threshold.

Validates the prompt change in PromptBuilderV3 that ties the per-data-source
``<status>`` block to how readily the planner reaches for the ``clarify`` tool:

  - ``draft``     (agent still being built) -> clarify freely, like today.
  - ``published`` (agent live in prod)      -> prefer common sense; assume,
                                               state it, proceed; clarify only
                                               for genuine blockers.

It builds the REAL v3 system prompt (PromptBuilderV3) with a REAL rendered
``<status>`` block (TablesSchemaContext) and asks an Anthropic Haiku model to
plan one turn over an ambiguous request. We then inspect whether the model's
first action is a ``clarify`` tool_use or a "just proceed" action.

The API key is read from ANTHROPIC_API_KEY — never hardcoded, never logged.

Run:
    cd backend
    export ANTHROPIC_API_KEY=...        # never commit this
    /tmp/venv312/bin/python scripts/clarify_status_feedback_loop.py
"""
from __future__ import annotations

import os
import sys

import anthropic

# Make `app...` importable when run from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.ai.prompt_formatters import Table, TableColumn
from app.schemas.ai.planner import PlannerInput, ToolDescriptor
from app.schemas.data_source_schema import DataSourceSummarySchema

HAIKU_MODEL = os.environ.get("HAIKU_MODEL", "claude-haiku-4-5-20251001")


# --- A users table with NO unambiguous "active" signal -------------------
USERS_TABLE = Table(
    name="users",
    columns=[
        TableColumn(name="id", dtype="int"),
        TableColumn(name="email", dtype="varchar"),
        TableColumn(name="created_at", dtype="timestamp"),
        TableColumn(name="last_login_at", dtype="timestamp"),
        TableColumn(name="plan", dtype="varchar"),
    ],
    pks=[], fks=[], is_active=True,
)


def schemas_for(publish_status: str) -> str:
    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(
            id="1", name="ProductDB", type="postgres", publish_status=publish_status
        ),
        tables=[USERS_TABLE],
    )
    ctx = TablesSchemaContext(data_sources=[ds])
    return ctx.render_combined(top_k_per_ds=10, index_limit=200)


def tool_catalog() -> list[ToolDescriptor]:
    return [
        ToolDescriptor(
            name="clarify",
            description="Ask the user a clarifying question when the request is ambiguous or a term is undefined.",
            schema={"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
        ),
        ToolDescriptor(
            name="create_data",
            description="Create a tracked data visualization/widget by running SQL against the connected data.",
            schema={"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
        ),
        ToolDescriptor(
            name="inspect_data",
            description="Peek at table values (max 2-3 LIMIT 3 queries) to validate assumptions.",
            schema={"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
        ),
        ToolDescriptor(
            name="describe_tables",
            description="Get column-level info for specific tables before creating a widget.",
            schema={"type": "object", "properties": {"tables": {"type": "array", "items": {"type": "string"}}}},
        ),
    ]


def build_request(publish_status: str, user_message: str):
    pi = PlannerInput(
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
        user_message=user_message,
        instructions="<organization_instructions>No instructions defined yet.</organization_instructions>",
        schemas_combined=schemas_for(publish_status),
        tool_catalog=tool_catalog(),
        mode="chat",
    )
    v3 = PromptBuilderV3.build(pi)
    return v3.system, v3.messages, v3.tools


def run_case(client, publish_status: str, user_message: str):
    system, messages, tools = build_request(publish_status, user_message)
    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1024,
        temperature=0,  # determinism for a stable assertion
        system=system,
        messages=messages,
        tools=tools,
    )
    tool_used = None
    text = ""
    for block in resp.content:
        if block.type == "tool_use":
            tool_used = block.name
        elif block.type == "text":
            text += block.text
    return tool_used, text.strip()


# A few non-clarify build/research tools — using any of them (or a text answer
# with no question) means the model "proceeded" rather than asking.
_PROCEED_TOOLS = {"create_data", "inspect_data", "describe_tables", "read_resources"}


def classify(tool_used, text) -> str:
    """CLARIFY = asked the user (clarify tool, OR a text question).
    PROCEED  = moved toward an answer (a build/research tool, or a text answer)."""
    if tool_used == "clarify":
        return "CLARIFY"
    if tool_used in _PROCEED_TOOLS:
        return "PROCEED"
    if tool_used is None:
        # No tool -> plain text. A question mark means it's clarifying in prose.
        return "CLARIFY" if "?" in (text or "") else "PROCEED"
    return "PROCEED"


REPEATS = int(os.environ.get("REPEATS", "2"))

# Class A — ORDINARY ambiguity (one sensible schema mapping, fuzzy scope).
# Intended behavior: a draft agent clarifies to capture the definition; a
# published agent uses common sense, assumes, and proceeds.
DISCRIMINATORS = [
    "How many users have logged in?",
    "Count users who have logged in.",
    "How many accounts have a login?",
]

# Class B — HARD blockers (a core term with several materially different
# meanings and no schema/instruction hint). BOTH postures should clarify:
# published is allowed to relax for ordinary ambiguity, NOT to recklessly guess
# genuinely undefined business terms.
HARD_BLOCKERS = [
    "How many active users do we have?",
    "Show me our power users.",
]


def _verdicts(client, status, msg):
    """Run REPEATS times; return list of CLARIFY/PROCEED verdicts."""
    return [classify(*run_case(client, status, msg)) for _ in range(REPEATS)]


def main() -> int:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ERROR: set ANTHROPIC_API_KEY in the environment.")
        return 2
    client = anthropic.Anthropic(api_key=key)

    print(f"model: {HAIKU_MODEL}  (repeats={REPEATS}, temperature=0)\n")
    all_ok = True

    print("== Class A: ordinary ambiguity — draft clarifies, published proceeds ==")
    for msg in DISCRIMINATORS:
        d = _verdicts(client, "draft", msg)
        p = _verdicts(client, "published", msg)
        # draft should clarify every run; published should never clarify
        ok = all(v == "CLARIFY" for v in d) and all(v == "PROCEED" for v in p)
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] \"{msg}\"")
        print(f"   draft={d}  published={p}")

    print("\n== Class B: hard blockers — BOTH clarify (published is not reckless) ==")
    for msg in HARD_BLOCKERS:
        d = _verdicts(client, "draft", msg)
        p = _verdicts(client, "published", msg)
        # both postures should clarify on a genuinely undefined term
        ok = all(v == "CLARIFY" for v in d) and all(v == "CLARIFY" for v in p)
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] \"{msg}\"  draft={d} published={p}")

    print("\nRESULT:", "ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
