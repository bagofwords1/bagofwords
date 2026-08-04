"""Standalone vision images must not displace tool_result blocks.

The agent passes tool-produced images (rendered pages, screenshots) to the LLM
call via the separate `images=` argument, at which point the last user message
is the tool_result turn. Anthropic requires tool_result blocks to be the first
content of that message — anything before them fails validation with
"tool_use ids were found without tool_result blocks immediately after".
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.llm.clients.anthropic_client import Anthropic  # noqa: E402
from app.ai.llm.types import Message  # noqa: E402

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}


def test_images_attach_after_tool_results():
    msgs = Anthropic._translate_messages([
        Message(role="user", content="read page 1"),
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "call_0", "name": "read_file", "input": {"pages": "1"}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_0", "content": "rendered 1 page"},
        ]),
    ])
    Anthropic._attach_images(msgs, [_IMG])

    last = msgs[-1]["content"]
    assert last[0]["type"] == "tool_result", "tool_result must stay the first block"
    assert any(b["type"] == "image" for b in last[1:]), "image must still be attached"


def test_images_precede_text_when_no_tool_results():
    msgs = Anthropic._translate_messages([Message(role="user", content="what does this show?")])
    Anthropic._attach_images(msgs, [_IMG])

    last = msgs[-1]["content"]
    assert last[0]["type"] == "image"
    assert last[-1]["type"] == "text"


def test_images_open_new_user_turn_after_assistant():
    msgs = Anthropic._translate_messages([
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ])
    Anthropic._attach_images(msgs, [_IMG])

    assert msgs[-1] == {"role": "user", "content": [_IMG]}
