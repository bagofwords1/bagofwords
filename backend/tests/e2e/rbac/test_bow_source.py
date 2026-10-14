"""Source queries use real permission grants, diagnosis rows, and rollups."""
import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from tests.e2e.rbac.test_diagnosis_explorer_scope import world  # shared API-seeded role world

pytestmark = pytest.mark.e2e


def run_service(world, actor, request, callback=None):
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.bow_source_service import BowSourceService

    async def run():
        url = os.environ["TEST_DATABASE_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1).replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                user = await db.get(User, world[actor]["user_id"])
                org = await db.get(Organization, world["org_id"])
                df = await BowSourceService().query(db, org, user, request)
                if callback:
                    await callback(db, user, df)
                return df
        finally:
            await engine.dispose()
    return asyncio.run(run())


@pytest.mark.parametrize("query", ["", "revenue", "tool:create_data tool.status:error", "NOT status:success"])
def test_manager_queries_never_expand_scope(world, query):
    df = run_service(world, "manager_b", {"dataset": "runs", "query": query})
    assert set(df.run_id) <= set(world["ids"]["b"])
    if not query:
        assert set(df.run_id) == set(world["ids"]["b"])
        assert all(names == [world["ds_b"]["name"]] for names in df.agent_names)


def test_aggregates_count_the_scoped_dataset(world):
    df = run_service(world, "manager_b", {"dataset": "runs", "group_by": ["status"], "metrics": [{"op": "count", "name": "runs"}]})
    assert df.runs.sum() == len(world["ids"]["b"])


def test_call_query_returns_only_matching_authorized_calls(world):
    df = run_service(world, "manager_b", {"dataset": "tool_calls", "query": "tool:create_data tool.status:error"})
    assert len(df) > 0
    assert set(df.run_id) <= set(world["ids"]["b"])
    assert set(df.tool) == {"create_data"}
    assert set(df.status) == {"error"}


def test_members_cannot_query_monitoring_data(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        run_service(world, "member", {"dataset": "runs"})
    assert exc.value.status_code == 403


def test_admin_rows_have_stable_ids_and_preserve_unknown_cost(world):
    from pandas.api.types import is_numeric_dtype
    df = run_service(world, "admin", {"dataset": "runs"})
    assert set(df.run_id) == {i for ids in world["ids"].values() for i in ids}
    assert df.cost_usd.isna().all()
    assert is_numeric_dtype(df.cost_usd)


def test_explicit_top_n_and_column_projection(world):
    df = run_service(world, "admin", {"dataset": "runs", "columns": ["run_id", "agent_names"], "limit": 2})
    assert len(df) == 2
    assert list(df.columns) == ["run_id", "agent_names"]


def test_agent_column_projection_does_not_drop_unassigned_runs(world):
    df = run_service(world, "admin", {"dataset": "runs", "columns": ["run_id", "agent_id", "agent_name"]})
    assert set(df.run_id) == {i for ids in world["ids"].values() for i in ids}


@pytest.mark.parametrize("request_mode,actor,visible", [("training", "manager_b", True), ("chat", "admin", False), ("training", "member", False)])
def test_bow_discovery_uses_current_request_mode_and_manage_access(world, test_client, request_mode, actor, visible):
    headers = {"Authorization": f"Bearer {world['admin']['token']}", "X-Organization-Id": world['org_id']}
    response = test_client.post('/api/reports', json={"title": "Source discovery"}, headers=headers)
    assert response.status_code == 200, response.text
    report_id = response.json()['id']

    async def verify(db, _user, _df):
        from app.models.report import Report
        from app.models.organization import Organization
        from app.models.user import User
        from app.ai.context.context_hub import ContextHub
        report = await db.get(Report, report_id)
        # A mode change can precede persistence of the report preference.
        report.mode = 'chat' if request_mode == 'training' else 'training'
        context = ContextHub(db, await db.get(Organization, world['org_id']), report, [],
            user=await db.get(User, world[actor]['user_id']), mode=request_mode)
        schema = await context.schema_builder.build(data_source_ids=['builtin:bow'], table_names=['bow.runs'])
        bow = [source for source in schema.data_sources if source.info.id == 'builtin:bow']
        assert bool(bow) is visible
        if visible:
            assert [table.name for table in bow[0].tables] == ['bow.runs']
        await db.rollback()
    run_service(world, 'admin', {"dataset":"runs"}, verify)


def test_monitoring_snapshot_requires_its_original_scope(world):
    from app.services.bow_source_access import can_read
    from app.models.user import User

    async def verify(db, user, df):
        access = dict(df.attrs["bow_source"])
        assert await can_read(db, access, user)
        assert not await can_read(db, access, None)
        member = await db.get(User, world["member"]["user_id"])
        assert not await can_read(db, access, member)
        access["scope_ids"] = [world["ds_a"]["id"], world["ds_b"]["id"]]
        assert not await can_read(db, access, user)
    run_service(world, "manager_b", {"dataset": "runs"}, verify)


@pytest.mark.parametrize("actor", ["admin", "manager_b"])
def test_saved_query_executes_bow_and_reloads_as_an_ordinary_table(world, test_client, actor):
    headers = {"Authorization": f"Bearer {world[actor]['token']}", "X-Organization-Id": world["org_id"]}
    expected = sum(map(len, world["ids"].values())) if actor == "admin" else len(world["ids"]["b"])
    response = test_client.post("/api/reports", json={"title": "Training source artifact", "mode": "training"}, headers=headers)
    assert response.status_code in (200, 201), response.text
    report = response.json()
    updated = test_client.put(f"/api/reports/{report['id']}", json={"mode": "training"}, headers=headers)
    assert updated.status_code == 200, updated.text
    response = test_client.post("/api/queries", json={"title": "Run counts", "report_id": report["id"]}, headers=headers)
    assert response.status_code == 200, response.text
    query = response.json()
    code = '''def generate_df(ds_clients, excel_files):
    return ds_clients["bow"].execute_query({"dataset":"runs","group_by":["status"],"metrics":[{"op":"count","name":"runs"}]})
'''
    response = test_client.post(f"/api/queries/{query['id']}/run", json={"code": code}, headers=headers)
    assert response.status_code == 200, response.text
    assert not response.json().get("error"), response.text
    saved = test_client.get(f"/api/queries/{query['id']}", headers=headers)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["source_refs"][0]["id"] == "builtin:bow"
    assert sum(r["runs"] for r in body["default_step"]["data"]["rows"]) == expected
    # An unrelated manager cannot read the admin's materialized snapshot, even with the query ID.
    other = {**headers, "Authorization": f"Bearer {world['member']['token']}"}
    assert test_client.get(f"/api/queries/{query['id']}", headers=other).status_code == 403
    rerun = test_client.post(f"/api/queries/{query['id']}/run", json={"mode": "viewer"}, headers=headers)
    assert rerun.status_code == 200, rerun.text
    assert sum(r["runs"] for r in rerun.json()["data"]["rows"]) == expected

    for share_type in ("artifact", "conversation"):
        published = test_client.put(f"/api/reports/{report['id']}/visibility/{share_type}",
            json={"visibility": "public"}, headers=headers)
        assert published.status_code == 403, published.text
    entity = test_client.post(f"/api/entities/from_step/{body['default_step']['id']}",
        json={"title":"Saved BOW counts", "publish":False}, headers=headers)
    assert entity.status_code == 200, entity.text
    refreshed = test_client.post(f"/api/entities/{entity.json()['id']}/run", json={}, headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    assert sum(row['runs'] for row in refreshed.json()['data']['rows']) == expected
    denied = test_client.get(f"/api/entities/{entity.json()['id']}", headers=other)
    assert denied.status_code in (403, 404), denied.text
    visible = test_client.get("/api/entities", headers=other)
    assert visible.status_code == 200, visible.text
    assert entity.json()['id'] not in {e['id'] for e in visible.json()}
    if actor == "manager_b":
        admin_headers = {**headers, "Authorization": f"Bearer {world['admin']['token']}"}
        grants = test_client.get(f"/api/organizations/{world['org_id']}/resource-grants", headers=admin_headers)
        assert grants.status_code == 200, grants.text
        for grant in grants.json():
            if grant['principal_id'] == world[actor]['user_id']:
                deleted = test_client.delete(f"/api/organizations/{world['org_id']}/resource-grants/{grant['id']}", headers=admin_headers)
                assert deleted.status_code == 204, deleted.text
        # Ownership and a previously successful cached viewer run do not survive revocation.
        assert test_client.get(f"/api/queries/{query['id']}", headers=headers).status_code == 403
        assert test_client.post(f"/api/queries/{query['id']}/run", json={"mode":"viewer"}, headers=headers).status_code == 403
        assert test_client.get(f"/api/entities/{entity.json()['id']}", headers=headers).status_code in (403,404)


def test_bow_execution_completes_with_an_active_agent_write_transaction(world, test_client):
    """The agent can have uncommitted event state when generated code queries BOW."""
    headers = {"Authorization": f"Bearer {world['admin']['token']}", "X-Organization-Id": world['org_id']}
    response = test_client.post('/api/reports', json={"title": "Active execution"}, headers=headers)
    assert response.status_code == 200, response.text
    report_id = response.json()['id']

    async def execute(db, user, _):
        from app.models.report import Report
        from app.models.organization import Organization
        from app.data_sources.clients.bow_client import install_bow_client
        from app.ai.code_execution.code_execution import StreamingCodeExecutor
        report = await db.get(Report, report_id)
        org = await db.get(Organization, world['org_id'])
        clients = {}
        await install_bow_client(db, org, user, report, clients, mode='training', writer_lock=asyncio.Lock())
        # Direct ORM write models the uncommitted execution state; no HTTP API
        # can keep a transaction open across a generated-code invocation.
        report.title = 'Execution in progress'
        await db.flush()
        result, _, _ = await asyncio.wait_for(StreamingCodeExecutor().execute_code_async(
            code='def generate_df(ds_clients, excel_files):\n    return ds_clients["bow"].execute_query({"dataset":"runs","metrics":[{"op":"count","name":"runs"}]})',
            ds_clients=clients, excel_files=[]), timeout=5)
        assert result['runs'].sum() == sum(map(len, world['ids'].values()))
        assert report.bow_source_access
    run_service(world, 'admin', {"dataset":"runs"}, execute)


# Runs the rollup never indexed. "abandoned": the process died mid-turn, so the
# run is still "in_progress" past STALE_AFTER and no finish hook will ever index
# it. "unhooked": it finished on a path that skips the finish hook. Both are
# normal production history; neither may take a query down with it.
UNINDEXED = {
    "abandoned": {"status": "in_progress", "hours_ago": 2, "tools": [{"name": "create_data", "status": "success"}]},
    "unhooked": {"status": "completed", "hours_ago": 5, "tools": [{"name": "create_data", "status": "success"}]},
}


def seed_unindexed(world, seed_agent_executions, kind):
    from datetime import datetime, timedelta
    spec = UNINDEXED[kind]
    # Seeded after the world's rollup, on a report both roles can see, so it stays unindexed.
    return seed_agent_executions(world["org_id"], world["reports"]["b"], [{
        "user_id": world["admin"]["user_id"], "prompt": f"{kind} question", "status": spec["status"],
        "created_at": datetime.utcnow().replace(microsecond=0) - timedelta(hours=spec["hours_ago"]), "tools": spec["tools"]}])[0]


@pytest.mark.parametrize("kind", sorted(UNINDEXED))
@pytest.mark.parametrize("actor", ["admin", "manager_b"])
def test_unindexed_runs_never_block_a_bow_query(world, seed_agent_executions, kind, actor):
    run_id = seed_unindexed(world, seed_agent_executions, kind)

    runs = run_service(world, actor, {"dataset": "runs", "query": ""})
    assert run_id in set(runs.run_id)
    calls = run_service(world, actor, {"dataset": "tool_calls", "query": "tool:create_data"})
    assert run_id in set(calls.run_id)
    # A filter the unindexed run does not match still answers for everything else.
    other = run_service(world, actor, {"dataset": "runs", "query": "revenue"})
    assert run_id not in set(other.run_id) and len(other) > 0
    counts = run_service(world, actor, {"dataset": "runs", "group_by": ["status"], "metrics": [{"op": "count", "name": "runs"}]})
    visible = sum(map(len, world["ids"].values())) if actor == "admin" else len(world["ids"]["b"])
    assert counts.runs.sum() == visible + 1


def test_saved_bow_query_answers_while_history_is_unindexed(world, seed_agent_executions, test_client):
    """The dashboard path: a saved query over bow.tool_calls re-runs with an unindexed run in range."""
    run_id = seed_unindexed(world, seed_agent_executions, "abandoned")
    headers = {"Authorization": f"Bearer {world['admin']['token']}", "X-Organization-Id": world["org_id"]}
    report = test_client.post("/api/reports", json={"title": "Gap dashboard", "mode": "training"}, headers=headers)
    assert report.status_code in (200, 201), report.text
    assert test_client.put(f"/api/reports/{report.json()['id']}", json={"mode": "training"}, headers=headers).status_code == 200
    query = test_client.post("/api/queries", json={"title": "Calls", "report_id": report.json()["id"]}, headers=headers)
    assert query.status_code == 200, query.text
    code = '''def generate_df(ds_clients, excel_files):
    return ds_clients["bow"].execute_query({"dataset": "tool_calls", "query": "tool:create_data", "columns": ["run_id", "tool"]})
'''
    ran = test_client.post(f"/api/queries/{query.json()['id']}/run", json={"code": code}, headers=headers)
    assert ran.status_code == 200, ran.text
    assert not ran.json().get("error"), ran.text
    rerun = test_client.post(f"/api/queries/{query.json()['id']}/run", json={"mode": "viewer"}, headers=headers)
    assert rerun.status_code == 200, rerun.text
    assert run_id in {row["run_id"] for row in rerun.json()["data"]["rows"]}


@pytest.mark.parametrize("kind", sorted(UNINDEXED))
def test_scheduled_sweep_indexes_runs_left_unindexed_after_startup(world, seed_agent_executions, kind):
    """The startup sweep runs once; a run orphaned by that very restart only turns
    stale later. The scheduled sweep must index it without another restart."""
    from unittest.mock import patch
    from app.models.agent_execution import AgentExecution
    from app.services.diagnosis.rollup import count_pending
    from app.services.diagnosis.sweep import scheduled_sweep
    run_id = seed_unindexed(world, seed_agent_executions, kind)

    async def check(db, _user, _df):
        assert await count_pending(db, AgentExecution.organization_id == world["org_id"]) == 1
        with patch("app.core.scheduler.claim_scheduled_run", return_value=True):
            await scheduled_sweep(async_sessionmaker(db.bind, expire_on_commit=False))
        assert await count_pending(db, AgentExecution.organization_id == world["org_id"]) == 0
        row = await db.get(AgentExecution, run_id, populate_existing=True)
        assert row.rollup_version is not None and row.prompt_text == f"{kind} question"
    run_service(world, "admin", {"dataset": "runs", "time_range": {"relative": "1h"}}, check)


def test_scheduled_sweep_skips_a_fire_another_worker_claimed(world, seed_agent_executions):
    from unittest.mock import patch
    from app.models.agent_execution import AgentExecution
    from app.services.diagnosis.rollup import count_pending
    from app.services.diagnosis.sweep import scheduled_sweep
    seed_unindexed(world, seed_agent_executions, "unhooked")

    async def check(db, _user, _df):
        with patch("app.core.scheduler.claim_scheduled_run", return_value=False):
            assert await scheduled_sweep(async_sessionmaker(db.bind, expire_on_commit=False)) == 0
        assert await count_pending(db, AgentExecution.organization_id == world["org_id"]) == 1
    run_service(world, "admin", {"dataset": "runs", "time_range": {"relative": "1h"}}, check)


def test_sweep_indexes_runs_whose_text_the_database_rejects(world, seed_agent_executions):
    """A NUL byte (driver errors, pasted binary) is legal in the source JSON but
    not in a Postgres TEXT column. The run is indexed without it, not skipped."""
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from app.models.agent_execution import AgentExecution
    from app.services.diagnosis.rollup import count_pending
    from app.services.diagnosis.sweep import scheduled_sweep
    run_id = seed_agent_executions(world["org_id"], world["reports"]["b"], [{
        "user_id": world["admin"]["user_id"], "prompt": "pasted\x00prompt", "status": "error",
        "error": "driver said bad\x00byte", "created_at": datetime.utcnow() - timedelta(hours=3)}])[0]

    async def check(db, _user, _df):
        with patch("app.core.scheduler.claim_scheduled_run", return_value=True):
            await scheduled_sweep(async_sessionmaker(db.bind, expire_on_commit=False))
        assert await count_pending(db, AgentExecution.organization_id == world["org_id"]) == 0
        row = await db.get(AgentExecution, run_id, populate_existing=True)
        assert "\x00" not in row.error_text and "bad" in row.error_text and "byte" in row.error_text
        assert "\x00" not in row.prompt_text and "pasted" in row.prompt_text
    run_service(world, "admin", {"dataset": "runs", "time_range": {"relative": "1h"}}, check)


def test_one_run_the_sweep_cannot_index_does_not_hold_back_the_rest(world, seed_agent_executions):
    """Any run whose rollup cannot be computed is skipped alone; every other
    pending run in its batch and after it is still indexed, and queries answer."""
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from app.models.agent_execution import AgentExecution
    from app.services.diagnosis.rollup import count_pending
    from app.services.diagnosis.sweep import scheduled_sweep
    now = datetime.utcnow().replace(microsecond=0)

    def seed(prompt, hours, **extra):
        return seed_agent_executions(world["org_id"], world["reports"]["b"], [{
            "user_id": world["admin"]["user_id"], "prompt": prompt, "status": "completed",
            "created_at": now - timedelta(hours=hours), **extra}])[0]

    newer = seed("newer ok", 2)
    broken = seed("broken usage", 3, token_usage_json={"total_tokens": "n/a"})
    older = seed("older ok", 4)

    async def check(db, _user, _df):
        with patch("app.core.scheduler.claim_scheduled_run", return_value=True):
            await scheduled_sweep(async_sessionmaker(db.bind, expire_on_commit=False))
        for run_id in (newer, older):
            row = await db.get(AgentExecution, run_id, populate_existing=True)
            assert row.rollup_version is not None, run_id
        assert await count_pending(db, AgentExecution.organization_id == world["org_id"]) <= 1
    run_service(world, "admin", {"dataset": "runs", "time_range": {"relative": "1h"}}, check)
    df = run_service(world, "admin", {"dataset": "runs", "query": ""})
    assert {newer, broken, older} <= set(df.run_id)
