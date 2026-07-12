"""Feedback loop — manual per-model vision toggle.

Proves the admin `toggle_vision` control:
  * flips the effective `supports_vision` flag read at inference time,
  * persists as an explicit override that SURVIVES catalog re-sync
    (the bug this feature closes: without the override, GET /llm/models
    re-syncs preset models from LLM_MODEL_DETAILS and reverts the choice),
  * gates image inputs in the LLM abstraction accordingly.

Loop A is fully self-contained: the provider is seeded directly in the DB
(no external LLM calls) and every assertion goes through the real
route -> service -> DB stack. The live-credentials leg lives in the
feedback-loop doc (Loop B).
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.dependencies import async_session_maker
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider
from app.ai.llm.llm import LLM
from app.ai.llm.types import ImageInput


def _run(coro):
    return asyncio.run(coro)


async def _seed_anthropic_preset(org_id):
    """Seed a preset Anthropic provider with a vision-capable catalog model.

    claude-sonnet-4-6 is `supports_vision=True` in LLM_MODEL_DETAILS, so this is
    exactly the case that regressed before the override existed.
    """
    async with async_session_maker() as db:
        provider = LLMProvider(
            organization_id=org_id,
            name="Anthropic",
            provider_type="anthropic",
            is_preset=True,
            is_enabled=True,
            use_preset_credentials=True,
        )
        db.add(provider)
        await db.flush()

        db.add(
            LLMModel(
                organization_id=org_id,
                provider_id=provider.id,
                name="Claude 4.6 Sonnet",
                model_id="claude-sonnet-4-6",
                is_preset=True,
                is_enabled=True,
                is_default=True,
                supports_vision=True,
            )
        )
        await db.commit()
        return str(provider.id)


def _find(models, model_id):
    return next(m for m in models if m["model_id"] == model_id)


@pytest.mark.e2e
def test_vision_toggle_persists_across_catalog_resync(
    create_user, login_user, whoami, get_models, test_client
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    _run(_seed_anthropic_preset(org_id))

    # Baseline: catalog says this model is vision-capable.
    model = _find(get_models(token, org_id), "claude-sonnet-4-6")
    assert model["supports_vision"] is True
    assert model["supports_vision_override"] in (None, False, True)
    model_id = model["id"]

    # Admin turns vision OFF.
    resp = test_client.post(
        f"/api/llm/models/{model_id}/toggle_vision", params={"enabled": False}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    # GET re-syncs preset providers from the catalog. The override must win:
    # effective flag stays OFF even though the catalog says True.
    model = _find(get_models(token, org_id), "claude-sonnet-4-6")
    assert model["supports_vision"] is False, "override was clobbered by catalog re-sync"
    assert model["supports_vision_override"] is False

    # Admin turns vision back ON; still survives another re-sync.
    resp = test_client.post(
        f"/api/llm/models/{model_id}/toggle_vision", params={"enabled": True}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    model = _find(get_models(token, org_id), "claude-sonnet-4-6")
    assert model["supports_vision"] is True
    assert model["supports_vision_override"] is True


@pytest.mark.e2e
def test_toggle_vision_route_is_permission_gated():
    """The endpoint carries the same manage_llm gate as every other LLM route."""
    from app.routes import llm as llm_routes

    route = next(
        r for r in llm_routes.router.routes
        if getattr(r, "path", "").endswith("/llm/models/{model_id}/toggle_vision")
    )
    # @requires_permission('manage_llm') wraps the handler; the permission name
    # is recorded on the endpoint by the decorator.
    perm = getattr(route.endpoint, "_required_permission", None) or getattr(
        route.endpoint, "required_permission", None
    )
    assert perm == "manage_llm", f"expected manage_llm gate, got {perm!r}"


@pytest.mark.e2e
def test_inference_gate_reads_supports_vision():
    """The image gate in the LLM abstraction keys off model.supports_vision."""
    image = ImageInput(data="Zm9v", media_type="image/png", source_type="base64")

    off = SimpleNamespace(model=SimpleNamespace(supports_vision=False), model_id="m")
    with pytest.raises(ValueError, match="does not support images"):
        LLM._validate_vision_support(off, [image])

    on = SimpleNamespace(model=SimpleNamespace(supports_vision=True), model_id="m")
    # Should not raise.
    LLM._validate_vision_support(on, [image])

    # No images -> never gated, regardless of flag.
    LLM._validate_vision_support(off, None)
