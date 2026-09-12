"""A delegated (user_required) source inherits table ACTIVATION from the
canonical catalog: the per-user overlay decides what a user can reach
upstream, the canonical `is_active` flag decides what the agent manager
selected for this agent, and the agent sees the intersection.

History: the overlay branch of SchemaContextBuilder used to ignore the
canonical flag entirely (emitting every accessible overlay table as active).
That was a workaround from when a user-discovered delegated model had no
canonical row at all, so nothing could be activated and the agent went empty.
`_upsert_user_overlay` now creates that canonical row on sync, so the wizard
can activate it like any other table — and the agent must honor the flag,
otherwise a user with broader upstream access than the agent's creator sees
(and queries) tables the creator never put in the agent.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.connection import Connection
from app.models.datasource_table import DataSourceTable
from app.models.user_data_source_overlay import UserDataSourceTable, UserDataSourceColumn
from app.ai.context.builders.schema_context_builder import SchemaContextBuilder


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


COLS = (("id", "Integer"), ("Region", "Text"), ("Amount", "Number"))


async def _seed_source(db):
    org = Organization(name="o")
    db.add(org)
    await db.flush()
    ds = DataSource(name="PowerBI verify-rls",
                    organization_id=str(org.id), is_active=True)
    db.add(ds)
    await db.flush()
    conn = Connection(name="c", type="powerbi", config={}, is_active=True,
                      auth_policy="user_required", organization_id=str(org.id))
    conn.data_sources.append(ds)  # M:N via domain_connection
    db.add(conn)
    await db.flush()
    await db.refresh(ds, attribute_names=["connections"])
    return org, ds


def _canonical(ds, name, active):
    return DataSourceTable(
        name=name, datasource_id=str(ds.id), is_active=active,
        columns=[{"name": n, "dtype": d} for n, d in COLS], pks=[], fks=[],
    )


async def _user_with_overlay(db, ds, email, table_names):
    user = User(name=email.split("@")[0], email=email, hashed_password="x")
    db.add(user)
    await db.flush()
    for name in table_names:
        ot = UserDataSourceTable(
            data_source_id=str(ds.id), user_id=str(user.id),
            table_name=name, is_accessible=True, status="accessible",
        )
        db.add(ot)
        await db.flush()
        for cn, dt in COLS:
            db.add(UserDataSourceColumn(user_data_source_table_id=str(ot.id),
                                        column_name=cn, is_accessible=True, data_type=dt))
    await db.flush()
    return user


def _force_delegated(monkeypatch):
    """Treat every connection on the agent as one this user runs delegated on.

    The fixture seeds a `user_required` connection but no credentials, so the
    real classifier resolves 'none'. Patch the SHARED classifier rather than a
    builder-private method: it is the seam the builder actually consults (and
    the one the tables selector consults too), so this test keeps testing the
    production path instead of a stub that has drifted away from it.
    """
    from app.services.data_source_service import DataSourceService

    async def _classify(self, db, data_source, current_user):
        conns = getattr(data_source, "connections", None) or []
        return [], [str(c.id) for c in conns], []

    monkeypatch.setattr(DataSourceService, "classify_connection_access", _classify)


async def _agent_tables(db, org, ds, user, monkeypatch, **build_kwargs):
    builder = SchemaContextBuilder(db, [ds], org, None, user=user)

    _force_delegated(monkeypatch)
    ctx = await builder.build(with_stats=False, **build_kwargs)
    return {getattr(t, "name", None)
            for dss in ctx.data_sources for t in (getattr(dss, "tables", []) or [])}


@pytest.mark.asyncio
async def test_inactive_canonical_hides_accessible_overlay_table(db, monkeypatch):
    """Accessible upstream but NOT activated by the manager → not in the agent."""
    org, ds = await _seed_source(db)
    db.add(_canonical(ds, "rls_sales/Sales", active=False))
    user = await _user_with_overlay(db, ds, "u@x.com", ["rls_sales/Sales"])

    assert await _agent_tables(db, org, ds, user, monkeypatch) == set()


@pytest.mark.asyncio
async def test_activating_canonical_row_surfaces_overlay_table_with_user_columns(db, monkeypatch):
    org, ds = await _seed_source(db)
    db.add(_canonical(ds, "rls_sales/Sales", active=True))
    user = await _user_with_overlay(db, ds, "u@x.com", ["rls_sales/Sales"])

    builder = SchemaContextBuilder(db, [ds], org, None, user=user)

    _force_delegated(monkeypatch)
    ctx = await builder.build(with_stats=False)
    tables = {getattr(t, "name", None)
              for dss in ctx.data_sources for t in (getattr(dss, "tables", []) or [])}
    assert tables == {"rls_sales/Sales"}
    cols = [c
            for dss in ctx.data_sources for t in (getattr(dss, "tables", []) or [])
            if getattr(t, "name", None) == "rls_sales/Sales"
            for c in (getattr(t, "columns", []) or [])]
    assert {"id", "Region", "Amount"} <= {getattr(c, "name", None) for c in cols}


@pytest.mark.asyncio
async def test_overlay_row_without_canonical_row_is_not_activated(db, monkeypatch):
    """A pre-union overlay row that never got its canonical row: nothing to
    activate, so it is not in the agent (the next sync creates the row)."""
    org, ds = await _seed_source(db)
    user = await _user_with_overlay(db, ds, "u@x.com", ["rls_sales/Sales"])

    assert await _agent_tables(db, org, ds, user, monkeypatch) == set()


@pytest.mark.asyncio
async def test_admin_activates_three_user_reaches_two(db, monkeypatch):
    """Manager selected {A, B, C}; user1's own creds reach {A, B, D}
    → user1's agent is {A, B}."""
    org, ds = await _seed_source(db)
    for name, active in (("m/A", True), ("m/B", True), ("m/C", True), ("m/D", False)):
        db.add(_canonical(ds, name, active))
    user1 = await _user_with_overlay(db, ds, "u1@x.com", ["m/A", "m/B", "m/D"])

    assert await _agent_tables(db, org, ds, user1, monkeypatch) == {"m/A", "m/B"}


