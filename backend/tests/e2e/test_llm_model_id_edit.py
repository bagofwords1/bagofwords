"""Custom models on admin-owned providers carry an editable model ID.

The promise:
  * a custom model on an Azure, AWS Bedrock or custom (OpenAI-compatible)
    provider can be re-pointed through PATCH /llm/models/{id}, because its
    model_id names something the admin created — an Azure deployment, a Bedrock
    model id, a self-hosted model — and those get typo'd or renamed,
  * anywhere else the model_id is the LLM_MODEL_DETAILS key that carries
    pricing and the context window, so the API refuses instead of silently
    detaching the row from its catalog entry,
  * the usual create-time guards still hold: no blank id, no duplicate id on
    the same provider,
  * a model still named after its old id follows it; an explicit name wins.

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


def _maybe_find(models, model_id):
    return next((m for m in models if m["model_id"] == model_id), None)


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
@pytest.mark.parametrize("provider_type", ["custom", "azure", "bedrock"])
def test_custom_model_id_is_editable_on_admin_owned_providers(
    provider_type, admin, get_models, test_client
):
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type=provider_type,
        is_preset=False,
        models=[dict(
            name="Production deployment",
            model_id="gpt-4o-typo",
            is_custom=True,
            is_preset=False,
            is_default=True,
        )],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), "gpt-4o-typo")

    resp = test_client.patch(
        f"/api/llm/models/{model['id']}",
        json={"model_id": "gpt-4o-prod"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text

    # GET re-syncs providers against the catalog on every call; the new id must
    # still be there afterwards.
    models = get_models(admin["token"], admin["org_id"])
    assert _maybe_find(models, "gpt-4o-typo") is None
    updated = _find(models, "gpt-4o-prod")
    assert updated["id"] == model["id"], "the row is re-pointed, not recreated"
    assert updated["name"] == "Production deployment", "an explicit display name is left alone"
    assert updated["is_default"] is True, "re-pointing keeps the model's default flag"


@pytest.mark.e2e
def test_model_id_edit_carries_a_name_that_echoed_the_old_id(admin, get_models, test_client):
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="bedrock",
        is_preset=False,
        models=[dict(
            name="anthropic.claude-3-sonnet",
            model_id="anthropic.claude-3-sonnet",
            is_custom=True,
            is_preset=False,
            is_default=True,
        )],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), "anthropic.claude-3-sonnet")

    resp = test_client.patch(
        f"/api/llm/models/{model['id']}",
        json={"model_id": "us.anthropic.claude-3-sonnet-v1:0"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text

    updated = _find(
        get_models(admin["token"], admin["org_id"]), "us.anthropic.claude-3-sonnet-v1:0"
    )
    assert updated["name"] == "us.anthropic.claude-3-sonnet-v1:0", (
        "a model labelled with its own id should not keep advertising the id it no longer uses"
    )


@pytest.mark.e2e
def test_explicit_name_in_the_same_request_wins_over_the_id_fallback(admin, get_models, test_client):
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="azure",
        is_preset=False,
        models=[dict(
            name="my-deployment",
            model_id="my-deployment",
            is_custom=True,
            is_preset=False,
            is_default=True,
        )],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), "my-deployment")

    resp = test_client.patch(
        f"/api/llm/models/{model['id']}",
        json={"model_id": "my-deployment-v2", "name": "GPT-4o (EU)"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text

    updated = _find(get_models(admin["token"], admin["org_id"]), "my-deployment-v2")
    assert updated["name"] == "GPT-4o (EU)"


@pytest.mark.e2e
def test_blank_name_alongside_an_id_edit_falls_back_to_the_new_id(admin, get_models, test_client):
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="custom",
        is_preset=False,
        models=[dict(
            name="Mixtral (cheap)",
            model_id="mixtral-8x7b",
            is_custom=True,
            is_preset=False,
            is_default=True,
        )],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), "mixtral-8x7b")

    resp = test_client.patch(
        f"/api/llm/models/{model['id']}",
        json={"model_id": "mixtral-8x22b", "name": "  "},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text

    updated = _find(get_models(admin["token"], admin["org_id"]), "mixtral-8x22b")
    assert updated["name"] == "mixtral-8x22b", "a cleared name resets to the id being saved"


@pytest.mark.e2e
@pytest.mark.parametrize("provider_type", ["openai", "anthropic", "google"])
def test_model_id_is_not_editable_on_catalog_providers(
    provider_type, admin, get_models, test_client
):
    """Elsewhere the id is the catalog key — editing it would orphan the row."""
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type=provider_type,
        is_preset=False,
        models=[dict(
            name="Some custom model",
            model_id="some-custom-model",
            is_custom=True,
            is_preset=False,
            is_default=True,
        )],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), "some-custom-model")

    resp = test_client.patch(
        f"/api/llm/models/{model['id']}",
        json={"model_id": "something-else"},
        headers=admin["headers"],
    )
    assert resp.status_code == 400, resp.text
    # The frontend localizes off error_code, not off the English detail.
    assert resp.json()["error_code"] == "llm.model_id_not_editable"

    unchanged = _find(get_models(admin["token"], admin["org_id"]), "some-custom-model")
    assert unchanged["model_id"] == "some-custom-model"


@pytest.mark.e2e
def test_preset_model_id_cannot_be_edited(admin, get_models, test_client):
    """A preset row on an editable provider type is still catalog-owned."""
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="custom",
        is_preset=False,
        models=[dict(
            name="Seeded preset",
            model_id="seeded-preset",
            is_custom=False,
            is_preset=True,
            is_default=True,
        )],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), "seeded-preset")

    resp = test_client.patch(
        f"/api/llm/models/{model['id']}",
        json={"model_id": "something-else"},
        headers=admin["headers"],
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error_code"] == "llm.model_id_not_editable"


@pytest.mark.e2e
def test_blank_model_id_is_rejected(admin, get_models, test_client):
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="custom",
        is_preset=False,
        models=[dict(
            name="Llama",
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
            json={"model_id": blank},
            headers=admin["headers"],
        )
        assert resp.status_code == 400, f"blank id {blank!r} should be rejected: {resp.text}"
        assert resp.json()["error_code"] == "llm.model_id_required"

    unchanged = _find(get_models(admin["token"], admin["org_id"]), "llama-3.1-70b")
    assert unchanged["model_id"] == "llama-3.1-70b"


@pytest.mark.e2e
def test_model_id_edit_cannot_collide_with_a_sibling_model(admin, get_models, test_client):
    """The same uniqueness rule POST /llm/models enforces at creation time."""
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="custom",
        is_preset=False,
        models=[
            dict(name="A", model_id="model-a", is_custom=True, is_preset=False, is_default=True),
            dict(name="B", model_id="model-b", is_custom=True, is_preset=False),
        ],
    ))
    model_b = _find(get_models(admin["token"], admin["org_id"]), "model-b")

    resp = test_client.patch(
        f"/api/llm/models/{model_b['id']}",
        json={"model_id": "model-a"},
        headers=admin["headers"],
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_code"] == "llm.model_id_duplicate"
    # params feed the locale interpolation, so the id has to travel structured.
    assert body["params"] == {"model_id": "model-a"}

    models = get_models(admin["token"], admin["org_id"])
    assert _find(models, "model-b")["id"] == model_b["id"]


@pytest.mark.e2e
def test_saving_the_same_model_id_is_a_no_op(admin, get_models, test_client):
    """Re-submitting the unchanged id must not trip the duplicate guard."""
    _run(_seed_provider_with_models(
        admin["org_id"],
        provider_type="azure",
        is_preset=False,
        models=[dict(
            name="Deployment",
            model_id="gpt-4o-prod",
            is_custom=True,
            is_preset=False,
            is_default=True,
        )],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), "gpt-4o-prod")

    resp = test_client.patch(
        f"/api/llm/models/{model['id']}",
        json={"model_id": "gpt-4o-prod"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert _find(get_models(admin["token"], admin["org_id"]), "gpt-4o-prod")["name"] == "Deployment"
