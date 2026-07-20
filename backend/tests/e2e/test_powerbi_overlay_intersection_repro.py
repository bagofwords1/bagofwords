"""Delegated (OBO) Power BI: a signed-in user's own-visible semantic models
must be selectable even when the service-principal catalog lacks them.

Reported symptom: "not all the semantic models are selectable in schema —
some are missing from the fetch". The selectable set used to be the
INTERSECTION of the user's delegated crawl and the SP-seeded canonical
catalog: `_upsert_user_overlay` linked overlay rows to `DataSourceTable` by
name and left `data_source_table_id = NULL` on a miss, and
`get_data_source_schema_paginated` scopes a signed-in user to overlay rows
with `data_source_table_id IS NOT NULL`. A model the user could see but the
SP could not was fetched into the overlay and then filtered out.

Fix 3: for user_required (delegated) connections, `_upsert_user_overlay`
creates the canonical `DataSourceTable` on demand from the user's crawl
(tagged `discovered_by="user"`, matched by dataset/table identity) and links
the overlay to it — so the model is selectable, ONE org-level row backs it
(usage/instructions aggregate across users), and per-user visibility stays
enforced by the overlay. SP re-index does not prune it (it prunes only
ConnectionTable-linked rows).

See docs/feedback-loops/powerbi-missing-semantic-models.md.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_powerbi_overlay_intersection_repro.py -v -s
"""
import uuid
import asyncio
from datetime import datetime, timezone

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


def _table_payload(name, dataset_id, table_name):
    return {
        "name": name,
        "columns": [{"name": "id", "dtype": "int"}],
        "pks": [], "fks": [],
        "metadata_json": {"powerbi": {"datasetId": dataset_id, "tableName": table_name}},
    }


class _FakeDelegatedPBIClient:
    """Stands in for the live Power BI client crawling with a USER's token.
    Both demo users can see ModelA and ModelB; the SP-seeded canonical catalog
    only contains ModelA (SP not a Member of ModelB's workspace)."""

    async def aget_schemas(self, progress_callback=None, prior_catalog=None):
        return [
            _table_payload("ModelA/T1", "ds-a", "T1"),
            _table_payload("ModelB/T2", "ds-b", "T2"),
        ]


class _FakeRenamedModelBClient:
    """Same datasets, but ModelB's dataset has been RENAMED (same datasetId
    ds-b, new display name). Identity (datasetId, tableName) is unchanged."""

    async def aget_schemas(self, progress_callback=None, prior_catalog=None):
        return [
            _table_payload("ModelA/T1", "ds-a", "T1"),
            _table_payload("RenamedB/T2", "ds-b", "T2"),
        ]


