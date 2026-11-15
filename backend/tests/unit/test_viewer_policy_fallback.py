"""DS-less reports must not slip past snapshot withholding.

Chat-created reports carry no report<->data-source association — execution
falls back to the org's data sources — so the withholding policy has to
evaluate that same fallback, filtered to the sources the step's code
addresses by client key.
"""

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.data_source import DataSource
from app.models.connection import Connection
from app.services.viewer_data_policy import (
    snapshot_withheld_for_viewers,
    sources_credential_scoped,
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed(db):
    org = Organization(name="o")
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add_all([org, user])
    await db.flush()
    report = Report(
        title="r", user_id=str(user.id), organization_id=str(org.id),
        slug="r1", status="published", shared_run_identity="viewer",
    )
    db.add(report)
    await db.flush()

    def make_ds(name, conn_name, auth_policy):
        conn = Connection(
            name=conn_name, type="powerbi" if auth_policy != "system_only" else "sqlite",
            config="{}", organization_id=str(org.id), is_active=True,
            auth_policy=auth_policy,
        )
        ds = DataSource(name=name, organization_id=str(org.id), owner_user_id=str(user.id))
        ds.connections.append(conn)
        db.add_all([conn, ds])
        return ds

    delegated = make_ds("PowerBI RLS", "powerbi-1", "user_required")
    plain = make_ds("Membership DB", "sqlite-1", "system_only")
    await db.flush()
    return org, report, delegated, plain


@pytest.mark.asyncio
async def test_dsless_report_code_naming_delegated_source_withholds(db):
    org, report, delegated, plain = await _seed(db)
    code = "def generate_df(ds_clients, excel_files):\n    ds_clients['PowerBI RLS:powerbi-1'].execute_query('EVALUATE sales')"
    assert await snapshot_withheld_for_viewers(
        db, str(report.id), "viewer",
        fallback_org_id=str(org.id), code=code,
    ) is True


@pytest.mark.asyncio
async def test_dsless_report_code_naming_system_source_is_served(db):
    org, report, delegated, plain = await _seed(db)
    code = "def generate_df(ds_clients, excel_files):\n    ds_clients['Membership DB:sqlite-1'].execute_query('select 1')"
    assert await snapshot_withheld_for_viewers(
        db, str(report.id), "viewer",
        fallback_org_id=str(org.id), code=code,
    ) is False


@pytest.mark.asyncio
async def test_dsless_report_no_code_withholds_conservatively(db):
    org, report, delegated, plain = await _seed(db)
    assert await snapshot_withheld_for_viewers(
        db, str(report.id), "viewer",
        fallback_org_id=str(org.id), code=None,
    ) is True


@pytest.mark.asyncio
async def test_no_fallback_org_keeps_legacy_behavior(db):
    org, report, delegated, plain = await _seed(db)
    assert await snapshot_withheld_for_viewers(db, str(report.id), "viewer") is False


@pytest.mark.asyncio
async def test_credential_scoped_follows_code_named_source(db):
    org, report, delegated, plain = await _seed(db)
    memo = {}
    assert await sources_credential_scoped(
        db, str(report.id), fallback_org_id=str(org.id),
        code="ds_clients['PowerBI RLS:powerbi-1']", memo=memo,
    ) is True
    assert await sources_credential_scoped(
        db, str(report.id), fallback_org_id=str(org.id),
        code="ds_clients['Membership DB:sqlite-1']", memo=memo,
    ) is False
    # memo keyed per (report, code) — both answers cached
    assert len([k for k in memo if isinstance(k, tuple)]) == 2


# ── what a step reaches through load_step (pure) ───────────────────────────

def test_a_load_step_call_spelled_with_a_full_width_underscore_is_followed():
    from app.services.viewer_data_policy import may_load_steps, referenced_step_ids

    code = "def generate_df(ds_clients, excel_files):\n    return load＿step('b-1')\n"
    assert may_load_steps(code)
    assert referenced_step_ids([("b-1", "b-slug", "B")], [code], set()) == ["b-1"]


def test_a_reference_reaches_every_step_it_can_match():
    """By id, slug or title, current or superseded: every match counts."""
    from app.services.viewer_data_policy import referenced_step_ids

    index = [("old-b", "b-old", "Sales"), ("new-b", "b-new", "Sales"), ("c", "c-slug", "C")]
    seen: set = set()
    reached = referenced_step_ids(index, ["load_step('sales')", "load_step('c-slug')"], seen)
    assert set(reached) == {"old-b", "new-b", "c"}
    # A step already reached is not reached again.
    assert referenced_step_ids(index, ["load_step('C')"], seen) == []


# ── the run-source rule, with a database ───────────────────────────────────

def _connection(org, name, auth_policy="system_only"):
    return Connection(name=name, type="sqlite", config="{}", organization_id=str(org.id),
                      is_active=True, auth_policy=auth_policy)


async def _org_with_a_plain_and_a_row_filtered_source(db, plain_name="db", rls_name="Warehouse"):
    """An organization with a plain source and a source exposing an RLS
    relation (Organization, DataSource and the relation seeded directly: the
    unit suite has no API)."""
    from app.models.connection_table import KIND_BOW, ConnectionTable
    from app.models.datasource_table import DataSourceTable

    org = Organization(name="o")
    db.add(org)
    await db.flush()
    plain = DataSource(name=plain_name, organization_id=str(org.id))
    plain.connections.append(_connection(org, "pg"))
    rls = DataSource(name=rls_name, organization_id=str(org.id))
    rls_conn = _connection(org, "duck")
    rls.connections.append(rls_conn)
    db.add_all([plain, rls])
    await db.flush()
    table = ConnectionTable(name="orders", connection_id=str(rls_conn.id), kind=KIND_BOW,
                            columns=[{"name": "x"}], pks=[], fks=[], rls_enabled=True)
    db.add(table)
    await db.flush()
    db.add(DataSourceTable(name="orders", datasource_id=str(rls.id), connection_table_id=str(table.id),
                           is_active=True))
    await db.flush()
    return org, plain, rls


@pytest.mark.asyncio
async def test_a_plain_source_named_in_an_identifier_does_not_narrow_code_reaching_every_client(db):
    """The org has a plain source `db`; the code reaches every client through
    .values() and holds `db_rows`. It runs on every org source, the RLS one
    included -- a name inside an identifier is not a client key."""
    from app.services.viewer_data_policy import _any_rls_relation, fallback_ds_ids_for_codes, _org_data_sources

    org, plain, rls = await _org_with_a_plain_and_a_row_filtered_source(db)
    code = ("def generate_df(ds_clients, excel_files):\n"
            "    db_rows = [c.execute_query('select 1') for c in ds_clients.values()]\n")
    ids = fallback_ds_ids_for_codes(await _org_data_sources(db, str(org.id)), [code])
    assert set(ids) == {str(plain.id), str(rls.id)}
    assert await _any_rls_relation(db, ids) is True


@pytest.mark.asyncio
async def test_code_reaching_only_a_plain_source_by_key_runs_on_that_source(db):
    from app.services.viewer_data_policy import fallback_ds_ids_for_codes, _org_data_sources

    org, plain, rls = await _org_with_a_plain_and_a_row_filtered_source(db)
    code = ("def generate_df(ds_clients, excel_files):\n"
            "    # Warehouse figures\n"
            "    return ds_clients['db:pg'].execute_query('SELECT Warehouse FROM t')\n")
    assert fallback_ds_ids_for_codes(await _org_data_sources(db, str(org.id)), [code]) == [str(plain.id)]


async def _release_copy(db, org, release_id, code, *, title, slug, default=True, query=None):
    """A copied query of a published dashboard and one of its steps (seeded
    directly, see above). `default=False` adds a superseded step to `query`."""
    from app.models.query import Query
    from app.models.step import Step

    if query is None:
        query = Query(title=title, organization_id=str(org.id), artifact_release_id=release_id)
        db.add(query)
        await db.flush()
    step = Step(title=title, slug=slug, status="success", query_id=str(query.id), code=code)
    db.add(step)
    await db.flush()
    if default:
        query.default_step_id = str(step.id)
        await db.flush()
    return query, step


@pytest.mark.asyncio
async def test_a_release_runs_on_the_sources_of_the_copies_its_copies_load(db):
    """Copy A reaches a plain source by key and load_step()s the superseded
    step of copy B, which reaches the RLS source; B's current step reaches the
    plain source. The release runs on the RLS source too, so 'run on my
    behalf' is not used for it -- the rule each copy's stored results are
    refused by (release_copy_data_source_ids) agrees."""
    from types import SimpleNamespace

    from app.services.dashboard_data import release_copy_data_source_ids, release_run_data_source_ids
    from app.services.dashboard_runs import run_identity

    org, plain, rls = await _org_with_a_plain_and_a_row_filtered_source(db)
    release_id = "rel-1"
    plain_code = "def generate_df(ds_clients, excel_files):\n    return ds_clients['db:pg'].execute_query('x')\n"
    rls_code = "def generate_df(ds_clients, excel_files):\n    return ds_clients['Warehouse:duck'].execute_query('x')\n"
    query_b, _old_b = await _release_copy(db, org, release_id, rls_code, title="B", slug="b-old")
    await _release_copy(db, org, release_id, plain_code, title="B", slug="b-new", query=query_b)
    a_code = ("def generate_df(ds_clients, excel_files):\n"
              "    ds_clients['db:pg'].execute_query('x')\n"
              "    return load_step('b-old')\n")
    await _release_copy(db, org, release_id, a_code, title="A", slug="a")

    release = SimpleNamespace(id=release_id, organization_id=str(org.id), bow_source_access=None)
    run_ids = await release_run_data_source_ids(db, release)
    assert str(rls.id) in run_ids
    assert set(await release_copy_data_source_ids(db, release, a_code)) <= set(run_ids)

    artifact = SimpleNamespace(shared_run_identity="creator", owner_id="owner", organization_id=str(org.id))
    caller = SimpleNamespace(id="reader")
    assert await run_identity(db, artifact, release, caller, refuse_strangers=False) == ("viewer", caller)
