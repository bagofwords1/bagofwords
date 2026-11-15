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


def _planner():
    import types
    from app.ai.agents.planner.planner_v3 import PlannerV3
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
    planner = PlannerV3.__new__(PlannerV3)  # skip __init__ (constructs a real LLM)
    planner.tool_catalog = []
    planner.prompt_builder = PromptBuilderV3()
    planner._tool_category = {}
    planner.llm = types.SimpleNamespace(model=types.SimpleNamespace(model_id="any-model", provider=types.SimpleNamespace(provider_type="custom")))
    return planner


async def _forced_decision(planner, emitted, seen):
    import asyncio
    from app.ai.llm.types import MessageStopEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent
    from app.schemas.ai.planner import PlannerInput, ToolDescriptor

    async def stream(**kwargs):
        seen.update(kwargs)
        yield ToolUseStartEvent(id="tu_1", name=emitted)
        yield ToolUseCompleteEvent(id="tu_1", name=emitted, input={"title": "x"})
        yield MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")
        yield UsageEvent(input_tokens=100, output_tokens=20)
    planner.llm.inference_stream_v2 = stream
    catalog = [ToolDescriptor(name=n, description=n, schema={"type": "object"}) for n in ("create_data", "create_artifact", "clarify")]
    final = None
    async for evt in planner.execute(PlannerInput(user_message="Build a dashboard", tool_catalog=catalog,
                                                  forced_tools=["create_artifact", "clarify"]), sigkill_event=asyncio.Event()):
        if getattr(evt, "type", "") == "planner.decision.final":
            final = evt.data
    return final


@pytest.mark.asyncio
async def test_a_forced_call_offers_only_the_build_tools_and_requires_one():
    seen = {}
    final = await _forced_decision(_planner(), "create_artifact", seen)
    assert sorted(t.name for t in seen["tools"]) == ["clarify", "create_artifact"]
    assert seen["require_tool"] is True and seen["thinking"] == {"type": "disabled"}
    assert final.error is None and final.actions[0].name == "create_artifact"


@pytest.mark.asyncio
async def test_a_stray_tool_on_a_forced_call_is_an_error_not_a_dispatch():
    final = await _forced_decision(_planner(), "create_data", {})
    assert final.error is not None and final.error.code == "tool_not_offered"
    assert not final.actions


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
