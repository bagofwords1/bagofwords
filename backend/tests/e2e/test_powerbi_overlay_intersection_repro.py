"""Reproduction: for a delegated (OBO) Power BI source, a signed-in user's
selectable tables are the INTERSECTION of their own live fetch and the
service-principal-seeded canonical catalog.

Reported symptom: "not all the semantic models are selectable in schema —
some are missing from the fetch". Even when the user's delegated crawl
returns a semantic model, it is only selectable if the service principal's
background indexing produced a canonical row with the SAME name:
`_upsert_user_overlay` links overlay rows to `DataSourceTable` by name and
leaves `data_source_table_id = NULL` on a miss, and
`get_data_source_schema_paginated` scopes a signed-in user to overlay rows
with `data_source_table_id IS NOT NULL`. Models visible to the user but not
to the SP (SP not a Member of that workspace, or the SP crawl dropped the
dataset) are fetched into the overlay and then filtered out of the selector.

See docs/feedback-loops/powerbi-missing-semantic-models.md.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_powerbi_overlay_intersection_repro.py -v -s --runxfail
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

XFAIL_REASON = (
    "known bug: overlay rows without a canonical name match get "
    "data_source_table_id=NULL and are filtered out of the tables selector; "
    "see docs/feedback-loops/powerbi-missing-semantic-models.md"
)


def _run(coro):
    return asyncio.run(coro)


def _table_payload(name):
    return {"name": name, "columns": [{"name": "id", "dtype": "int"}],
            "pks": [], "fks": [], "metadata_json": {}}


class _FakeDelegatedPBIClient:
    """Stands in for the live Power BI client crawling with the USER's token.
    The user can see two semantic models; the SP-seeded canonical catalog only
    contains the first one."""

    async def aget_schemas(self, progress_callback=None, prior_catalog=None):
        return [_table_payload("ModelA/T1"), _table_payload("ModelB/T2")]


async def _seed():
    """Direct DB seeding: this state (delegated token present without a real
    OAuth round-trip, SP catalog narrower than the user's access) cannot be
    produced through the API in a sandbox without live Entra credentials."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"PBI Overlay Org {suffix}")
        db.add(org)
        await db.flush()

        analyst = User(
            name="Analyst",
            email=f"analyst-{suffix}@example.com",
            hashed_password="x",
            is_active=True, is_superuser=False, is_verified=True,
        )
        db.add(analyst)
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
            metadata_json={},
        )
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(
            name="ModelA/T1",
            datasource_id=ds.id,
            connection_table_id=ct.id,
            is_active=False,
            metadata_json={},
            columns=[{"name": "id", "dtype": "int"}],
            pks=[], fks=[], no_rows=0,
        ))

        # The analyst HAS signed in: a real delegated token row exists.
        cred = UserConnectionCredentials(
            connection_id=str(conn.id),
            user_id=str(analyst.id),
            organization_id=str(org.id),
            auth_mode="oauth",
            is_active=True,
            is_primary=True,
            last_used_at=datetime.now(timezone.utc),
        )
        cred.encrypt_credentials({"access_token": "delegated-token"})
        db.add(cred)

        await db.commit()
        return {"org_id": org.id, "ds_id": ds.id, "user_id": analyst.id}


async def _sync_overlay_and_paginate(ids):
    svc = DataSourceService()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, ids["user_id"])
        ds = (await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .where(DataSource.id == ids["ds_id"])
        )).scalar_one()

        # Post-sign-in overlay sync (what the OAuth callback / refresh runs),
        # with the user's crawl returning BOTH models.
        fetched = await svc.get_user_data_source_schema(db=db, data_source=ds, user=user)
        fetched_names = sorted(t.name for t in fetched)

        overlay_rows = (await db.execute(
            select(UserOverlayTable).where(
                UserOverlayTable.data_source_id == str(ds.id),
                UserOverlayTable.user_id == str(user.id),
                UserOverlayTable.is_accessible.is_(True),
            )
        )).scalars().all()
        overlay = sorted(
            (r.table_name, r.data_source_table_id is not None) for r in overlay_rows
        )

        resp = await svc.get_data_source_schema_paginated(
            db=db,
            data_source_id=ids["ds_id"],
            organization=org,
            page=1,
            page_size=100,
            include_inactive=True,
            current_user=user,
        )
        selectable = sorted(t.name for t in resp.tables)
        return fetched_names, overlay, selectable


@pytest.mark.e2e
@pytest.mark.xfail(strict=True, reason=XFAIL_REASON)
def test_user_visible_model_missing_from_sp_catalog_is_still_selectable(monkeypatch):
    async def _fake_construct_client(self, db, data_source, current_user=None, **kw):
        assert current_user is not None, "overlay sync must run with the user's identity"
        return _FakeDelegatedPBIClient()

    monkeypatch.setattr(DataSourceService, "construct_client", _fake_construct_client)

    ids = _run(_seed())
    fetched, overlay, selectable = _run(_sync_overlay_and_paginate(ids))
    print(f"\n[repro] user's live fetch returned:   {fetched}")
    print(f"[repro] overlay rows (name, linked-to-canonical): {overlay}")
    print(f"[repro] selectable tables in schema:  {selectable}")

    # Guard: fail for the RIGHT reason — the user's fetch and overlay really
    # do contain both models before the selector drops one.
    assert fetched == ["ModelA/T1", "ModelB/T2"]
    assert ("ModelB/T2", False) in overlay, (
        "expected the SP-invisible model to land in the overlay without a "
        "canonical link (the mechanism under test)"
    )

    # The invariant (currently violated): every semantic model the signed-in
    # user's own credentials can see must be selectable for them.
    assert "ModelB/T2" in selectable, (
        "model returned by the user's own fetch is not selectable — dropped "
        "by the overlay∩canonical intersection"
    )
