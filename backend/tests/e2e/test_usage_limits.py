import asyncio
import uuid

import pandas as pd
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.ai.code_execution.code_execution import StreamingCodeExecutor, estimate_result_size_bytes
from app.ai.llm.llm import LLM
from app.ai.llm.types import LLMResponse, LLMUsage
from app.dependencies import async_session_maker
from app.ee import license as ee_license
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.query import Query
from app.models.report import Report
from app.models.widget import Widget
from app.models.usage_policy import UsageCounter
from app.schemas.query_schema import QueryRunRequest
from app.schemas.usage_policy_schema import (
    UsagePolicyAssignmentInput,
    UsagePolicyConnectionOverrideInput,
    UsagePolicyCreate,
)
from app.services.query_service import QueryService
from app.services.llm_usage_recorder import LLMUsageRecorderService
from app.services.usage_policy_service import (
    METRIC_DATA_BYTES,
    METRIC_DATA_QUERIES,
    METRIC_LLM_COST,
    METRIC_LLM_TOKENS,
    SCOPE_CONNECTION,
    UsageLimitContext,
    UsageLimitExceeded,
    current_month_window,
    usage_policy_service,
)


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _run(coro):
    return asyncio.run(coro)


async def _create_policy(org_id, **kwargs):
    async with async_session_maker() as db:
        return await usage_policy_service.create_policy(
            db,
            org_id,
            UsagePolicyCreate(**kwargs),
        )


async def _counter_used(org_id, user_id, metric, *, scope_type="organization", scope_ref_id=""):
    window_start, _ = current_month_window()
    async with async_session_maker() as db:
        result = await db.execute(
            select(UsageCounter).where(
                UsageCounter.organization_id == org_id,
                UsageCounter.user_id == user_id,
                UsageCounter.metric == metric,
                UsageCounter.scope_type == scope_type,
                UsageCounter.scope_ref_id == scope_ref_id,
                UsageCounter.window_start == window_start,
            )
        )
        counter = result.scalar_one_or_none()
        return counter.used if counter else 0


async def _record_usage_summary_sample(org_id, user_id, conn_id):
    async with async_session_maker() as db:
        await usage_policy_service.record_llm_tokens(
            db,
            org_id=org_id,
            user_id=user_id,
            amount=3,
            source="test.summary",
        )
        await usage_policy_service.consume_data_query(
            db,
            org_id=org_id,
            user_id=user_id,
            connection_id=conn_id,
            source="test.summary",
        )
        await usage_policy_service.consume_data_query(
            db,
            org_id=org_id,
            user_id=user_id,
            connection_id=conn_id,
            source="test.summary",
        )
        await usage_policy_service.consume_data_bytes(
            db,
            org_id=org_id,
            user_id=user_id,
            connection_id=conn_id,
            amount=50,
            source="test.summary",
        )
        await db.commit()


async def _create_connection(org_id, name):
    async with async_session_maker() as db:
        conn = Connection(
            organization_id=org_id,
            name=name,
            type="sqlite",
            config={},
            credentials=None,
        )
        db.add(conn)
        await db.commit()
        await db.refresh(conn)
        return str(conn.id)


async def _create_query_context(org_id, user_id):
    async with async_session_maker() as db:
        suffix = uuid.uuid4().hex[:8]
        report = Report(
            title=f"Quota report {suffix}",
            slug=f"quota-report-{suffix}",
            status="draft",
            user_id=user_id,
            organization_id=org_id,
        )
        data_source = DataSource(
            name=f"Quota source {suffix}",
            organization_id=org_id,
            is_active=True,
            is_public=True,
        )
        widget = Widget(
            title=f"Quota widget {suffix}",
            slug=f"quota-widget-{suffix}",
            status="draft",
            report=report,
        )
        query = Query(
            title=f"Quota query {suffix}",
            report=report,
            widget=widget,
            organization_id=org_id,
            user_id=user_id,
        )
        report.data_sources.append(data_source)
        db.add_all([report, data_source, widget, query])
        await db.commit()
        await db.refresh(query)
        return str(query.id)