@pytest.mark.asyncio
async def test_creator_activates_four_user_with_ten_sees_only_the_four(db, monkeypatch):
    """user1 built the agent on 4 of their tables; user2 reaches 10 upstream
    (the 4 plus 6 more that got inactive canonical rows from user2's own
    sync) → user2's agent is exactly the 4."""
    org, ds = await _seed_source(db)
    four = [f"m/T{i}" for i in range(4)]
    six = [f"m/T{i}" for i in range(4, 10)]
    for name in four:
        db.add(_canonical(ds, name, True))
    for name in six:
        db.add(_canonical(ds, name, False))
    user1 = await _user_with_overlay(db, ds, "u1@x.com", four)
    user2 = await _user_with_overlay(db, ds, "u2@x.com", four + six)

    assert await _agent_tables(db, org, ds, user1, monkeypatch) == set(four)
    assert await _agent_tables(db, org, ds, user2, monkeypatch) == set(four)


@pytest.mark.asyncio
async def test_user_with_no_upstream_access_sees_nothing(db, monkeypatch):
    org, ds = await _seed_source(db)
    for i in range(4):
        db.add(_canonical(ds, f"m/T{i}", True))
    user2 = await _user_with_overlay(db, ds, "u2@x.com", [])

    assert await _agent_tables(db, org, ds, user2, monkeypatch) == set()


