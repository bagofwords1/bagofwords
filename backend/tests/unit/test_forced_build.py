"""A build that was asked for and not done gets one forced call: build, or ask.

Haiku ended a quarter of the build turns on the benchmark with the deliverable
written in chat ("The scorecard is built" over a markdown table). A one-time
reminder did not change it. Now, when a person asked for a page or a document
and the turn tries to end with neither, the next planner call offers only the
build tools and clarify and requires a tool call, once per turn.

This file covers the parts with a public seam of their own: the build-ask
detector, the note and the provider clients' tool_choice. The agent turn
itself (the forced call's tools, the retry after a stray tool, steering,
clarify) runs through the completions route in
tests/e2e/test_forced_build_turn.py.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.agent_v2 import build_ask_kinds, forced_build_note  # noqa: E402
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


# A prohibition that targets the build, or an explicit ask for a chat-only
# answer, is not a build ask: the turn may end in text.
REFUSED_BUILDS = [
    "Do not create a dashboard; answer in chat only.",
    "Don't build a page for this, just answer.",
    "No need to make a scorecard, give me the top three reps.",
    "Please never generate a report for this one.",
    "What was revenue last month? Without a dashboard please.",
    "Revenue by region, answer in chat, I don't need a dashboard.",
    "List the top customers in text only, I will build the dashboard myself later.",
    "אל תבנה דשבורד, רק תענה מה היו ההכנסות בחודש שעבר.",
    "אין צורך בדשבורד, כמה לקוחות חדשים היו ברבעון?",
    "תן לי את ההכנסות לפי אזור, בלי דשבורד.",
    "No crees un tablero, solo responde cuántos clientes hay.",
    "No hace falta un panel, ¿cuál fue el ingreso del mes pasado?",
    "Dame los ingresos por región, sin dashboard.",
    "Escribe la respuesta aquí, sin panel ni informe: ¿cuánto fue el ingreso del mes pasado?",
    # The refusal alone decides these: no chat-only phrase, and another build
    # verb ("write", "escribe", "כתוב") is live in the next clause.
    "No hagas un tablero, escribe el total aquí",
    "No crees un panel, prepara una lista corta en el chat",
    "No construyas un tablero; haz una tabla simple aquí",
    "No crees un informe de esto, escribe la cifra aquí.",
    "אל תעשה דשבורד, כתוב את המספר",
    "לא ליצור דשבורד. כתוב לי תשובה קצרה",
    "Write the totals here without a dashboard or a report",
    "No dashboard or report please, write the top 5 customers here",
    "Instead of building a dashboard, write the answer",
    "Please do not, under any circumstances, build a dashboard",
    "Don't bother putting together a report, write the number here.",
    "Rather than creating a page, write the numbers here.",
    "Give me the churn figure without a report or a dashboard.",
    "No prepares un informe, escribe el total aquí.",
    "No me generes un reporte; dame solo la cifra.",
    "En lugar de crear un tablero, escribe cuántos pedidos hubo.",
    "אל תכין דוח, תגיד לי רק את הסכום.",
    "בלי לבנות דשבורד, כתוב לי כמה הזמנות היו החודש.",
    "תן לי את המספרים בלי דשבורד או דוח, כתוב אותם כאן.",
    # "just answer", "only answer", "answer here" open the message: nothing was
    # asked for before them, and the build verb after is the person's own plan.
    "Just answer: what was revenue last month? I'll build the dashboard myself.",
    "Just tell me the top customers, I'll make the dashboard myself.",
    "Only answer the question, I'll put together the report later.",
    "Answer here, I will make the dashboard later.",
    "Solo responde: escribe el total. El tablero lo hago yo.",
    "רק תענה, אני אבנה את הדשבורד בעצמי. כתוב את הסכום",
]
# A negation that targets something else in a build ask leaves the ask standing.
STILL_BUILDS = [
    ("Build a dashboard of revenue by month, don't include refunds.", {"page"}),
    ("Create a sales page and do not show the cost columns.", {"page"}),
    ("Don't just answer in chat: build a dashboard of revenue by month.", {"page"}),
    ("Don't make a dashboard, write a short report instead.", {"doc"}),
    ("Create a report without a dashboard.", {"doc"}),
    ("בנה דשבורד הכנסות לפי חודש, אל תכלול החזרים.", {"page"}),
    ("Crea un panel de ventas por mes, no incluyas las devoluciones.", {"page"}),
    # "no" as an interjection before the verb is not a refusal.
    ("no build the dashboard", {"page"}),
    ("no just build the dashboard", {"page"}),
    ("No please build a dashboard of revenue", {"page"}),
    ("no make it a dashboard", {"page"}),
    ("No worries, create a report on churn.", {"doc"}),
    ("No tengo tiempo, crea un informe de ventas.", {"doc"}),
    ("אין לי זמן, צור דוח מכירות.", {"doc"}),
    # "just tell me", "reply here" and "only answer" say nothing about where
    # the deliverable goes: the build ask stands.
    ("Build a dashboard and just tell me when it is ready", {"page"}),
    ("Create a report and reply here with the link", {"doc"}),
    ("Build a dashboard. If something is unclear just reply with a question.", {"page"}),
    ("I asked you to build a dashboard and you only answer in text. Build the dashboard.", {"page"}),
    ("I asked for a dashboard, not a text-only answer. Build it.", {"page"}),
    ("Crea un tablero y solo dime cuando esté listo", {"page"}),
    ("בנה דשבורד ורק תענה לי כשזה מוכן", {"page"}),
    # "or" between two nouns that are asked for carries nothing.
    ("Build a dashboard, no need to ask me questions first.", {"page"}),
    ("Make a dashboard or a report of sales by region.", {"page", "doc"}),
    ("Haz un tablero de ventas y no olvides los filtros.", {"page"}),
    ("Prepara un resumen o un informe de gastos.", {"doc"}),
    ("בנה דשבורד ואל תשכח את המסננים.", {"page"}),
    ("הכן דוח או סיכום של ההוצאות.", {"doc"}),
    # "לא" and "not" also stand alone as "no": an aside after them is not a
    # refusal of the verb that follows it.
    ("לא, כמו שאמרתי, תבנה דשבורד", {"page"}),
    ("לא, בבקשה, תבנה דשבורד", {"page"}),
    ("לא, לא ככה, תבנה דשבורד חדש", {"page"}),
    ("If not, as discussed, build a dashboard", {"page"}),
    ("Sure, why not, go ahead, build the dashboard", {"page"}),
    # "instead of writing…", "without creating…", "לא רוצה לכתוב…" with no
    # comma: the refused part ends where the ask's own verb starts.
    ("Instead of writing a long answer build a dashboard", {"page"}),
    ("Rather than writing it all out in chat just build a dashboard", {"page"}),
    ("Without creating new queries build a dashboard of revenue", {"page"}),
    ("אני לא רוצה לכתוב שאילתות אז תבנה לי דשבורד", {"page"}),
]
# Known misses. "no/not + noun" as a statement of absence ("we have no
# dashboard, build one") reads to a pattern exactly like a refusal ("no
# dashboard, write the number"), so the detector takes it as one and the turn
# is not forced. That is the safe side: a miss leaves the model its own choice,
# a false hit would override a chat answer the person asked for.
KNOWN_MISSES = [
    "We have no dashboard for sales, build one",
    "There is no sales dashboard yet. Create one with revenue by month.",
    "This is not the dashboard I asked for, build it again with revenue by month",
]


def test_a_prohibited_build_or_a_chat_only_ask_is_not_a_build_ask():
    for m in REFUSED_BUILDS:
        assert build_ask_kinds(m) == set(), m


def test_a_negation_aimed_elsewhere_keeps_the_build_ask():
    for m, kinds in STILL_BUILDS:
        assert build_ask_kinds(m) == kinds, m


def test_a_missing_page_reads_as_a_refusal_and_is_not_forced():
    for m in KNOWN_MISSES:
        assert build_ask_kinds(m) == set(), m


def test_the_note_names_the_build_and_the_exit():
    note = forced_build_note({"page"})
    assert "create_artifact" in note and "clarify" in note and "a page" in note


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


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id,expected", [
    ("claude-haiku-5-5", "any"),
    ("claude-sonnet-5-5", "auto"),   # rejects a forced tool_choice with HTTP 400
    ("claude-fable-5-1", "auto"),
    ("claude-fable-5-2", "auto"),
    ("claude-mythos-5-1", "auto"),
    ("claude-mythos-5-2", "auto"),
    ("claude-opus-5-5", "auto"),
    ("claude-fable-5", "any"),       # the 5.0 models still accept forcing
    ("claude-mythos-5", "any"),
])
async def test_models_that_reject_a_forced_tool_choice_get_auto(model_id, expected):
    client = Anthropic(api_key="test-key-not-used")
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)
        return _EmptyStream()
    client.async_client.messages.create = create
    async for _ in client.inference_stream_v2(model_id=model_id, messages=[Message(role="user", content="x")],
                                              tools=TOOLS, thinking={"type": "disabled"}, require_tool=True):
        pass
    assert seen["extra_body"]["tool_choice"]["type"] == expected
