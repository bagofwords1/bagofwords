"""bow.runs splits tokens into input / output / cache read with one meaning
for every provider: input includes cached tokens, cache read is a subset of
input, and input + output is the run's token total.

Providers disagree on the wire: the Anthropic Messages API (anthropic, and
Claude on Vertex or Azure) and Bedrock Converse report cache reads/writes beside
the prompt count, while OpenAI-shaped clients (openai, Azure OpenAI
deployments, and custom gateways such as LiteLLM — even when they front a
Claude model) fold them into it. The Cost console follows the same rule.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.e2e

NOW = datetime.utcnow().replace(microsecond=0)


def _usage(provider, model, prompt, completion, cache_read=0, cache_write=0, scope="planner"):
    return {"provider": provider, "model": model, "prompt_tokens": prompt, "completion_tokens": completion,
            "cache_read_tokens": cache_read, "cache_creation_tokens": cache_write, "cost": 0.01, "scope": scope}


# (label, usage rows, expected input, expected output, expected cache read)
CASES = [
    ("anthropic", [_usage("anthropic", "claude-haiku-4-5", 1_000, 200, cache_read=4_000, cache_write=500)], 5_500, 200, 4_000),
    ("bedrock_non_claude", [_usage("bedrock", "amazon.nova-pro-v1:0", 300, 50, cache_read=700)], 1_000, 50, 700),
    ("vertex_claude", [_usage("vertex", "claude-sonnet-4-5", 10, 5, cache_read=90, cache_write=20)], 120, 5, 90),
    ("openai", [_usage("openai", "gpt-6-luna", 6_000, 400, cache_read=2_500)], 6_000, 400, 2_500),
    ("azure", [_usage("azure", "gpt-4.1", 800, 70, cache_read=128)], 800, 70, 128),
    ("azure_claude", [_usage("azure", "claude-haiku-4-5", 200, 10, cache_read=1_800, cache_write=100)], 2_100, 10, 1_800),
    ("custom_gateway_claude", [_usage("custom", "claude-haiku-4-5", 3_000, 100, cache_read=1_200)], 3_000, 100, 1_200),
    ("google", [_usage("google", "gemini-2.5-flash", 900, 30)], 900, 30, 0),
    ("mixed", [_usage("anthropic", "claude-haiku-4-5", 2_000, 300, cache_read=8_000),
               _usage("openai", "gpt-4.1-mini", 1_500, 60, cache_read=1_024, scope="tool_call_judge")], 11_500, 360, 9_024),
]


@pytest.fixture
def token_world(create_user, login_user, whoami, create_report, seed_agent_executions, rollup_agent_executions):
    owner = create_user()
    token = login_user(owner["email"], owner["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    report = create_report(title=f"Tokens {uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    runs = [dict(user_id=info["id"], prompt=f"run {label}", created_at=NOW - timedelta(hours=i + 1),
                 status="success", duration_ms=1_000, usage=usage)
            for i, (label, usage, *_rest) in enumerate(CASES)]
    # A run from before usage was attributed per call: only the planner's own
    # count exists, so cache is unknown rather than zero.
    runs.append(dict(user_id=info["id"], prompt="legacy run", created_at=NOW - timedelta(hours=20),
                     status="success", duration_ms=1_000,
                     token_usage_json={"prompt_tokens": 700, "completion_tokens": 40, "total_tokens": 740}))
    ids = seed_agent_executions(org_id, report["id"], runs)
    rollup_agent_executions()
    return {"org_id": org_id, "user_id": info["id"], "ids": ids, "token": token}


def _headers(world):
    return {"Authorization": f"Bearer {world['token']}", "X-Organization-Id": world["org_id"]}


def _query(world, request):
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.bow_source_service import BowSourceService

    async def run():
        url = os.environ["TEST_DATABASE_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1).replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                user = await db.get(User, world["user_id"])
                org = await db.get(Organization, world["org_id"])
                return await BowSourceService().query(db, org, user, request)
        finally:
            await engine.dispose()
    return asyncio.run(run())


def test_runs_split_tokens_the_same_way_for_every_provider(token_world):
    df = _query(token_world, {"dataset": "runs", "columns": [
        "run_id", "tokens", "input_tokens", "output_tokens", "cache_read_tokens"]}).set_index("run_id")
    for run_id, (label, _usage_rows, expected_in, expected_out, expected_cache) in zip(token_world["ids"], CASES):
        row = df.loc[run_id]
        assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (expected_in, expected_out, expected_cache), label
        assert row.input_tokens + row.output_tokens == row.tokens, label
        assert row.cache_read_tokens <= row.input_tokens, label


def test_runs_without_per_call_usage_report_unknown_cache(token_world):
    df = _query(token_world, {"dataset": "runs", "columns": [
        "run_id", "tokens", "input_tokens", "output_tokens", "cache_read_tokens"]}).set_index("run_id")
    legacy = df.loc[token_world["ids"][-1]]
    assert (legacy.input_tokens, legacy.output_tokens, legacy.tokens) == (700, 40, 740)
    assert legacy.isna()["cache_read_tokens"]


def test_token_split_columns_aggregate(token_world):
    df = _query(token_world, {"dataset": "runs", "metrics": [
        {"op": "sum", "field": "input_tokens", "name": "input"},
        {"op": "sum", "field": "cache_read_tokens", "name": "cached"},
        {"op": "sum", "field": "tokens", "name": "total"},
        {"op": "sum", "field": "output_tokens", "name": "output"}]})
    assert int(df.input[0]) == sum(c[2] for c in CASES) + 700
    assert int(df.cached[0]) == sum(c[4] for c in CASES)
    assert int(df.input[0]) + int(df.output[0]) == int(df.total[0])


def _ids_matching(test_client, world, q):
    resp = test_client.get(
        "/api/console/diagnosis/runs",
        params={"q": q, "start": (NOW - timedelta(days=2)).isoformat() + "Z", "end": (NOW + timedelta(days=1)).isoformat() + "Z", "limit": 100},
        headers={"Authorization": f"Bearer {world['token']}", "X-Organization-Id": world["org_id"]},
    )
    assert resp.status_code == 200, resp.json()
    return {item["id"] for item in resp.json()["items"]}


@pytest.mark.parametrize("field, index", [("tokens.in", 2), ("tokens.out", 3), ("tokens.cache_read", 4)])
@pytest.mark.parametrize("threshold", [100, 2_500, 5_000])
def test_explorer_filters_on_the_same_split(test_client, token_world, field, index, threshold):
    expected = {run_id for run_id, case in zip(token_world["ids"], CASES) if case[index] >= threshold}
    # The legacy run has input/output counts but unknown cache, so it never matches a cache filter.
    legacy = {"tokens.in": 700, "tokens.out": 40}.get(field)
    if legacy is not None and legacy >= threshold:
        expected.add(token_world["ids"][-1])
    assert _ids_matching(test_client, token_world, f"{field}:>={threshold}") == expected


def test_split_fields_are_offered_in_the_filter_builder(test_client, token_world):
    resp = test_client.get("/api/console/diagnosis/fields",
                           headers={"Authorization": f"Bearer {token_world['token']}", "X-Organization-Id": token_world["org_id"]})
    assert resp.status_code == 200
    fields = {f["name"]: f for f in resp.json()["fields"]}
    for name in ("tokens.in", "tokens.out", "tokens.cache_read"):
        assert fields[name]["builder"] and fields[name]["type"] == "number"


def test_cost_console_totals_agree_with_the_run_split(test_client, token_world):
    """The console totals the same usage rows; a cached prefix must be counted
    once whichever wire format reported it."""
    resp = test_client.get("/api/console/metrics/llm-usage", headers=_headers(token_world), params={
        "start_date": (NOW - timedelta(days=2)).isoformat(), "end_date": (NOW + timedelta(days=1)).isoformat()})
    assert resp.status_code == 200, resp.json()
    expected = sum(c[2] + c[3] for c in CASES)
    assert sum(i["total_tokens"] for i in resp.json()["items"]) == expected
