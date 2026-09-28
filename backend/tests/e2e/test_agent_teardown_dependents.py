"""Tearing an agent down — unlinking one of its connections, deleting it, or
deleting its only connection — must succeed and leave nothing pointing at the
rows it removed.

These are bulk/secondary-table deletes the ORM cascade never sees, and the
referencing FKs have no ON DELETE rule, so on PostgreSQL a single leftover row
turned the whole request into a 500 (e.g. unlinking a connection whose tables
had ever been queried: table_stats_datasource_table_id_fkey; deleting an agent
that was a project default: project_data_source_association_data_source_id_fkey).
SQLite never enforces those FKs, so the invariant asserted here is the
backend-independent one: every dependent row is gone, and nothing belonging to
what stays was touched. On --db=postgres the status assertions additionally
cover the FK violations.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_agent_teardown_dependents.py -v
"""
import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.dependencies import async_session_maker
from app.models.connection_table import ConnectionTable
from app.models.data_source_connection_tool import DataSourceConnectionTool
from app.models.datasource_table import DataSourceTable
from app.models.instruction_reference import InstructionReference
from app.models.project import project_data_source_association
from app.models.report import Report
from app.models.table_feedback_event import TableFeedbackEvent
from app.models.table_stats import TableStats
from app.models.table_usage_event import TableUsageEvent
from app.models.user_data_source_overlay import UserDataSourceColumn, UserDataSourceTable
from app.project_manager import ProjectManager
from app.schemas.table_usage_schema import TableUsageEventCreate

CHINOOK = Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"

pytestmark = pytest.mark.skipif(not CHINOOK.exists(), reason=f"missing {CHINOOK}")


def _run(coro):
    return asyncio.run(coro)


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _sqlite_connection(test_client, token, org_id, name):
    r = test_client.post(
        "/api/connections",
        json={"name": name, "type": "sqlite", "config": {"database": str(CHINOOK)},
              "credentials": {}, "auth_policy": "system_only"},
        headers=_h(token, org_id),
    )
    assert r.status_code == 200, r.json()
    return r.json()["id"]


def _agent(test_client, token, org_id, connection_ids):
    r = test_client.post(
        "/api/data_sources",
        json={"name": f"agent-{uuid.uuid4().hex[:6]}", "connection_ids": connection_ids},
        headers=_h(token, org_id),
    )
    assert r.status_code == 200, r.json()
    return r.json()["id"]


async def _agent_tables(ds_id):
    """{connection_id: [datasource_table_id, ...]} for the agent."""
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(DataSourceTable.id, ConnectionTable.connection_id)
            .join(ConnectionTable, ConnectionTable.id == DataSourceTable.connection_table_id)
            .where(DataSourceTable.datasource_id == ds_id)
        )).all()
    out = {}
    for tid, cid in rows:
        out.setdefault(str(cid), []).append(str(tid))
    return out


async def _record_query(report_id, ds_id, table_rows):
    """Table usage is written only from inside the agent loop (agent_v2 after a
    successful create_data step, via ProjectManager + TableUsageService). Make
    the same calls with the table ids already resolved — both connections here
    share one catalog, so name resolution would be ambiguous — and attach a
    completion to the step so thumbs feedback can resolve its tables.
    table_rows: [(datasource_table_id, table_name)]."""
    async with async_session_maker() as db:
        report = (await db.execute(
            select(Report).options(selectinload(Report.data_sources)).where(Report.id == report_id)
        )).scalar_one()
        pm = ProjectManager()
        widget = await pm.create_widget(db, report, "q")
        step = await pm.create_step(db, "q", widget, "table")
        await pm.update_step_status(db, step, "success")
        for tid, name in table_rows:
            await pm.table_usage_service.record_usage_event(db=db, payload=TableUsageEventCreate(
                org_id=str(report.organization_id), report_id=str(report.id), data_source_id=ds_id,
                step_id=str(step.id), user_id=str(report.user_id), table_fqn=name.lower(),
                datasource_table_id=tid, source_type="sql", success=True, user_role="admin",
            ))
        completion = await pm.create_message(db, report, message="q", status="success", step=step, role="system")
        await db.commit()
        return str(completion.id), str(report.user_id)


