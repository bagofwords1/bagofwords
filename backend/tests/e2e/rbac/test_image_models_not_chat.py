"""Image-generation models (they produce images, they cannot chat) must never
become the model a chat runs on — whatever screen or API picks it.

The main prompt-box picker already hid them client-side, but the server
accepted them as a personal default, a report/dashboard model, an explicit
completion model, a fallback target and a routing candidate, and several
other pickers listed them. These tests pin the server-side contract.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.ee import license as ee_license


@pytest.fixture(autouse=True)
def _license():
    saved = (ee_license._cached_license, ee_license._cache_initialized)
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="image-model-tests",
        features=["model_routing", "llm_fallback"], license_id="test-image-models",
    )
    ee_license._cache_initialized = True
    try:
        yield
    finally:
        ee_license._cached_license, ee_license._cache_initialized = saved


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(test_client, bootstrap_admin):
    """An OpenAI provider: the catalog sync adds chat models and the
    GPT Image models (supports_image_generation)."""
    admin = bootstrap_admin(f"img-{uuid.uuid4().hex[:6]}")
    t, org = admin["token"], admin["org_id"]
    r = test_client.post("/api/llm/providers", json={
        "name": "oai", "provider_type": "openai", "credentials": {"api_key": "dummy-key"}, "models": [],
    }, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    models = test_client.get("/api/llm/models", headers=_h(t, org)).json()
    image = [m for m in models if m.get("supports_image_generation")]
    chat = [m for m in models if not m.get("supports_image_generation") and m["is_enabled"]]
    assert image and chat, "the OpenAI catalog should provide both kinds"
    return {"admin": admin, "t": t, "org": org, "uid": admin["user_id"], "image": image, "chat": chat}


def _report(client, t, org):
    r = client.post("/api/reports", json={"title": "x", "files": [], "data_sources": []}, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    return r.json()


@pytest.mark.e2e
@pytest.mark.parametrize("which", [0, -1])
def test_image_model_cannot_be_a_personal_default(test_client, world, which):
    t, org = world["t"], world["org"]
    r = test_client.put("/api/users/me/default_model", json={"model_id": world["image"][which]["id"]},
                        headers=_h(t, org))
    assert r.status_code == 400, r.text
    # A chat model still works.
    r = test_client.put("/api/users/me/default_model", json={"model_id": world["chat"][0]["id"]},
                        headers=_h(t, org))
    assert r.status_code == 200, r.text


@pytest.mark.e2e
def test_image_model_cannot_be_a_report_model(test_client, world):
    t, org = world["t"], world["org"]
    rep = _report(test_client, t, org)
    r = test_client.put(f"/api/reports/{rep['id']}", json={"model_id": world["image"][0]["id"]}, headers=_h(t, org))
    assert r.status_code == 400, r.text
    r = test_client.put(f"/api/reports/{rep['id']}", json={"model_id": world["chat"][0]["id"]}, headers=_h(t, org))
    assert r.status_code == 200, r.text


@pytest.mark.e2e
def test_explicit_completion_pick_of_an_image_model_is_rejected(test_client, world):
    from fastapi import HTTPException
    from app.services.completion_service import CompletionService
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.report import Report

    rep = _report(test_client, world["t"], world["org"])

    async def _go(model_id):
        async with async_session_maker() as db:
            o = await db.get(Organization, world["org"])
            u = await db.get(User, world["uid"])
            r = await db.get(Report, rep["id"])
            return await CompletionService()._resolve_completion_models(db, o, u, r, model_id)

    with pytest.raises(HTTPException) as exc:
        _run(_go(world["image"][0]["id"]))
    assert exc.value.status_code == 400
    model, _small, _meta = _run(_go(world["chat"][0]["id"]))
    assert str(model.id) == world["chat"][0]["id"]


@pytest.mark.e2e
@pytest.mark.parametrize("where", ["user_default", "report_model"])
def test_a_stale_saved_image_model_falls_back_to_a_chat_model(test_client, world, where):
    """A preference saved before this guard existed must never break chat:
    resolution skips the image model and uses the next default."""
    from sqlalchemy import update
    from app.models.membership import Membership
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.llm_service import LLMService

    rep = _report(test_client, world["t"], world["org"])
    image_id = world["image"][0]["id"]

    async def _go():
        async with async_session_maker() as db:
            # Direct write: the API now refuses this state, which is the point —
            # it simulates a row saved before the guard existed.
            if where == "user_default":
                await db.execute(update(Membership)
                                 .where(Membership.user_id == world["uid"], Membership.organization_id == world["org"])
                                 .values(default_llm_model_id=image_id))
            else:
                await db.execute(update(Report).where(Report.id == rep["id"]).values(model_id=image_id))
            await db.commit()
            o, u = await db.get(Organization, world["org"]), await db.get(User, world["uid"])
            r = await db.get(Report, rep["id"])
            return await LLMService().get_default_model_for_report(db, o, u, r)

    model = _run(_go())
    assert model is not None
    assert not model.supports_image_generation


@pytest.mark.e2e
def test_image_model_cannot_be_in_the_fallback_order(test_client, world):
    t, org = world["t"], world["org"]
    r = test_client.post("/api/llm/fallback_order", json={"model_ids": [world["image"][0]["id"]]}, headers=_h(t, org))
    assert r.status_code == 400, r.text
    r = test_client.post("/api/llm/fallback_order", json={"model_ids": [world["chat"][-1]["id"]]}, headers=_h(t, org))
    assert r.status_code == 200, r.text


@pytest.mark.e2e
def test_image_model_is_never_a_routing_candidate(test_client, world):
    from app.ai.model_router import resolve_routing_candidates
    from app.models.llm_model import LLMModel
    from app.models.organization import Organization
    from app.models.user import User

    async def _go():
        async with async_session_maker() as db:
            for m in world["image"] + world["chat"][:1]:
                row = await db.get(LLMModel, m["id"])
                row.config = {**(row.config or {}), "routing_hint": "pick me"}
            await db.commit()
            o, u = await db.get(Organization, world["org"]), await db.get(User, world["uid"])
            return {str(c.id) for c in await resolve_routing_candidates(db, o, u)}

    ids = _run(_go())
    assert world["chat"][0]["id"] in ids
    assert not ids & {m["id"] for m in world["image"]}


@pytest.mark.e2e
def test_eval_judge_model_options_exclude_image_models(test_client, world):
    from app.models.user import User
    from app.services.test_suite_service import TestSuiteService

    async def _go():
        async with async_session_maker() as db:
            u = await db.get(User, world["uid"])
            catalog = await TestSuiteService().get_test_catalog(db, world["org"], u)
            for cat in catalog.categories:
                if cat.id == "judge":
                    for f in cat.fields:
                        if f.key == "model_id":
                            return {o["value"] for o in (f.options or [])}
            return set()

    values = _run(_go())
    assert values, "judge model options should be populated"
    assert not values & {m["model_id"] for m in world["image"]}
