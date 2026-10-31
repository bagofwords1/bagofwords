"""Uploaded references stay on the ask; later captures stay on their results.

Exercise public prompt construction and provider streaming APIs, replacing only
the external SDK calls. No model credentials or network are needed.
"""
import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.ai.context.parts import ToolCallPart, ToolResultPart
from app.ai.context.transcript import Transcript
from app.ai.llm.clients.anthropic_client import Anthropic
from app.ai.llm.clients.azure_client import AzureClient
from app.ai.llm.clients.bedrock_client import BedrockClient
from app.ai.llm.clients.google_client import Google
from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.llm import LLM
from app.ai.llm.types import ImageInput, Message
from app.schemas.ai.planner import PlannerInput

PROVIDERS = [Anthropic, OpenAi, AzureClient, OpenAIResponsesClient, BedrockClient, Google]
ASK = "Please update the layout shown in my reference."


def _image(color):
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, format="PNG")
    return ImageInput(data=base64.b64encode(buf.getvalue()).decode())


def _plain(value):
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _blob(value):
    return json.dumps(_plain(value))


class _EmptyStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def aclose(self):
        pass


async def _request(provider, built):
    """Capture the final SDK payload after the real provider adapter runs."""
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return _EmptyStream()

    def converse_stream(**kwargs):
        captured.update(kwargs)
        return {"stream": []}

    def generate_content_stream(**kwargs):
        captured.update(kwargs)
        return []

    client = provider.__new__(provider)
    client.temperature = 0.3
    client.max_tokens = 8192
    client.enable_web_search = False
    client.prompt_cache_markers = False
    client.async_client = SimpleNamespace(
        messages=SimpleNamespace(create=create),
        responses=SimpleNamespace(create=create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    client.client = SimpleNamespace(
        converse_stream=converse_stream,
        models=SimpleNamespace(generate_content_stream=generate_content_stream),
    )
    async for _ in client.inference_stream_v2(
        model_id="test-vision-model",
        messages=[Message(**m) for m in built.messages],
        images=built.images,
    ):
        pass
    return _plain(captured.get("messages", captured.get("input", captured.get("contents"))))


def _input(steps, uploads, live):
    observations = []
    transcript = Transcript()
    # Prior history makes sure uploads are not simply moved to the first user
    # message, which could belong to a different request altogether.
    transcript.add_user_text("Earlier request without a picture")
    transcript.add_assistant_step(text="Earlier reply")
    transcript.history_turn_count = len(transcript.turns)
    for i, screenshot in enumerate(steps):
        result = {"artifact_id": "example", "version": i + 2, "code_hash": f"revision-{i}"}
        result["images"] = [vars(screenshot)]
        observations.append({
            "loop_index": i, "tool_name": "browser_navigate", "tool_input": {},
            "observation": result,
        })
        transcript.add_assistant_step(calls=[ToolCallPart(id=f"call-{i}", tool_name="browser_navigate")])
        transcript.add_tool_results([ToolResultPart(
            call_id=f"call-{i}", tool_name="browser_navigate",
            content=json.dumps({k: v for k, v in result.items() if k != "images"}),
            images=[vars(screenshot)],
        )])
    return PlannerInput(
        user_message=ASK, images=uploads, timezone="UTC", tool_catalog=[],
        transcript=transcript if live else None, past_observations=observations,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.__name__)
@pytest.mark.parametrize("live", [False, True], ids=["reconstructed", "live-history"])
async def test_uploads_remain_with_the_original_request_across_iterations(provider, live, monkeypatch):
    monkeypatch.delenv("BOW_PLANNER_TRANSCRIPT", raising=False)
    uploads = [_image("red"), _image("blue")]
    screenshots = [_image("green"), _image("yellow"), _image("purple")]
    for count in (0, 1, 3):
        built = PromptBuilderV3.build(_input(screenshots[:count], uploads, live))
        messages = await _request(provider, built)
        ask_indices = [i for i, m in enumerate(messages) if ASK in _blob(m)]
        assert len(ask_indices) == 1
        ask_index = ask_indices[0]
        for upload in uploads:
            image_indices = [i for i, m in enumerate(messages) if upload.data in _blob(m)]
            assert image_indices == [ask_index], "An old reference moved onto a later tool-result message"
            assert _blob(messages).count(upload.data) == 1, "Upload was duplicated within the request"
        for screenshot in screenshots[:count]:
            image_indices = [i for i, m in enumerate(messages) if screenshot.data in _blob(m)]
            assert len(image_indices) == 1 and image_indices[0] > ask_index
    assert not built.images, "Anchored images must not also use the provider's latest-message channel"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.__name__)
async def test_legacy_prompt_keeps_upload_and_tool_images_available(provider, monkeypatch):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "0")
    upload, capture = _image("red"), _image("green")
    planner_input = _input([capture], [upload, capture], live=False)
    for entry in planner_input.past_observations:
        entry["observation"].pop("images")
        entry["observation"]["images_provided_as_vision"] = True
    built = PromptBuilderV3.build(planner_input)
    messages = await _request(provider, built)
    for image in (upload, capture):
        assert _blob(messages).count(image.data) == 1


def test_uploads_are_budgeted_and_survive_tool_image_decay():
    upload = _image("red")
    transcript = Transcript()
    transcript.add_user_text(ASK)
    text_cost = transcript.tokens()
    with_upload = Transcript()
    with_upload.add_user_text(ASK, images=[vars(upload)])
    assert with_upload.tokens() > text_cost
    assert with_upload.image_count() == 1
    floor = with_upload.floor_tokens()
    # The original reference remains available even under budget pressure,
    # and its cost cannot disappear from the non-decayable floor.
    stats = with_upload.fit_to_budget(1)
    assert not stats["reached"] and stats["after"] == floor
    assert upload.data in _blob([m.content for m in with_upload.to_model_messages()])
    assert upload.data not in _blob([
        m.content for m in with_upload.to_model_messages(supports_images=False)
    ])


def test_render_does_not_leave_uploads_on_the_live_transcript(monkeypatch):
    monkeypatch.delenv("BOW_PLANNER_TRANSCRIPT", raising=False)
    upload = _image("red")
    planner_input = _input([_image("green")], [upload], live=True)
    first = PromptBuilderV3.build(planner_input)
    assert upload.data in _blob(first.messages)
    planner_input.images = None
    following = PromptBuilderV3.build(planner_input)
    assert upload.data not in _blob(following.messages)
    assert not following.images


def test_text_prompt_preview_keeps_image_bytes_out_of_prompt_text(monkeypatch):
    monkeypatch.delenv("BOW_PLANNER_TRANSCRIPT", raising=False)
    upload = _image("red")
    text = PromptBuilderV3.build_prompt(_input([], [upload], live=False))
    assert ASK in text
    assert upload.data not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["side-channel", "native-message"])
async def test_text_only_models_refuse_images_before_calling_a_provider(native):
    # Validation must happen before consulting any external provider. Construct
    # the facade without SDK credentials; nothing beyond its model is needed.
    llm = LLM.__new__(LLM)
    llm.model = SimpleNamespace(supports_vision=False, is_small_default=True)
    llm.model_id = "text-only-test-model"
    llm.provider = "anthropic"
    upload = _image("red")
    transcript = Transcript()
    transcript.add_user_text(ASK, images=[vars(upload)] if native else [])
    with pytest.raises(ValueError, match="does not support images"):
        async for _ in llm.inference_stream_v2(
            messages=transcript.to_model_messages(), images=None if native else [upload],
            should_record=False,
        ):
            pass
