"""Decision models: a separate model kind (typed questions in, probabilities
out) that can never be used for chat, and that the Auto router may use to pick
a run's starting model.

The provider boundary is tests/mocks/fake_decision_server.py (an httpx
transport): everything inside the app — routes, services, DB, the LLM wrapper —
runs for real.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.ee import license as ee_license
from tests.mocks.fake_decision_server import FakeDecisionServer


# ── fixtures & helpers ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _license_with_model_routing():
    saved = (ee_license._cached_license, ee_license._cache_initialized)
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="decision-tests",
        features=["model_routing", "llm_fallback"], license_id="test-decision-models",
    )
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


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


def _create_chat_provider(client, t, org, n=3):
    sfx = uuid.uuid4().hex[:6]
    r = client.post("/api/llm/providers", json={
        "name": f"chat-{sfx}", "provider_type": "anthropic",
        "credentials": {"api_key": "dummy-key"},
        "models": [{"model_id": f"chat-{sfx}-{i}", "name": f"Chat {sfx} {i}", "is_custom": True} for i in range(n)],
    }, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    return r.json()


def _create_decision_provider(client, t, org, fmt="systemone", model_id="jev-1.14.0", extra_config=None):
    sfx = uuid.uuid4().hex[:6]
    cfg = {"decision_format": fmt, **(extra_config or {})}
    r = client.post("/api/llm/providers", json={
        "name": f"decide-{sfx}", "provider_type": "custom",
        "credentials": {"base_url": "https://decisions.example.com/v1", "api_key": "dummy-key"},
        "models": [{"model_id": model_id, "name": f"Decider {sfx}", "is_custom": True,
                    "model_kind": "decision", "config": cfg}],
    }, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    return r.json()


def _models(client, t, org, kind=None):
    params = {"kind": kind} if kind else {}
    r = client.get("/api/llm/models", params=params, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    return r.json()


def _decision_model(client, t, org):
    ms = [m for m in _models(client, t, org, kind="decision")]
    assert ms, "a decision model should be listed with kind=decision"
    return ms[0]


def _set_default(client, t, org, model_id, small):
    return client.post(f"/api/llm/models/{model_id}/set_default?small={'true' if small else 'false'}",
                       headers=_h(t, org))


def _set_hint(client, t, org, model_id, hint):
    r = client.post(f"/api/llm/models/{model_id}/routing_hint", json={"hint": hint}, headers=_h(t, org))
    assert r.status_code == 200, r.json()


def _put_settings(client, t, org, config):
    return client.put("/api/organization/settings", json={"config": config}, headers=_h(t, org))


def _routing(client, t, org, on=True, method=None, decision_model_id=None, min_confidence=None):
    cfg = {"model_routing": {"value": bool(on)}}
    if method is not None:
        val = {"method": method}
        if decision_model_id is not None:
            val["decision_model_id"] = decision_model_id
        if min_confidence is not None:
            val["min_confidence"] = min_confidence
        cfg["model_routing_decision"] = {"value": val}
    return _put_settings(client, t, org, cfg)


@pytest.fixture
def world(test_client, bootstrap_admin):
    """Three guided chat models (small/default/other) + one decision model."""
    admin = bootstrap_admin("decide")
    t, org = admin["token"], admin["org_id"]
    _create_chat_provider(test_client, t, org)
    chat = _models(test_client, t, org)
    small, default, other = chat[0]["id"], chat[1]["id"], chat[2]["id"]
    assert _set_default(test_client, t, org, small, small=True).status_code == 200
    assert _set_default(test_client, t, org, default, small=False).status_code == 200
    _set_hint(test_client, t, org, small, "simple lookups")
    _set_hint(test_client, t, org, default, "standard analysis and dashboards")
    _set_hint(test_client, t, org, other, "hard multi-source reasoning")
    _create_decision_provider(test_client, t, org)
    dm = _decision_model(test_client, t, org)
    by_id = {m["id"]: m for m in chat}
    return {
        "admin": admin, "t": t, "org": org, "uid": admin["user_id"],
        "small": small, "default": default, "other": other,
        "small_mid": by_id[small]["model_id"], "default_mid": by_id[default]["model_id"],
        "other_mid": by_id[other]["model_id"], "decision": dm,
    }


async def _decide(org_id, user_id, prompt="what is total revenue by region?"):
    """Drive the router's upfront decision exactly as a run does."""
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
            db, org, settings, candidates=candidates, small_model=small, prompt_text=prompt,
        )


