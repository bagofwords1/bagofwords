"""Standalone vision images must survive the OpenAI-family v2 paths.

The agent passes tool-produced and user-uploaded images to the LLM call via
the separate `images=` argument, at which point the last message is usually a
tool_result turn. The Chat Completions clients (OpenAI, Azure, and any
OpenAI-compatible endpoint such as a LiteLLM proxy) used to drop that argument
entirely in inference_stream_v2, and the Responses client attached it only
when the last message was a plain-string user message — so mid tool loop a
vision model silently never saw the image.
"""
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.llm.clients.azure_client import AzureClient  # noqa: E402
from app.ai.llm.clients.openai_client import OpenAi  # noqa: E402
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient  # noqa: E402
from app.ai.llm.types import ImageInput, Message  # noqa: E402

_IMAGES = [ImageInput(data="IMGDATA", media_type="image/png", source_type="base64")]

_TOOL_LOOP_MESSAGES = [
    Message(role="user", content="read page 1"),
    Message(role="assistant", content=[
        {"type": "tool_use", "id": "call_0", "name": "read_page", "input": {"page": 1}},
    ]),
    Message(role="user", content=[
        {"type": "tool_result", "tool_use_id": "call_0", "content": "rendered 1 page"},
    ]),
]

# (label, translate, attach, image part type, tool item predicate)
CHAT_CLIENTS = [
    ("openai", OpenAi, "image_url", lambda m: m.get("role") == "tool"),
    ("azure", AzureClient, "image_url", lambda m: m.get("role") == "tool"),
    ("responses", OpenAIResponsesClient, "input_image",
     lambda m: m.get("type") == "function_call_output"),
]


def _image_parts(message, part_type):
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [p for p in content if p.get("type") == part_type]


@pytest.mark.parametrize("label,client,part_type,is_tool_item", CHAT_CLIENTS)
def test_images_survive_a_tool_result_tail(label, client, part_type, is_tool_item):
    """Mid tool loop the translated tail is tool results — the images must
    still reach the model, on a user message appended after them."""
    out = client._translate_messages(_TOOL_LOOP_MESSAGES)
    client._attach_images(out, _IMAGES)

    assert is_tool_item(out[-2]), f"{label}: tool result no longer precedes the image turn"
    last = out[-1]
    assert last["role"] == "user", f"{label}: images must ride on a user message"
    parts = _image_parts(last, part_type)
    assert parts, f"{label}: images were dropped"
    assert "IMGDATA" in str(parts), f"{label}: image payload missing"


@pytest.mark.parametrize("label,client,part_type,is_tool_item", CHAT_CLIENTS)
def test_images_merge_into_a_plain_user_message(label, client, part_type, is_tool_item):
    out = client._translate_messages([Message(role="user", content="what does this show?")])
    client._attach_images(out, _IMAGES)

    assert len(out) == 1, f"{label}: a trailing user message must be merged into, not duplicated"
    last = out[-1]
    content = last["content"]
    assert isinstance(content, list), f"{label}: content should be multipart"
    assert any(p.get("type") == part_type for p in content), f"{label}: image part missing"
    assert any("what does this show?" in str(p) for p in content), f"{label}: text lost"


@pytest.mark.parametrize("label,client,part_type,is_tool_item", CHAT_CLIENTS)
def test_url_images_pass_through(label, client, part_type, is_tool_item):
    out = client._translate_messages([Message(role="user", content="look")])
    client._attach_images(out, [ImageInput(data="https://example.com/x.png", source_type="url")])

    parts = _image_parts(out[-1], part_type)
    assert parts and "https://example.com/x.png" in str(parts), f"{label}: url image lost"