def _bootstrap_admin(create_user, login_user, whoami):
    user = create_user(email=f"usage_{uuid.uuid4().hex[:8]}@test.com")
    token = login_user(user["email"], user["password"])
    profile = whoami(token)
    org_id = profile["organizations"][0]["id"]
    return token, org_id, profile["id"]


@pytest.fixture(autouse=True)
def _enable_usage_limits_license():
    """Patch the EE license cache so every test in this module sees usage_limits as active."""
    saved_cached = ee_license._cached_license
    saved_initialized = ee_license._cache_initialized
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True,
        tier="enterprise",
        org_name="tests",
        features=["usage_limits", "custom_roles"],
        license_id="test-usage-limits",
    )
    ee_license._cache_initialized = True
    yield
    ee_license._cached_license = saved_cached
    ee_license._cache_initialized = saved_initialized


@pytest.mark.e2e
def test_usage_policy_routes_default_unlimited_and_direct_assignment(test_client, create_user, login_user, whoami):
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)

    effective = test_client.get(
        f"/api/organizations/{org_id}/usage-policies/effective/{user_id}",
        headers=_headers(token, org_id),
    )
    assert effective.status_code == 200, effective.json()
    assert effective.json()["monthly_token_limit"] is None
    assert effective.json()["monthly_query_limit"] is None
    assert effective.json()["monthly_data_bytes_limit"] is None
    assert effective.json()["resolution_source"] == "default"

    created = test_client.post(
        f"/api/organizations/{org_id}/usage-policies",
        json={
            "name": "Analyst cap",
            "monthly_token_limit": 1000,
            "monthly_query_limit": 25,
            "monthly_data_bytes_limit": 1000000,
            "assignments": [{"principal_type": "user", "principal_id": user_id}],
        },
        headers=_headers(token, org_id),
    )
    assert created.status_code == 200, created.json()
    assert created.json()["assignments"][0]["principal_type"] == "user"

    effective = test_client.get(
        f"/api/organizations/{org_id}/usage-policies/effective/{user_id}",
        headers=_headers(token, org_id),
    )
    assert effective.status_code == 200, effective.json()
    assert effective.json()["monthly_token_limit"] == 1000
    assert effective.json()["monthly_query_limit"] == 25
    assert effective.json()["monthly_data_bytes_limit"] == 1000000
    assert effective.json()["resolution_source"] == "direct"


@pytest.mark.e2e
def test_usage_policy_update_persists_connection_overrides_with_unlimited_defaults(test_client, create_user, login_user, whoami):
    token, org_id, _ = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(_create_connection(org_id, "warehouse"))

    created = test_client.post(
        f"/api/organizations/{org_id}/usage-policies",
        json={
            "name": "Connection-only cap",
            "monthly_token_limit": None,
            "monthly_query_limit": None,
            "monthly_data_bytes_limit": None,
        },
        headers=_headers(token, org_id),
    )
    assert created.status_code == 200, created.json()

    updated = test_client.put(
        f"/api/organizations/{org_id}/usage-policies/{created.json()['id']}",
        json={
            "name": "Connection-only cap",
            "monthly_token_limit": None,
            "monthly_query_limit": None,
            "monthly_data_bytes_limit": None,
            "connection_overrides": [
                {
                    "connection_id": conn_id,
                    "monthly_query_limit": 2,
                    "monthly_data_bytes_limit": None,
                }
            ],
            "enabled": True,
        },
        headers=_headers(token, org_id),
    )
    assert updated.status_code == 200, updated.json()
    assert updated.json()["monthly_query_limit"] is None
    assert updated.json()["connection_overrides"] == [
        {
            "id": updated.json()["connection_overrides"][0]["id"],
            "policy_id": created.json()["id"],
            "organization_id": org_id,
            "connection_id": conn_id,
            "monthly_query_limit": 2,
            "monthly_data_bytes_limit": None,
        }
    ]


