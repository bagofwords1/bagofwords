import asyncio
import uuid

import pandas as pd
import pytest
from sqlalchemy import select

from app.ai.code_execution.code_execution import StreamingCodeExecutor, estimate_result_size_bytes
from app.ai.llm.llm import LLM
from app.ai.llm.types import LLMResponse, LLMUsage
from app.dependencies import async_session_maker
from app.ee import license as ee_license
from app.models.connection import Connection
from app.models.usage_policy import UsageCounter
from app.schemas.usage_policy_schema import (
    UsagePolicyAssignmentInput,
    UsagePolicyConnectionOverrideInput,
    UsagePolicyCreate,
)
from app.services.usage_policy_service import (
    METRIC_DATA_BYTES,
    METRIC_DATA_QUERIES,
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


def _bootstrap_admin(create_user, login_user, whoami):
    user = create_user(email=f"usage_{uuid.uuid4().hex[:8]}@test.com")
    token = login_user(user["email"], user["password"])
    profile = whoami(token)
    org_id = profile["organizations"][0]["id"]
    return token, org_id, profile["id"]


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
    assert _quota_llm(org_id, user_id, success_client).inference("hello") == "ok"
    assert success_client.calls == 1
    assert _run(_counter_used(org_id, user_id, METRIC_LLM_TOKENS)) == 5

    failing_client = _FailingLLMClient()
    with pytest.raises(RuntimeError):
        _quota_llm(org_id, user_id, failing_client).inference("hello")
    assert failing_client.calls == 1
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
    assert _run(_counter_used(
        org_id,
        user_id,
        METRIC_DATA_QUERIES,
        scope_type=SCOPE_CONNECTION,
        scope_ref_id=conn_id,
    )) == 1


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
