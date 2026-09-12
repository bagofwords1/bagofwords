"""Regressions found reviewing the per-connection overlay change.

Four defects, two of them access-control:

1. COLUMN MASKING. `get_data_source_schema` merges the caller's overlay with
   the canonical catalog, keyed on the table id. `read_user_data_source_schema`
   built its `Table` DTOs without setting `id`, so every overlay table failed
   the merge filter and the canonical row — carrying the FULL column set the
   service principal discovered — was served in its place. The per-user column
   masking the overlay exists to apply was silently off on that path.

2. CANONICAL FALLBACK. The same merge fell back to canonical rows for
   delegated connections whenever the overlay read raised, turning a transient
   failure into the same masking bypass.

3. REVOKED ACCESS. `_resolve_catalog_scope` left the overlay subquery
   unrestricted whenever the caller had no currently-authorized connection, so
   the unlinked-row branch kept admitting semantic models they discovered
   before their credentials were deactivated.

4. IDENTITY MATCHING. `_upsert_user_overlay` scoped the name index to the
   connection being synced but left the (datasetId, tableName) index — which is
   consulted FIRST — indexing every canonical row, so the same Power BI dataset
   reached through two delegated connections collapsed onto one canonical row.

5. WARMING. First-read warming short-circuited on the first overlay row found
   anywhere on the agent, so a connection attached after the user's first read
   never got a catalog.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_overlay_review_regressions.py -v -s
"""
import uuid
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select, update
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
from app.models.user_data_source_overlay import (
    UserDataSourceTable as UserOverlayTable,
    UserDataSourceColumn as UserOverlayColumn,
)
from app.services.data_source_service import DataSourceService, _WARM_ATTEMPTS


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_warm_cache():
    """Warming is guarded by a process-local attempt cache; tests must not
    inherit another test's attempts."""
    _WARM_ATTEMPTS.clear()
    yield
    _WARM_ATTEMPTS.clear()


def _payload(name, columns):
    return {
        "name": name,
        "columns": [{"name": c, "dtype": "text"} for c in columns],
        "pks": [], "fks": [],
        "metadata_json": {"powerbi": {"datasetId": f"ds-{name}", "tableName": name}},
    }


class _FakeClient:
    """A delegated catalog: what THIS user's own token sees, columns included."""

    def __init__(self, tables):
        self._tables = tables

    async def aget_schemas(self, progress_callback=None, prior_tables=None):
        return [_payload(n, cols) for n, cols in self._tables.items()]


