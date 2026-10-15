"""A Priority subform works as a table through the whole platform.

Priority serves a subform (ORDERITEMS under ORDERS) only nested inside its
parent: `GET {root}/ORDERITEMS` is a 404. The connector presents it as a table
and answers it through the parent's `$expand`. The unit tests prove that
against the client alone; this one goes through what production actually
runs, where each hop has dropped connector metadata before:

  1. indexing persists the subform's access path (`metadata_json`) in the DB,
  2. the planner's schema context — built from the DB, not from the client —
     shows the agent that the table is a subform and which columns are its
     order's,
  3. a client built by the platform for a query gets the indexed catalog and
     answers a subform query without re-crawling `$metadata`.

The Priority side is the protocol-level mock (tools/priority/mock_server.py)
served over real HTTP — the external boundary is the only thing faked.
"""
import asyncio
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import async_session_maker
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.organization import Organization
from app.models.user import User


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def priority_mock():
    tools = Path(__file__).resolve().parents[3] / "tools" / "priority"
    sys.path.insert(0, str(tools))
    import mock_server  # noqa: E402

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mock_server.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("mock Priority server did not start")
    yield mock_server, f"http://127.0.0.1:{port}{mock_server.ROOT}"
    server.should_exit = True
    thread.join(timeout=10)


async def _indexed_tables(ds_id, deadline_s=30.0):
    """The agent's tables once indexing has written them."""
    end = time.monotonic() + deadline_s
    while True:
        async with async_session_maker() as db:
            rows = (await db.execute(
                select(DataSourceTable).where(DataSourceTable.datasource_id == ds_id)
            )).scalars().all()
            if any(r.name == "ORDERITEMS" for r in rows) or time.monotonic() > end:
                for r in rows:
                    r.is_active = True
                    db.add(r)
                await db.commit()
                return {r.name: r.metadata_json for r in rows}
        await asyncio.sleep(0.2)


async def _load(db, ds_id, user_id):
    ds = (await db.execute(
        select(DataSource).options(selectinload(DataSource.connections)).where(DataSource.id == ds_id)
    )).scalar_one()
    return ds, await db.get(User, user_id)


async def _agent_context(ds_id, org_id, user_id):
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    async with async_session_maker() as db:
        ds, user = await _load(db, ds_id, user_id)
        org = await db.get(Organization, org_id)
        ctx = await SchemaContextBuilder(
            db=db, data_sources=[ds], organization=org, report=None, user=user
        ).build(with_stats=False)
        return ctx.render() if ctx else ""


async def _platform_client(ds_id, user_id):
    from app.services.data_source_service import DataSourceService
    async with async_session_maker() as db:
        ds, user = await _load(db, ds_id, user_id)
        clients = await DataSourceService().construct_clients(db, ds, current_user=user)
        return next(iter(clients.values()))


@pytest.mark.e2e
def test_subform_is_a_table_from_indexing_to_query(
    priority_mock, create_user, login_user, whoami, create_data_source,
):
    mock, root = priority_mock
    mock._calls.clear()
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    ds = create_data_source(
        name=f"Priority {uuid.uuid4().hex[:6]}",
        type="priority_erp",
        config={"service_root": root},
        credentials={"pat": "DEMOTOKEN"},
        user_token=token,
        org_id=org_id,
    )

    # 1. the access path survives persistence
    tables = _run(_indexed_tables(ds["id"]))
    assert "ORDERITEMS" in tables, f"indexed: {sorted(tables)}"
    meta = tables["ORDERITEMS"]["priority_erp"]
    assert meta["subform_of"] == "ORDERS"
    assert meta["navigation"] == "ORDERITEMS_SUBFORM"
    assert "subform_of" not in tables["ORDERS"]["priority_erp"]

    # 2. the planner sees it — from the DB, through the real builder
    ctx = _run(_agent_context(ds["id"], org_id, me["id"]))
    assert 'subform_of="ORDERS"' in ctx
    assert 'navigation="ORDERITEMS_SUBFORM"' in ctx
    assert 'from_form="ORDERS"' in ctx

    # 3. a platform-built client answers the subform from the attached catalog
    client = _run(_platform_client(ds["id"], me["id"]))
    mock.REQUEST_LOG.clear()
    df = client.execute_query(
        "ORDERITEMS?$filter=CURDATE ge 2026-02-01T00:00:00Z and PRICE gt 1000&$select=ORDNAME,PRICE"
    )
    assert sorted(df.itertuples(index=False, name=None)) == [("SO25002", 1150.0), ("SO25003", 1750.05)]
    assert not any("metadata" in p.lower() for p in mock.REQUEST_LOG), mock.REQUEST_LOG
