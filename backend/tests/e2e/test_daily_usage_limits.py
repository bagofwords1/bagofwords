"""Daily usage caps, alongside the monthly ones.

The promise: every metric (LLM tokens, LLM spend, data queries, data scanned)
can be capped per day, per month, both, or neither. A request is blocked when
any cap it would pass is reached. The day is midnight to midnight in the
organization's timezone, so yesterday's usage never counts against today.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.ee import license as ee_license
from app.models.connection import Connection
from app.models.organization_settings import OrganizationSettings
from app.models.usage_policy import UsageCounter, UsagePolicy
from app.schemas.usage_policy_schema import UsagePolicyAssignmentInput, UsagePolicyCreate
from app.services.usage_policy_service import (
    METRIC_DATA_BYTES,
    METRIC_DATA_QUERIES,
    METRIC_LLM_COST,
    METRIC_LLM_TOKENS,
    SCOPE_ORGANIZATION,
    WINDOW_DAILY,
    WINDOW_MONTHLY,
    UsageLimitContext,
    UsageLimitExceeded,
    current_day_window,
    daily_metric,
    usage_policy_service,
)


def _run(coro):
    return asyncio.run(coro)


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


@pytest.fixture(autouse=True)
def _enable_usage_limits_license():
    saved = (ee_license._cached_license, ee_license._cache_initialized)
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="tests",
        features=["usage_limits", "custom_roles"], license_id="test-daily-usage-limits",
    )
    ee_license._cache_initialized = True
    yield
    ee_license._cached_license, ee_license._cache_initialized = saved


def _bootstrap(create_user, login_user, whoami):
    user = create_user(email=f"daily_{uuid.uuid4().hex[:8]}@test.com")
    token = login_user(user["email"], user["password"])
    profile = whoami(token)
    return token, profile["organizations"][0]["id"], profile["id"]


def _assign(org_id, user_id, **limits):
    async def _create():
        async with async_session_maker() as db:
            return await usage_policy_service.create_policy(db, org_id, UsagePolicyCreate(
                name=f"policy-{uuid.uuid4().hex[:6]}",
                assignments=[UsagePolicyAssignmentInput(principal_type="user", principal_id=user_id)],
                **limits,
            ))
    return _run(_create())


def _set_org_timezone(org_id, tz_name):
    async def _set():
        async with async_session_maker() as db:
            row = (await db.execute(
                select(OrganizationSettings).where(OrganizationSettings.organization_id == org_id)
            )).scalars().first()
            row.config = {**(row.config or {}), "timezone": tz_name}
            await db.commit()
    _run(_set())


def _create_connection(org_id, name):
    async def _create():
        async with async_session_maker() as db:
            conn = Connection(organization_id=org_id, name=name, type="sqlite", config={}, credentials=None)
            db.add(conn)
            await db.commit()
            await db.refresh(conn)
            return str(conn.id)
    return _run(_create())


def _ctx(org_id, user_id):
    return UsageLimitContext(
        organization_id=org_id, user_id=user_id, source="agent",
        source_ref_id="completion-1", session_maker=async_session_maker,
    )


def _record(org_id, user_id, *, tokens=0, cost_micro=0, query_events=()):
    """Record usage through the same buffered path an agent run uses."""
    async def _go():
        ctx = _ctx(org_id, user_id)
        ctx.add_tokens(tokens)
        ctx.add_cost(cost_micro)
        for connection_id, metric, amount in query_events:
            if metric == METRIC_DATA_QUERIES:
                ctx.add_data_query(connection_id)
            else:
                ctx.add_data_bytes(connection_id, amount)
        await ctx.flush()
    _run(_go())


def _daily_counter(org_id, user_id, metric, tz_name="UTC", now=None):
    day_start, _ = current_day_window(tz_name, now)

    async def _get():
        async with async_session_maker() as db:
            return int((await db.execute(select(UsageCounter.used).where(
                UsageCounter.organization_id == org_id,
                UsageCounter.user_id == user_id,
                UsageCounter.metric == daily_metric(metric),
                UsageCounter.scope_type == SCOPE_ORGANIZATION,
                UsageCounter.window_start == day_start,
            ))).scalar_one_or_none() or 0)
    return _run(_get())


def _blocked(coro_factory):
    try:
        _run(coro_factory())
    except UsageLimitExceeded as exc:
        return exc
    return None


# ── policy CRUD ────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_daily_limits_round_trip_through_the_api_in_any_combination(test_client, create_user, login_user, whoami):
    token, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    base = f"/api/organizations/{org_id}/usage-policies"
    combos = [
        {"daily_token_limit": 1000},                                     # daily only
        {"monthly_token_limit": 9000},                                   # monthly only
        {"daily_token_limit": 1000, "monthly_token_limit": 9000,
         "daily_query_limit": 5, "daily_data_bytes_limit": 2048,
         "daily_spend_limit_usd": 1.25, "monthly_spend_limit_usd": 10},  # both, every metric
    ]
    for i, limits in enumerate(combos):
        res = test_client.post(base, json={"name": f"combo-{i}", **limits}, headers=_headers(token, org_id))
        assert res.status_code == 200, res.text
        body = res.json()
        for key in ("daily_token_limit", "daily_query_limit", "daily_data_bytes_limit",
                    "daily_spend_limit_usd", "monthly_token_limit", "monthly_spend_limit_usd"):
            assert body[key] == limits.get(key), key

    # Clearing a daily cap (null) on update leaves the monthly one alone.
    created = test_client.get(base, headers=_headers(token, org_id)).json()
    both = next(p for p in created if p["name"] == "combo-2")
    res = test_client.put(f"{base}/{both['id']}", json={"daily_token_limit": None}, headers=_headers(token, org_id))
    assert res.status_code == 200
    assert res.json()["daily_token_limit"] is None
    assert res.json()["monthly_token_limit"] == 9000


@pytest.mark.e2e
@pytest.mark.parametrize("daily_key,monthly_key,daily,monthly", [
    ("daily_token_limit", "monthly_token_limit", 501, 500),
    ("daily_query_limit", "monthly_query_limit", 11, 10),
    ("daily_data_bytes_limit", "monthly_data_bytes_limit", 2049, 2048),
    ("daily_spend_limit_usd", "monthly_spend_limit_usd", 5.01, 5.0),
])
def test_daily_limit_above_monthly_is_rejected_on_create_and_on_update(
    test_client, create_user, login_user, whoami, daily_key, monthly_key, daily, monthly,
):
    token, org_id, _ = _bootstrap(create_user, login_user, whoami)
    base = f"/api/organizations/{org_id}/usage-policies"
    res = test_client.post(base, json={"name": "bad", daily_key: daily, monthly_key: monthly},
                           headers=_headers(token, org_id))
    assert res.status_code == 422

    # Valid on create (equal), then an update that lowers only the monthly cap
    # must be checked against the stored daily cap.
    ok = test_client.post(base, json={"name": "ok", daily_key: monthly, monthly_key: monthly},
                          headers=_headers(token, org_id))
    assert ok.status_code == 200, ok.text
    res = test_client.put(f"{base}/{ok.json()['id']}", json={monthly_key: monthly / 2},
                          headers=_headers(token, org_id))
    assert res.status_code == 422


@pytest.mark.e2e
def test_effective_daily_limits_take_the_most_restrictive_policy_and_org_timezone(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    _set_org_timezone(org_id, "Asia/Jerusalem")
    _assign(org_id, user_id, daily_token_limit=300, daily_spend_limit_usd=2.0)

    async def _resolve():
        async with async_session_maker() as db:
            return await usage_policy_service.resolve_effective_limits(db, org_id, user_id)
    limits = _run(_resolve())
    assert limits.daily_token_limit == 300
    assert limits.daily_spend_micro_usd == 2_000_000
    assert limits.daily_query_limit is None and limits.daily_data_bytes_limit is None
    assert limits.timezone == "Asia/Jerusalem"

    # A user inheriting several policies (groups/roles) gets the tightest cap
    # per metric; an empty cap never loosens another policy's.
    composed = usage_policy_service._compose_limits(org_id, user_id, [
        UsagePolicy(id="a", daily_token_limit=500, daily_spend_limit_usd=2.0, connection_overrides=[]),
        UsagePolicy(id="b", daily_token_limit=300, daily_query_limit=7, connection_overrides=[]),
    ], "inherited", "Asia/Jerusalem")
    assert (composed.daily_token_limit, composed.daily_query_limit) == (300, 7)
    assert composed.daily_spend_micro_usd == 2_000_000
    assert composed.daily_data_bytes_limit is None


# ── enforcement, one test per metric, every window combination ─────────────

WINDOWS = ["daily", "monthly", "both"]


def _limits_for(window, daily_key, monthly_key, value):
    if window == "daily":
        return {daily_key: value}
    if window == "monthly":
        return {monthly_key: value}
    # Both set: the daily cap is the tighter one and must be what blocks.
    return {daily_key: value, monthly_key: value * 10}


@pytest.mark.e2e
@pytest.mark.parametrize("window", WINDOWS)
def test_token_caps_block_the_next_llm_call_once_reached(create_user, login_user, whoami, window):
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    _assign(org_id, user_id, **_limits_for(window, "daily_token_limit", "monthly_token_limit", 1000))

    _record(org_id, user_id, tokens=990)
    assert _blocked(lambda: _ctx(org_id, user_id).check_tokens(10)) is None  # exactly at the cap is fine

    exc = _blocked(lambda: _ctx(org_id, user_id).check_tokens(11))
    assert exc is not None and exc.metric == METRIC_LLM_TOKENS
    assert exc.window == (WINDOW_MONTHLY if window == "monthly" else WINDOW_DAILY)
    assert exc.limit == 1000 and exc.used == 990


@pytest.mark.e2e
@pytest.mark.parametrize("window", WINDOWS)
def test_spend_caps_block_once_the_budget_is_spent(create_user, login_user, whoami, window):
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    _assign(org_id, user_id, **_limits_for(window, "daily_spend_limit_usd", "monthly_spend_limit_usd", 1.0))

    _record(org_id, user_id, cost_micro=999_999)
    assert _blocked(lambda: _ctx(org_id, user_id).check_tokens(10)) is None

    _record(org_id, user_id, cost_micro=1)
    exc = _blocked(lambda: _ctx(org_id, user_id).check_tokens(10))
    assert exc is not None and exc.metric == METRIC_LLM_COST
    assert exc.window == (WINDOW_MONTHLY if window == "monthly" else WINDOW_DAILY)


@pytest.mark.e2e
@pytest.mark.parametrize("window", ["daily", "both"])
def test_daily_query_cap_counts_every_connection_together(create_user, login_user, whoami, window):
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    conn_a = _create_connection(org_id, "a")
    conn_b = _create_connection(org_id, "b")
    _assign(org_id, user_id, **_limits_for(window, "daily_query_limit", "monthly_query_limit", 3))

    _record(org_id, user_id, query_events=[(conn_a, METRIC_DATA_QUERIES, 1)] * 2 + [(conn_b, METRIC_DATA_QUERIES, 1)])
    assert _daily_counter(org_id, user_id, METRIC_DATA_QUERIES) == 3

    for conn in (conn_a, conn_b):
        exc = _blocked(lambda: _ctx(org_id, user_id).check_data_query(conn))
        assert exc is not None and exc.window == WINDOW_DAILY and exc.metric == METRIC_DATA_QUERIES


@pytest.mark.e2e
@pytest.mark.parametrize("window", WINDOWS)
def test_data_scanned_caps_block_a_query_that_would_pass_them(create_user, login_user, whoami, window):
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    conn = _create_connection(org_id, "warehouse")
    _assign(org_id, user_id, **_limits_for(window, "daily_data_bytes_limit", "monthly_data_bytes_limit", 1000))

    _record(org_id, user_id, query_events=[(conn, METRIC_DATA_BYTES, 900)])
    assert _blocked(lambda: _ctx(org_id, user_id).check_data_bytes(conn, 100)) is None
    exc = _blocked(lambda: _ctx(org_id, user_id).check_data_bytes(conn, 101))
    assert exc is not None and exc.metric == METRIC_DATA_BYTES
    assert exc.window == (WINDOW_MONTHLY if window == "monthly" else WINDOW_DAILY)


@pytest.mark.e2e
def test_in_run_usage_counts_toward_the_daily_cap_before_it_is_flushed(create_user, login_user, whoami):
    """One long agent run must not overshoot a daily cap just because its usage
    is still buffered."""
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    conn = _create_connection(org_id, "warehouse")
    _assign(org_id, user_id, daily_token_limit=100, daily_query_limit=2)

    async def _run_agent():
        ctx = _ctx(org_id, user_id)
        await ctx.check_tokens(60)
        ctx.add_tokens(60)
        await ctx.check_data_query(conn)
        ctx.add_data_query(conn)
        await ctx.check_data_query(conn)
        ctx.add_data_query(conn)
        with pytest.raises(UsageLimitExceeded) as tokens_exc:
            await ctx.check_tokens(41)
        with pytest.raises(UsageLimitExceeded) as query_exc:
            await ctx.check_data_query(conn)
        await ctx.flush()
        return tokens_exc.value, query_exc.value
    tokens_exc, query_exc = _run(_run_agent())
    assert tokens_exc.window == query_exc.window == WINDOW_DAILY
    assert _daily_counter(org_id, user_id, METRIC_LLM_TOKENS) == 60
    assert _daily_counter(org_id, user_id, METRIC_DATA_QUERIES) == 2


@pytest.mark.e2e
def test_when_both_windows_are_exhausted_the_monthly_one_is_reported(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    _assign(org_id, user_id, daily_token_limit=100, monthly_token_limit=100)
    _record(org_id, user_id, tokens=100)
    exc = _blocked(lambda: _ctx(org_id, user_id).check_tokens(1))
    assert exc.window == WINDOW_MONTHLY  # it lasts longer, so it's the useful answer


@pytest.mark.e2e
def test_yesterdays_usage_does_not_count_against_today(create_user, login_user, whoami):
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    _set_org_timezone(org_id, "America/Los_Angeles")
    _assign(org_id, user_id, daily_token_limit=100)

    async def _spend_yesterday():
        yesterday = current_day_window("America/Los_Angeles", datetime.utcnow() - timedelta(days=1))
        async with async_session_maker() as db:
            await usage_policy_service._increment_counter(
                db, org_id=org_id, user_id=user_id, metric=daily_metric(METRIC_LLM_TOKENS),
                scope_type=SCOPE_ORGANIZATION, scope_ref_id="", amount=100, limit=None,
                enforce_limit=False, window=yesterday,
            )
            await db.commit()
    _run(_spend_yesterday())
    assert _blocked(lambda: _ctx(org_id, user_id).check_tokens(100)) is None


@pytest.mark.e2e
def test_a_running_context_starts_the_new_day_at_zero(create_user, login_user, whoami):
    """A context that loaded its cache before midnight must not keep applying
    yesterday's total after it (the 30s cache TTL alone wouldn't save it if a
    refresh happened to land just before midnight)."""
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    _assign(org_id, user_id, daily_token_limit=100)
    _record(org_id, user_id, tokens=100)

    async def _scenario():
        ctx = _ctx(org_id, user_id)
        with pytest.raises(UsageLimitExceeded):
            await ctx.check_tokens(1)
        # Simulate midnight passing: today's counter is (genuinely) empty in
        # the new window and the cached window has ended.
        ctx._cached_day_end = datetime.utcnow() - timedelta(seconds=1)
        ctx._cached_daily_used = 100
        real_window = current_day_window

        import app.services.usage_policy_service as svc
        svc.current_day_window = lambda tz=None, now=None: real_window(tz, (now or datetime.utcnow()) + timedelta(days=1))
        try:
            await ctx.check_tokens(1)  # must not raise
        finally:
            svc.current_day_window = real_window
    _run(_scenario())


@pytest.mark.e2e
def test_quota_summary_reports_todays_usage_in_the_org_timezone(create_user, login_user, whoami):
    token, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    conn = _create_connection(org_id, "warehouse")
    _set_org_timezone(org_id, "Pacific/Kiritimati")
    _assign(org_id, user_id, daily_token_limit=1000, daily_query_limit=10,
            daily_data_bytes_limit=4096, daily_spend_limit_usd=2.0, monthly_token_limit=5000)
    _record(org_id, user_id, tokens=250, cost_micro=500_000,
            query_events=[(conn, METRIC_DATA_QUERIES, 1), (conn, METRIC_DATA_BYTES, 1024)])

    quota = next(o for o in whoami(token)["organizations"] if o["id"] == org_id)["usage_quota"]
    daily = quota["daily"]
    start, end = current_day_window("Pacific/Kiritimati")
    assert daily["timezone"] == "Pacific/Kiritimati"
    assert (daily["window_start"], daily["window_end"]) == (start.isoformat(), end.isoformat())
    assert daily["tokens"] == {"used": 250, "limit": 1000, "remaining": 750, "percent": 25.0}
    assert daily["queries"]["used"] == 1 and daily["queries"]["limit"] == 10
    assert daily["data_bytes"]["used"] == 1024 and daily["data_bytes"]["percent"] == 25.0
    assert daily["spend"]["used"] == 0.5 and daily["spend"]["limit"] == 2.0
    # The monthly side is untouched by the daily addition.
    assert quota["tokens"]["used"] == 250 and quota["tokens"]["limit"] == 5000


@pytest.mark.e2e
def test_direct_consume_path_enforces_the_daily_data_caps(create_user, login_user, whoami):
    """consume_data_query/consume_data_bytes (the unbuffered path the query
    rerun route uses) enforce the daily cap too, not just the monthly one."""
    _, org_id, user_id = _bootstrap(create_user, login_user, whoami)
    conn = _create_connection(org_id, "warehouse")
    _assign(org_id, user_id, daily_query_limit=1, daily_data_bytes_limit=100)

    async def _consume(kind, **kw):
        async with async_session_maker() as db:
            fn = usage_policy_service.consume_data_query if kind == "q" else usage_policy_service.consume_data_bytes
            await fn(db, org_id=org_id, user_id=user_id, connection_id=conn, **kw)
            await db.commit()

    _run(_consume("q"))
    exc = _blocked(lambda: _consume("q"))
    assert exc is not None and exc.window == WINDOW_DAILY and exc.metric == METRIC_DATA_QUERIES
    exc = _blocked(lambda: _consume("b", amount=101))
    assert exc is not None and exc.window == WINDOW_DAILY and exc.metric == METRIC_DATA_BYTES
