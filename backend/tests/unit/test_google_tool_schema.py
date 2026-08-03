"""Unit tests for the Google client's JSON Schema → Gemini FunctionDeclaration
translation.

Regression guard: Gemini's schema validator rejects a handful of JSON Schema
keywords ("title", "default", "examples", ...), so we strip them — but the keys
inside a "properties" map are *parameter names*, not keywords. Stripping there
silently deleted required parameters such as create_data's `title`, so Gemini
never emitted them and every call failed downstream with
"title: Field required".
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.llm.clients.google_client import Google  # noqa: E402


def test_property_named_title_survives():
    schema = {
        "type": "object",
        "title": "CreateDataInput",
        "properties": {
            "title": {"type": "string", "title": "Title", "description": "Artifact title"},
            "user_prompt": {"type": "string", "title": "User Prompt"},
        },
        "required": ["title", "user_prompt"],
    }
    out = Google._resolve_schema_refs(schema)

    assert set(out["properties"]) == {"title", "user_prompt"}
    assert out["required"] == ["title", "user_prompt"]
    # The JSON Schema "title" keyword is still stripped, at the root and inside
    # the property schema itself.
    assert "title" not in out
    assert "title" not in out["properties"]["title"]
    assert out["properties"]["title"]["description"] == "Artifact title"


def test_other_keyword_named_properties_survive():
    schema = {
        "type": "object",
        "properties": {
            "default": {"type": "string"},
            "examples": {"type": "array", "items": {"type": "string"}},
            "additionalProperties": {"type": "boolean"},
        },
        "required": ["default", "examples"],
    }
    out = Google._resolve_schema_refs(schema)

    assert set(out["properties"]) == {"default", "examples", "additionalProperties"}
    assert out["required"] == ["default", "examples"]


def test_refs_inlined_and_nested_properties_preserved():
    schema = {
        "type": "object",
        "properties": {
            "rows": {"type": "array", "items": {"$ref": "#/$defs/Row"}},
            "kind": {"$ref": "#/$defs/Kind"},
        },
        "required": ["rows"],
        "$defs": {
            "Row": {
                "type": "object",
                "title": "Row",
                "properties": {"title": {"type": "string", "title": "Title"}},
                "required": ["title"],
            },
            "Kind": {"const": "table", "title": "Kind"},
        },
    }
    out = Google._resolve_schema_refs(schema)

    assert "$defs" not in out
    row = out["properties"]["rows"]["items"]
    assert set(row["properties"]) == {"title"}
    assert row["required"] == ["title"]
    # const → enum, keyword "title" stripped from the def itself
    assert out["properties"]["kind"] == {"enum": ["table"]}


def test_required_entries_without_properties_are_dropped():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a", "ghost"],
    }
    out = Google._resolve_schema_refs(schema)
    assert out["required"] == ["a"]


def test_create_data_schema_keeps_required_title():
    from app.ai.tools.schemas.create_data import CreateDataInput

    out = Google._resolve_schema_refs(CreateDataInput.model_json_schema())
    assert "title" in out["properties"]
    assert "title" in out["required"]


def test_thinking_budget_never_zero():
    """gemini-flash-latest / gemini-flash-lite-latest reject thinking_budget=0
    with a hard 400 INVALID_ARGUMENT, so the floor must be the minimum Gemini
    accepts — not 0, and not model-name guesswork."""
    assert Google._thinking_budget() == Google.MIN_THINKING_BUDGET
    assert Google._thinking_budget(0) == Google.MIN_THINKING_BUDGET
    assert Google.MIN_THINKING_BUDGET > 0
    # An explicit, larger budget is passed through untouched.
    assert Google._thinking_budget(4096) == 4096
