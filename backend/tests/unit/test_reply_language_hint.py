"""Per-turn reply-language hint for app conversations (findings 6 and 10).

A short Hebrew follow-up ("ומה עם מרץ?") in an app conversation was sometimes
answered in English. ``PromptBuilderV3._format_reply_language`` can add one
unlabelled line at the very end of the per-turn content, on the conversation
surface only. ``REPLY_LANGUAGE_MODE`` picks the variant ("off", "tail",
"detected"); ``BOW_REPLY_LANGUAGE_MODE`` overrides it per call so the live
measurement can switch variants without editing source. The line never sits
inside ``<artifact_chat_context>`` and never carries a ``LANGUAGE`` label.
"""
from __future__ import annotations

import re

import pytest

from app.ai.agents.planner import prompt_builder_v3
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.ai.message_script import detect_message_script
from app.schemas.ai.planner import PlannerInput

TAIL = "Reply in the language of the viewer's latest message."
DETECTED_HE = "The viewer's latest message is in Hebrew: write this turn's text in Hebrew."
HEBREW_FOLLOW_UP = "ומה עם מרץ?"
CTX = {"artifact_title": "Sales overview", "source_report_title": "Sales overview", "scope": "agents"}


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("BOW_REPLY_LANGUAGE_MODE", raising=False)


# --- T5.2: detect_message_script -------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("ומה עם מרץ?", "he"),
    ("show me לקוחות", None),  # mixed: Hebrew is 5 of 11 letters, below 60%
    ("¿y marzo?", None),  # Latin scripts are never named
    ("14", None),  # no letters
    ("", None),
    ("ما هي المبيعات؟", "ar"),
    ("а что с мартом?", "ru"),
    ("א", None),  # a single letter is not enough
    ("Q3 לפי מדינה", "he"),  # 8 of 9 letters are Hebrew
])
def test_detect_message_script(text, expected):
    assert detect_message_script(text) == expected


def test_detect_message_script_tolerates_none():
    assert detect_message_script(None) is None


# --- T5.3: _format_reply_language -------------------------------------------------

def _input(surface="conversation", platform="artifact_chat", message=HEBREW_FOLLOW_UP) -> PlannerInput:
    ctx = dict(CTX)
    if surface is not None:
        ctx["surface"] = surface
    return PlannerInput(
        user_message=message, organization_name="Acme", organization_ai_analyst_name="Analyst",
        external_platform=platform, platform_context=ctx if platform else None)


def _hint(mode, monkeypatch, **kw) -> str:
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", mode)
    return PromptBuilderV3._format_reply_language(_input(**kw))


def test_the_committed_mode_is_off():
    assert prompt_builder_v3.REPLY_LANGUAGE_MODE == "off"


def test_tail_mode_line(monkeypatch):
    assert _hint("tail", monkeypatch) == TAIL
    assert _hint("tail", monkeypatch, message="and March?") == TAIL


def test_detected_mode_names_the_script_in_english(monkeypatch):
    assert _hint("detected", monkeypatch) == DETECTED_HE
    assert _hint("detected", monkeypatch, message="а что с мартом?") == (
        "The viewer's latest message is in Russian: write this turn's text in Russian.")


def test_detected_mode_is_silent_without_a_named_script(monkeypatch):
    for message in ("and March?", "show me לקוחות", "14"):
        assert _hint("detected", monkeypatch, message=message) == ""


def test_off_mode_is_silent(monkeypatch):
    assert _hint("off", monkeypatch) == ""


@pytest.mark.parametrize("mode", ["off", "tail", "detected"])
@pytest.mark.parametrize("kw", [
    {"surface": None},  # the bubble
    {"surface": "bubble"},
    {"platform": None, "surface": None},  # a plain report
    {"platform": "slack", "surface": "conversation"},
])
def test_no_line_outside_the_conversation_surface(mode, kw, monkeypatch):
    assert _hint(mode, monkeypatch, **kw) == ""


