"""Unit tests for image generation (gpt-image-1) support.

Covers the client generate_image method (both OpenAI clients), the LLM facade
capability gate, the catalog entry, tool registration, the create_artifact
file-embedding schema/prompt wiring, and the <BowFile> sandbox contract doc.
Deterministic — no network, no DB.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.types import ImageOutput
from app.models.llm_model import LLM_MODEL_DETAILS


def _fake_image_response():
    item = SimpleNamespace(b64_json="QUJDRA==", revised_prompt="a cat, revised")
    usage = SimpleNamespace(input_tokens=11, output_tokens=222)
    return SimpleNamespace(data=[item], usage=usage)


@pytest.mark.parametrize("cls", [OpenAi, OpenAIResponsesClient])
def test_client_generate_image(cls):
    client = cls(api_key="x")
    client.client.images.generate = MagicMock(return_value=_fake_image_response())
    out = asyncio.run(client.generate_image("gpt-image-1", "a cat", size="1024x1024", quality="high"))
    assert isinstance(out, ImageOutput)
    assert out.data == "QUJDRA==" and out.media_type == "image/png"
    assert out.revised_prompt == "a cat, revised"
    assert out.usage.prompt_tokens == 11 and out.usage.completion_tokens == 222
    # size + quality forwarded
    _, kwargs = client.client.images.generate.call_args
    assert kwargs["size"] == "1024x1024" and kwargs["quality"] == "high"


def test_client_generate_image_no_payload_raises():
    client = OpenAi(api_key="x")
    empty = SimpleNamespace(data=[SimpleNamespace(b64_json=None)], usage=None)
    client.client.images.generate = MagicMock(return_value=empty)
    with pytest.raises(RuntimeError):
        asyncio.run(client.generate_image("gpt-image-1", "x"))


def test_facade_gate_rejects_non_image_model():
    """LLM.generate_image must refuse a model without supports_image_generation."""
    from app.ai.llm.llm import LLM

    llm = LLM.__new__(LLM)  # bypass provider/credential setup
    llm.model = SimpleNamespace(supports_image_generation=False)
    llm.model_id = "gpt-5.6-luna"
    llm.provider = "openai"
    with pytest.raises(ValueError):
        llm._validate_image_generation_support()


def test_catalog_has_image_model():
    imgs = [m for m in LLM_MODEL_DETAILS if m.get("supports_image_generation")]
    assert any(m["model_id"] == "gpt-image-1" for m in imgs)
    for m in imgs:
        assert m["provider_type"] == "openai"
        assert not m.get("is_default") and not m.get("is_small_default")


def test_generate_image_tool_registered():
    from app.ai.registry import ToolRegistry
    reg = ToolRegistry()
    meta = reg.get_metadata("generate_image")
    assert meta is not None and meta.category == "action"


def test_create_artifact_accepts_file_ids():
    from app.ai.tools.schemas.create_artifact import CreateArtifactInput
    # file-only artifact: empty visualization_ids is allowed when file_ids given
    data = CreateArtifactInput(prompt="p", visualization_ids=[], file_ids=["abc-123"])
    assert data.file_ids == ["abc-123"]


def test_page_prompt_mentions_bowfile_for_files():
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool
    tool = CreateArtifactTool()
    prompt = tool._build_page_prompt(
        user_prompt="show the image",
        title="Gallery",
        viz_profiles=[],
        instructions_context="",
        report_title="Gallery",
        allow_llm_see_data=True,
        files=[{"id": "file-xyz", "content_type": "image/png", "filename": "a.png"}],
    )
    assert "BowFile" in prompt
    assert "file-xyz" in prompt


def test_sandbox_contract_documents_bowfile():
    from app.ai.tools.implementations._sandbox_context import SANDBOX_RUNTIME_PROMPT
    assert "BowFile" in SANDBOX_RUNTIME_PROMPT


def test_generate_image_digest_surfaces_file_id():
    """On later turns the planner must see a generated image's file_id inline in
    history, so it can pass it to create_artifact (file_ids) or read it."""
    from app.ai.context.builders.message_context_builder import _digest_generate_image

    class _TE:
        def __init__(self, rj):
            self.tool_name = "generate_image"
            self.result_json = rj

    ok = _digest_generate_image(_TE({
        "success": True, "file_id": "abc-123", "filename": "elephant.png",
        "revised_prompt": "a cheerful cartoon elephant",
    }))
    assert "file_id: abc-123" in ok
    assert "elephant.png" in ok
    assert "create_artifact" in ok  # tells the planner what to do with it

    assert "failed" in _digest_generate_image(_TE({"success": False, "error_message": "no image model"}))
    assert _digest_generate_image(_TE({"success": True})) == ""  # no file_id -> nothing


def test_file_embed_token_roundtrip():
    """A minted token verifies only for its own file id; a tampered/foreign
    token or file id fails."""
    from app.core.file_tokens import mint_file_token, verify_file_token, file_embed_url
    fid = "11111111-1111-1111-1111-111111111111"
    tok = mint_file_token(fid)
    assert verify_file_token(tok, fid) is True
    assert verify_file_token(tok, "22222222-2222-2222-2222-222222222222") is False
    assert verify_file_token("not-a-token", fid) is False
    assert verify_file_token("", fid) is False
    assert file_embed_url(fid, tok) == f"/api/files/{fid}/embed?token={tok}"


def test_file_embed_token_expires():
    from app.core.file_tokens import mint_file_token, verify_file_token
    fid = "33333333-3333-3333-3333-333333333333"
    expired = mint_file_token(fid, ttl_seconds=-10)  # already expired
    assert verify_file_token(expired, fid) is False


def test_model_schema_exposes_image_generation_flag():
    """The chat picker filters on supports_image_generation, so the API must
    expose it."""
    from app.schemas.llm_schema import LLMModelSchema
    assert "supports_image_generation" in LLMModelSchema.model_fields


def test_model_schema_exposes_image_generation_override():
    """The admin toggle persists via supports_image_generation_override, which the
    settings page reads/writes."""
    from app.schemas.llm_schema import LLMModelSchema, LLMModelBase
    assert "supports_image_generation_override" in LLMModelSchema.model_fields
    assert "supports_image_generation_override" in LLMModelBase.model_fields


def test_image_generation_override_resolution():
    """A non-null admin override always wins over the catalog default (reuses the
    same resolver as vision)."""
    from app.services.llm_service import LLMService
    r = LLMService._resolve_supports_vision
    assert r(True, False) is True    # admin marked a non-catalog model as image
    assert r(False, True) is False   # admin un-marked a catalog image model
    assert r(None, True) is True     # no override -> follow catalog
    assert r(None, False) is False


def test_toggle_image_generation_endpoint_registered():
    """The toggle route is wired so the settings UI can mark a model."""
    import app.routes.llm as llm_routes
    paths = {getattr(r, "path", "") for r in llm_routes.router.routes}
    assert any(p.endswith("/toggle_image_generation") for p in paths)


def test_image_model_never_auto_default_in_catalog():
    """Belt-and-suspenders: the catalog image model must not carry default flags
    (the sync also refuses to auto-assign them)."""
    imgs = [m for m in LLM_MODEL_DETAILS if m.get("supports_image_generation")]
    assert imgs
    for m in imgs:
        assert m.get("is_default") is not True
        assert m.get("is_small_default") is not True
