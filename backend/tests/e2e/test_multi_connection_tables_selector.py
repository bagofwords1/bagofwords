"""A multi-connection agent must show EVERY connection's tables — and only the
tables the caller may actually see on each of them.

Reported symptom: "an agent with 2+ connections often shows only 1 source of
tables". The tables selector (`get_data_source_schema_paginated`) decided ONCE
per data source, from `data_source.connections[0]`, whether to scope rows to
the caller's per-user overlay:

    conn0 = data_source.connections[0]
    if (conn0.auth_policy or "system_only") == "user_required": ...

Two failures fell out of that, and which one you got depended on the order the
database happened to return the connections in (the relationship had no
ORDER BY, so it could differ between two requests on the same agent):

  * delegated connection FIRST — the whole catalog was filtered through an
    overlay that only ever described that one connection (the sync built its
    client with `construct_client`, documented as "first connection only"), so
    every OTHER connection's tables vanished from the rows, the counts, and the
    filter dropdowns. This is the reported bug.

  * delegated connection SECOND — no scoping was applied at all, so one user
    saw another user's delegated Power BI models. This is the security half,
    and it is the one a "shows all the tables now" fix can easily leave open.

The fix resolves the scope PER CONNECTION (`_resolve_catalog_scope`) and syncs
the overlay per connection (`_sync_user_overlay_for_connection`), so each
connection contributes exactly what its own auth policy and this user's own
access allow.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_multi_connection_tables_selector.py -v -s
"""
import uuid
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.connection_table import ConnectionTable
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.user_connection_credentials import UserConnectionCredentials
from app.models.user_data_source_overlay import UserDataSourceTable as UserOverlayTable
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


# Warehouse (system_only) catalog — one shared catalog, same for every user.
WAREHOUSE_TABLES = ["public.customers", "public.orders", "shared_name"]

# What each user's OWN Power BI token can see. Both see ModelA (which the
# service principal also indexed) and a `shared_name` that collides with a
# warehouse table; each also sees one model the other cannot.
PBI_BY_USER = {
    "analyst1": ["ModelA/T1", "ModelU1/T9", "shared_name"],
    "analyst2": ["ModelA/T1", "ModelU2/T8", "shared_name"],
}


def _pbi_payload(name):
    return {
        "name": name,
        "columns": [{"name": "id", "dtype": "int"}],
        "pks": [], "fks": [],
        "metadata_json": {"powerbi": {"datasetId": f"ds-{name}", "tableName": name}},
    }


class _FakeDelegatedPBIClient:
    def __init__(self, table_names):
        self._names = table_names

    async def aget_schemas(self, progress_callback=None, prior_tables=None):
        return [_pbi_payload(n) for n in self._names]