async def _usage_rows(org_id, scope):
    from sqlalchemy import select
    from app.models.llm_usage_record import LLMUsageRecord
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(LLMUsageRecord).where(LLMUsageRecord.organization_id == org_id,
                                         LLMUsageRecord.scope == scope)
        )).scalars().all()
        return list(rows)


# ── kind & catalog ─────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_openai_provider_gets_decision_catalog_model_with_wire_id(test_client, bootstrap_admin):
    admin = bootstrap_admin("decide-oai")
    t, org = admin["token"], admin["org_id"]
    r = test_client.post("/api/llm/providers", json={
        "name": "oai", "provider_type": "openai", "credentials": {"api_key": "dummy-key"}, "models": [],
    }, headers=_h(t, org))
    assert r.status_code == 200, r.json()

    decision = _models(test_client, t, org, kind="decision")
    luna_dec = [m for m in decision if m["model_id"] == "gpt-6-luna-decisions"]
    assert luna_dec, "OpenAI providers get the decisions catalog entry automatically"
    m = luna_dec[0]
    assert m["model_kind"] == "decision" and m["is_enabled"]
    assert m["decision"]["api_model_id"] == "gpt-6-luna"
    assert m["decision"]["decision_format"] == "openai"
    # The chat model with the same wire id is untouched and still a chat model.
    chat_ids = {x["model_id"]: x for x in _models(test_client, t, org)}
    assert "gpt-6-luna" in chat_ids and chat_ids["gpt-6-luna"]["model_kind"] == "chat"
    assert "gpt-6-luna-decisions" not in chat_ids


@pytest.mark.e2e
def test_bedrock_provider_rejects_decision_models(test_client, bootstrap_admin):
    admin = bootstrap_admin("decide-br")
    t, org = admin["token"], admin["org_id"]
    r = test_client.post("/api/llm/providers", json={
        "name": "br", "provider_type": "bedrock",
        "credentials": {"region": "us-east-1", "auth_mode": "api_key", "api_key": "dummy"},
        "models": [{"model_id": "strands-decider", "name": "SD", "is_custom": True, "model_kind": "decision"}],
    }, headers=_h(t, org))
    assert r.status_code == 400


# ── hidden from chat ───────────────────────────────────────────────────────

@pytest.mark.e2e
def test_model_list_excludes_decision_models_by_default(test_client, world):
    t, org = world["t"], world["org"]
    default_list = _models(test_client, t, org)
    assert all(m["model_kind"] == "chat" for m in default_list)
    assert world["decision"]["id"] not in {m["id"] for m in default_list}
    all_list = _models(test_client, t, org, kind="all")
    assert world["decision"]["id"] in {m["id"] for m in all_list}
    assert {m["id"] for m in default_list} <= {m["id"] for m in all_list}


@pytest.mark.e2e
def test_decision_model_cannot_be_default_small_default_or_fallback(test_client, world):
    t, org, dm = world["t"], world["org"], world["decision"]["id"]
    assert _set_default(test_client, t, org, dm, small=False).status_code == 400
    assert _set_default(test_client, t, org, dm, small=True).status_code == 400
    r = test_client.post("/api/llm/fallback_order", json={"model_ids": [dm]}, headers=_h(t, org))
    assert r.status_code == 400


