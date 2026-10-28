"""Tool images ride the transcript — every provider must deliver them.

On the native-transcript path (the default) a tool's images (read_file
as_images pages, screenshots, rendered charts) are NOT sent on the side
channel: ``AgentV2._planner_images`` leaves them to the transcript, which
hoists them as ``{"type": "image"}`` blocks into the user turn that carries the
tool results. A provider translator that only understands text / tool_use /
tool_result blocks therefore drops the picture silently — the model reads
"8 pages returned as images", sees none, and answers "I have no OCR tool".

Contract under test: for every provider client, an image a tool returned —
and the text riding in the same turn (clock, steering, nudges) — reaches the
provider request, and the tool results still lead their turn (Anthropic-family
models on Bedrock reject a turn whose tool results are not first).
"""
import base64
import io
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from app.ai.context.parts import ToolCallPart, ToolResultPart  # noqa: E402
from app.ai.context.transcript import Transcript  # noqa: E402
from app.ai.llm.clients.anthropic_client import Anthropic  # noqa: E402
from app.ai.llm.clients.azure_client import AzureClient  # noqa: E402
from app.ai.llm.clients.bedrock_client import BedrockClient  # noqa: E402
from app.ai.llm.clients.google_client import Google  # noqa: E402
from app.ai.llm.clients.openai_client import OpenAi  # noqa: E402
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient  # noqa: E402

TRANSLATORS = [
    ("anthropic", Anthropic._translate_messages),
    ("openai", OpenAi._translate_messages),
    ("azure", AzureClient._translate_messages),
    ("responses", OpenAIResponsesClient._translate_messages),
    ("bedrock", BedrockClient._translate_messages),
    ("google", Google._translate_messages),
]


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def _image(raw: bytes) -> dict:
    # The shape read_file / screenshots put on an observation (ImageInput).
    return {"data": base64.b64encode(raw).decode(), "media_type": "image/png", "source_type": "base64"}


def _transcript(results) -> list:
    """A rendered transcript: one step of calls answered by ``results``
    ([(tool_name, text, [raw_png, ...]), ...]) plus the per-turn head text."""
    t = Transcript()
    t.add_user_text("read the KB article")
    calls, parts = [], []
    for i, (name, text, pngs) in enumerate(results):
        calls.append(ToolCallPart(id=f"call_{i}", tool_name=name, args={"file_id": f"f{i}"}))
        parts.append(ToolResultPart(
            call_id=f"call_{i}", tool_name=name, content=text,
            images=[_image(p) for p in pngs],
        ))
    t.add_assistant_step(calls=calls)
    t.add_tool_results(parts)
    t.turns[-1].parts.append(type(t.turns[0].parts[0])(text="HEAD_MARKER: now 10:00"))
    return t.to_model_messages()


def _payloads(translated):
    """Every bytes payload and every string in the provider request — the
    image may travel as raw bytes (Bedrock, Gemini) or base64 (the rest)."""
    found_bytes, found_text = [], []

    def walk(o):
        if isinstance(o, (bytes, bytearray)):
            found_bytes.append(bytes(o))
        elif isinstance(o, str):
            found_text.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
        elif hasattr(o, "model_dump"):
            walk(o.model_dump())

    walk(translated)
    return found_bytes, found_text


def _carries_image(translated, raw: bytes) -> bool:
    blobs, texts = _payloads(translated)
    b64 = base64.b64encode(raw).decode()
    return raw in blobs or any(b64 in t for t in texts)


def _carries_text(translated, needle: str) -> bool:
    return any(needle in t for t in _payloads(translated)[1])


@pytest.mark.parametrize("label,translate", TRANSLATORS)
@pytest.mark.parametrize("pages", [1, 3])
def test_tool_result_images_reach_the_provider(label, translate, pages):
    pngs = [_png((40 * i, 90, 200 - 30 * i)) for i in range(pages)]
    out = translate(_transcript([("read_file", "8 pages rendered", pngs)]))
    for i, raw in enumerate(pngs):
        assert _carries_image(out, raw), f"{label}: page {i + 1} of {pages} never reached the request"
    assert _carries_text(out, "8 pages rendered"), f"{label}: tool result text lost"


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_every_image_of_a_parallel_batch_reaches_the_provider(label, translate):
    a, b = _png((255, 0, 0)), _png((0, 0, 255))
    out = translate(_transcript([
        ("read_file", "RESULT_A", [a]),
        ("describe_tables", "RESULT_NO_IMAGE", []),
        ("browser_vision", "RESULT_B", [b]),
    ]))
    assert _carries_image(out, a) and _carries_image(out, b), f"{label}: a batch image was dropped"
    for marker in ("RESULT_A", "RESULT_NO_IMAGE", "RESULT_B"):
        assert _carries_text(out, marker), f"{label}: {marker} lost"


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_turn_text_alongside_tool_results_reaches_the_provider(label, translate):
    out = translate(_transcript([("read_file", "8 pages rendered", [_png((1, 2, 3))])]))
    assert _carries_text(out, "HEAD_MARKER"), f"{label}: text riding with the tool results was dropped"


