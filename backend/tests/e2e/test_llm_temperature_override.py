"""Feedback loop — manual per-model sampling temperature.

Proves the admin `set_temperature` control:
  * stores an explicit temperature on ``LLMModel.config['temperature']`` that
    the LLM client construction reads (model config wins over the provider's
    additional_config escape hatch; unset means no temperature parameter is
    sent at all),
  * persists across catalog re-sync (GET /llm/models re-syncs preset
    providers from LLM_MODEL_DETAILS; ``config`` is admin-owned and must
    survive),
  * treats 0 as a legitimate explicit value, distinct from unset,
  * clears back to "send nothing" when called without a value,
  * rejects values outside the accepted [0, 2] range without touching the row.

Fully self-contained: providers are seeded directly in the DB (no external
LLM calls) and every assertion goes through the real route -> service -> DB
stack.
"""
import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider


def _run(coro):
    return asyncio.run(coro)


async def _seed_anthropic_preset(org_id):
    """Preset provider + catalog model, so GET /llm/models re-syncs it."""
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
                name="Claude Opus 5",
                model_id="claude-opus-5",
                is_preset=True,
                is_enabled=True,
                is_default=True,
                supports_vision=True,
                context_window_tokens=1_000_000,
            )
        )
        await db.commit()
        return str(provider.id)


def _find(models, model_id):
    return next(m for m in models if m["model_id"] == model_id)


@pytest.mark.e2e
def test_temperature_override_set_persist_clear(
    create_user, login_user, whoami, get_models, test_client
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    _run(_seed_anthropic_preset(org_id))

    # Baseline: no temperature configured.
    model = _find(get_models(token, org_id), "claude-opus-5")
    assert (model.get("config") or {}).get("temperature") is None
    model_id = model["id"]

    # Admin sets an explicit temperature.
    resp = test_client.post(
        f"/api/llm/models/{model_id}/set_temperature",
        params={"temperature": 0.7},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # GET re-syncs preset providers from the catalog; the admin-owned config
    # must survive.
    model = _find(get_models(token, org_id), "claude-opus-5")
    assert (model.get("config") or {}).get("temperature") == 0.7, (
        "temperature was clobbered by catalog re-sync"
    )

    # 0 is a deliberate explicit value — must be stored, not treated as unset.
    resp = test_client.post(
        f"/api/llm/models/{model_id}/set_temperature",
        params={"temperature": 0},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    model = _find(get_models(token, org_id), "claude-opus-5")
    assert (model.get("config") or {}).get("temperature") == 0

    # Clearing (no param) removes the key entirely — back to "send nothing".
    resp = test_client.post(
        f"/api/llm/models/{model_id}/set_temperature", headers=headers
    )
    assert resp.status_code == 200, resp.text
    model = _find(get_models(token, org_id), "claude-opus-5")
    assert "temperature" not in (model.get("config") or {})


@pytest.mark.e2e
def test_temperature_out_of_range_rejected(
    create_user, login_user, whoami, get_models, test_client
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    _run(_seed_anthropic_preset(org_id))
    model = _find(get_models(token, org_id), "claude-opus-5")
    model_id = model["id"]

    resp = test_client.post(
        f"/api/llm/models/{model_id}/set_temperature",
        params={"temperature": 0.5},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    for bad in (-0.1, 2.5):
        resp = test_client.post(
            f"/api/llm/models/{model_id}/set_temperature",
            params={"temperature": bad},
            headers=headers,
        )
        assert resp.status_code in (400, 422), resp.text

    # The stored value is untouched by the rejected writes.
    model = _find(get_models(token, org_id), "claude-opus-5")
    assert (model.get("config") or {}).get("temperature") == 0.5


@pytest.mark.e2e
def test_set_temperature_route_is_permission_gated():
    """Same manage_llm gate as every other LLM admin route."""
    from app.routes import llm as llm_routes

    route = next(
        r for r in llm_routes.router.routes
        if getattr(r, "path", "").endswith("/llm/models/{model_id}/set_temperature")
    )
    perm = getattr(route.endpoint, "_required_permission", None) or getattr(
        route.endpoint, "required_permission", None
    )
    assert perm == "manage_llm", f"expected manage_llm gate, got {perm!r}"
