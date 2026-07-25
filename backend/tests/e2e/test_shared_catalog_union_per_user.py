"""Fix validation: the shared catalog is the UNION of every identity's view.

On a `user_required` connection (delegated/OBO tokens, per-user DB logins, or
Kerberos SSO) `refresh_schema` crawls with the CALLER's credentials, so the
result is that one identity's slice of the catalog. Before the fix it was
written back over the org-shared `ConnectionTable` catalog and every row the
caller could not see was DELETED — so a least-privileged member clicking
"Reload tables" pruned the catalog for the whole org (and silently dropped the
agent's table selection with it).

The rule now:
  - a per-user crawl may only ADD tables (union); it never rewrites or prunes
    shared rows — the caller's own view lives in their overlay;
  - an org-identity crawl is authoritative and prunes, but keeps rows that at
    least one user can still see (per-user grants can exceed the service
    account's).

Covers the three deployment shapes:
  S1 org identity sees everything, users see different subsets
  S2 org identity sees LESS than the users (canonical must still be the union)
  S3 usage metrics (table_stats) stay org-wide regardless of who reloads

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_shared_catalog_union_per_user.py -v -s
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.organization import Organization
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.models.user_connection_overlay import UserConnectionTable
from app.services.connection_service import ConnectionService


def _run(coro):
    return asyncio.run(coro)


def _table(name):
    return {
        "name": name,
        "columns": [{"name": "id", "dtype": "int"}],
        "pks": [], "fks": [], "metadata_json": {},
    }


class _FakeClient:
    """Returns whatever slice of the catalog the identity is supposed to see."""

    def __init__(self, names):
        self._names = list(names)

    async def aget_schemas(self, progress_callback=None, prior_catalog=None, **kw):
        return [_table(n) for n in self._names]


async def _seed(canonical_names, user_overlay=None):
    """A postgres-style `user_required` connection (per-user DB logins — the
    NON-OAuth path) with a seeded canonical catalog and optional per-user
    overlay rows."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Union Org {suffix}")
        db.add(org)
        await db.flush()

        owner = User(name="Owner", email=f"owner-{suffix}@example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        user2 = User(name="User Two", email=f"u2-{suffix}@example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        user3 = User(name="User Three", email=f"u3-{suffix}@example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        db.add_all([owner, user2, user3])
        await db.flush()

        conn = Connection(
            organization_id=org.id,
            name=f"PG {suffix}",
            type="postgresql",
            config={"host": "127.0.0.1", "port": 5432, "database": "d"},
            auth_policy="user_required",
            allowed_user_auth_modes=["basic"],
        )
        conn.encrypt_credentials({"user": "svc", "password": "p"})
        db.add(conn)
        await db.flush()

        ds = DataSource(name=f"PG DS {suffix}", organization_id=org.id,
                        is_active=True, owner_user_id=owner.id)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=ds.id, connection_id=conn.id))

        ct_ids = {}
        for tname in canonical_names:
            ct = ConnectionTable(name=tname, connection_id=conn.id,
                                 columns=[{"name": "id", "dtype": "int"}],
                                 pks=[], fks=[], no_rows=0, metadata_json={})
            db.add(ct)
            await db.flush()
            ct_ids[tname] = ct.id
            db.add(DataSourceTable(
                name=tname, datasource_id=ds.id, connection_table_id=ct.id,
                is_active=True, metadata_json={},
                columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[], no_rows=0))

        # Per-user login (the non-OAuth `user_required` flavor).
        for u in (user2, user3):
            row = UserConnectionCredentials(
                connection_id=str(conn.id), user_id=str(u.id),
                organization_id=str(org.id), auth_mode="basic",
                is_active=True, is_primary=True,
            )
            row.encrypt_credentials({"user": u.email, "password": "p"})
            db.add(row)

        for uid_key, names in (user_overlay or {}).items():
            u = {"user2": user2, "user3": user3, "owner": owner}[uid_key]
            for tname in names:
                db.add(UserConnectionTable(
                    connection_id=str(conn.id), user_id=str(u.id),
                    table_name=tname, connection_table_id=ct_ids.get(tname),
                    is_accessible=True, status="accessible"))

        await db.commit()
        return {"org_id": org.id, "ds_id": ds.id, "conn_id": conn.id,
                "owner_id": owner.id, "user2_id": user2.id, "user3_id": user3.id}


async def _refresh(ids, user_id, visible_names):
    """Run the real refresh_schema; only the network client is faked, so the
    identity classification in resolve_credentials runs for real."""
    svc = ConnectionService()

    async def _fake_construct_client(self, db, connection, current_user=None, **kw):
        # Exercise the REAL credential resolution so last_credential_identity
        # is set exactly as it would be in production.
        await self.resolve_credentials(db, connection, current_user)
        return _FakeClient(visible_names)

    orig = ConnectionService.construct_client
    ConnectionService.construct_client = _fake_construct_client
    try:
        async with async_session_maker() as db:
            conn = await db.get(Connection, ids["conn_id"])
            user = await db.get(User, user_id) if user_id else None
            await svc.refresh_schema(db=db, connection=conn, current_user=user)
            return getattr(svc, "last_credential_identity", None)
    finally:
        ConnectionService.construct_client = orig