async def _seed_overlays(ds_id, user_id, rows):
    """Per-user overlays need per-user auth against a live source; seed their
    shape directly. rows: [(connection_id | None, datasource_table_id)]."""
    async with async_session_maker() as db:
        for cid, tid in rows:
            t = UserDataSourceTable(
                data_source_id=ds_id, user_id=user_id, connection_id=cid,
                table_name=f"t_{uuid.uuid4().hex[:6]}", data_source_table_id=tid,
                is_accessible=True, status="accessible",
            )
            db.add(t)
            await db.flush()
            db.add(UserDataSourceColumn(user_data_source_table_id=t.id, column_name="c",
                                        is_accessible=True, is_masked=False))
        await db.commit()


async def _rows_referencing(table_ids):
    """Every dependent row that points at any of the given agent table ids."""
    ids = list(table_ids)
    async with async_session_maker() as db:
        found = {}
        for label, stmt in {
            "table_stats": select(TableStats.id).where(TableStats.datasource_table_id.in_(ids)),
            "table_usage_events": select(TableUsageEvent.id).where(TableUsageEvent.datasource_table_id.in_(ids)),
            "table_feedback_events": select(TableFeedbackEvent.id).where(TableFeedbackEvent.datasource_table_id.in_(ids)),
            "instruction_references": select(InstructionReference.id).where(
                InstructionReference.object_type == "datasource_table",
                InstructionReference.object_id.in_(ids)),
            "user_overlay_tables": select(UserDataSourceTable.id).where(UserDataSourceTable.data_source_table_id.in_(ids)),
        }.items():
            found[label] = len((await db.execute(stmt)).all())
        return found