@pytest.mark.e2e
def test_whoami_includes_usage_quota_summary(create_user, login_user, whoami):
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(_create_connection(org_id, "warehouse"))
    _run(_create_policy(
        org_id,
        name="Visible quota",
        monthly_token_limit=5,
        monthly_query_limit=100,
        monthly_data_bytes_limit=1000,
        assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
        connection_overrides=[
            UsagePolicyConnectionOverrideInput(
                connection_id=conn_id,
                monthly_query_limit=2,
                monthly_data_bytes_limit=200,
            ),
        ],
    ))
    _run(_record_usage_summary_sample(org_id, user_id, conn_id))

    profile = whoami(token)
    org = next(item for item in profile["organizations"] if item["id"] == org_id)
    quota = org["usage_quota"]
    assert quota["enabled"] is True
    assert quota["resolution_source"] == "direct"
    assert quota["tokens"]["used"] == 3
    assert quota["tokens"]["limit"] == 5
    assert quota["tokens"]["remaining"] == 2
    assert quota["queries"]["used"] == 2
    assert quota["queries"]["limit"] == 100
    assert quota["data_bytes"]["used"] == 50
    assert quota["data_bytes"]["limit"] == 1000
    assert quota["connections"] == [
        {
            "id": conn_id,
            "name": "warehouse",
            "queries": {
                "used": 2,
                "limit": 2,
                "remaining": 0,
                "percent": 100.0,
            },
            "data_bytes": {
                "used": 50,
                "limit": 200,
                "remaining": 150,
                "percent": 25.0,
            },
        }
    ]


@pytest.mark.e2e
def test_uncapped_user_usage_is_still_recorded_and_visible_in_whoami(create_user, login_user, whoami):
    """Profile "Usage" tab regression: a user with NO capped policy (the default
    state) must still see real usage accrue. Counters are recorded whenever the
    usage_limits feature is licensed, cap or no cap."""
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(_create_connection(org_id, "warehouse"))

    # No policy assigned: resolution_source == "default", every limit is None.
    _run(_record_usage_summary_sample(org_id, user_id, conn_id))

    async def _record_cost():
        async with async_session_maker() as db:
            await usage_policy_service.record_llm_cost(
                db,
                org_id=org_id,
                user_id=user_id,
                amount_micro_usd=250_000,
                source="test.uncapped",
            )
            await db.commit()

    _run(_record_cost())

    profile = whoami(token)
    org = next(item for item in profile["organizations"] if item["id"] == org_id)
    quota = org["usage_quota"]
    assert quota["enabled"] is True
    assert quota["resolution_source"] == "default"
    assert quota["tokens"]["used"] == 3
    assert quota["tokens"]["limit"] is None
    assert quota["queries"]["used"] == 2
    assert quota["queries"]["limit"] is None
    assert quota["data_bytes"]["used"] == 50
    assert quota["data_bytes"]["limit"] is None
    assert quota["spend"]["used"] == 0.25
    assert quota["spend"]["limit"] is None