async def _canonical(ids):
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(ConnectionTable.name).where(
                ConnectionTable.connection_id == ids["conn_id"],
                ConnectionTable.deleted_at.is_(None),
            )
        )).scalars().all()
        return sorted(rows)


@pytest.mark.e2e
def test_restricted_user_reload_does_not_prune_shared_catalog():
    """S1: org identity sees {sales, finance}; user2 only {sales}. user2's
    Reload must NOT delete finance from the org-shared catalog."""
    ids = _run(_seed(["sales", "finance"]))
    assert _run(_canonical(ids)) == ["finance", "sales"]

    identity = _run(_refresh(ids, ids["user2_id"], ["sales"]))
    after = _run(_canonical(ids))
    print(f"\n[S1] crawl identity={identity!r}; canonical after restricted reload={after}")

    assert identity == "user", "per-user login must classify as a user identity"
    assert after == ["finance", "sales"], (
        "a restricted user's reload must not prune the org-shared catalog"
    )


@pytest.mark.e2e
def test_user_reload_unions_new_tables_into_shared_catalog():
    """S2: org identity only ever saw {table1}; user2 is granted more. Their
    reload must ADD the extra tables so the shared catalog is the union."""
    ids = _run(_seed(["table1"]))

    identity = _run(_refresh(ids, ids["user2_id"], ["table1", "table2", "table3"]))
    after = _run(_canonical(ids))
    print(f"\n[S2] crawl identity={identity!r}; canonical after user reload={after}")

    assert identity == "user"
    assert after == ["table1", "table2", "table3"], (
        "tables a user can see but the service account cannot must be unioned in"
    )


@pytest.mark.e2e
def test_org_identity_prunes_only_tables_no_user_can_see():
    """An org-identity crawl is authoritative, but must keep rows some user can
    still see (their grants exceed the service account's)."""
    ids = _run(_seed(
        ["sales", "finance", "ghost"],
        user_overlay={"user2": ["sales", "finance"]},
    ))

    # System crawl (no user in context) now sees only `sales`.
    identity = _run(_refresh(ids, None, ["sales"]))
    after = _run(_canonical(ids))
    print(f"\n[prune] crawl identity={identity!r}; canonical after org reload={after}")

    assert identity == "system"
    assert "ghost" not in after, "a table no identity can see must be pruned"
    assert after == ["finance", "sales"], (
        "a table still visible to a user must survive an org-identity prune"
    )


@pytest.mark.e2e
def test_prune_check_handles_more_names_than_the_bind_parameter_limit():
    """The prune check asks "which of these can any user still see?" with an
    IN (...) over every table the org identity just failed to see. Unchunked,
    a few thousand names exceed the driver's bind-parameter ceiling (SQLite's
    default is 999) and the prune errors out instead of running."""
    ids = _run(_seed(["sales"]))
    names = [f"t_{i:05d}" for i in range(3000)]

    async def _check():
        svc = ConnectionService()
        async with async_session_maker() as db:
            return await svc._user_visible_table_names(db, ids["conn_id"], names)

    visible = _run(_check())
    print(f"\n[chunking] queried {len(names)} candidate names -> {len(visible)} visible")
    assert visible == set(), "no overlay rows exist for these names"


@pytest.mark.e2e
def test_usage_metrics_stay_org_wide_across_per_user_reloads():
    """S3: table_stats are keyed by (org, data source, table) — never by user —
    and survive reloads driven by any identity."""
    from app.models.table_stats import TableStats

    ids = _run(_seed(["sales", "finance"]))

    async def _seed_stats():
        async with async_session_maker() as db:
            ds_tables = (await db.execute(
                select(DataSourceTable).where(DataSourceTable.datasource_id == ids["ds_id"])
            )).scalars().all()
            by_name = {t.name: t for t in ds_tables}
            for name, count in (("sales", 42), ("finance", 7)):
                db.add(TableStats(
                    org_id=ids["org_id"], report_id=None,
                    data_source_id=ids["ds_id"], table_fqn=name,
                    datasource_table_id=by_name[name].id,
                    usage_count=count, success_count=count,
                    weighted_usage_count=float(count), unique_users=2,
                ))
            await db.commit()

    async def _read_stats():
        async with async_session_maker() as db:
            rows = (await db.execute(
                select(TableStats.table_fqn, TableStats.usage_count, TableStats.unique_users)
                .where(TableStats.data_source_id == ids["ds_id"],
                       TableStats.report_id.is_(None))
            )).all()
            return {r[0]: (int(r[1]), int(r[2])) for r in rows}

    _run(_seed_stats())
    before = _run(_read_stats())

    # A restricted user reloads (sees only `sales`), then the org identity does.
    _run(_refresh(ids, ids["user2_id"], ["sales"]))
    _run(_refresh(ids, None, ["sales", "finance"]))
    after = _run(_read_stats())

    print(f"\n[S3] table_stats before={before} after={after}")
    assert after == before == {"sales": (42, 2), "finance": (7, 2)}, (
        "usage metrics are org-wide rollups (no user_id dimension) and must not "
        "be reset or fragmented by per-user reloads"
    )
