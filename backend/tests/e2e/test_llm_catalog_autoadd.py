"""Feedback loop — catalog auto-add for customer-created providers.

A deployment upgrade that adds models to LLM_MODEL_DETAILS must reach
existing orgs, including the (typical) bring-your-own-key providers created
through the UI, which are NOT preset. GET /llm/models is the sync point.

Proves, for a customer-managed (non-preset) provider:
  * catalog models the org never had are auto-added, enabled, on the next
    models-list load — but ONLY catalog-enabled entries; entries the catalog
    ships disabled are skipped entirely (no dead rows in a curated list),
  * the org's default/small-default stay exactly where the admin put them,
  * a model the admin soft-deleted is a tombstone — never resurrected,
  * a model the admin disabled stays disabled (no enabled-state sync),
  * a preset row whose model id is no longer in the catalog (e.g. an older
    dated Anthropic snapshot) keeps the admin's enabled/default choices —
    the sync must not force-disable it on every models-list load,
  * provider types with no catalog entries (the OpenAI-compatible "custom"
    type) are left completely untouched.

Self-contained: providers are seeded in the DB, assertions go through the
real route -> service -> DB stack.
"""
import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.llm_model import LLMModel, LLM_MODEL_DETAILS
from app.models.llm_provider import LLMProvider

ANTHROPIC_CATALOG_IDS = {
    m["model_id"] for m in LLM_MODEL_DETAILS
    if m["provider_type"] == "anthropic" and m.get("is_enabled", True)
}
CATALOG_DISABLED_IDS = {
    m["model_id"] for m in LLM_MODEL_DETAILS if not m.get("is_enabled", True)
}
# A catalog model the seeded org starts with (stays its default throughout).
SEEDED_MODEL_ID = next(
    m["model_id"] for m in LLM_MODEL_DETAILS
    if m["provider_type"] == "anthropic" and "haiku" in m["model_id"]
)


def _run(coro):
    return asyncio.run(coro)


async def _seed_customer_anthropic(org_id):
    """A bring-your-own-key Anthropic provider with ONE catalog model checked
    (the shape create_provider leaves after the admin picks a subset)."""
    async with async_session_maker() as db:
        provider = LLMProvider(
            organization_id=org_id,
            name="Anthropic BYOK",
            provider_type="anthropic",
            is_preset=False,
            is_enabled=True,
            use_preset_credentials=False,
        )
        db.add(provider)
        await db.flush()
        db.add(LLMModel(
            organization_id=org_id,
            provider_id=provider.id,
            name="Seeded Haiku",
            model_id=SEEDED_MODEL_ID,
            is_preset=True,
            is_enabled=True,
            is_default=True,
            is_small_default=True,
            context_window_tokens=200_000,
        ))
        # An OpenAI BYOK provider with no models: the catalog has entries that
        # ship disabled under this type, so the skip-disabled rule is exercised.
        openai_provider = LLMProvider(
            organization_id=org_id,
            name="OpenAI BYOK",
            provider_type="openai",
            is_preset=False,
            is_enabled=True,
            use_preset_credentials=False,
        )
        db.add(openai_provider)
        await db.commit()
        return str(provider.id)


async def _seed_custom_provider(org_id):
    """An OpenAI-compatible provider — no catalog exists for it."""
    async with async_session_maker() as db:
        provider = LLMProvider(
            organization_id=org_id,
            name="LiteLLM Gateway",
            provider_type="custom",
            is_preset=False,
            is_enabled=True,
            use_preset_credentials=False,
            additional_config={"base_url": "http://gw.local/v1"},
        )
        db.add(provider)
        await db.flush()
        db.add(LLMModel(
            organization_id=org_id,
            provider_id=provider.id,
            name="alias-model",
            model_id="alias-model",
            is_preset=False,
            is_custom=True,
            is_enabled=True,
            context_window_tokens=100_000,
        ))
        await db.commit()
        return str(provider.id)


def _by_id(models):
    return {m["model_id"]: m for m in models}


