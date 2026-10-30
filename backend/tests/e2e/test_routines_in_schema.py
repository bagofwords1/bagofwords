"""Stored procedures / functions ride the normal schema catalog end to end.

A SQL client's `get_schemas()` returns its routines as entries marked
``metadata_json["type"] == "routine"`` (app.data_sources.routines). From there
the regular table machinery carries them: indexing into the catalog, the
agent's schema API, activation, pruning on reindex, and the agent's schema
context. This drives that whole path through the real API and services; only
the database driver is stubbed (`get_tables` / `get_routines`, the two catalog
reads `get_schemas` composes — the catalog SQL itself is covered against real
engines in tests/integrations/test_routine_discovery.py).

Contracts:
* routines are indexed next to the tables, unchanged tables included
* the schema API returns a routine's kind, signature and source
* routines are opt-in per connection (`index_routines`): off, the routine
  catalog is never read
* a failing routine read never costs the source its tables, and keeps the
  routines indexed on an earlier run instead of pruning them
* a routine dropped upstream is pruned on the next authoritative reindex
* the agent's standing schema context lists routines by signature only and
  says they are not tables; describe_tables mode adds the source
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.data_sources.routines import RoutineVariant, build_routine_tables

TABLES = [
    Table(name="sales.orders", columns=[TableColumn(name="order_id", dtype="integer"),
                                        TableColumn(name="status", dtype="text")],
          pks=[], fks=[], metadata_json={"schema": "sales"}),
    Table(name="sales.customers", columns=[TableColumn(name="customer_id", dtype="integer")],
          pks=[], fks=[], metadata_json={"schema": "sales"}),
]

PROC_SOURCE = (
    "CREATE PROCEDURE sales.load_net_revenue(p_from date) AS $$ "
    "SELECT sum(gross - discount) FROM sales.orders WHERE status = 'complete' $$"
)


def _routines(include_fiscal=True):
    variants = [RoutineVariant(
        schema="sales", name="load_net_revenue", kind="procedure",
        params=[{"name": "p_from", "dtype": "date", "mode": "IN"}],
        definition=PROC_SOURCE, description="Canonical net revenue")]
    if include_fiscal:
        variants.append(RoutineVariant(
            schema="sales", name="fiscal_quarter", kind="function",
            params=[{"name": "d", "dtype": "date", "mode": "IN"}], returns="text",
            definition="CREATE FUNCTION sales.fiscal_quarter(d date) RETURNS text ..."))
    return build_routine_tables(variants, [t.name for t in TABLES], lambda s, n: f"{s}.{n}")


@pytest.fixture
def catalog(monkeypatch):
    """The source's catalog, mutable per test: what the 'database' holds now."""
    state = {"routines": _routines(), "fail": False, "reads": 0}

    def get_tables(self, progress_callback=None):
        return [t.model_copy(deep=True) for t in TABLES]

    def get_routines(self, table_names=()):
        state["reads"] += 1
        if state["fail"]:
            raise RuntimeError("permission denied for pg_proc")
        return [r.model_copy(deep=True) for r in state["routines"]]

    monkeypatch.setattr(PostgresqlClient, "test_connection",
                        lambda self: {"success": True, "message": "ok"})
    monkeypatch.setattr(PostgresqlClient, "get_tables", get_tables)
    monkeypatch.setattr(PostgresqlClient, "get_routines", get_routines)
    return state


def _make_agent(create_user, login_user, whoami, create_data_source, *, index_routines):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    config = {"host": "db.invalid", "port": 5432, "database": "sales", "schema": "sales"}
    if index_routines is not None:
        config["index_routines"] = index_routines
    ds = create_data_source(
        name=f"Sales {uuid.uuid4().hex[:6]}", type="postgresql",
        config=config,
        credentials={"user": "u", "password": "p"},
        user_token=token, org_id=org_id,
    )
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    return {"id": ds["id"], "headers": headers, "org_id": org_id}


@pytest.fixture
def agent(create_user, login_user, whoami, create_data_source, catalog):
    return _make_agent(create_user, login_user, whoami, create_data_source, index_routines=True)


