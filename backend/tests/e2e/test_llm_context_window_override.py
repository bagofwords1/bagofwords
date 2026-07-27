"""Feedback loop — manual per-model context-window size.

Proves the admin `set_context_window` control:
  * sets the effective `context_window_tokens` the agent's token budget and
    prompt-size estimates read,
  * persists as an explicit override that SURVIVES catalog re-sync
    (the bug this feature closes: without the override, GET /llm/models
    re-syncs preset models from LLM_MODEL_DETAILS and reverts the value),
  * lets admins size custom models (e.g. Bedrock deployments capped at 100k)
    that have no catalog entry at all,
  * clears back to the catalog default when called without a value.

Loop A is fully self-contained: providers are seeded directly in the DB
(no external LLM calls) and every assertion goes through the real
route -> service -> DB stack.
"""
import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider

# Catalog value for claude-sonnet-4-6 in LLM_MODEL_DETAILS.
CATALOG_CONTEXT_WINDOW = 1_000_000


def _run(coro):
    return asyncio.run(coro)


async def _seed_anthropic_preset(org_id):
    """Seed a preset Anthropic provider with a catalog model.

    claude-sonnet-4-6 has context_window_tokens=1_000_000 in LLM_MODEL_DETAILS,
    so any direct edit to the row is clobbered on the next catalog re-sync —
    exactly the case the override closes.
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
                context_window_tokens=CATALOG_CONTEXT_WINDOW,
            )
        )
        await db.commit()
        return str(provider.id)


async def _seed_bedrock_custom(org_id):
    """Seed a custom (non-catalog) provider+model, the Bedrock-style case."""
    async with async_session_maker() as db:
        provider = LLMProvider(
            organization_id=org_id,
            name="Bedrock",
            provider_type="bedrock",
            is_preset=False,
            is_enabled=True,
            use_preset_credentials=False,
        )
        db.add(provider)
        await db.flush()

        db.add(
            LLMModel(
                organization_id=org_id,
                provider_id=provider.id,
                name="anthropic.claude-sonnet-4-6",
                model_id="anthropic.claude-sonnet-4-6",
                is_preset=False,
                is_custom=True,
                is_enabled=True,
                context_window_tokens=None,
            )
        )
        await db.commit()
        return str(provider.id)


def _find(models, model_id):
    return next(m for m in models if m["model_id"] == model_id)


@pytest.mark.e2e
def test_context_window_override_persists_across_catalog_resync(
    create_user, login_user, whoami, get_models, test_client
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    _run(_seed_anthropic_preset(org_id))

    # Baseline: catalog value is exposed.
    model = _find(get_models(token, org_id), "claude-sonnet-4-6")
    assert model["context_window_tokens"] == CATALOG_CONTEXT_WINDOW
    assert model.get("context_window_tokens_override") is None
    model_id = model["id"]

    # Admin caps the context window (the Bedrock-style 100k case).
    resp = test_client.post(
        f"/api/llm/models/{model_id}/set_context_window",
        params={"tokens": 100_000},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # GET re-syncs preset providers from the catalog. The override must win:
    # effective value stays 100k even though the catalog says 1M.
    model = _find(get_models(token, org_id), "claude-sonnet-4-6")
    assert model["context_window_tokens"] == 100_000, (
        "override was clobbered by catalog re-sync"
    )
    assert model["context_window_tokens_override"] == 100_000

    # Clearing the override (no tokens) restores the catalog default.
    resp = test_client.post(
        f"/api/llm/models/{model_id}/set_context_window", headers=headers
    )
    assert resp.status_code == 200, resp.text
    model = _find(get_models(token, org_id), "claude-sonnet-4-6")
    assert model["context_window_tokens"] == CATALOG_CONTEXT_WINDOW
    assert model["context_window_tokens_override"] is None


@pytest.mark.e2e
def test_context_window_settable_on_custom_model(
    create_user, login_user, whoami, get_models, test_client
):
    """Custom (non-catalog) models — the actual Bedrock case — are sizable too."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    _run(_seed_bedrock_custom(org_id))

    model = _find(get_models(token, org_id), "anthropic.claude-sonnet-4-6")
    assert model["context_window_tokens"] is None
    model_id = model["id"]

    resp = test_client.post(
        f"/api/llm/models/{model_id}/set_context_window",
        params={"tokens": 100_000},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    model = _find(get_models(token, org_id), "anthropic.claude-sonnet-4-6")
    assert model["context_window_tokens"] == 100_000
    assert model["context_window_tokens_override"] == 100_000

    # Nonsense sizes are rejected before touching the row.
    resp = test_client.post(
        f"/api/llm/models/{model_id}/set_context_window",
        params={"tokens": 0},
        headers=headers,
    )
    assert resp.status_code in (400, 422), resp.text
    model = _find(get_models(token, org_id), "anthropic.claude-sonnet-4-6")
    assert model["context_window_tokens"] == 100_000


@pytest.mark.e2e
def test_set_context_window_route_is_permission_gated():
    """The endpoint carries the same manage_llm gate as every other LLM route."""
    from app.routes import llm as llm_routes

    route = next(
        r for r in llm_routes.router.routes
        if getattr(r, "path", "").endswith("/llm/models/{model_id}/set_context_window")
    )
    perm = getattr(route.endpoint, "_required_permission", None) or getattr(
        route.endpoint, "required_permission", None
    )
    assert perm == "manage_llm", f"expected manage_llm gate, got {perm!r}"


@pytest.mark.e2e
def test_context_budget_reads_model_context_window():
    """The prompt trimmer keys off model.context_window_tokens, so the
    admin-set size actually governs how much context is sent to the model."""
    from types import SimpleNamespace

    from app.ai.context.context_hub import trim_context_to_budget

    def planner_input():
        # ~50k tokens of conversation (4 chars/token heuristic) — comfortably
        # inside the 200k default budget, far over a 20k admin-set window.
        return SimpleNamespace(messages_context="m" * 200_000, past_observations=None)

    untouched = planner_input()
    trim_context_to_budget(untouched, model_context_window=None)
    assert len(untouched.messages_context) == 200_000  # default budget: no trim

    capped = planner_input()
    trim_context_to_budget(capped, model_context_window=20_000)
    assert len(capped.messages_context) < 200_000, (
        "a small admin-set context window must trim the prompt"
    )
