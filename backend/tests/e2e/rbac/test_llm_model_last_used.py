"""Per-user model recency (``LLMModelSchema.last_used_at`` on ``GET /llm/models``).

The prompt box's model picker leads with a "Recent" section, which only means
something if "used" is defined as *the models this user chose to chat on*:

  - a chat turn (the planner's usage) on a model stamps it for that user;
  - the newest turn wins;
  - recency is per-user — a teammate's turns are not your recency;
  - turns the Auto router picked (``routed``) are not the user's choice;
  - background scopes (judge, titles, coders, ...) run on models the user never
    picked, so they don't count either;
  - a model the user never chatted on reports ``None``.

Usage rows are written with the real ``LLMUsageRecorderService`` — the same
call every LLM inference makes — because producing them through the API would
need a live LLM provider.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker

pytestmark = pytest.mark.e2e


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


def _last_used(test_client, token, org_id):
    resp = test_client.get("/api/llm/models", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return {m["id"]: m["last_used_at"] for m in resp.json()}


async def _record(org_id, user_id, model_id, *, scope="planner", routed=False, at=None):
    """Write one usage row for `user_id` on `model_id`, as an inference would."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.llm_model import LLMModel
    from app.services.llm_usage_recorder import LLMUsageRecorderService

    async with async_session_maker() as db:
        model = (await db.execute(
            select(LLMModel).options(selectinload(LLMModel.provider)).where(LLMModel.id == model_id)
        )).scalar_one()
        rec = await LLMUsageRecorderService(db).record(
            scope=scope, scope_ref_id=None, llm_model=model,
            prompt_tokens=10, completion_tokens=5,
            organization_id=str(org_id), user_id=str(user_id), routed=routed,
        )
        if at is not None:
            rec.created_at = at
        await db.commit()


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org):
    """Admin + member in one org, plus a provider with a few models."""
    admin = bootstrap_admin("recent")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    suffix = uuid.uuid4().hex[:6]
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"prov-{suffix}",
            "provider_type": "anthropic",
            "credentials": {"api_key": "dummy-key"},
            "models": [
                {"model_id": f"claude-recent-{suffix}-{i}", "name": f"Recent Model {suffix} {i}", "is_custom": True}
                for i in range(4)
            ],
        },
        headers=_headers(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    models = test_client.get(
        "/api/llm/models", headers=_headers(admin["token"], admin["org_id"])
    ).json()
    assert len(models) >= 3
    return {
        "admin": admin,
        "member": member,
        "org_id": admin["org_id"],
        "model_ids": [m["id"] for m in models],
    }


def test_never_used_models_have_no_recency(test_client, cast):
    used = _last_used(test_client, cast["member"]["token"], cast["org_id"])
    assert set(used) == set(cast["model_ids"])
    assert all(v is None for v in used.values()), used


def test_chat_turns_stamp_models_and_newest_wins(test_client, cast):
    org, member = cast["org_id"], cast["member"]
    a, b, never = cast["model_ids"][:3]
    old = datetime.utcnow() - timedelta(days=4)
    newer = datetime.utcnow() - timedelta(hours=2)

    _run(_record(org, member["user_id"], a, at=old))
    _run(_record(org, member["user_id"], b, at=newer))
    # A later turn on `a` moves it ahead of `b`.
    latest = datetime.utcnow() - timedelta(minutes=5)
    _run(_record(org, member["user_id"], a, at=latest))

    used = _last_used(test_client, member["token"], org)
    assert used[never] is None
    assert used[a] is not None and used[b] is not None
    assert used[a] > used[b], "the newest turn on a model is its recency"


def test_recency_is_per_user(test_client, cast):
    org = cast["org_id"]
    model = cast["model_ids"][1]
    _run(_record(org, cast["admin"]["user_id"], model))

    assert _last_used(test_client, cast["member"]["token"], org)[model] is None
    assert _last_used(test_client, cast["admin"]["token"], org)[model] is not None


@pytest.mark.parametrize("kwargs", [
    {"routed": True},            # the Auto router chose it, not the user
    {"scope": "tool_call_judge"},  # background scope on a model nobody picked
    {"scope": "answer"},
])
def test_usage_the_user_did_not_choose_does_not_count(test_client, cast, kwargs):
    org, member = cast["org_id"], cast["member"]
    model = cast["model_ids"][2]
    _run(_record(org, member["user_id"], model, **kwargs))

    assert _last_used(test_client, member["token"], org)[model] is None
