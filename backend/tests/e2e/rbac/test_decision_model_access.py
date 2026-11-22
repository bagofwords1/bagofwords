"""The org's decision model is held to the same access rule as any model a
user chats with.

Story: two groups, each with its own provider. Finance may use provider A,
Marketing provider B. The Auto router's decision model (one org-wide setting)
lives on provider A and is restricted to Finance. Before this change every
user's request was sent to it — Marketing's prompts went to provider A. Now a
user who may not use the decision model starts on the small model as usual,
and the MCP tool-safety judge falls back to the small model the same way.

The provider boundary is tests/mocks/fake_decision_server.py; everything
inside the app runs for real.
"""
import asyncio
import types
import uuid

import pytest

from app.dependencies import async_session_maker
from tests.mocks.fake_decision_server import FakeDecisionServer


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


LICENSE_FEATURES = ["model_routing", "llm_fallback", "llm_access_control", "custom_roles", "audit_logs"]


@pytest.fixture
def routing_license(enterprise_license, monkeypatch):
    """enterprise_license, with a license that also lists the routing and
    access-control features (modules that imported has_feature at load time
    read them through get_license_info)."""
    from app.ee import license as ee_license
    info = ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="decision-access", features=LICENSE_FEATURES,
        license_id="test-decision-access",
    )
    monkeypatch.setattr(ee_license, "get_license_info", lambda force_refresh=False: info)
    saved = (ee_license._cached_license, ee_license._cache_initialized)
    ee_license._cached_license = info
    ee_license._cache_initialized = True
    try:
        yield
    finally:
        ee_license._cached_license, ee_license._cache_initialized = saved


@pytest.fixture
def fake_decisions(monkeypatch):
    from app.ai.llm import decisions
    server = FakeDecisionServer()
    monkeypatch.setattr(decisions, "TRANSPORT_OVERRIDE", server.transport())
    return server