@pytest.mark.e2e
@pytest.mark.parametrize("removed_index", [0, 1])
def test_unlinking_a_used_connection_removes_its_dependents_and_keeps_the_rest(
    removed_index, test_client, create_user, login_user, whoami, create_report,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    conns = [_sqlite_connection(test_client, token, org_id, f"wh-{i}-{uuid.uuid4().hex[:4]}") for i in range(2)]
    ds_id = _agent(test_client, token, org_id, conns)
    tables = _run(_agent_tables(ds_id))
    assert set(tables) == set(conns) and all(tables.values())
    removed, kept = conns[removed_index], conns[1 - removed_index]

    # Query and rate one table from EACH connection, reference both from an
    # instruction, and give the user an overlay on both (plus a legacy
    # connection-less overlay row on the removed connection's table).
    report = create_report(title="r", user_token=token, org_id=org_id, data_sources=[ds_id])
    async def _names():
        async with async_session_maker() as db:
            return {str(r.id): r.name for r in (await db.execute(
                select(DataSourceTable).where(DataSourceTable.datasource_id == ds_id))).scalars()}
    names = _run(_names())
    # Different table names per connection: stats are keyed by table name.
    picked = {conns[0]: tables[conns[0]][0]}
    picked[conns[1]] = next(t for t in tables[conns[1]] if names[t] != names[picked[conns[0]]])
    completion_id, user_id = _run(_record_query(
        report["id"], ds_id, [(picked[c], names[picked[c]]) for c in conns]))
    fb = test_client.post(f"/api/completions/{completion_id}/feedback",
                          json={"direction": -1}, headers=_h(token, org_id))
    assert fb.status_code == 200, fb.json()
    instr = test_client.post("/api/instructions", headers=_h(token, org_id), json={
        "text": "join rule", "status": "published", "category": "general", "data_source_ids": [ds_id],
        "references": [{"object_type": "datasource_table", "object_id": picked[c]} for c in conns],
    })
    assert instr.status_code == 200, instr.json()
    _run(_seed_overlays(ds_id, user_id, [(removed, picked[removed]), (None, picked[removed]),
                                          (kept, picked[kept])]))

    before_removed = _run(_rows_referencing(tables[removed]))
    before_kept = _run(_rows_referencing(tables[kept]))
    for counts in (before_removed, before_kept):
        assert all(counts.values()), counts  # the scenario really has every kind of dependent

    r = test_client.delete(f"/api/data_sources/{ds_id}/connections/{removed}", headers=_h(token, org_id))
    assert r.status_code == 200, r.text

    after = _run(_agent_tables(ds_id))
    assert removed not in after and sorted(after[kept]) == sorted(tables[kept])
    assert all(v == 0 for v in _run(_rows_referencing(tables[removed])).values())
    assert _run(_rows_referencing(tables[kept])) == before_kept

    async def _overlay_columns_orphaned():
        async with async_session_maker() as db:
            return (await db.execute(text(
                "select count(*) from user_data_source_columns c where not exists "
                "(select 1 from user_data_source_tables t where t.id = c.user_data_source_table_id)"
            ))).scalar()
    assert _run(_overlay_columns_orphaned()) == 0

    # The instruction itself survives; it just stops pointing at removed tables.
    got = test_client.get(f"/api/instructions/{instr.json()['id']}", headers=_h(token, org_id))
    assert got.status_code == 200
    assert {ref["object_id"] for ref in got.json()["references"]} == {picked[kept]}


@pytest.mark.e2e
def test_unlinking_a_tool_connection_drops_only_this_agents_policies_for_it(
    test_client, create_user, login_user, whoami,
    create_custom_api_connection,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    api = create_custom_api_connection(user_token=token, org_id=org_id, endpoints=[
        {"name": f"get_{n}", "method": "GET", "path": f"/{n}", "description": n, "parameters": []}
        for n in ("orders", "customers", "invoices")
    ])
    sql = _sqlite_connection(test_client, token, org_id, f"wh-{uuid.uuid4().hex[:4]}")
    agent_a = _agent(test_client, token, org_id, [sql, api["id"]])
    agent_b = _agent(test_client, token, org_id, [sql, api["id"]])
    tools = [t for t in test_client.get(f"/api/data_sources/{agent_a}/tools", headers=_h(token, org_id)).json()
             if t["connection_id"] == api["id"]]
    assert len(tools) == 3
    for agent in (agent_a, agent_b):
        for tool in tools:
            r = test_client.put(f"/api/data_sources/{agent}/tools/{tool['id']}",
                                json={"policy": "deny"}, headers=_h(token, org_id))
            assert r.status_code == 200, r.json()

    async def _policies(agent):
        async with async_session_maker() as db:
            return len((await db.execute(select(DataSourceConnectionTool.id).where(
                DataSourceConnectionTool.data_source_id == agent))).all())
    assert _run(_policies(agent_a)) == len(tools) == _run(_policies(agent_b))

    r = test_client.delete(f"/api/data_sources/{agent_a}/connections/{api['id']}", headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    assert _run(_policies(agent_a)) == 0
    assert _run(_policies(agent_b)) == len(tools)  # another agent's policies are its own

    # Relinking starts from defaults instead of resurrecting the old policies.
    r = test_client.post(f"/api/data_sources/{agent_a}/connections/{api['id']}", headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    listed = test_client.get(f"/api/data_sources/{agent_a}/tools", headers=_h(token, org_id))
    assert listed.status_code == 200
    assert not [t for t in listed.json() if t["connection_id"] == api["id"] and t["has_overlay"]]


async def _project_links(project_id):
    async with async_session_maker() as db:
        return {str(r[0]) for r in (await db.execute(
            select(project_data_source_association.c.data_source_id).where(
                project_data_source_association.c.project_id == project_id))).all()}


def _project_with_defaults(test_client, token, org_id, create_project, agent_ids):
    project = create_project(name=f"p-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    r = test_client.put(f"/api/projects/{project['id']}/data_sources",
                        json={"data_source_ids": agent_ids}, headers=_h(token, org_id))
    assert r.status_code == 200, r.json()
    assert _run(_project_links(project["id"])) == set(agent_ids)
    return project["id"]


@pytest.mark.e2e
def test_deleting_an_agent_that_is_a_project_default(
    test_client, create_user, login_user, whoami, create_project,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    conn = _sqlite_connection(test_client, token, org_id, f"wh-{uuid.uuid4().hex[:4]}")
    doomed, other = (_agent(test_client, token, org_id, [conn]) for _ in range(2))
    project_id = _project_with_defaults(test_client, token, org_id, create_project, [doomed, other])

    r = test_client.delete(f"/api/data_sources/{doomed}", headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    assert _run(_project_links(project_id)) == {other}
    assert test_client.get(f"/api/projects/{project_id}", headers=_h(token, org_id)).status_code == 200


@pytest.mark.e2e
def test_deleting_the_only_connection_of_a_project_default_agent(
    test_client, create_user, login_user, whoami, create_project,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    sole = _sqlite_connection(test_client, token, org_id, f"wh-{uuid.uuid4().hex[:4]}")
    shared = _sqlite_connection(test_client, token, org_id, f"wh-{uuid.uuid4().hex[:4]}")
    only_on_sole = _agent(test_client, token, org_id, [sole])
    survives = _agent(test_client, token, org_id, [sole, shared])
    project_id = _project_with_defaults(test_client, token, org_id, create_project, [only_on_sole, survives])

    r = test_client.delete(f"/api/connections/{sole}", headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    assert r.json()["deleted_agents"] and len(r.json()["deleted_agents"]) == 1
    assert _run(_project_links(project_id)) == {survives}