@pytest.mark.e2e
def test_daily_usage_series_groups_events_by_day_and_zero_fills(test_client, create_user, login_user, whoami):
    """The profile Usage tab's daily charts: events aggregate per UTC day per
    metric, and every day from the 1st through today is present (zero-filled)."""
    from datetime import datetime as dt, timedelta as td

    from app.models.usage_policy import UsageEvent

    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(_create_connection(org_id, "warehouse"))

    today = dt.utcnow().date()
    window_start, _ = current_month_window()
    # Two days of activity: today and (when the month allows) an earlier day.
    earlier = max(window_start.date(), today - td(days=3))

    async def _seed_events():
        async with async_session_maker() as db:
            def event(metric, amount, day, **kw):
                return UsageEvent(
                    organization_id=org_id,
                    user_id=user_id,
                    metric=metric,
                    amount=amount,
                    scope_type=kw.get("scope_type", "organization"),
                    scope_ref_id=kw.get("scope_ref_id", ""),
                    source="test.daily",
                    created_at=dt(day.year, day.month, day.day, 12, 0, 0),
                )
            db.add_all([
                event(METRIC_LLM_TOKENS, 100, earlier),
                event(METRIC_LLM_TOKENS, 40, today),
                event(METRIC_LLM_TOKENS, 2, today),
                event(METRIC_DATA_QUERIES, 1, today, scope_type=SCOPE_CONNECTION, scope_ref_id=conn_id),
                event(METRIC_DATA_BYTES, 512, today, scope_type=SCOPE_CONNECTION, scope_ref_id=conn_id),
                event(METRIC_LLM_COST, 250_000, today),  # $0.25
            ])
            await db.commit()

    _run(_seed_events())

    resp = test_client.get(
        f"/api/organizations/{org_id}/usage/daily",
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    series = resp.json()
    assert series["enabled"] is True

    days = {point["date"]: point for point in series["days"]}
    # Zero-filled: one point per day from the 1st through today, no gaps.
    expected_count = (today - window_start.date()).days + 1
    assert len(series["days"]) == expected_count
    assert series["days"][0]["date"] == window_start.date().isoformat()
    assert series["days"][-1]["date"] == today.isoformat()

    assert days[today.isoformat()]["tokens"] == 42
    assert days[today.isoformat()]["queries"] == 1
    assert days[today.isoformat()]["data_bytes"] == 512
    assert days[today.isoformat()]["spend_usd"] == 0.25
    if earlier != today:
        assert days[earlier.isoformat()]["tokens"] == 100
        assert days[earlier.isoformat()]["queries"] == 0

    # Self-serve only for members: once the membership is gone, the same
    # authenticated user is rejected.
    async def _remove_membership():
        from app.models.membership import Membership
        async with async_session_maker() as db:
            await db.execute(
                update(Membership)
                .where(Membership.organization_id == org_id, Membership.user_id == user_id)
                .values(deleted_at=dt.utcnow())
            )
            await db.commit()

    _run(_remove_membership())
    denied = test_client.get(
        f"/api/organizations/{org_id}/usage/daily",
        headers=_headers(token, org_id),
    )
    assert denied.status_code == 403, denied.json()


@pytest.mark.e2e
def test_usage_policy_persists_and_resolves_monthly_spend_limit_usd(test_client, create_user, login_user, whoami):
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)

    created = test_client.post(
        f"/api/organizations/{org_id}/usage-policies",
        json={
            "name": "Spend cap",
            "monthly_token_limit": None,
            "monthly_spend_limit_usd": 12.5,
            "assignments": [{"principal_type": "user", "principal_id": user_id}],
        },
        headers=_headers(token, org_id),
    )
    assert created.status_code == 200, created.json()
    assert created.json()["monthly_spend_limit_usd"] == 12.5

    effective = test_client.get(
        f"/api/organizations/{org_id}/usage-policies/effective/{user_id}",
        headers=_headers(token, org_id),
    )
    assert effective.status_code == 200, effective.json()
    assert effective.json()["monthly_spend_limit_usd"] == 12.5

    updated = test_client.put(
        f"/api/organizations/{org_id}/usage-policies/{created.json()['id']}",
        json={"monthly_spend_limit_usd": None},
        headers=_headers(token, org_id),
    )
    assert updated.status_code == 200, updated.json()
    assert updated.json()["monthly_spend_limit_usd"] is None