async def _seed(n_delegated=1):
    """One agent: a system_only warehouse plus `n_delegated` OBO Power BI
    connections, one signed-in user."""
    suffix = uuid.uuid4().hex[:8]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with async_session_maker() as db:
        org = Organization(name=f"Review Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="member", email=f"member-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        db.add(user)
        await db.flush()

        wh = Connection(
            organization_id=org.id, name=f"Warehouse {suffix}", type="postgresql",
            config={"host": "localhost", "port": 5432, "database": "demo_db"},
            auth_policy="system_only", created_at=base,
        )
        wh.encrypt_credentials({"user": "u", "password": "p"})
        db.add(wh)

        pbis = []
        for i in range(n_delegated):
            pbi = Connection(
                organization_id=org.id, name=f"PowerBI {i} {suffix}", type="powerbi",
                config={}, auth_policy="user_required", allowed_user_auth_modes=["oauth"],
                created_at=base + timedelta(hours=i + 1),
            )
            pbi.encrypt_credentials({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
            db.add(pbi)
            pbis.append(pbi)
        await db.flush()

        ds = DataSource(name=f"Review Agent {suffix}", organization_id=org.id, is_active=True)
        db.add(ds)
        await db.flush()
        for conn in [wh, *pbis]:
            await db.execute(domain_connection.insert().values(
                data_source_id=ds.id, connection_id=conn.id,
            ))

        # Warehouse canonical catalog.
        ct = ConnectionTable(
            name="public.customers", connection_id=wh.id,
            columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[], no_rows=0,
            metadata_json={"schema": "public"},
        )
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(
            name="public.customers", datasource_id=ds.id, connection_table_id=ct.id,
            is_active=True, metadata_json={"schema": "public"},
            columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[], no_rows=0,
        ))

        for u in (user,):
            for pbi in pbis:
                cred = UserConnectionCredentials(
                    connection_id=str(pbi.id), user_id=str(u.id),
                    organization_id=str(org.id), auth_mode="oauth",
                    is_active=True, is_primary=True,
                    last_used_at=datetime.now(timezone.utc),
                )
                cred.encrypt_credentials({"access_token": "delegated-token"})
                db.add(cred)

        await db.commit()
        return {
            "org_id": org.id, "ds_id": ds.id, "user_id": user.id,
            "wh_id": str(wh.id), "pbi_ids": [str(p.id) for p in pbis],
        }


async def _load_ds(db, ds_id):
    return (await db.execute(
        select(DataSource)
        .options(selectinload(DataSource.connections))
        .where(DataSource.id == ds_id)
    )).scalar_one()


def _install_catalog(monkeypatch, by_conn_id):
    """by_conn_id: {connection_id: {table_name: [column, ...]}}"""
    async def _fake(self, db, data_source, connection=None, user=None, **kw):
        return _FakeClient(by_conn_id[str(connection.id)])
    monkeypatch.setattr(DataSourceService, "_construct_user_catalog_client", _fake)


async def _seed_wide_canonical(ds_id, conn_id):
    """The service principal's crawl of `Secrets` found a column this user's
    own token cannot see."""
    meta = {"powerbi": {"datasetId": "ds-Secrets", "tableName": "Secrets"}}
    cols = [{"name": "id", "dtype": "int"}, {"name": "salary", "dtype": "int"}]
    async with async_session_maker() as db:
        ct = ConnectionTable(
            name="Secrets", connection_id=conn_id, columns=cols,
            pks=[], fks=[], no_rows=0, metadata_json=meta,
        )
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(
            name="Secrets", datasource_id=ds_id, connection_table_id=ct.id,
            is_active=True, metadata_json=meta, columns=cols,
            pks=[], fks=[], no_rows=0,
        ))
        await db.commit()


async def _full_schema(ids):
    svc = DataSourceService()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, ids["user_id"])
        tables = await svc.get_data_source_schema(
            db=db, data_source_id=ids["ds_id"], organization=org,
            include_inactive=True, current_user=user,
        )
        return {t.name: sorted(c.name for c in (t.columns or [])) for t in tables}


# ---------------------------------------------------------------------------
# 1 + 2: column masking on the merged full-schema path
# ---------------------------------------------------------------------------

def test_full_schema_serves_the_users_masked_columns(monkeypatch):
    """The canonical row for `Secrets` carries `salary`; this user's own token
    does not see it. The merged schema must show the overlay's column set."""
    ids = _run(_seed())
    pbi_id = ids["pbi_ids"][0]
    _install_catalog(monkeypatch, {pbi_id: {"Secrets": ["id"]}})

    _run(_seed_wide_canonical(ids["ds_id"], pbi_id))

    by_name = _run(_full_schema(ids))
    assert "Secrets" in by_name, "the delegated table vanished from the schema"
    assert by_name["Secrets"] == ["id"], (
        f"canonical columns leaked through the merge: {by_name['Secrets']}"
    )
    # The open connection still contributes its canonical row.
    assert "public.customers" in by_name


def test_overlay_read_failure_does_not_fall_back_to_canonical(monkeypatch):
    """A failed overlay read must cost the delegated tables, not unmask them."""
    ids = _run(_seed())
    pbi_id = ids["pbi_ids"][0]
    _install_catalog(monkeypatch, {pbi_id: {"Secrets": ["id"]}})

    _run(_seed_wide_canonical(ids["ds_id"], pbi_id))

    _run(_full_schema(ids))   # warm the overlay first

    async def _boom(self, *a, **kw):
        raise RuntimeError("overlay store unavailable")
    monkeypatch.setattr(DataSourceService, "read_user_data_source_schema", _boom)

    by_name = _run(_full_schema(ids))
    assert "public.customers" in by_name, "the open connection must still serve"
    assert "Secrets" not in by_name, (
        "a failed overlay read served the canonical (unmasked) delegated row"
    )