def _schema(test_client, agent):
    r = test_client.get(f"/api/data_sources/{agent['id']}/full_schema", headers=agent["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body.get("tables") if isinstance(body, dict) else body
    return {t["name"]: t for t in rows}


def _reindex(test_client, refresh_schema, agent):
    refresh_schema(data_source_id=agent["id"], user_token=agent["headers"]["Authorization"][7:],
                   org_id=agent["org_id"])


def _is_routine(row):
    return (row.get("metadata_json") or {}).get("type") == "routine"


@pytest.mark.e2e
def test_routines_are_indexed_with_signature_and_source(test_client, refresh_schema, agent):
    _reindex(test_client, refresh_schema, agent)
    rows = _schema(test_client, agent)

    assert {n for n, r in rows.items() if not _is_routine(r)} == {"sales.orders", "sales.customers"}
    assert {n for n, r in rows.items() if _is_routine(r)} == {"sales.load_net_revenue", "sales.fiscal_quarter"}

    proc = rows["sales.load_net_revenue"]["metadata_json"]["routine"]
    assert proc["kind"] == "procedure"
    assert proc["signatures"] and "p_from date" in proc["signatures"][0]
    assert "status = 'complete'" in proc["definition"]
    assert rows["sales.fiscal_quarter"]["metadata_json"]["routine"]["kind"] == "function"


@pytest.mark.e2e
@pytest.mark.parametrize("index_routines", [None, False])
def test_routines_are_not_read_unless_the_connection_opts_in(
    test_client, refresh_schema, create_user, login_user, whoami, create_data_source, catalog, index_routines,
):
    agent = _make_agent(create_user, login_user, whoami, create_data_source, index_routines=index_routines)
    _reindex(test_client, refresh_schema, agent)
    rows = _schema(test_client, agent)
    assert {"sales.orders", "sales.customers"} <= set(rows)
    assert not any(_is_routine(r) for r in rows.values())
    assert catalog["reads"] == 0


@pytest.mark.e2e
def test_failing_routine_read_keeps_the_tables(test_client, refresh_schema, agent, catalog):
    catalog["fail"] = True
    _reindex(test_client, refresh_schema, agent)
    rows = _schema(test_client, agent)
    assert {"sales.orders", "sales.customers"} <= set(rows)
    assert not any(_is_routine(r) for r in rows.values())


@pytest.mark.e2e
def test_failing_routine_read_keeps_routines_indexed_earlier(test_client, refresh_schema, agent, catalog):
    _reindex(test_client, refresh_schema, agent)
    assert "sales.load_net_revenue" in _schema(test_client, agent)

    # A timeout or revoked catalog grant on a later run is not "every routine
    # was dropped": the routines stay, so their selection is not lost.
    catalog["fail"] = True
    _reindex(test_client, refresh_schema, agent)
    rows = _schema(test_client, agent)
    assert {"sales.load_net_revenue", "sales.fiscal_quarter", "sales.orders"} <= set(rows)


@pytest.mark.e2e
def test_routine_dropped_upstream_is_pruned_on_reindex(test_client, refresh_schema, agent, catalog):
    _reindex(test_client, refresh_schema, agent)
    assert "sales.fiscal_quarter" in _schema(test_client, agent)

    catalog["routines"] = _routines(include_fiscal=False)
    _reindex(test_client, refresh_schema, agent)
    rows = _schema(test_client, agent)
    assert "sales.fiscal_quarter" not in rows
    assert "sales.load_net_revenue" in rows and "sales.orders" in rows


@pytest.mark.e2e
def test_agent_context_lists_routines_and_describe_adds_source(test_client, refresh_schema, agent):
    _reindex(test_client, refresh_schema, agent)
    r = test_client.post(f"/api/data_sources/{agent['id']}/bulk_update_tables",
                         json={"action": "activate"}, headers=agent["headers"])
    assert r.status_code == 200, r.text

    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.organization import Organization

    async def render():
        async with async_session_maker() as db:
            ds = (await db.execute(
                select(DataSource).options(selectinload(DataSource.connections))
                .where(DataSource.id == agent["id"]))).scalar_one()
            org = await db.get(Organization, agent["org_id"])
            ctx = await SchemaContextBuilder(db, [ds], org, None).build(with_stats=False)
            return (ctx.render_combined(top_k_per_ds=20),
                    ctx.render_combined(top_k_per_ds=20, include_routine_definitions=True))

    standing, described = asyncio.run(render())

    # Standing context: every routine, by signature, flagged as not-a-table.
    assert '<routine name="sales.load_net_revenue" kind="procedure"' in standing
    assert '<routine name="sales.fiscal_quarter" kind="function"' in standing
    assert "<routines_note>" in standing
    assert '<table name="sales.load_net_revenue"' not in standing
    # ...but never the source: that would bloat every turn.
    assert "status = &#x27;complete&#x27;" not in standing and "status = 'complete'" not in standing
    assert "<definition" not in standing
    # Tables render as before.
    assert '<table name="sales.orders"' in standing

    # describe_tables mode carries the source.
    assert "<definition" in described
    assert "complete" in described.split('<routine name="sales.load_net_revenue"', 1)[1].split("</routine>", 1)[0]