@pytest.mark.e2e
def test_record_llm_cost_counts_in_micro_usd_and_surfaces_in_summary(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    _run(_create_policy(
        org_id,
        name="Spend visible",
        monthly_token_limit=None,
        monthly_spend_limit_usd=5.0,
        assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
    ))

    async def _record_cost():
        async with async_session_maker() as db:
            # $1.25 -> 1_250_000 micro-USD
            await usage_policy_service.record_llm_cost(
                db,
                org_id=org_id,
                user_id=user_id,
                amount_micro_usd=1_250_000,
                source="test.spend",
            )
            await db.commit()

    _run(_record_cost())
    assert _run(_counter_used(org_id, user_id, METRIC_LLM_COST)) == 1_250_000

    async def _summary():
        async with async_session_maker() as db:
            return await usage_policy_service.get_user_quota_summary(db, org_id, user_id)

    summary = _run(_summary())
    assert summary.spend.limit == 5.0
    assert summary.spend.used == 1.25
    assert summary.spend.remaining == 3.75
    assert summary.spend.percent == 25.0


@pytest.mark.e2e
def test_spend_cap_blocks_next_llm_call_once_budget_consumed(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    _run(_create_policy(
        org_id,
        name="Spend block",
        monthly_token_limit=None,
        monthly_spend_limit_usd=1.0,
        assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
    ))

    async def _record_cost(amount_micro):
        async with async_session_maker() as db:
            await usage_policy_service.record_llm_cost(
                db,
                org_id=org_id,
                user_id=user_id,
                amount_micro_usd=amount_micro,
                source="test.spend",
            )
            await db.commit()

    # Spend exactly the $1.00 budget, then the next preflight check must block.
    _run(_record_cost(1_000_000))

    ctx = UsageLimitContext(
        organization_id=org_id,
        user_id=user_id,
        source="agent",
        source_ref_id="completion-1",
        session_maker=async_session_maker,
    )
    with pytest.raises(UsageLimitExceeded) as exc_info:
        _run(ctx.check_tokens(10))
    assert exc_info.value.metric == METRIC_LLM_COST


@pytest.mark.e2e
def test_principal_quota_assignment_replaces_existing_direct_policy(test_client, create_user, login_user, whoami):
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)

    first = test_client.post(
        f"/api/organizations/{org_id}/usage-policies",
        json={"name": "First quota", "monthly_token_limit": 100},
        headers=_headers(token, org_id),
    )
    assert first.status_code == 200, first.json()
    second = test_client.post(
        f"/api/organizations/{org_id}/usage-policies",
        json={"name": "Second quota", "monthly_token_limit": 10},
        headers=_headers(token, org_id),
    )
    assert second.status_code == 200, second.json()

    assigned = test_client.put(
        f"/api/organizations/{org_id}/usage-policy-assignments/principal",
        json={"principal_type": "user", "principal_id": user_id, "policy_id": first.json()["id"]},
        headers=_headers(token, org_id),
    )
    assert assigned.status_code == 200, assigned.json()
    assert assigned.json()["policy_id"] == first.json()["id"]

    reassigned = test_client.put(
        f"/api/organizations/{org_id}/usage-policy-assignments/principal",
        json={"principal_type": "user", "principal_id": user_id, "policy_id": second.json()["id"]},
        headers=_headers(token, org_id),
    )
    assert reassigned.status_code == 200, reassigned.json()
    effective = test_client.get(
        f"/api/organizations/{org_id}/usage-policies/effective/{user_id}",
        headers=_headers(token, org_id),
    )
    assert effective.status_code == 200, effective.json()
    assert effective.json()["monthly_token_limit"] == 10
    assert effective.json()["policy_ids"] == [second.json()["id"]]

    policies = test_client.get(
        f"/api/organizations/{org_id}/usage-policies",
        headers=_headers(token, org_id),
    )
    assert policies.status_code == 200, policies.json()
    assignments = [
        assignment
        for policy in policies.json()
        for assignment in policy["assignments"]
        if assignment["principal_type"] == "user" and assignment["principal_id"] == user_id
    ]
    assert len(assignments) == 1
    assert assignments[0]["policy_id"] == second.json()["id"]

    cleared = test_client.put(
        f"/api/organizations/{org_id}/usage-policy-assignments/principal",
        json={"principal_type": "user", "principal_id": user_id, "policy_id": None},
        headers=_headers(token, org_id),
    )
    assert cleared.status_code == 200, cleared.json()
    assert cleared.json()["policy_id"] is None
    effective = test_client.get(
        f"/api/organizations/{org_id}/usage-policies/effective/{user_id}",
        headers=_headers(token, org_id),
    )
    assert effective.status_code == 200, effective.json()
    assert effective.json()["resolution_source"] == "default"


@pytest.mark.e2e
def test_connection_override_applies_per_policy_and_falls_back_to_default(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_a = _run(_create_connection(org_id, "warehouse-a"))
    conn_b = _run(_create_connection(org_id, "warehouse-b"))

    _run(_create_policy(
        org_id,
        name="Connection caps",
        monthly_token_limit=None,
        monthly_query_limit=100,
        monthly_data_bytes_limit=1000,
        assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
        connection_overrides=[
            UsagePolicyConnectionOverrideInput(
                connection_id=conn_a,
                monthly_query_limit=12,
                monthly_data_bytes_limit=120,
            ),
        ],
    ))

    async def _resolve():
        async with async_session_maker() as db:
            return await usage_policy_service.resolve_effective_limits(db, org_id, user_id)

    effective = _run(_resolve())
    assert effective.query_limit_for_connection(conn_a) == 12
    assert effective.query_limit_for_connection(conn_b) == 100
    assert effective.data_bytes_limit_for_connection(conn_a) == 120
    assert effective.data_bytes_limit_for_connection(conn_b) == 1000


@pytest.mark.e2e
def test_default_unlimited_usage_limits_record_counters_without_enforcement(create_user, login_user, whoami):
    """Unlimited (default) limits never *block*, but usage is still recorded —
    the per-user counters back the profile Usage tab and keep a cap accurate
    if an admin introduces one mid-month."""
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(_create_connection(org_id, "warehouse"))

    async def _exercise_default_limits():
        async with async_session_maker() as db:
            await usage_policy_service.check_llm_tokens_available(
                db,
                org_id=org_id,
                user_id=user_id,
                requested_tokens=10,
            )
            await usage_policy_service.record_llm_tokens(
                db,
                org_id=org_id,
                user_id=user_id,
                amount=10,
                source="test.default",
            )
            await usage_policy_service.consume_data_query(
                db,
                org_id=org_id,
                user_id=user_id,
                connection_id=conn_id,
                source="test.default",
            )
            await usage_policy_service.consume_data_bytes(
                db,
                org_id=org_id,
                user_id=user_id,
                connection_id=conn_id,
                amount=100,
                source="test.default",
            )
            await db.commit()

    _run(_exercise_default_limits())
    assert _run(_counter_used(org_id, user_id, METRIC_LLM_TOKENS)) == 10
    assert _run(_counter_used(
        org_id,
        user_id,
        METRIC_DATA_QUERIES,
        scope_type=SCOPE_CONNECTION,
        scope_ref_id=conn_id,
    )) == 1
    assert _run(_counter_used(
        org_id,
        user_id,
        METRIC_DATA_BYTES,
        scope_type=SCOPE_CONNECTION,
        scope_ref_id=conn_id,
    )) == 100


class _FakeProvider:
    provider_type = "custom"
    additional_config = {"base_url": "http://example.invalid"}

    def decrypt_credentials(self):
        return [""]


class _FakeModel:
    id = "fake-model-row"
    model_id = "fake-model"
    provider = _FakeProvider()
    supports_vision = False

    def get_input_cost_rate(self):
        return 0

    def get_output_cost_rate(self):
        return 0


class _SuccessfulLLMClient:
    def __init__(self):
        self.calls = 0

    def inference(self, **kwargs):
        self.calls += 1
        return LLMResponse(text="ok", usage=LLMUsage(prompt_tokens=2, completion_tokens=3))


class _FailingLLMClient:
    def __init__(self):
        self.calls = 0

    def inference(self, **kwargs):
        self.calls += 1
        raise RuntimeError("provider down")


def _quota_llm(org_id, user_id, client):
    llm = object.__new__(LLM)
    llm.model = _FakeModel()
    llm.model_id = "fake-model"
    llm.provider = "custom"
    llm.client = client
    llm._usage_session_maker = async_session_maker
    llm._usage_limit_context = UsageLimitContext(
        organization_id=org_id,
        user_id=user_id,
        source="test.llm",
        source_ref_id="completion-1",
        session_maker=async_session_maker,
    )
    llm._count_tokens = lambda text: 1
    llm._estimate_tokens_fast = lambda text: 1
    return llm


@pytest.mark.e2e
def test_llm_usage_history_sqlite_lock_is_best_effort(monkeypatch, create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    calls = {"count": 0}

    async def _exercise_locked_history_recording():
        done = asyncio.Event()

        async def locked_record(self, **kwargs):
            calls["count"] += 1
            done.set()
            raise OperationalError("insert llm usage", {}, Exception("database is locked"))

        monkeypatch.setattr(LLMUsageRecorderService, "record", locked_record)
        llm = _quota_llm(org_id, user_id, _SuccessfulLLMClient())
        llm._schedule_usage_record(
            scope="planner",
            scope_ref_id="completion-1",
            prompt_tokens=2,
            completion_tokens=3,
            should_record=True,
        )
        await asyncio.wait_for(done.wait(), timeout=1)
        await asyncio.sleep(0)

    _run(_exercise_locked_history_recording())
    assert calls["count"] == 1


@pytest.mark.e2e
def test_llm_success_counts_tokens_and_failed_provider_counts_nothing(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    _run(_create_policy(
        org_id,
        name="Token cap",
        monthly_token_limit=10,
        monthly_query_limit=None,
        assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
    ))

    success_client = _SuccessfulLLMClient()
    success_llm = _quota_llm(org_id, user_id, success_client)
    assert success_llm.inference("hello") == "ok"
    assert success_client.calls == 1
    # `_record_usage_limit_*` buffers token writes on the
    # UsageLimitContext and lets the agent run flush them in one go
    # at end-of-execution. This test wires an LLM directly without an
    # agent, so we drive the flush ourselves before reading the
    # counter.
    _run(success_llm._usage_limit_context.flush())
    assert _run(_counter_used(org_id, user_id, METRIC_LLM_TOKENS)) == 5

    failing_client = _FailingLLMClient()
    failing_llm = _quota_llm(org_id, user_id, failing_client)
    with pytest.raises(RuntimeError):
        failing_llm.inference("hello")
    assert failing_client.calls == 1
    # Provider error short-circuits before the buffer ever sees those
    # tokens, so this flush is a no-op - kept for symmetry.
    _run(failing_llm._usage_limit_context.flush())
    assert _run(_counter_used(org_id, user_id, METRIC_LLM_TOKENS)) == 5


@pytest.mark.e2e
def test_admin_is_still_blocked_by_token_quota_before_provider_call(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    _run(_create_policy(
        org_id,
        name="Blocked admin",
        monthly_token_limit=0,
        monthly_query_limit=None,
        assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
    ))

    client = _SuccessfulLLMClient()
    with pytest.raises(UsageLimitExceeded):
        _quota_llm(org_id, user_id, client).inference("hello")
    assert client.calls == 0


class _FakeQueryClient:
    def __init__(self, connection_id, fail=False):
        self._bow_connection_id = connection_id
        self._bow_connection_name = "warehouse"
        self._bow_data_source_id = "ds-1"
        self._bow_data_source_name = "Domain"
        self.calls = 0
        self.fail = fail

    def execute_query(self, query):
        self.calls += 1
        if self.fail:
            raise RuntimeError("bad sql")
        return pd.DataFrame({"x": [1]})


@pytest.mark.e2e
def test_execute_query_inside_quota_context_counts_failures_and_blocks_n_plus_one(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(_create_connection(org_id, "warehouse"))
    _run(_create_policy(
        org_id,
        name="Query cap",
        monthly_token_limit=None,
        monthly_query_limit=1,
        assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
    ))

    usage_ctx = UsageLimitContext(
        organization_id=org_id,
        user_id=user_id,
        source="create_data",
        source_ref_id="tool-1",
        session_maker=async_session_maker,
    )
    executor = StreamingCodeExecutor(usage_context=usage_ctx)
    client = _FakeQueryClient(conn_id)
    code = """
def generate_df(ds_clients, excel_files):
    first = ds_clients["main"].execute_query("select 1")
    ds_clients["main"].execute_query("select 2")
    return first
"""
    with pytest.raises(UsageLimitExceeded):
        _run(executor.execute_code_async(code=code, ds_clients={"main": client}, excel_files=[]))
    assert client.calls == 1
    # Data-query metering is buffered during execution and persisted by the
    # owner's end-of-run flush (agent_v2 does this in a finally block, even
    # when the run raises). Simulate that flush here before asserting counters.
    _run(usage_ctx.flush())
    assert _run(_counter_used(
        org_id,
        user_id,
        METRIC_DATA_QUERIES,
        scope_type=SCOPE_CONNECTION,
        scope_ref_id=conn_id,
    )) == 1


@pytest.mark.e2e
def test_query_preview_enforces_connection_query_quota(monkeypatch, create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(_create_connection(org_id, "warehouse"))
    _run(_create_policy(
        org_id,
        name="Query preview cap",
        monthly_query_limit=2,
        assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
    ))
    query_id = _run(_create_query_context(org_id, user_id))
    client = _FakeQueryClient(conn_id)

    async def fake_construct_clients(self, db, data_source, current_user=None):
        return {"main": client}

    monkeypatch.setattr(
        "app.services.data_source_service.DataSourceService.construct_clients",
        fake_construct_clients,
    )

    code = """
def generate_df(ds_clients, excel_files):
    return ds_clients["main"].execute_query("select 1")
"""

    async def run_previews():
        service = QueryService()
        async with async_session_maker() as db:
            first = await service.preview_query_code(
                db,
                query_id,
                QueryRunRequest(code=code),
                organization_id=org_id,
                user_id=user_id,
            )
            second = await service.preview_query_code(
                db,
                query_id,
                QueryRunRequest(code=code),
                organization_id=org_id,
                user_id=user_id,
            )
            third = await service.preview_query_code(
                db,
                query_id,
                QueryRunRequest(code=code),
                organization_id=org_id,
                user_id=user_id,
            )
            return first, second, third

    first, second, third = _run(run_previews())
    assert first["preview"] is not None
    assert second["preview"] is not None
    assert "Monthly usage quota exceeded" in third["error"]
    assert client.calls == 2
    assert _run(_counter_used(
        org_id,
        user_id,
        METRIC_DATA_QUERIES,
        scope_type=SCOPE_CONNECTION,
        scope_ref_id=conn_id,
    )) == 2


@pytest.mark.e2e
def test_execute_query_records_result_bytes_and_blocks_large_results(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(_create_connection(org_id, "warehouse"))
    sample_df = pd.DataFrame({"x": ["alpha", "beta"]})
    sample_bytes = estimate_result_size_bytes(sample_df)
    _run(_create_policy(
        org_id,
        name="Data size cap",
        monthly_token_limit=None,
        monthly_query_limit=10,
        monthly_data_bytes_limit=sample_bytes,
        assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
    ))

    usage_ctx = UsageLimitContext(
        organization_id=org_id,
        user_id=user_id,
        source="inspect_data",
        source_ref_id="tool-2",
        session_maker=async_session_maker,
    )
    executor = StreamingCodeExecutor(usage_context=usage_ctx)

    class _SizedQueryClient(_FakeQueryClient):
        def execute_query(self, query):
            self.calls += 1
            if "big" in query:
                return pd.DataFrame({"x": ["alpha", "beta", "gamma"]})
            return sample_df

    client = _SizedQueryClient(conn_id)
    code = """
def generate_df(ds_clients, excel_files):
    first = ds_clients["main"].execute_query("select small")
    ds_clients["main"].execute_query("select big")
    return first
"""
    with pytest.raises(UsageLimitExceeded) as exc_info:
        _run(executor.execute_code_async(code=code, ds_clients={"main": client}, excel_files=[]))
    assert exc_info.value.metric == METRIC_DATA_BYTES
    assert client.calls == 2
    # Buffered metering is persisted by the owner's end-of-run flush; simulate it.
    _run(usage_ctx.flush())
    assert _run(_counter_used(
        org_id,
        user_id,
        METRIC_DATA_QUERIES,
        scope_type=SCOPE_CONNECTION,
        scope_ref_id=conn_id,
    )) == 2
    assert _run(_counter_used(
        org_id,
        user_id,
        METRIC_DATA_BYTES,
        scope_type=SCOPE_CONNECTION,
        scope_ref_id=conn_id,
    )) == sample_bytes


@pytest.mark.e2e
def test_usage_limits_feature_disabled_is_inert(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(_create_connection(org_id, "warehouse"))

    saved_cached = ee_license._cached_license
    saved_initialized = ee_license._cache_initialized
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True,
        tier="enterprise",
        org_name="tests",
        features=["custom_roles"],
        license_id="without-usage-limits",
    )
    ee_license._cache_initialized = True
    try:
        async def _consume():
            async with async_session_maker() as db:
                await usage_policy_service.consume_data_query(
                    db,
                    org_id=org_id,
                    user_id=user_id,
                    connection_id=conn_id,
                    source="create_data",
                )
                await db.commit()

        _run(_consume())
        assert _run(_counter_used(
            org_id,
            user_id,
            METRIC_DATA_QUERIES,
            scope_type=SCOPE_CONNECTION,
            scope_ref_id=conn_id,
        )) == 0
    finally:
        ee_license._cached_license = saved_cached
        ee_license._cache_initialized = saved_initialized
