"""Clearing a model's price override is possible, and only clears what was sent.

The pricing endpoint (``POST /llm/models/{id}/pricing``) stores an admin rate
as an override that wins over the catalog. Setting a rate always worked; undoing
it did not — the request body carried the two fields as ``null``, which the
service read as "leave unchanged", so once a price was set there was no way to
remove it.

The contract these tests pin:
  * a field sent *explicitly as null* clears the override and reverts the
    resolved rate to the catalog price (``None`` where there is no catalog row),
  * an *omitted* field is still left untouched, so one field can be edited
    without disturbing the other.

Everything is seeded through the real routes / the DB; no external LLM calls.
"""
import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider


# A catalog preset and its published rate — picked here, not taken from the bug
# report, so the test asserts "reverts to the catalog price" rather than a
# memorized number. If the catalog rate for this id changes, update the expected
# values; the behavior under test is the revert, not the figure.
CATALOG_MODEL_ID = "gpt-6-astra"
CATALOG_INPUT_COST = 10.00
CATALOG_OUTPUT_COST = 50.00


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
            db.add(LLMModel(organization_id=org_id, provider_id=provider.id, is_enabled=True, **spec))
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
        "headers": {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    }


def _set_pricing(test_client, headers, model_id, body):
    return test_client.post(f"/api/llm/models/{model_id}/pricing", json=body, headers=headers)


@pytest.mark.e2e
def test_clearing_a_price_override_reverts_to_the_catalog_price(admin, get_models, test_client):
    _run(_seed_provider_with_models(
        admin["org_id"], provider_type="openai", is_preset=True,
        models=[dict(name="GPT-6 Astra", model_id=CATALOG_MODEL_ID, is_custom=False, is_preset=True, is_default=True)],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), CATALOG_MODEL_ID)

    # Set an admin override well away from the catalog rate.
    resp = _set_pricing(test_client, admin["headers"], model["id"], {
        "input_cost_per_million_tokens_usd": 7.5,
        "output_cost_per_million_tokens_usd": 3.25,
    })
    assert resp.status_code == 200, resp.text
    overridden = _find(get_models(admin["token"], admin["org_id"]), CATALOG_MODEL_ID)
    assert overridden["input_cost_per_million_tokens_usd"] == 7.5
    assert overridden["output_cost_per_million_tokens_usd"] == 3.25

    # Clear it: both fields sent explicitly as null.
    resp = _set_pricing(test_client, admin["headers"], model["id"], {
        "input_cost_per_million_tokens_usd": None,
        "output_cost_per_million_tokens_usd": None,
    })
    assert resp.status_code == 200, resp.text

    # The override is gone, so the resolved rate is the catalog price again —
    # and it stays that way across the GET's own catalog re-sync.
    cleared = _find(get_models(admin["token"], admin["org_id"]), CATALOG_MODEL_ID)
    assert cleared["input_cost_per_million_tokens_usd"] == CATALOG_INPUT_COST
    assert cleared["output_cost_per_million_tokens_usd"] == CATALOG_OUTPUT_COST


@pytest.mark.e2e
def test_an_omitted_pricing_field_is_left_unchanged(admin, get_models, test_client):
    _run(_seed_provider_with_models(
        admin["org_id"], provider_type="openai", is_preset=True,
        models=[dict(name="GPT-6 Astra", model_id=CATALOG_MODEL_ID, is_custom=False, is_preset=True, is_default=True)],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), CATALOG_MODEL_ID)

    assert _set_pricing(test_client, admin["headers"], model["id"], {
        "input_cost_per_million_tokens_usd": 7.5,
        "output_cost_per_million_tokens_usd": 3.25,
    }).status_code == 200

    # Edit only output; the input field is absent from the body entirely.
    resp = _set_pricing(test_client, admin["headers"], model["id"], {
        "output_cost_per_million_tokens_usd": 9.0,
    })
    assert resp.status_code == 200, resp.text

    updated = _find(get_models(admin["token"], admin["org_id"]), CATALOG_MODEL_ID)
    assert updated["input_cost_per_million_tokens_usd"] == 7.5, "the untouched field keeps its override"
    assert updated["output_cost_per_million_tokens_usd"] == 9.0


@pytest.mark.e2e
def test_clearing_on_a_custom_model_with_no_catalog_entry_leaves_it_unpriced(admin, get_models, test_client):
    custom_id = "my-self-hosted-llm"
    _run(_seed_provider_with_models(
        admin["org_id"], provider_type="custom", is_preset=False,
        models=[dict(name="Self-hosted", model_id=custom_id, is_custom=True, is_preset=False, is_default=True)],
    ))
    model = _find(get_models(admin["token"], admin["org_id"]), custom_id)

    assert _set_pricing(test_client, admin["headers"], model["id"], {
        "input_cost_per_million_tokens_usd": 7.5,
        "output_cost_per_million_tokens_usd": 3.25,
    }).status_code == 200

    resp = _set_pricing(test_client, admin["headers"], model["id"], {
        "input_cost_per_million_tokens_usd": None,
        "output_cost_per_million_tokens_usd": None,
    })
    assert resp.status_code == 200, resp.text

    # No catalog row to fall back to, so the model ends up with no price at all —
    # the admin can start over rather than being stuck with the old one.
    cleared = _find(get_models(admin["token"], admin["org_id"]), custom_id)
    assert cleared["input_cost_per_million_tokens_usd"] is None
    assert cleared["output_cost_per_million_tokens_usd"] is None
