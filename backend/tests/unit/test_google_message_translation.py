"""Unit tests for the Google client's Message → Gemini Content translation.

Regression guard: Gemini gives function calls no ids of its own, so the client
mints them — and the counter restarts at 0 on every request. Resolving
tool_use_id → tool name from a map built over the *whole* history therefore let
a later turn's "call_0" overwrite an earlier one, and every earlier
function_response went to Gemini labelled with the wrong tool's name.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.llm.clients.google_client import Google  # noqa: E402
from app.ai.llm.types import Message  # noqa: E402


def _flatten(contents):
    out = []
    for c in contents:
        for p in c.parts:
            if p.function_call:
                out.append((c.role, "call", p.function_call.name))
            elif p.function_response:
                out.append((c.role, "response", p.function_response.name))
            else:
                out.append((c.role, "text", p.text))
    return out


def test_recycled_call_ids_resolve_to_the_right_tool():
    messages = [
        Message(role="user", content="how many invoices?"),
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "call_0", "name": "describe_tables", "input": {"query": "invoice"}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_0", "content": "Invoice table found"},
        ]),
        # Second turn: the counter restarted, so this call reuses "call_0".
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "call_0", "name": "create_data", "input": {"title": "Invoices"}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_0", "content": "412 rows"},
        ]),
    ]

    assert _flatten(Google._translate_messages(messages)) == [
        ("user", "text", "how many invoices?"),
        ("model", "call", "describe_tables"),
        ("user", "response", "describe_tables"),
        ("model", "call", "create_data"),
        ("user", "response", "create_data"),
    ]


def test_parallel_tool_calls_in_one_turn():
    messages = [
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "call_0", "name": "describe_tables", "input": {}},
            {"type": "tool_use", "id": "call_1", "name": "inspect_data", "input": {}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_1", "content": "b"},
            {"type": "tool_result", "tool_use_id": "call_0", "content": "a"},
        ]),
    ]

    assert _flatten(Google._translate_messages(messages)) == [
        ("model", "call", "describe_tables"),
        ("model", "call", "inspect_data"),
        # Results keep their own order but each resolves to its own tool.
        ("user", "response", "inspect_data"),
        ("user", "response", "describe_tables"),
    ]


def test_non_string_tool_result_is_json_encoded():
    messages = [
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "call_0", "name": "create_data", "input": {}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_0", "content": {"rows": 412}},
        ]),
    ]
    contents = Google._translate_messages(messages)
    assert contents[-1].parts[0].function_response.response == {"output": '{"rows": 412}'}


def test_thought_signature_round_trips_onto_the_replayed_call():
    """Gemini 3 rejects a replayed function call with no thought_signature
    (400 "Function call is missing a thought_signature in functionCall parts"),
    so the opaque bytes must survive the trip through the str-typed event."""
    raw = b"\x00\x8a\xff signature bytes"
    encoded = Google._encode_signature(raw)
    assert isinstance(encoded, str)
    assert Google._decode_signature(encoded) == raw

    messages = [
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "call_0", "name": "create_data",
             "input": {"title": "t"}, "signature": encoded},
        ]),
    ]
    part = Google._translate_messages(messages)[0].parts[0]
    assert part.function_call.name == "create_data"
    assert part.thought_signature == raw


def test_missing_signature_is_left_off():
    messages = [
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "call_0", "name": "create_data", "input": {}},
        ]),
    ]
    part = Google._translate_messages(messages)[0].parts[0]
    assert part.thought_signature is None
    assert Google._encode_signature(None) is None
    assert Google._decode_signature(None) is None


def test_unknown_tool_use_id_falls_back_to_the_id():
    messages = [
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_9", "content": "orphan"},
        ]),
    ]
    contents = Google._translate_messages(messages)
    assert contents[0].parts[0].function_response.name == "call_9"