def _models(client, t, org, kind=None):
    r = client.get("/api/llm/models", params={"kind": kind} if kind else {}, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    return r.json()


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group,
          grant_resource, routing_license):
    admin = bootstrap_admin("dmacc")
    t, org = admin["token"], admin["org_id"]
    sfx = uuid.uuid4().hex[:6]

    r = test_client.post("/api/llm/providers", json={
        "name": f"chat-{sfx}", "provider_type": "anthropic", "credentials": {"api_key": "dummy-key"},
        "models": [{"model_id": f"chat-{sfx}-{i}", "name": f"Chat {sfx} {i}", "is_custom": True} for i in range(3)],
    }, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    chat = _models(test_client, t, org)
    small, default, other = (m["id"] for m in chat[:3])
    by_id = {m["id"]: m for m in chat}
    assert test_client.post(f"/api/llm/models/{small}/set_default?small=true", headers=_h(t, org)).status_code == 200
    assert test_client.post(f"/api/llm/models/{default}/set_default?small=false", headers=_h(t, org)).status_code == 200
    for mid, hint in ((small, "simple lookups"), (default, "standard analysis"), (other, "hard reasoning")):
        assert test_client.post(f"/api/llm/models/{mid}/routing_hint", json={"hint": hint},
                                headers=_h(t, org)).status_code == 200

    r = test_client.post("/api/llm/providers", json={
        "name": f"decide-{sfx}", "provider_type": "custom",
        "credentials": {"base_url": "https://decisions.example.com/v1", "api_key": "dummy-key"},
        "models": [{"model_id": "jev-1.14.0", "name": f"Decider {sfx}", "is_custom": True,
                    "model_kind": "decision", "config": {"decision_format": "systemone"}}],
    }, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    dm = _models(test_client, t, org, kind="decision")[0]["id"]

    r = test_client.put("/api/organization/settings", json={"config": {
        "model_routing": {"value": True},
        "model_routing_decision": {"value": {"method": "decision", "decision_model_id": dm}},
    }}, headers=_h(t, org))
    assert r.status_code == 200, r.json()

    finance = create_group(name=f"Finance-{sfx}", user_token=t, org_id=org).json()["id"]
    marketing = create_group(name=f"Marketing-{sfx}", user_token=t, org_id=org).json()["id"]
    fiona = invite_user_to_org(org_id=org, admin_token=t)
    mark = invite_user_to_org(org_id=org, admin_token=t)
    for gid, u in ((finance, fiona), (marketing, mark)):
        assert add_user_to_group(group_id=gid, user_id=u["user_id"], user_token=t, org_id=org).status_code in (200, 201)

    # The decision model is Finance's: restricted, granted to Finance only.
    assert test_client.put(f"/api/llm/models/{dm}/restricted", json={"is_restricted": True},
                           headers=_h(t, org)).status_code == 200
    g = grant_resource(resource_type="llm_model", resource_id=dm, principal_type="group",
                       principal_id=finance, permissions=["use"], user_token=t, org_id=org)
    assert g.status_code in (200, 201), g.json()

    return types.SimpleNamespace(
        t=t, org=org, admin=admin, fiona=fiona, mark=mark, decision=dm,
        small=small, default=default, other=other,
        default_mid=by_id[default]["model_id"], other_mid=by_id[other]["model_id"],
    )


async def _decide(org_id, user_id, prompt="what is total revenue by region?"):
    from app.ai.model_router import decide_starting_model, resolve_routing_candidates
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.llm_service import LLMService

    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        settings = await org.get_settings(db)
        candidates = await resolve_routing_candidates(db, org, user)
        small = await LLMService().get_default_model(db, org, user, is_small=True)
        return await decide_starting_model(
            db, org, settings, candidates=candidates, small_model=small, prompt_text=prompt, user=user,
        )


async def _candidates(org_id, user_id):
    from app.ai.model_router import resolve_routing_candidates
    from app.models.organization import Organization
    from app.models.user import User
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        return {str(m.id) for m in await resolve_routing_candidates(db, org, user)}


# ── upfront routing decision ───────────────────────────────────────────────

@pytest.mark.e2e
def test_member_allowed_the_decision_model_is_routed_by_it(test_client, world, fake_decisions):
    fake_decisions.pick(world.other_mid, confidence=0.93)
    outcome = _run(_decide(world.org, world.fiona["user_id"]))
    assert fake_decisions.calls == 1
    assert outcome.model is not None and outcome.model.model_id == world.other_mid


@pytest.mark.e2e
def test_member_not_allowed_the_decision_model_never_sends_it_their_request(test_client, world, fake_decisions):
    fake_decisions.pick(world.other_mid, confidence=0.93)
    outcome = _run(_decide(world.org, world.mark["user_id"], prompt="marketing secret plan"))
    assert fake_decisions.calls == 0, "Marketing's prompt must not reach Finance's decision model"
    assert outcome.model is None
    assert outcome.reason == "decision_model_not_allowed"


@pytest.mark.e2e
def test_opening_the_decision_model_to_everyone_routes_everyone(test_client, world, fake_decisions):
    assert test_client.put(f"/api/llm/models/{world.decision}/restricted", json={"is_restricted": False},
                           headers=_h(world.t, world.org)).status_code == 200
    fake_decisions.pick(world.default_mid, confidence=0.9)
    outcome = _run(_decide(world.org, world.mark["user_id"]))
    assert fake_decisions.calls == 1
    assert outcome.model is not None and outcome.model.model_id == world.default_mid


@pytest.mark.e2e
@pytest.mark.parametrize("restricted,expect_calls", [(True, 0), (False, 1)])
def test_access_check_error_skips_a_restricted_decision_model_only(
    test_client, world, fake_decisions, monkeypatch, restricted, expect_calls,
):
    if not restricted:
        assert test_client.put(f"/api/llm/models/{world.decision}/restricted", json={"is_restricted": False},
                               headers=_h(world.t, world.org)).status_code == 200
    from app.core import permission_resolver

    async def _boom(*a, **k):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(permission_resolver, "user_can_use_model", _boom)
    fake_decisions.pick(world.other_mid, confidence=0.93)
    _run(_decide(world.org, world.fiona["user_id"]))
    assert fake_decisions.calls == expect_calls


# ── routing candidates ─────────────────────────────────────────────────────

@pytest.mark.e2e
def test_access_check_error_drops_restricted_candidates_and_keeps_open_ones(
    test_client, world, monkeypatch,
):
    assert test_client.put(f"/api/llm/models/{world.other}/restricted", json={"is_restricted": True},
                           headers=_h(world.t, world.org)).status_code == 200
    from app.core import permission_resolver

    async def _boom(*a, **k):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(permission_resolver, "user_can_use_model", _boom)
    got = _run(_candidates(world.org, world.fiona["user_id"]))
    assert world.other not in got
    assert {world.small, world.default} <= got


# ── MCP tool-safety judge ──────────────────────────────────────────────────

async def _judge(org_id, user_id, monkeypatch):
    """Drive ExecuteMCPTool._mode_judge with the org's decision model; the
    small-model fallback is stubbed so we only observe which judge ran."""
    from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool
    from app.models.organization import Organization
    from app.models.user import User
    from app.services import tool_permission_mode as pm

    tool = ExecuteMCPTool()
    small_calls = []

    async def _small(self, db, connection, tool_record, data, runtime_ctx, report):
        small_calls.append(1)
        return types.SimpleNamespace(approve=True, confidence=0.99, reason="small model ok")
    monkeypatch.setattr(ExecuteMCPTool, "_auto_judge", _small)

    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        settings = await org.get_settings(db)
        runtime_ctx = {"organization": org, "settings": settings, "user": user}
        mode_settings = types.SimpleNamespace(
            judge_decision_model_id=None, allow_threshold=0.9, refuse_threshold=0.1,
        )
        data = types.SimpleNamespace(tool_name="crm_lookup", arguments={"account": "Acme secret"})
        verdict, by, reason, _raw = await tool._mode_judge(
            db, types.SimpleNamespace(name="CRM"), types.SimpleNamespace(description="look up"),
            data, runtime_ctx, None, mode_settings,
        )
        return by, small_calls, pm


@pytest.mark.e2e
def test_mcp_judge_uses_the_decision_model_only_for_users_allowed_it(test_client, world, fake_decisions, monkeypatch):
    fake_decisions.pick("yes", confidence=0.95)  # predicate answers use this confidence

    by, small_calls, pm = _run(_judge(world.org, world.mark["user_id"], monkeypatch))
    assert fake_decisions.calls == 0, "Marketing's tool call must not reach Finance's decision model"
    assert by == pm.BY_SMALL_MODEL and small_calls == [1]

    by, small_calls, pm = _run(_judge(world.org, world.fiona["user_id"], monkeypatch))
    assert fake_decisions.calls == 1
    assert by == pm.BY_DECISION_MODEL and small_calls == []