@pytest.mark.asyncio
async def test_relationship_targets_restricted_to_activated_tables(db, monkeypatch):
    """A relationship from an activated table must not point at a table the
    activation gate hides — that would hand the agent a join target it
    cannot query and disclose the hidden name."""
    org, ds = await _seed_source(db)
    a = _canonical(ds, "m/A", True)
    a.fks = [{"column": {"name": "b_id", "dtype": "Integer"},
              "references_name": "m/B",
              "references_column": {"name": "id", "dtype": "Integer"}}]
    db.add(a)
    db.add(_canonical(ds, "m/B", False))
    user = await _user_with_overlay(db, ds, "u@x.com", ["m/A", "m/B"])

    builder = SchemaContextBuilder(db, [ds], org, None, user=user)

    _force_delegated(monkeypatch)
    ctx = await builder.build(with_stats=False)
    a_tables = [t for dss in ctx.data_sources for t in (getattr(dss, "tables", []) or [])
                if getattr(t, "name", None) == "m/A"]
    assert len(a_tables) == 1
    assert not (getattr(a_tables[0], "fks", None) or [])


@pytest.mark.asyncio
async def test_active_only_false_still_emits_inactive_overlay_table_flagged(db, monkeypatch):
    """Management surfaces that ask for everything get the inactive table,
    flagged inactive — the same contract as the service-account path."""
    org, ds = await _seed_source(db)
    db.add(_canonical(ds, "m/A", False))
    user = await _user_with_overlay(db, ds, "u@x.com", ["m/A"])

    builder = SchemaContextBuilder(db, [ds], org, None, user=user)

    _force_delegated(monkeypatch)
    ctx = await builder.build(with_stats=False, active_only=False)
    rows = [t for dss in ctx.data_sources for t in (getattr(dss, "tables", []) or [])]
    assert [getattr(t, "name", None) for t in rows] == ["m/A"]
    assert getattr(rows[0], "is_active", None) is False


@pytest.mark.asyncio
async def test_renamed_dataset_overlay_linked_by_id_follows_canonical_activation(db, monkeypatch):
    """A Power BI dataset rename leaves the user's overlay named
    `SalesV2/Orders` linked by data_source_table_id to the activated canonical
    row still named `Sales/Orders` (the sync matches on dataset/table identity
    and keeps the service-principal name). Activation follows the id link."""
    org, ds = await _seed_source(db)
    canon = _canonical(ds, "Sales/Orders", True)
    db.add(canon)
    await db.flush()
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add(user)
    await db.flush()
    ot = UserDataSourceTable(
        data_source_id=str(ds.id), user_id=str(user.id),
        table_name="SalesV2/Orders", is_accessible=True, status="accessible",
        data_source_table_id=str(canon.id),
    )
    db.add(ot)
    await db.flush()
    for cn, dt in COLS:
        db.add(UserDataSourceColumn(user_data_source_table_id=str(ot.id),
                                    column_name=cn, is_accessible=True, data_type=dt))
    await db.flush()

    assert await _agent_tables(db, org, ds, user, monkeypatch) == {"SalesV2/Orders"}

    # ...and deactivating that canonical row hides it, name notwithstanding.
    canon.is_active = False
    db.add(canon)
    await db.flush()
    assert await _agent_tables(db, org, ds, user, monkeypatch) == set()


@pytest.mark.asyncio
async def test_id_link_wins_over_a_coincidental_name_match(db, monkeypatch):
    """Overlay `m/A` linked by id to an INACTIVE canonical row must stay
    hidden even if some other active canonical row happens to be named m/A."""
    org, ds = await _seed_source(db)
    inactive = _canonical(ds, "m/A-old", False)
    db.add(inactive)
    db.add(_canonical(ds, "m/A", True))
    await db.flush()
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add(user)
    await db.flush()
    db.add(UserDataSourceTable(
        data_source_id=str(ds.id), user_id=str(user.id),
        table_name="m/A", is_accessible=True, status="accessible",
        data_source_table_id=str(inactive.id),
    ))
    await db.flush()

    assert await _agent_tables(db, org, ds, user, monkeypatch) == set()