async def _seed(delegated_first: bool):
    """One agent, two connections: a system_only warehouse and a delegated
    (OBO) Power BI. `delegated_first` controls which one the ordered
    relationship yields as `connections[0]`, which is the whole point."""
    suffix = uuid.uuid4().hex[:8]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with async_session_maker() as db:
        org = Organization(name=f"MultiConn Org {suffix}")
        db.add(org)
        await db.flush()

        users = {}
        for uname in ("analyst1", "analyst2"):
            u = User(
                name=uname,
                email=f"{uname}-{suffix}@example.com",
                hashed_password="x",
                is_active=True, is_superuser=False, is_verified=True,
            )
            db.add(u)
            users[uname] = u
        await db.flush()

        wh = Connection(
            organization_id=org.id,
            name=f"Warehouse {suffix}",
            type="postgresql",
            config={"host": "localhost", "port": 5432, "database": "demo_db"},
            auth_policy="system_only",
            created_at=base if not delegated_first else base + timedelta(hours=1),
        )
        wh.encrypt_credentials({"user": "u", "password": "p"})
        pbi = Connection(
            organization_id=org.id,
            name=f"PowerBI {suffix}",
            type="powerbi",
            config={},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            created_at=base if delegated_first else base + timedelta(hours=1),
        )
        pbi.encrypt_credentials({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
        db.add_all([wh, pbi])
        await db.flush()

        ds = DataSource(name=f"Mixed Agent {suffix}", organization_id=org.id, is_active=True)
        db.add(ds)
        await db.flush()
        for conn in (wh, pbi):
            await db.execute(domain_connection.insert().values(
                data_source_id=ds.id, connection_id=conn.id,
            ))

        # Canonical catalog. The warehouse's whole catalog is shared; the SP's
        # Power BI crawl only reached ModelA and the colliding `shared_name`.
        for conn, names in ((wh, WAREHOUSE_TABLES), (pbi, ["ModelA/T1", "shared_name"])):
            for name in names:
                ct = ConnectionTable(
                    name=name, connection_id=conn.id,
                    columns=[{"name": "id", "dtype": "int"}],
                    pks=[], fks=[], no_rows=0,
                    metadata_json={"schema": "public"},
                )
                db.add(ct)
                await db.flush()
                db.add(DataSourceTable(
                    name=name, datasource_id=ds.id, connection_table_id=ct.id,
                    is_active=True, metadata_json={"schema": "public"},
                    columns=[{"name": "id", "dtype": "int"}],
                    pks=[], fks=[], no_rows=0,
                ))

        # Both analysts have signed in to Power BI: real delegated tokens exist.
        for u in users.values():
            cred = UserConnectionCredentials(
                connection_id=str(pbi.id),
                user_id=str(u.id),
                organization_id=str(org.id),
                auth_mode="oauth",
                is_active=True, is_primary=True,
                last_used_at=datetime.now(timezone.utc),
            )
            cred.encrypt_credentials({"access_token": "delegated-token"})
            db.add(cred)

        await db.commit()
        return {
            "org_id": org.id, "ds_id": ds.id,
            "wh_id": str(wh.id), "pbi_id": str(pbi.id),
            "users": {k: v.id for k, v in users.items()},
            "names": {v.id: k for k, v in users.items()},
        }


async def _load_ds(db, ds_id):
    return (await db.execute(
        select(DataSource)
        .options(selectinload(DataSource.connections))
        .where(DataSource.id == ds_id)
    )).scalar_one()


async def _sync(ids, user_id):
    svc = DataSourceService()
    async with async_session_maker() as db:
        user = await db.get(User, user_id)
        ds = await _load_ds(db, ids["ds_id"])
        return sorted(t.name for t in await svc.get_user_data_source_schema(
            db=db, data_source=ds, user=user))


async def _selector(ids, user_id):
    """Exactly what the TablesSelector grid renders for this user."""
    svc = DataSourceService()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, user_id)
        resp = await svc.get_data_source_schema_paginated(
            db=db, data_source_id=ids["ds_id"], organization=org,
            page=1, page_size=500, include_inactive=True, current_user=user,
        )
        return {
            "names": sorted(t.name for t in resp.tables),
            "by_connection": sorted(
                (t.connection_name or "?", t.name) for t in resp.tables
            ),
            "connection_ids": sorted({str(c.id) for c in resp.connections}),
            "total": resp.total,
        }


def _install_fake_pbi(monkeypatch, ids, calls):
    async def _fake(self, db, data_source, connection=None, user=None, **kw):
        assert connection is not None, "the overlay sync must name its connection"
        assert str(connection.id) == ids["pbi_id"], (
            "only the delegated connection has a per-user catalog; the "
            "system_only warehouse must never be crawled per user"
        )
        calls.append((str(connection.id), user.name))
        return _FakeDelegatedPBIClient(PBI_BY_USER[user.name])
    monkeypatch.setattr(DataSourceService, "_construct_user_catalog_client", _fake)


@pytest.mark.e2e
@pytest.mark.parametrize("delegated_first", [True, False], ids=["delegated-first", "delegated-second"])
def test_mixed_agent_shows_every_connection_scoped_per_user(monkeypatch, delegated_first):
    calls = []
    ids = _run(_seed(delegated_first))
    _install_fake_pbi(monkeypatch, ids, calls)

    u1 = ids["users"]["analyst1"]
    u2 = ids["users"]["analyst2"]

    fetched1 = _run(_sync(ids, u1))
    fetched2 = _run(_sync(ids, u2))
    assert fetched1 == sorted(PBI_BY_USER["analyst1"])
    assert fetched2 == sorted(PBI_BY_USER["analyst2"])
    assert {c[0] for c in calls} == {ids["pbi_id"]}

    view1 = _run(_selector(ids, u1))
    view2 = _run(_selector(ids, u2))
    print(f"\n[delegated_first={delegated_first}] analyst1 sees: {view1['names']}")
    print(f"[delegated_first={delegated_first}] analyst2 sees: {view2['names']}")

    # 1. THE REPORTED BUG: the warehouse's tables are present no matter which
    #    connection sorts first.
    for name in WAREHOUSE_TABLES:
        assert name in view1["names"], f"warehouse table {name} missing for analyst1"
        assert name in view2["names"], f"warehouse table {name} missing for analyst2"

    # 2. Both connections are represented in the rows AND in the filter
    #    dropdown, so the grid can't claim a connection it shows nothing for.
    assert view1["connection_ids"] == sorted([ids["wh_id"], ids["pbi_id"]])
    conns_with_rows = {c for c, _ in view1["by_connection"]}
    assert len(conns_with_rows) == 2, f"only one connection produced rows: {conns_with_rows}"

    # 3. THE SECURITY HALF: each analyst sees their OWN delegated models and
    #    never the other's — including when the delegated connection is not
    #    first, which used to skip overlay scoping entirely.
    assert "ModelU1/T9" in view1["names"]
    assert "ModelU2/T8" not in view1["names"], "analyst1 sees analyst2's Power BI model"
    assert "ModelU2/T8" in view2["names"]
    assert "ModelU1/T9" not in view2["names"], "analyst2 sees analyst1's Power BI model"

    # 4. A name shared across two connections stays two distinct rows — one per
    #    connection — rather than collapsing into one.
    shared = [c for c in view1["by_connection"] if c[1] == "shared_name"]
    assert len(shared) == 2, f"expected shared_name on both connections, got {shared}"

    # 5. The header count agrees with the rows actually rendered.
    assert view1["total"] == len(view1["names"])


@pytest.mark.e2e
def test_overlay_rows_are_attributed_to_their_connection(monkeypatch):
    """Every overlay row a sync writes names the connection it came from, so a
    second connection's sync can reconcile its own rows without revoking these."""
    calls = []
    ids = _run(_seed(delegated_first=True))
    _install_fake_pbi(monkeypatch, ids, calls)
    u1 = ids["users"]["analyst1"]
    _run(_sync(ids, u1))

    async def _rows():
        async with async_session_maker() as db:
            return (await db.execute(
                select(UserOverlayTable).where(
                    UserOverlayTable.data_source_id == ids["ds_id"],
                    UserOverlayTable.user_id == u1,
                )
            )).scalars().all()

    rows = _run(_rows())
    assert rows, "the sync wrote no overlay rows"
    assert all(str(r.connection_id) == ids["pbi_id"] for r in rows), (
        f"overlay rows not attributed to their connection: "
        f"{[(r.table_name, r.connection_id) for r in rows]}"
    )


@pytest.mark.e2e
def test_second_sync_does_not_revoke_the_other_connections_rows(monkeypatch):
    """Re-syncing is idempotent per connection: it must not mark another
    connection's rows revoked just because they're absent from this snapshot."""
    calls = []
    ids = _run(_seed(delegated_first=True))
    _install_fake_pbi(monkeypatch, ids, calls)
    u1 = ids["users"]["analyst1"]

    _run(_sync(ids, u1))
    first = _run(_selector(ids, u1))
    _run(_sync(ids, u1))
    second = _run(_selector(ids, u1))

    assert first["names"] == second["names"], (
        f"re-syncing changed what the user sees:\n  before={first['names']}\n  after={second['names']}"
    )


@pytest.mark.e2e
def test_connection_filter_reaches_user_discovered_rows(monkeypatch):
    """Filtering to the delegated connection must keep the models only this
    user can see. Those rows have no ConnectionTable to join through, so the
    old JOIN-based filter dropped them — the user filtered to their Power BI
    connection and lost exactly the tables that were theirs."""
    calls = []
    ids = _run(_seed(delegated_first=True))
    _install_fake_pbi(monkeypatch, ids, calls)
    u1 = ids["users"]["analyst1"]
    _run(_sync(ids, u1))

    async def _filtered(connection_ids):
        svc = DataSourceService()
        async with async_session_maker() as db:
            org = await db.get(Organization, ids["org_id"])
            user = await db.get(User, u1)
            resp = await svc.get_data_source_schema_paginated(
                db=db, data_source_id=ids["ds_id"], organization=org,
                page=1, page_size=500, include_inactive=True, current_user=user,
                connection_filter=connection_ids,
            )
            return sorted(t.name for t in resp.tables), resp.total

    pbi_names, pbi_total = _run(_filtered([ids["pbi_id"]]))
    wh_names, wh_total = _run(_filtered([ids["wh_id"]]))
    print(f"\nfiltered to PowerBI:   {pbi_names}")
    print(f"filtered to Warehouse: {wh_names}")

    assert "ModelU1/T9" in pbi_names, "the user's own discovered model was filtered away"
    assert sorted(pbi_names) == sorted(PBI_BY_USER["analyst1"])
    assert pbi_total == len(pbi_names), "count disagrees with the filtered rows"

    assert sorted(wh_names) == sorted(WAREHOUSE_TABLES)
    assert "ModelU1/T9" not in wh_names
    assert wh_total == len(wh_names)