@pytest.mark.e2e
def test_decision_model_cannot_be_a_user_or_report_model(test_client, world):
    t, org, dm = world["t"], world["org"], world["decision"]["id"]
    r = test_client.put("/api/users/me/default_model", json={"model_id": dm}, headers=_h(t, org))
    assert r.status_code in (400, 404), r.text
    rep = test_client.post("/api/reports", json={"title": "x", "files": [], "data_sources": []},
                           headers=_h(t, org)).json()
    r = test_client.put(f"/api/reports/{rep['id']}", json={"model_id": dm}, headers=_h(t, org))
    assert r.status_code == 400


@pytest.mark.e2e
def test_completion_with_decision_model_id_is_rejected(test_client, world):
    from fastapi import HTTPException
    from app.services.completion_service import CompletionService
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.report import Report

    t, org = world["t"], world["org"]
    rep = test_client.post("/api/reports", json={"title": "x", "files": [], "data_sources": []},
                           headers=_h(t, org)).json()

    async def _go():
        async with async_session_maker() as db:
            o, u, r = await db.get(Organization, org), await db.get(User, world["uid"]), await db.get(Report, rep["id"])
            return await CompletionService()._resolve_completion_models(db, o, u, r, world["decision"]["id"])

    with pytest.raises(HTTPException) as exc:
        _run(_go())
    assert exc.value.status_code == 400


@pytest.mark.e2e
def test_decision_model_is_never_a_routing_candidate(test_client, world):
    from app.ai.model_router import resolve_routing_candidates
    from app.models.organization import Organization
    from app.models.user import User

    t, org = world["t"], world["org"]
    # Even with a routing hint written straight into its config.
    async def _go():
        from app.models.llm_model import LLMModel
        async with async_session_maker() as db:
            m = await db.get(LLMModel, world["decision"]["id"])
            m.config = {**(m.config or {}), "routing_hint": "pick me"}
            await db.commit()
            o, u = await db.get(Organization, org), await db.get(User, world["uid"])
            return [str(c.id) for c in await resolve_routing_candidates(db, o, u)]

    assert world["decision"]["id"] not in _run(_go())


# ── per-model decision settings ────────────────────────────────────────────

