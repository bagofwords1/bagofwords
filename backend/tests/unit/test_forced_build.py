"""A build that was asked for and not done gets one forced call: build, or ask.

Haiku ended a quarter of the build turns on the benchmark with the deliverable
written in chat ("The scorecard is built" over a markdown table). A one-time
reminder did not change it. Now, when a person asked for a page or a document
and the turn tries to end with neither, the next planner call offers only the
build tools and clarify and requires a tool call, once per turn.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.agent_v2 import AgentV2, build_ask_kinds, forced_build_note  # noqa: E402
from app.ai.llm.clients.anthropic_client import Anthropic  # noqa: E402
from app.ai.llm.clients.openai_client import OpenAi  # noqa: E402
from app.ai.llm.types import Message, ToolSpec  # noqa: E402

PAGES = [
    "Build a sales dashboard: revenue by month, top countries and headline KPIs.",
    "Make a scorecard for the sales support reps: customers, revenue and month-over-month change.",
    "Create an overview screen of the top 15 artists by revenue, with a search box.",
    "Make a shipment tracker table for the operations team.",
    "בנה דשבורד מכירות לחנות: הכנסות לפי חודש והמדינות המובילות.",
    "צור פורטל קטן לצוות המכירות עם רשימת הלקוחות.",
    "Crea un panel de gastos por centro de costo con la tendencia mensual.",
]
DOCS = [
    "Write a month-end close summary for December for the CFO.",
    "Write a weekly support review for the last week of the year.",
    "Prepara un informe trimestral del pipeline de ventas por segmento.",
    "הכן דוח רבעוני להנהלה על ביצועי המכירות, עם גרפים.",
]
NOT_BUILDS = [
    "What was revenue by country last quarter?",
    "Which are the top 10 genres by revenue?",
    "כמה לקוחות יש לנו בכל מדינה?",
    "How do I create a dashboard?",
    "Why is the dashboard slow?",
    "",
]


def test_page_and_document_asks_are_recognised():
    for m in PAGES:
        assert "page" in build_ask_kinds(m), m
    for m in DOCS:
        assert build_ask_kinds(m) == {"doc"}, m


def test_questions_and_how_to_are_not_build_asks():
    for m in NOT_BUILDS:
        assert build_ask_kinds(m) == set(), m


def test_the_note_names_the_build_and_the_exit():
    note = forced_build_note({"page"})
    assert "create_artifact" in note and "clarify" in note and "a page" in note


def agent(tools):
    a = AgentV2.__new__(AgentV2)
    a.planner = SimpleNamespace(tool_catalog=[SimpleNamespace(name=n) for n in tools])
    a._forced_tools_next = None
    return a


def test_forced_tools_are_the_build_tools_offered_plus_clarify():
    a = agent(["create_data", "create_artifact", "edit_artifact", "create_doc", "clarify"])
    assert a._forced_tools_for({"page"}) == ["create_artifact", "edit_artifact", "clarify"]
    assert a._forced_tools_for({"doc"}) == ["create_doc", "clarify"]
    assert agent(["create_data", "clarify"])._forced_tools_for({"page"}) == [], "no build tool here: nothing to force"


def test_the_forced_set_applies_to_one_planner_call():
    a = agent(["create_artifact", "clarify"])
    a._forced_tools_next = ["create_artifact", "clarify"]
    assert a._take_forced_tools() == ["create_artifact", "clarify"]
    assert a._take_forced_tools() is None


class _EmptyStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


TOOLS = [ToolSpec(name="create_artifact", description="build a page", input_schema={"type": "object"})]


@pytest.mark.asyncio
async def test_anthropic_requires_a_tool_call_when_asked():
    client = Anthropic(api_key="test-key-not-used")
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)
        return _EmptyStream()
    client.async_client.messages.create = create
    async for _ in client.inference_stream_v2(model_id="claude-haiku-4-5-20251001", messages=[Message(role="user", content="x")],
                                              tools=TOOLS, thinking={"type": "disabled"}, require_tool=True):
        pass
    assert seen["extra_body"]["tool_choice"]["type"] == "any"
    seen.clear()
    async for _ in client.inference_stream_v2(model_id="claude-haiku-4-5-20251001", messages=[Message(role="user", content="x")], tools=TOOLS):
        pass
    assert seen["extra_body"]["tool_choice"]["type"] == "auto", "unchanged by default"


@pytest.mark.asyncio
async def test_openai_requires_a_tool_call_when_asked():
    client = OpenAi(api_key="test-key")
    create = AsyncMock(side_effect=lambda **kw: _EmptyStream())
    client.async_client.chat.completions.create = create
    async for _ in client.inference_stream_v2("gpt-4o", [Message(role="user", content="x")], tools=TOOLS, require_tool=True):
        pass
    assert create.call_args.kwargs["tool_choice"] == "required"