def test_the_line_never_carries_a_language_label(monkeypatch):
    for mode in ("tail", "detected"):
        line = _hint(mode, monkeypatch)
        assert line and not re.match(r"^\s*language\s*:", line, re.I)
        assert "LANGUAGE" not in line


# --- env override ------------------------------------------------------------------

def test_env_override_switches_the_variant_without_editing_source(monkeypatch):
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", "off")
    monkeypatch.setenv("BOW_REPLY_LANGUAGE_MODE", "tail")
    assert PromptBuilderV3._format_reply_language(_input()) == TAIL
    monkeypatch.setenv("BOW_REPLY_LANGUAGE_MODE", " Detected ")  # trimmed, case-insensitive
    assert PromptBuilderV3._format_reply_language(_input()) == DETECTED_HE
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", "tail")
    monkeypatch.setenv("BOW_REPLY_LANGUAGE_MODE", "off")
    assert PromptBuilderV3._format_reply_language(_input()) == ""


@pytest.mark.parametrize("value", ["loud", "", "tail,detected", "1"])
def test_an_invalid_env_value_falls_back_to_the_constant(value, monkeypatch):
    monkeypatch.setenv("BOW_REPLY_LANGUAGE_MODE", value)
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", "tail")
    assert PromptBuilderV3._format_reply_language(_input()) == TAIL
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", "off")
    assert PromptBuilderV3._format_reply_language(_input()) == ""


def test_the_env_override_still_respects_the_surface(monkeypatch):
    monkeypatch.setenv("BOW_REPLY_LANGUAGE_MODE", "tail")
    assert PromptBuilderV3._format_reply_language(_input(surface=None)) == ""


# --- placement on both V3 paths -----------------------------------------------------

def _prompt(transcript, monkeypatch, **kw) -> str:
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", transcript)
    return PromptBuilderV3.build_prompt(_input(**kw))


def _last_text(transcript, monkeypatch) -> str:
    """The text of the last part of the last message actually sent."""
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", transcript)
    content = PromptBuilderV3.build(_input()).messages[-1]["content"]
    if isinstance(content, list):
        content = [p for p in content if p.get("type") == "text"][-1]["text"]
    return content


@pytest.mark.parametrize("transcript", ["1", "0"])
@pytest.mark.parametrize("mode, line", [("tail", TAIL), ("detected", DETECTED_HE)])
def test_the_line_is_the_last_part_of_the_turn_on_both_paths(transcript, mode, line, monkeypatch):
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", mode)
    prompt = _prompt(transcript, monkeypatch)
    assert prompt.count(line) == 1
    assert _last_text(transcript, monkeypatch).endswith("\n" + line)
    block = re.search(r"<artifact_chat_context>.*?</artifact_chat_context>", prompt, re.S).group(0)
    assert line not in block
    assert prompt.index(line) > prompt.index("</artifact_chat_context>")


@pytest.mark.parametrize("transcript", ["1", "0"])
@pytest.mark.parametrize("mode", ["tail", "detected"])
@pytest.mark.parametrize("kw", [{"surface": None}, {"platform": None, "surface": None}])
def test_the_bubble_and_plain_reports_are_byte_identical_in_every_mode(transcript, mode, kw, monkeypatch):
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", "off")
    baseline = _prompt(transcript, monkeypatch, **kw)
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", mode)
    assert _prompt(transcript, monkeypatch, **kw) == baseline


@pytest.mark.parametrize("transcript", ["1", "0"])
def test_turn_head_and_user_message_end_with_the_line(transcript, monkeypatch):
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", "tail")
    assert PromptBuilderV3._build_turn_head(_input()).endswith("\n" + TAIL)
    assert PromptBuilderV3._build_user_message(_input()).endswith("\n" + TAIL)
    monkeypatch.setattr(prompt_builder_v3, "REPLY_LANGUAGE_MODE", "off")
    assert not PromptBuilderV3._build_turn_head(_input()).endswith(TAIL)
    assert PromptBuilderV3._build_user_message(_input()).endswith("</context>")
