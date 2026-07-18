"""E2E tests for the Auto model router.

Behavior under test (see docs/design/auto-model-routing.md):
- when the org's ``model_routing`` setting is ON, the user picked no model
  (neither per-message nor report-pinned), a small model exists and differs
  from the default, and at least one *guided* candidate exists, a completion
  run STARTS on the small model and carries routing_meta with the baseline
  (default) model id — this is the "what is total revenue?" case;
- an explicit per-message pick or a report-pinned model bypasses routing
  entirely — the "create a dashboard with model X" / pinned-report case;
- with routing OFF, resolution is unchanged (the default runs);
- only models with a routing hint are offered as escalation candidates, capped;
- the routing-hint endpoint stores guidance on config.routing_hint (manage_llm);
- realized savings surface on the LLM usage metrics once routed usage exists.

Resolution is asserted through the same service methods the three chat paths
call, so the test tracks the contract, not one incidental scenario.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


def _create_provider_with_models(test_client, token, org_id, n_models=3):
    suffix = uuid.uuid4().hex[:6]
    models = [
        {"model_id": f"routed-{suffix}-{i}", "name": f"Routed Model {suffix} {i}", "is_custom": True}
        for i in range(n_models)
    ]
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"prov-{suffix}",
            "provider_type": "anthropic",
            "credentials": {"api_key": "dummy-key"},
            "models": models,
        },
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _list_models(test_client, token, org_id):
    resp = test_client.get("/api/llm/models", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _set_routing(test_client, token, org_id, on: bool):
    resp = test_client.put(
        "/api/organization/settings",
        json={"config": {"model_routing": {"value": bool(on)}}},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _set_hint(test_client, token, org_id, model_id, hint):
    return test_client.post(
        f"/api/llm/models/{model_id}/routing_hint",
        json={"hint": hint},
        headers=_headers(token, org_id),
    )


def _create_report(test_client, token, org_id, title="Routing Test"):
    resp = test_client.post(
        "/api/reports",
        json={"title": title, "files": [], "data_sources": []},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _put_report_model(test_client, token, org_id, report_id, model_id):
    return test_client.put(
        f"/api/reports/{report_id}",
        json={"model_id": model_id},
        headers=_headers(token, org_id),
    )


async def _resolve(org_id, user_id, report_id, prompt_model_id=None):
    """Resolve (model, small_model, routing_meta) the way the chat paths do."""
    from app.services.completion_service import CompletionService
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.report import Report

    async with async_session_maker() as db:
        organization = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        report = await db.get(Report, report_id)
        return await CompletionService()._resolve_completion_models(
            db, organization, user, report, prompt_model_id
        )


async def _resolve_candidates(org_id, user_id):
    from app.ai.model_router import resolve_routing_candidates
    from app.models.organization import Organization
    from app.models.user import User

    async with async_session_maker() as db:
        organization = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        return await resolve_routing_candidates(db, organization, user)


def _set_default(test_client, token, org_id, model_id, small: bool):
    return test_client.post(
        f"/api/llm/models/{model_id}/set_default?small={'true' if small else 'false'}",
        headers=_headers(token, org_id),
    )


@pytest.fixture
def cast(test_client, bootstrap_admin):
    admin = bootstrap_admin("autoroute")
    t, org = admin["token"], admin["org_id"]
    _create_provider_with_models(test_client, t, org, n_models=3)
    models = _list_models(test_client, t, org)
    # Assign THREE distinct roles explicitly so small != default (the provider
    # seed may otherwise mark one model as both).
    ids = [m["id"] for m in models]
    small_id, default_id, other_id = ids[0], ids[1], ids[2]
    assert _set_default(test_client, t, org, small_id, small=True).status_code == 200
    assert _set_default(test_client, t, org, default_id, small=False).status_code == 200
    return {
        "admin": admin, "org_id": org, "models": models,
        "small_id": small_id, "default_id": default_id, "other_id": other_id,
    }


# ── routing hint endpoint ──────────────────────────────────────────────────

@pytest.mark.e2e
def test_routing_hint_endpoint_sets_and_clears_guidance(test_client, cast):
    t, org = cast["admin"]["token"], cast["org_id"]
    r = _set_hint(test_client, t, org, cast["default_id"], "standard analysis and dashboards")
    assert r.status_code == 200, r.json()

    models = _list_models(test_client, t, org)
    tgt = next(m for m in models if m["id"] == cast["default_id"])
    assert (tgt.get("config") or {}).get("routing_hint") == "standard analysis and dashboards"

    # Clearing removes it (model leaves the routing set).
    r2 = _set_hint(test_client, t, org, cast["default_id"], "")
    assert r2.status_code == 200, r2.json()
    models = _list_models(test_client, t, org)
    tgt = next(m for m in models if m["id"] == cast["default_id"])
    assert (tgt.get("config") or {}).get("routing_hint") is None


@pytest.mark.e2e
def test_routing_hint_requires_manage_llm(test_client, cast, bootstrap_admin, invite_user_to_org):
    member = invite_user_to_org(org_id=cast["org_id"], admin_token=cast["admin"]["token"])
    r = _set_hint(test_client, member["token"], cast["org_id"], cast["default_id"], "x")
    assert r.status_code in (401, 403)


# ── candidate resolution: only guided models, capped ───────────────────────

@pytest.mark.e2e
def test_only_guided_models_are_routing_candidates(test_client, cast):
    t, org, uid = cast["admin"]["token"], cast["org_id"], cast["admin"]["user_id"]
    # No hints yet → no candidates.
    assert _run(_resolve_candidates(org, uid)) == []
    # Guide two models → exactly those two become candidates.
    _set_hint(test_client, t, org, cast["default_id"], "standard analysis")
    _set_hint(test_client, t, org, cast["other_id"], "complex multi-source work")
    cands = _run(_resolve_candidates(org, uid))
    guided_ids = {str(c.id) for c in cands}
    assert guided_ids == {cast["default_id"], cast["other_id"]}
    assert cast["small_id"] not in guided_ids  # unguided small stays out


# ── the routing decision (the user story) ──────────────────────────────────

@pytest.mark.e2e
def test_nothing_picked_and_routing_on_starts_on_small_with_baseline(test_client, cast):
    """'what is total revenue?' → run starts on the small model."""
    t, org, uid = cast["admin"]["token"], cast["org_id"], cast["admin"]["user_id"]
    _set_hint(test_client, t, org, cast["default_id"], "standard analysis and dashboards")
    _set_routing(test_client, t, org, True)
    report = _create_report(test_client, t, org)

    model, small_model, meta = _run(_resolve(org, uid, report["id"], prompt_model_id=None))

    assert str(model.id) == cast["small_id"], "should START on the small model"
    assert str(small_model.id) == cast["small_id"]
    assert meta.get("routed") is True
    assert meta.get("baseline_model_id") == cast["default_id"], "baseline is the default it would have used"


@pytest.mark.e2e
def test_explicit_pick_bypasses_routing(test_client, cast):
    """'create a dashboard with <model>' — an explicit pick is never routed."""
    t, org, uid = cast["admin"]["token"], cast["org_id"], cast["admin"]["user_id"]
    _set_hint(test_client, t, org, cast["default_id"], "standard analysis")
    _set_routing(test_client, t, org, True)
    report = _create_report(test_client, t, org)

    model, _small, meta = _run(_resolve(org, uid, report["id"], prompt_model_id=cast["other_id"]))
    assert str(model.id) == cast["other_id"]
    assert not meta.get("routed")


@pytest.mark.e2e
def test_report_pinned_model_bypasses_routing(test_client, cast):
    t, org, uid = cast["admin"]["token"], cast["org_id"], cast["admin"]["user_id"]
    _set_hint(test_client, t, org, cast["default_id"], "standard analysis")
    _set_routing(test_client, t, org, True)
    report = _create_report(test_client, t, org)
    assert _put_report_model(test_client, t, org, report["id"], cast["other_id"]).status_code == 200

    model, _small, meta = _run(_resolve(org, uid, report["id"], prompt_model_id=None))
    assert str(model.id) == cast["other_id"], "pinned report model wins; no routing"
    assert not meta.get("routed")


@pytest.mark.e2e
def test_routing_off_runs_default(test_client, cast):
    t, org, uid = cast["admin"]["token"], cast["org_id"], cast["admin"]["user_id"]
    _set_hint(test_client, t, org, cast["default_id"], "standard analysis")
    _set_routing(test_client, t, org, False)
    report = _create_report(test_client, t, org)

    model, _small, meta = _run(_resolve(org, uid, report["id"], prompt_model_id=None))
    assert str(model.id) == cast["default_id"]
    assert not meta.get("routed")


@pytest.mark.e2e
def test_routing_on_but_no_guided_candidate_runs_default(test_client, cast):
    """Routing can't 'activate' with nothing to escalate to — stay on default."""
    t, org, uid = cast["admin"]["token"], cast["org_id"], cast["admin"]["user_id"]
    _set_routing(test_client, t, org, True)  # on, but no hints written
    report = _create_report(test_client, t, org)

    model, _small, meta = _run(_resolve(org, uid, report["id"], prompt_model_id=None))
    assert str(model.id) == cast["default_id"]
    assert not meta.get("routed")