@pytest.mark.e2e
def test_each_decision_setting_persists_and_reaches_the_request(test_client, world, fake_decisions):
    t, org, dm = world["t"], world["org"], world["decision"]["id"]
    body = {"decision_format": "systemone", "decision_path": "/providers/x/v1/systemone",
            "api_model_id": "deploy-77", "max_questions": 32, "timeout_ms": 2500,
            "keep_alive": "5m"}
    r = test_client.put(f"/api/llm/models/{dm}/decision", json=body, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    saved = r.json()["decision"]
    for k, v in body.items():
        assert saved[k] == v

    r = test_client.post(f"/api/llm/models/{dm}/test_decision", headers=_h(t, org))
    assert r.status_code == 200, r.json()
    assert 0.0 <= r.json()["probability"] <= 1.0
    sent = fake_decisions.requests[-1]
    assert sent["path"].endswith("/providers/x/v1/systemone")
    assert sent["json"]["model"] == "deploy-77"
    assert sent["json"]["keep_alive"] == "5m"


@pytest.mark.e2e
@pytest.mark.parametrize("bad", [
    {"decision_format": "xml"},
    {"max_questions": 0},
    {"max_questions": 100000},
    {"timeout_ms": 10},
    {"timeout_ms": 999999},
    {"decision_path": "no-leading-slash"},
])
def test_out_of_range_decision_settings_are_rejected(test_client, world, bad):
    t, org, dm = world["t"], world["org"], world["decision"]["id"]
    r = test_client.put(f"/api/llm/models/{dm}/decision", json=bad, headers=_h(t, org))
    assert r.status_code in (400, 422)


@pytest.mark.e2e
def test_chat_only_settings_are_rejected_on_decision_models_and_vice_versa(test_client, world, fake_decisions):
    t, org, dm = world["t"], world["org"], world["decision"]["id"]
    assert test_client.post(f"/api/llm/models/{dm}/set_temperature?temperature=0.3",
                            headers=_h(t, org)).status_code == 400
    assert test_client.post(f"/api/llm/models/{dm}/routing_hint", json={"hint": "x"},
                            headers=_h(t, org)).status_code == 400
    assert test_client.put(f"/api/llm/models/{world['default']}/decision", json={"timeout_ms": 2000},
                           headers=_h(t, org)).status_code == 400
    # And a temperature smuggled into config is never sent.
    test_client.post(f"/api/llm/models/{dm}/test_decision", headers=_h(t, org))
    assert fake_decisions.requests, "test_decision should reach the provider"
    for req in fake_decisions.requests:
        assert "temperature" not in req["json"]


@pytest.mark.e2e
def test_decision_settings_require_manage_llm(test_client, world, invite_user_to_org):
    member = invite_user_to_org(org_id=world["org"], admin_token=world["t"], role="member")
    r = test_client.put(f"/api/llm/models/{world['decision']['id']}/decision",
                        json={"timeout_ms": 2000}, headers=_h(member["token"], world["org"]))
    assert r.status_code == 403


# ── routing setting ────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_routing_method_defaults_to_small_model(test_client, world):
    from app.ai.model_router import routing_decision_settings

    async def _go():
        from app.models.organization import Organization
        async with async_session_maker() as db:
            org = await db.get(Organization, world["org"])
            return routing_decision_settings(await org.get_settings(db))

    s = _run(_go())
    assert s["method"] == "small_model"


@pytest.mark.e2e
def test_decision_method_requires_an_enabled_decision_model(test_client, world):
    t, org = world["t"], world["org"]
    assert _routing(test_client, t, org, method="decision").status_code == 400
    assert _routing(test_client, t, org, method="decision", decision_model_id=world["default"]).status_code == 400
    assert _routing(test_client, t, org, method="decision", decision_model_id=world["decision"]["id"],
                    min_confidence=1.7).status_code == 400
    assert _routing(test_client, t, org, method="nonsense").status_code == 400
    assert _routing(test_client, t, org, method="decision",
                    decision_model_id=world["decision"]["id"]).status_code == 200


# ── upfront routing ────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_small_model_method_never_calls_the_decision_model(test_client, world, fake_decisions):
    t, org = world["t"], world["org"]
    assert _routing(test_client, t, org, method="small_model").status_code == 200
    outcome = _run(_decide(org, world["uid"]))
    assert outcome.model is None
    assert fake_decisions.calls == 0


@pytest.mark.e2e
@pytest.mark.parametrize("target", ["default_mid", "other_mid"])
def test_confident_decision_starts_run_on_the_chosen_model(test_client, world, fake_decisions, target):
    t, org = world["t"], world["org"]
    assert _routing(test_client, t, org, method="decision", decision_model_id=world["decision"]["id"]).status_code == 200
    fake_decisions.pick(world[target], confidence=0.92)

    outcome = _run(_decide(org, world["uid"]))

    assert outcome.model is not None and outcome.model.model_id == world[target]
    assert outcome.confidence == pytest.approx(0.92)
    sent = fake_decisions.requests[-1]["json"]
    # One choice question whose options are exactly the routing candidates.
    (q,) = sent["questions"].values()
    assert set(q["criteria"]) == {world["small_mid"], world["default_mid"], world["other_mid"]}
    assert "what is total revenue" in str(sent["state"])


@pytest.mark.e2e
def test_low_confidence_or_small_pick_starts_on_small_model(test_client, world, fake_decisions):
    t, org = world["t"], world["org"]
    assert _routing(test_client, t, org, method="decision", decision_model_id=world["decision"]["id"],
                    min_confidence=0.8).status_code == 200
    fake_decisions.pick(world["other_mid"], confidence=0.55)
    assert _run(_decide(org, world["uid"])).model is None
    fake_decisions.pick(world["small_mid"], confidence=0.99)
    assert _run(_decide(org, world["uid"])).model is None


@pytest.mark.e2e
@pytest.mark.parametrize("failure", ["error", "timeout", "refusal", "disabled"])
def test_decision_failure_falls_back_to_todays_routing(test_client, world, fake_decisions, failure):
    t, org, dm = world["t"], world["org"], world["decision"]["id"]
    assert _routing(test_client, t, org, method="decision", decision_model_id=dm).status_code == 200
    if failure == "error":
        fake_decisions.fail(503)
    elif failure == "timeout":
        test_client.put(f"/api/llm/models/{dm}/decision", json={"timeout_ms": 200}, headers=_h(t, org))
        fake_decisions.delay(1.0)
    elif failure == "refusal":
        fake_decisions.refuse()
    else:
        assert test_client.post(f"/api/llm/models/{dm}/toggle?enabled=false", headers=_h(t, org)).status_code == 200

    outcome = _run(_decide(org, world["uid"]))
    assert outcome.model is None
    assert outcome.reason


@pytest.mark.e2e
def test_decision_call_is_recorded_as_unrouted_usage_on_its_own_scope(test_client, world, fake_decisions):
    t, org = world["t"], world["org"]
    assert _routing(test_client, t, org, method="decision", decision_model_id=world["decision"]["id"]).status_code == 200
    fake_decisions.pick(world["default_mid"], confidence=0.9)
    _run(_decide(org, world["uid"]))
    rows = _run(_usage_rows(org, "model_routing.decision"))
    assert len(rows) == 1
    assert rows[0].prompt_tokens > 0
    assert rows[0].routed is False, "the decision's own call must never count as routed savings"
    assert str(rows[0].llm_model_id) == world["decision"]["id"]


@pytest.mark.e2e
def test_decision_cost_nets_out_of_routing_savings(test_client, world):
    async def _go():
        from app.services.llm_usage_recorder import LLMUsageRecorderService
        from app.services.console_service import ConsoleService
        from app.schemas.console_schema import MetricsQueryParams
        from app.models.organization import Organization
        from app.models.llm_model import LLMModel

        async with async_session_maker() as db:
            o = await db.get(Organization, world["org"])
            small, base = await db.get(LLMModel, world["small"]), await db.get(LLMModel, world["default"])
            dm = await db.get(LLMModel, world["decision"]["id"])
            base.input_cost_per_million_tokens_usd, base.output_cost_per_million_tokens_usd = 10.0, 30.0
            small.input_cost_per_million_tokens_usd, small.output_cost_per_million_tokens_usd = 0.5, 1.5
            dm.input_cost_per_million_tokens_usd, dm.output_cost_per_million_tokens_usd = 2.0, 0.0
            await db.flush()
            rec = LLMUsageRecorderService(db)
            await rec.record(scope="planner", scope_ref_id=None, llm_model=small,
                             prompt_tokens=1_000_000, completion_tokens=0,
                             organization_id=world["org"], routed=True, baseline_model_id=world["default"])
            before = (await ConsoleService().get_llm_usage_metrics(db, o, MetricsQueryParams())).routing
            await rec.record(scope="model_routing.decision", scope_ref_id=None, llm_model=dm,
                             prompt_tokens=1_000_000, completion_tokens=0, organization_id=world["org"])
            await db.commit()
            after = (await ConsoleService().get_llm_usage_metrics(db, o, MetricsQueryParams())).routing
            return before, after

    before, after = _run(_go())
    assert after.decision_calls == 1
    assert after.decision_cost_usd == pytest.approx(2.0)
    assert after.savings_usd == pytest.approx(before.savings_usd - 2.0)