async def _seed():
    """Direct DB seeding: this state (delegated token present without a real
    OAuth round-trip, SP catalog narrower than the users' access) cannot be
    produced through the API in a sandbox without live Entra credentials."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"PBI Overlay Org {suffix}")
        db.add(org)
        await db.flush()

        users = []
        for uname in ("analyst1", "analyst2"):
            u = User(
                name=uname,
                email=f"{uname}-{suffix}@example.com",
                hashed_password="x",
                is_active=True, is_superuser=False, is_verified=True,
            )
            db.add(u)
            users.append(u)
        await db.flush()

        conn = Connection(
            organization_id=org.id,
            name=f"PowerBI {suffix}",
            type="powerbi",
            config={},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
        )
        conn.encrypt_credentials({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
        db.add(conn)
        await db.flush()

        ds = DataSource(
            name=f"PBI DS {suffix}",
            organization_id=org.id,
            is_active=True,
        )
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=ds.id, connection_id=conn.id,
        ))

        # Canonical catalog seeded by the SERVICE PRINCIPAL's crawl: it only
        # saw ModelA (e.g. the SP is not a Member of ModelB's workspace).
        ct = ConnectionTable(
            name="ModelA/T1",
            connection_id=conn.id,
            columns=[{"name": "id", "dtype": "int"}],
            pks=[], fks=[], no_rows=0,
            metadata_json={"powerbi": {"datasetId": "ds-a", "tableName": "T1"}},
        )
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(
            name="ModelA/T1",
            datasource_id=ds.id,
            connection_table_id=ct.id,
            is_active=False,
            metadata_json={"powerbi": {"datasetId": "ds-a", "tableName": "T1"}},
            columns=[{"name": "id", "dtype": "int"}],
            pks=[], fks=[], no_rows=0,
        ))

        # Both analysts HAVE signed in: real delegated token rows exist.
        for u in users:
            cred = UserConnectionCredentials(
                connection_id=str(conn.id),
                user_id=str(u.id),
                organization_id=str(org.id),
                auth_mode="oauth",
                is_active=True,
                is_primary=True,
                last_used_at=datetime.now(timezone.utc),
            )
            cred.encrypt_credentials({"access_token": "delegated-token"})
            db.add(cred)

        await db.commit()
        return {
            "org_id": org.id, "ds_id": ds.id,
            "user_ids": [u.id for u in users],
        }


async def _sync_overlay(ids, user_id):
    """Run the post-sign-in overlay sync for one user (the OAuth callback path)."""
    svc = DataSourceService()
    async with async_session_maker() as db:
        user = await db.get(User, user_id)
        ds = (await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .where(DataSource.id == ids["ds_id"])
        )).scalar_one()
        fetched = await svc.get_user_data_source_schema(db=db, data_source=ds, user=user)
        return sorted(t.name for t in fetched)


async def _paginate(ids, user_id):
    """What the tables selector shows this user."""
    svc = DataSourceService()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, user_id)
        resp = await svc.get_data_source_schema_paginated(
            db=db,
            data_source_id=ids["ds_id"],
            organization=org,
            page=1, page_size=100,
            include_inactive=True,
            current_user=user,
        )
        return sorted(t.name for t in resp.tables)


async def _canonical_snapshot(ids):
    """Return canonical DataSourceTable rows: {name: (discovered_by, id)} and
    the overlay links per user, to prove aggregation onto ONE org-level row."""
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(DataSourceTable).where(DataSourceTable.datasource_id == str(ids["ds_id"]))
        )).scalars().all()
        canonical = {
            r.name: ((r.metadata_json or {}).get("discovered_by"), str(r.id))
            for r in rows
        }
        overlay = (await db.execute(
            select(UserOverlayTable).where(
                UserOverlayTable.data_source_id == str(ids["ds_id"]),
                UserOverlayTable.is_accessible.is_(True),
            )
        )).scalars().all()
        links = {}
        for r in overlay:
            links.setdefault(r.table_name, set()).add(r.data_source_table_id)
        return canonical, links


@pytest.mark.e2e
def test_user_visible_model_missing_from_sp_catalog_is_selectable(monkeypatch):
    async def _fake_construct_client(self, db, data_source, current_user=None, **kw):
        assert current_user is not None, "overlay sync must run with the user's identity"
        return _FakeDelegatedPBIClient()

    monkeypatch.setattr(DataSourceService, "construct_client", _fake_construct_client)

    ids = _run(_seed())
    u1, u2 = ids["user_ids"]

    fetched1 = _run(_sync_overlay(ids, u1))
    selectable1 = _run(_paginate(ids, u1))
    print(f"\n[fixed] user1 live fetch:  {fetched1}")
    print(f"[fixed] user1 selectable:  {selectable1}")

    # The user's own crawl saw both models...
    assert fetched1 == ["ModelA/T1", "ModelB/T2"]
    # ...and BOTH are now selectable — the SP-invisible model included.
    assert "ModelB/T2" in selectable1, (
        "model returned by the user's own fetch must be selectable after Fix 3"
    )
    assert selectable1 == ["ModelA/T1", "ModelB/T2"]

    # A second user signs in and also sees ModelB.
    _run(_sync_overlay(ids, u2))

    canonical, links = _run(_canonical_snapshot(ids))
    print(f"[fixed] canonical rows (name -> discovered_by,id): {canonical}")
    print(f"[fixed] overlay links (name -> set of canonical ids): {links}")

    # ModelB now has exactly ONE org-level canonical row, created from user
    # discovery (provenance tagged) — not one per user.
    assert set(canonical) == {"ModelA/T1", "ModelB/T2"}
    assert canonical["ModelB/T2"][0] == "user", "user-discovered row must be tagged"
    assert canonical["ModelA/T1"][0] != "user", "SP-seeded row keeps its provenance"

    # Usage aggregation: both users' overlay rows for ModelB point at the SAME
    # canonical id (a single non-NULL link), so usage/instructions keyed on the
    # canonical table aggregate across users instead of fragmenting per user.
    model_b_links = links.get("ModelB/T2", set())
    assert None not in model_b_links, "overlay link must be populated, not NULL"
    assert len(model_b_links) == 1, (
        f"both users must link to one canonical ModelB row, got {model_b_links}"
    )
    assert next(iter(model_b_links)) == canonical["ModelB/T2"][1]


@pytest.mark.e2e
def test_user_discovered_model_rename_updates_canonical_name(monkeypatch):
    """A dataset/table rename keeps its (datasetId, tableName) identity but gets
    a new display name. The user-discovered canonical row must adopt the new
    name (so the selector shows it), not stay stuck on the old one — while
    remaining ONE row (matched by identity, no duplicate)."""
    clients = {"impl": _FakeDelegatedPBIClient()}

    async def _fake_construct_client(self, db, data_source, current_user=None, **kw):
        return clients["impl"]

    monkeypatch.setattr(DataSourceService, "construct_client", _fake_construct_client)

    ids = _run(_seed())
    u1 = ids["user_ids"][0]

    # First sign-in: ModelB discovered under its original name.
    _run(_sync_overlay(ids, u1))
    assert "ModelB/T2" in _run(_paginate(ids, u1))

    # Dataset renamed upstream; the same user re-syncs.
    clients["impl"] = _FakeRenamedModelBClient()
    _run(_sync_overlay(ids, u1))

    selectable = _run(_paginate(ids, u1))
    canonical, _ = _run(_canonical_snapshot(ids))
    print(f"\n[fixed] selectable after rename: {selectable}")
    print(f"[fixed] canonical rows after rename: {sorted(canonical)}")

    # New name is selectable; stale name is gone; still exactly one ModelB-ish row.
    assert "RenamedB/T2" in selectable
    assert "ModelB/T2" not in selectable
    assert "RenamedB/T2" in canonical and "ModelB/T2" not in canonical
    # ModelA untouched; catalog didn't grow a duplicate for the renamed dataset.
    assert set(canonical) == {"ModelA/T1", "RenamedB/T2"}