@pytest.mark.e2e
def test_new_catalog_models_autoadd_to_customer_provider(
    create_user, login_user, whoami, get_models, test_client
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    _run(_seed_customer_anthropic(org_id))
    _run(_seed_custom_provider(org_id))

    # The GET itself is the upgrade sync point.
    models = _by_id(get_models(token, org_id))

    # Every catalog-ENABLED anthropic model now exists, enabled.
    missing = ANTHROPIC_CATALOG_IDS - set(models)
    assert not missing, f"catalog models not auto-added: {missing}"
    assert len(ANTHROPIC_CATALOG_IDS) > 1, "catalog too small for the test to mean anything"
    for mid in ANTHROPIC_CATALOG_IDS:
        assert models[mid]["is_enabled"], f"auto-added model {mid} should be enabled"

    # Catalog entries that ship disabled are NOT added at all — no dead rows.
    added_disabled = CATALOG_DISABLED_IDS & set(models)
    assert not added_disabled, f"catalog-disabled entries were added: {added_disabled}"

    # The admin's defaults are untouched: still the seeded model, on both flags.
    assert models[SEEDED_MODEL_ID]["is_default"], "org default was re-pointed by the sync"
    assert models[SEEDED_MODEL_ID]["is_small_default"], "small default was re-pointed by the sync"
    for mid, m in models.items():
        if mid != SEEDED_MODEL_ID:
            assert not m["is_default"] and not m["is_small_default"]

    # The custom (no-catalog) provider is untouched: exactly its one model.
    assert "alias-model" in models
    custom_models = [m for m in models.values() if m["provider"]["provider_type"] == "custom"]
    assert len(custom_models) == 1


@pytest.mark.e2e
def test_deleted_and_disabled_choices_survive_resync(
    create_user, login_user, whoami, get_models, test_client
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    _run(_seed_customer_anthropic(org_id))

    models = _by_id(get_models(token, org_id))  # triggers the auto-add
    victim = next(
        mid for mid in models
        if mid != SEEDED_MODEL_ID and mid in ANTHROPIC_CATALOG_IDS
    )
    disabled = next(
        mid for mid in models
        if mid not in (SEEDED_MODEL_ID, victim) and mid in ANTHROPIC_CATALOG_IDS
    )

    # Admin deletes one auto-added model and disables another.
    resp = test_client.delete(f"/api/llm/models/{models[victim]['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    resp = test_client.post(
        f"/api/llm/models/{models[disabled]['id']}/toggle",
        params={"enabled": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # The next sync must honor both choices.
    models = _by_id(get_models(token, org_id))
    assert victim not in models, "soft-deleted model was resurrected by the sync"
    assert models[disabled]["is_enabled"] is False, "disabled model was re-enabled by the sync"

    # And once more, to prove it is stable, not a one-GET grace period.
    models = _by_id(get_models(token, org_id))
    assert victim not in models
    assert models[disabled]["is_enabled"] is False


# A preset model id that used to ship in the catalog but no longer does —
# still a perfectly valid id at the provider, just not curated any more.
EX_CATALOG_MODEL_ID = "claude-opus-4-6"
assert EX_CATALOG_MODEL_ID not in {m["model_id"] for m in LLM_MODEL_DETAILS}


async def _seed_ex_catalog_model(org_id, provider_id, enabled):
    async with async_session_maker() as db:
        db.add(LLMModel(
            organization_id=org_id,
            provider_id=provider_id,
            name="Claude 4.6 Opus",
            model_id=EX_CATALOG_MODEL_ID,
            is_preset=True,
            is_enabled=enabled,
            context_window_tokens=200_000,
        ))
        await db.commit()


@pytest.mark.e2e
def test_ex_catalog_model_keeps_admin_toggle_on_customer_provider(
    create_user, login_user, whoami, get_models, test_client
):
    """A preset row whose id fell out of the curated catalog stays exactly as
    the admin set it on a customer-managed provider. Before the fix the sync
    force-disabled it on every GET /llm/models, so enabling it through the UI
    silently reverted ("Could not update model" for the customer)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    provider_id = _run(_seed_customer_anthropic(org_id))
    _run(_seed_ex_catalog_model(org_id, provider_id, enabled=False))

    # The sync leaves the disabled ex-catalog row disabled...
    models = _by_id(get_models(token, org_id))
    assert models[EX_CATALOG_MODEL_ID]["is_enabled"] is False

    # ...and the admin can enable it, and the choice SURVIVES the next syncs.
    resp = test_client.post(
        f"/api/llm/models/{models[EX_CATALOG_MODEL_ID]['id']}/toggle",
        params={"enabled": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    for _ in range(2):
        models = _by_id(get_models(token, org_id))
        assert models[EX_CATALOG_MODEL_ID]["is_enabled"] is True, (
            "sync force-disabled an ex-catalog model the admin enabled"
        )

    # It can also be made the org default, and the default sticks.
    resp = test_client.post(
        f"/api/llm/models/{models[EX_CATALOG_MODEL_ID]['id']}/set_default",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    models = _by_id(get_models(token, org_id))
    assert models[EX_CATALOG_MODEL_ID]["is_default"] is True, (
        "sync re-pointed the org default away from an ex-catalog model"
    )


@pytest.mark.e2e
def test_ex_catalog_model_still_disabled_on_preset_provider(
    create_user, login_user, whoami, get_models, test_client
):
    """On a BOW-managed preset provider the catalog IS exhaustive, so a row
    whose id left it really is retired: the sync keeps disabling it."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    async def _seed_preset_anthropic():
        async with async_session_maker() as db:
            provider = LLMProvider(
                organization_id=org_id,
                name="claude",
                provider_type="anthropic",
                is_preset=True,
                is_enabled=True,
                use_preset_credentials=True,
            )
            db.add(provider)
            await db.flush()
            await db.commit()
            return str(provider.id)

    provider_id = _run(_seed_preset_anthropic())
    _run(_seed_ex_catalog_model(org_id, provider_id, enabled=True))

    models = _by_id(get_models(token, org_id))
    assert models[EX_CATALOG_MODEL_ID]["is_enabled"] is False, (
        "retired model on a preset provider was not disabled by the sync"
    )
