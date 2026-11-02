"""The planner keeps answering in the user's language through a tool loop.

Contract: whatever tool results come back (usually English SQL, logs and
column names), the newest message the model reads ends with a reminder of the
language the user wrote in. The reminder is built from the user's own words —
never from tool output — and never enters the cached system prompt.
"""
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3  # noqa: E402
from app.ai.prompt_language import (  # noqa: E402
    detect_single_language_script,
    reply_language_reminder,
)
from app.schemas.ai.planner import PlannerInput  # noqa: E402


def _english_tool_results(n: int) -> list:
    return [
        {
            "execution_number": i,
            "tool_name": "inspect_data",
            "loop_index": i,
            "tool_input": {"user_prompt": f"Check the date range of Invoice.InvoiceDate ({i})"},
            "observation": {
                "summary": "Inspection finished",
                "success": True,
                "execution_log": "min_date max_date row_count total_sum\n2021-01-01 2025-12-22 412 2328.6",
            },
        }
        for i in range(n)
    ]


def _input(user_message: str, *, tool_results: int = 2, **kw) -> PlannerInput:
    obs = _english_tool_results(tool_results)
    return PlannerInput(
        organization_name="Org",
        organization_ai_analyst_name="AI",
        timezone="UTC",
        user_message=user_message,
        past_observations=obs,
        last_observation=obs[-1]["observation"] if obs else None,
        tool_catalog=[],
        schemas_combined="<schemas><table name='Invoice'/></schemas>",
        **kw,
    )


def _last_message_text(v3) -> str:
    return json.dumps(v3.messages[-1]["content"], ensure_ascii=False, default=str)


@pytest.fixture(params=["transcript", "legacy"])
def planner_path(request, monkeypatch):
    """Both prompt shapes the planner can run with must carry the reminder."""
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1" if request.param == "transcript" else "0")
    return request.param


@pytest.mark.parametrize(
    "ask, language",
    [
        ("כמה מכרנו בשלוש שנים האחרונות ?", "Hebrew"),
        ("הראה לי את Invoice.Total לפי שנה", "Hebrew"),
        ("Πόσες πωλήσεις είχαμε πέρσι;", "Greek"),
        ("昨年の売上はいくらでしたか", "Japanese"),
    ],
)
@pytest.mark.parametrize("tool_results", [0, 1, 3])
def test_newest_message_names_the_users_language_after_english_tool_results(
    planner_path, ask, language, tool_results
):
    v3 = PromptBuilderV3.build(_input(ask, tool_results=tool_results))
    tail = _last_message_text(v3)
    assert "<reply_language>" in tail
    assert f"in {language}" in tail
    # The reminder is the very end of what the model reads before answering.
    assert tail.rstrip('"').rstrip().endswith("</reply_language>")


def test_reminder_stays_out_of_the_cached_system_prompt(planner_path):
    ask = "מה היו המכירות ברבעון האחרון?"
    v3 = PromptBuilderV3.build(_input(ask))
    assert "<reply_language>" not in v3.system
    assert ask not in v3.system


@pytest.mark.parametrize(
    "ask",
    ["¿Cuánto vendimos en los últimos tres años?", "Combien avons-nous vendu en 2024 ?"],
)
def test_shared_script_languages_are_matched_by_quote_not_named(planner_path, ask):
    # Latin script is shared by many languages, so the reminder quotes the
    # user's words instead of guessing a name that might be wrong.
    tail = _last_message_text(PromptBuilderV3.build(_input(ask)))
    assert "same language as the user's message" in tail
    assert ask[:20] in tail
    assert "in English" not in tail


@pytest.mark.parametrize("short_ask", ["ok", "2024", "👍", "https://example.com/report?id=7"])
def test_short_message_keeps_the_conversations_language(short_ask):
    reminder = reply_language_reminder(
        short_ask,
        recent_user_messages=["הראה לי את המכירות לפי שנה", "hi"],
        locale="en",
    )
    assert "in Hebrew" in reminder


def test_short_message_with_no_history_falls_back_to_org_locale():
    he = reply_language_reminder("2024", recent_user_messages=None, locale="he")
    assert "Hebrew" in he
    en = reply_language_reminder("ok", recent_user_messages=[], locale=None)
    assert "English" in en


def test_the_newest_steer_decides_the_language():
    # A mid-run steer is the user's newest words; its English framing is not.
    message = (
        "Show sales by year\n\n<steering_updates>\n"
        "URGENT — the user sent these instructions WHILE you were working.\n"
        "- תוסיף גם את 2020\n</steering_updates>"
    )
    assert "in Hebrew" in reply_language_reminder(message)


def test_sub_agents_get_no_reminder(monkeypatch):
    # A sub-agent's goal is written by the parent agent and its text goes
    # back to the parent, so the user's language does not apply.
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1")
    v3 = PromptBuilderV3.build(_input("Count invoices per year", mode="subagent"))
    assert "<reply_language>" not in json.dumps(v3.messages, ensure_ascii=False, default=str)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("כמה לקוחות יש לנו בטבלת Customer?", "Hebrew"),
        ("How many customers do we have?", None),
        ("Сколько у нас клиентов?", None),  # Cyrillic: several languages
        ("كم عدد العملاء لدينا؟", None),  # Arabic script: several languages
        ("שלום", "Hebrew"),
        ("12345", None),
    ],
)
def test_script_detection_only_names_one_language_scripts(text, expected):
    assert detect_single_language_script(text) == expected
