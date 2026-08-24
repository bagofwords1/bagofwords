"""Custom models carry an admin-editable display name.

The promise:
  * a custom model can be renamed through PATCH /llm/models/{id}, and the new
    name survives the catalog re-sync that GET /llm/models runs on every load,
  * a blank name resets it to the model_id (the column is NOT NULL, and the
    model_id is what custom models were always named before),
  * a preset model CANNOT be renamed — its name is owned by LLM_MODEL_DETAILS
    and would be silently reverted by the same re-sync, so the API says no
    instead of pretending it worked,
  * both creation paths (POST /llm/models and the models array inside
    POST /llm/providers) keep a supplied display name and fall back to the
    model_id when none is given.

Everything is seeded through the real routes or directly in the DB — no
external LLM calls, no provider credentials required.
"""
import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider


def _run(coro):
    return asyncio.run(coro)


async def _seed_provider_with_models(org_id, *, provider_type, is_preset, models):
    """Seed one provider and its models straight into the DB.

    Direct seeding (rather than the API) keeps the preset case reachable: the
    public provider endpoint only ever creates customer-managed providers.
    """
    async with async_session_maker() as db:
        provider = LLMProvider(
            organization_id=org_id,
            name=f"{provider_type} provider",
            provider_type=provider_type,
            is_preset=is_preset,
            is_enabled=True,
            use_preset_credentials=True,
        )
        db.add(provider)
        await db.flush()

        for spec in models:
            db.add(
                LLMModel(
                    organization_id=org_id,
                    provider_id=provider.id,
                    is_enabled=True,
                    **spec,
                )
            )
        await db.commit()
        return str(provider.id)


def _find(models, model_id):
    return next(m for m in models if m["model_id"] == model_id)


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return {
        "token": token,
        "org_id": org_id,
        "headers": {
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": str(org_id),
        },
    }


@pytest.mark.e2e
def test_custom_model_rename_persists_across_catalog_resync(admin, get_models, test_client):
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="custom",
        is_preset=False,
        models=[dict(
            name="llama-3.1-70b",
            model_id="llama-3.1-70b",
            is_custom=True,
            is_preset=False,
            is_default=True,
        )],
    ))

    model = _find(get_models(admin["token"], admin["org_id"]), "llama-3.1-70b")
    assert model["name"] == "llama-3.1-70b", "custom models start out named after their model_id"

    resp = test_client.patch(
        f"/api/llm/models/{model['id']}",
        json={"name": "Fast Llama"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text

    # GET re-syncs providers against the catalog on every call; the rename must
    # still be there afterwards.
    renamed = _find(get_models(admin["token"], admin["org_id"]), "llama-3.1-70b")
    assert renamed["name"] == "Fast Llama"
    assert renamed["model_id"] == "llama-3.1-70b", "renaming must not touch the provider-facing id"


@pytest.mark.e2e
def test_blank_name_resets_custom_model_to_its_model_id(admin, get_models, test_client):
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="custom",
        is_preset=False,
        models=[dict(
            name="Fast Llama",
            model_id="llama-3.1-70b",
            is_custom=True,
            is_preset=False,
            is_default=True,
        )],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), "llama-3.1-70b")

    for blank in ("", "   "):
        resp = test_client.patch(
            f"/api/llm/models/{model['id']}",
            json={"name": blank},
            headers=admin["headers"],
        )
        assert resp.status_code == 200, resp.text
        reset = _find(get_models(admin["token"], admin["org_id"]), "llama-3.1-70b")
        assert reset["name"] == "llama-3.1-70b", f"blank name {blank!r} should fall back to the model_id"


@pytest.mark.e2e
def test_preset_model_cannot_be_renamed(admin, get_models, test_client):
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="anthropic",
        is_preset=True,
        models=[dict(
            name="Claude Opus 5",
            model_id="claude-opus-5",
            is_custom=False,
            is_preset=True,
            is_default=True,
            supports_vision=True,
        )],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), "claude-opus-5")

    resp = test_client.patch(
        f"/api/llm/models/{model['id']}",
        json={"name": "My Claude"},
        headers=admin["headers"],
    )
    assert resp.status_code == 400, resp.text

    unchanged = _find(get_models(admin["token"], admin["org_id"]), "claude-opus-5")
    assert unchanged["name"] == "Claude Opus 5"


@pytest.mark.e2e
def test_create_model_keeps_display_name_and_falls_back_to_model_id(admin, get_models, test_client):
    provider_id = _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="custom",
        is_preset=False,
        models=[dict(
            name="seed",
            model_id="seed-model",
            is_custom=True,
            is_preset=False,
            is_default=True,
        )],
    ))

    named = test_client.post(
        "/api/llm/models",
        json={
            "provider_id": provider_id,
            "model_id": "mixtral-8x7b",
            "name": "Mixtral (cheap)",
            "is_custom": True,
        },
        headers=admin["headers"],
    )
    assert named.status_code == 200, named.text

    unnamed = test_client.post(
        "/api/llm/models",
        json={
            "provider_id": provider_id,
            "model_id": "qwen-2.5-72b",
            "is_custom": True,
        },
        headers=admin["headers"],
    )
    assert unnamed.status_code == 200, unnamed.text

    models = get_models(admin["token"], admin["org_id"])
    assert _find(models, "mixtral-8x7b")["name"] == "Mixtral (cheap)"
    assert _find(models, "qwen-2.5-72b")["name"] == "qwen-2.5-72b"


@pytest.mark.e2e
def test_provider_payload_keeps_custom_display_names(admin, get_models, test_client):
    """The models array inside POST /llm/providers honors a display name too.

    This is the path the provider dialog uses, and the one that used to have no
    name fallback at all — a payload without `name` hit the NOT NULL column.
    """
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": "Local Ollama",
            "provider_type": "custom",
            "credentials": {"base_url": "http://localhost:11434/v1"},
            "models": [
                {"model_id": "llama3.1:8b", "name": "Llama (local)", "is_custom": True, "is_enabled": True},
                {"model_id": "phi4:14b", "is_custom": True, "is_enabled": True},
            ],
        },
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text

    models = get_models(admin["token"], admin["org_id"])
    assert _find(models, "llama3.1:8b")["name"] == "Llama (local)"
    assert _find(models, "phi4:14b")["name"] == "phi4:14b", "a nameless custom model falls back to its model_id"


@pytest.mark.e2e
def test_rename_route_is_permission_gated():
    """PATCH carries the same manage_llm gate as the rest of the LLM routes."""
    from app.routes import llm as llm_routes

    route = next(
        r for r in llm_routes.router.routes
        if getattr(r, "path", "").endswith("/llm/models/{model_id}")
        and "PATCH" in getattr(r, "methods", set())
    )
    perm = getattr(route.endpoint, "_required_permission", None) or getattr(
        route.endpoint, "required_permission", None
    )
    assert perm == "manage_llm", f"expected manage_llm gate, got {perm!r}"
