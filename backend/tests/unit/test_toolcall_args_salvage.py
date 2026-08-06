"""Salvage ladder for streamed tool-call argument JSON.

Models mangle tool-call argument JSON in predictable ways — prose wrappers,
Python-style single quotes, unescaped in-word quotes in Hebrew/Arabic values
(ארה"ב), raw newlines inside strings. Each used to collapse the whole call
into an ``_unparsable`` marker whose downstream symptom was the misleading
"user_prompt: Field required". The ladder recovers every predictable case and
falls back to an accurate marker for the rest.
"""
from __future__ import annotations

import json

import pytest

from app.ai.llm.toolcall_args import (
    ERROR_KEY,
    RAW_KEY,
    UNPARSABLE_KEY,
    parse_tool_call_arguments,
)


def test_valid_json_passes_through():
    args = parse_tool_call_arguments('{"user_prompt": "צור טבלה", "title": "מס 2025"}')
    assert args == {"user_prompt": "צור טבלה", "title": "מס 2025"}


def test_empty_buffer_is_empty_object():
    assert parse_tool_call_arguments("") == {}
    assert parse_tool_call_arguments("   ") == {}


# --- unescaped in-word quotes (the Hebrew abbreviation case) ---------------

def test_unescaped_hebrew_abbreviation_quote_in_value():
    raw = '{"user_prompt": "ניתוח חברות ארה"ב לפי שנה", "title": "Analysis"}'
    args = parse_tool_call_arguments(raw, "write_csv")
    assert args["user_prompt"] == 'ניתוח חברות ארה"ב לפי שנה'
    assert args["title"] == "Analysis"


def test_multiple_embedded_quotes():
    raw = '{"user_prompt": "השווה בין צה"ל, מנכ"ל וארה"ב", "title": "T"}'
    args = parse_tool_call_arguments(raw)
    assert args["user_prompt"] == 'השווה בין צה"ל, מנכ"ל וארה"ב'


def test_embedded_quote_followed_by_comma_inside_text():
    # The "," after the embedded quote must not close the string: what follows
    # the comma is prose, not a "key": pattern.
    raw = '{"user_prompt": "אמר "שלום", ואז הלך", "title": "T"}'
    args = parse_tool_call_arguments(raw)
    assert args["user_prompt"] == 'אמר "שלום", ואז הלך'
    assert args["title"] == "T"


def test_raw_newline_inside_string_value():
    raw = '{"user_prompt": "line one\nline two"}'
    args = parse_tool_call_arguments(raw)
    assert args["user_prompt"] == "line one\nline two"


# --- prose wrappers --------------------------------------------------------

def test_markdown_fence_wrapper():
    raw = '```json\n{"user_prompt": "build table"}\n```'
    args = parse_tool_call_arguments(raw)
    assert args == {"user_prompt": "build table"}


def test_tool_name_prefix_wrapper():
    raw = 'tools.write_csv: {"user_prompt": "build table"}'
    args = parse_tool_call_arguments(raw)
    assert args == {"user_prompt": "build table"}


def test_long_leading_junk_is_not_stripped():
    # Stripping a large prefix would mean silently discarding data.
    raw = ("x" * 200) + '{"a": 1}'
    args = parse_tool_call_arguments(raw)
    assert args.get(UNPARSABLE_KEY) is True


# --- python-literal syntax -------------------------------------------------

def test_single_quoted_python_style_dict():
    raw = "{'user_prompt': 'ניתוח ארה\"ב', 'title': 'T'}"
    args = parse_tool_call_arguments(raw)
    assert args["user_prompt"] == 'ניתוח ארה"ב'
    assert args["title"] == "T"


# --- honest failure --------------------------------------------------------

def test_truncated_buffer_fails_with_marker():
    raw = '{"user_prompt": "cut off mid-str'
    args = parse_tool_call_arguments(raw, "write_csv")
    assert args[UNPARSABLE_KEY] is True
    assert args[RAW_KEY] == raw
    assert args[ERROR_KEY]


def test_non_object_json_fails_with_marker():
    args = parse_tool_call_arguments('["a", "b"]')
    assert args[UNPARSABLE_KEY] is True
    assert "not an object" in args[ERROR_KEY]


def test_salvaged_output_is_round_trippable():
    raw = '{"user_prompt": "דו"ח שנתי", "title": "R"}'
    args = parse_tool_call_arguments(raw)
    assert json.loads(json.dumps(args, ensure_ascii=False)) == args


# --- runner integration ----------------------------------------------------

@pytest.mark.asyncio
async def test_runner_reports_json_error_not_missing_field():
    from app.ai.runner.tool_runner import ToolRunner
    from pydantic import BaseModel, Field
    from typing import Type

    class _In(BaseModel):
        user_prompt: str = Field(...)

    class _Tool:
        name = "write_csv"
        spec = None
        metadata = None
        output_model = None

        @property
        def input_model(self) -> Type[BaseModel]:
            return _In

    async def _emit(evt):
        return None

    runner = ToolRunner()
    marker = parse_tool_call_arguments('{"user_prompt": "totally broken')
    result = await runner.run(_Tool(), marker, {"mode": "chat"}, _emit)

    observation = result["observation"]
    assert observation["success"] is False
    assert observation["error"]["type"] == "malformed_tool_arguments"
    # The feedback must describe broken JSON, not a missing field.
    assert "Field required" not in observation["error"]["message"]
    assert "JSON" in observation["error"]["message"]
    assert result["output"]["success"] is False