def test_bedrock_tool_results_lead_their_turn():
    """Anthropic models on Bedrock 400 when anything precedes the toolResult
    blocks of the turn answering a toolUse."""
    out = BedrockClient._translate_messages(
        _transcript([("read_file", "x", [_png((9, 9, 9))]), ("read_file", "y", [])])
    )
    turn = next(m for m in out if any("toolResult" in b for b in m["content"]))
    kinds = ["toolResult" if "toolResult" in b else "other" for b in turn["content"]]
    assert kinds[:2] == ["toolResult", "toolResult"], kinds
    assert "other" in kinds, "image / text never joined the tool-result turn"


def test_gemini_images_follow_the_function_response_turn():
    """A Gemini function-response turn carries function responses only; the
    images and turn text arrive in the user turn right after it, before the
    model speaks again."""
    out = Google._translate_messages(_transcript([("read_file", "x", [_png((9, 9, 9))])]))
    i = next(n for n, c in enumerate(out) if any(p.function_response for p in c.parts))
    assert all(p.function_response for p in out[i].parts), "function-response turn mixed with other parts"
    follow = out[i + 1]
    assert follow.role == "user" and any(p.inline_data for p in follow.parts), "image never followed the responses"


# ── side channel: images handed to inference_stream_v2 directly ──────────────
# User uploads always ride here (and tool images on the legacy planner path).
# The provider SDK call is the boundary: it is replaced by a recorder.

def _upload(raw: bytes):
    from app.ai.llm.types import ImageInput
    return ImageInput(data=base64.b64encode(raw).decode(), media_type="image/png", source_type="base64")


async def _drain(gen):
    async for _ in gen:
        pass


@pytest.mark.parametrize("history", ["plain", "after_tool_results"])
def test_gemini_side_channel_images_reach_the_request(history):
    import asyncio

    from app.ai.llm.types import Message

    raw = _png((200, 10, 10))
    messages = ([Message(role="user", content="what is in the upload?")] if history == "plain"
                else _transcript([("read_file", "1 page", [])]))
    sent = {}
    client = Google(api_key="test")

    def _record(*, model, contents, config):
        sent["contents"] = contents
        return iter(())

    client.client.models.generate_content_stream = _record
    asyncio.run(_drain(client.inference_stream_v2(model_id="gemini-3.6-flash", messages=messages,
                                                  images=[_upload(raw)])))
    contents = sent["contents"]
    assert _carries_image(contents, raw), "upload never reached Gemini"
    assert contents[-1].role == "user"
    for c in contents:  # never mixed into a function-response turn
        if any(p.function_response for p in c.parts):
            assert all(p.function_response for p in c.parts)


@pytest.mark.parametrize("history", ["plain", "after_tool_results"])
def test_bedrock_side_channel_images_reach_the_request(history):
    import asyncio

    from app.ai.llm.types import Message

    raw = _png((10, 200, 10))
    messages = ([Message(role="user", content="what is in the upload?")] if history == "plain"
                else _transcript([("read_file", "1 page", [])]))
    sent = {}
    client = BedrockClient(region="us-east-1", auth_mode="access_keys",
                           aws_access_key_id="AKIATEST", aws_secret_access_key="test")

    def _record(**kwargs):
        sent.update(kwargs)
        return {"stream": []}

    client.client.converse_stream = _record
    asyncio.run(_drain(client.inference_stream_v2(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                                                  messages=messages, images=[_upload(raw)])))
    assert _carries_image(sent["messages"], raw), "upload never reached Bedrock"
    for m in sent["messages"]:  # toolResult blocks still lead their turn
        kinds = ["toolResult" in b for b in m["content"]]
        if any(kinds):
            assert kinds[: kinds.count(True)] == [True] * kinds.count(True), kinds