# ── savings surface on the usage metrics ───────────────────────────────────

@pytest.mark.e2e
def test_routed_usage_produces_savings_kpi(test_client, cast):
    """A routed usage record priced below its baseline shows positive savings."""
    t, org = cast["admin"]["token"], cast["org_id"]

    async def _seed_and_read():
        from app.services.llm_usage_recorder import LLMUsageRecorderService
        from app.services.console_service import ConsoleService
        from app.schemas.console_schema import MetricsQueryParams
        from app.models.organization import Organization
        from app.models.llm_model import LLMModel
        from sqlalchemy import select

        async with async_session_maker() as db:
            organization = await db.get(Organization, org)
            small = await db.get(LLMModel, cast["small_id"])
            baseline = await db.get(LLMModel, cast["default_id"])
            # Give the baseline (default) a known price so savings is computable;
            # the small model stays cheap (near-zero), so routing saves money.
            baseline.input_cost_per_million_tokens_usd = 10.0
            baseline.output_cost_per_million_tokens_usd = 30.0
            small.input_cost_per_million_tokens_usd = 0.5
            small.output_cost_per_million_tokens_usd = 1.5
            await db.flush()
            # Record a routed call on the small model, baseline = default.
            await LLMUsageRecorderService(db).record(
                scope="planner", scope_ref_id=None, llm_model=small,
                prompt_tokens=1_000_000, completion_tokens=1_000_000,
                organization_id=org, routed=True, baseline_model_id=cast["default_id"],
            )
            await db.commit()
            metrics = await ConsoleService().get_llm_usage_metrics(
                db, organization, MetricsQueryParams()
            )
            return metrics

    metrics = _run(_seed_and_read())
    assert metrics.routing.routed_calls >= 1
    # Default (baseline) is pricier than small, so savings must be positive.
    assert metrics.routing.savings_usd > 0
    assert 0 < metrics.routing.routed_share <= 1