# ---------------------------------------------------------------------------
# 3: revoked access must not leave stale models visible
# ---------------------------------------------------------------------------

def test_deactivated_credentials_hide_previously_discovered_models(monkeypatch):
    ids = _run(_seed())
    pbi_id = ids["pbi_ids"][0]
    _install_catalog(monkeypatch, {pbi_id: {"Private/T9": ["id"]}})

    before = _run(_full_schema(ids))
    assert "Private/T9" in before, "the user's own model should be visible while connected"

    async def _revoke():
        async with async_session_maker() as db:
            await db.execute(
                update(UserConnectionCredentials)
                .where(UserConnectionCredentials.connection_id == pbi_id)
                .values(is_active=False)
            )
            await db.commit()
    _run(_revoke())
    _WARM_ATTEMPTS.clear()

    after = _run(_full_schema(ids))
    assert "public.customers" in after, "the open connection is unaffected"
    assert "Private/T9" not in after, (
        "a model discovered under revoked credentials stayed visible"
    )


# ---------------------------------------------------------------------------
# 4: two connections must not share one canonical row
# ---------------------------------------------------------------------------

def test_same_dataset_through_two_connections_keeps_separate_rows(monkeypatch):
    """Both connections expose dataset `ds-Shared` / table `Shared`. Matching on
    (datasetId, tableName) across the whole agent pointed both overlays at one
    canonical row, losing per-connection attribution and activation."""
    ids = _run(_seed(n_delegated=2))
    a, b = ids["pbi_ids"]
    _install_catalog(monkeypatch, {
        a: {"Shared": ["id"]},
        b: {"Shared": ["id"]},
    })

    async def _sync():
        svc = DataSourceService()
        async with async_session_maker() as db:
            user = await db.get(User, ids["user_id"])
            ds = await _load_ds(db, ids["ds_id"])
            await svc.get_user_data_source_schema(db=db, data_source=ds, user=user)
    _run(_sync())

    async def _inspect():
        async with async_session_maker() as db:
            rows = (await db.execute(
                select(UserOverlayTable).where(
                    UserOverlayTable.data_source_id == str(ids["ds_id"]),
                    UserOverlayTable.table_name == "Shared",
                )
            )).scalars().all()
            return [(str(r.connection_id), str(r.data_source_table_id)) for r in rows]

    rows = _run(_inspect())
    assert len(rows) == 2, f"expected one overlay row per connection, got {rows}"
    assert {c for c, _ in rows} == {a, b}
    canonical_ids = {t for _, t in rows}
    assert len(canonical_ids) == 2, (
        f"both connections' overlays point at the same canonical row: {rows}"
    )


# ---------------------------------------------------------------------------
# 5: a connection attached later still gets warmed
# ---------------------------------------------------------------------------

def test_newly_attached_connection_is_warmed(monkeypatch):
    ids = _run(_seed(n_delegated=2))
    a, b = ids["pbi_ids"]
    _install_catalog(monkeypatch, {a: {"FromA": ["id"]}, b: {"FromB": ["id"]}})

    # Simulate "B attached after the first read": drop B's overlay rows, so
    # only A is warm — exactly the state the short-circuit never recovered from.
    _run(_full_schema(ids))

    async def _forget_b():
        async with async_session_maker() as db:
            ids_b = (await db.execute(
                select(UserOverlayTable.id).where(UserOverlayTable.connection_id == b)
            )).scalars().all()
            for row_id in ids_b:
                await db.execute(
                    UserOverlayColumn.__table__.delete().where(
                        UserOverlayColumn.user_data_source_table_id == row_id
                    )
                )
            await db.execute(
                UserOverlayTable.__table__.delete().where(UserOverlayTable.connection_id == b)
            )
            await db.commit()
    _run(_forget_b())
    _WARM_ATTEMPTS.clear()

    by_name = _run(_full_schema(ids))
    assert "FromA" in by_name
    assert "FromB" in by_name, "the later-attached connection was never warmed"
