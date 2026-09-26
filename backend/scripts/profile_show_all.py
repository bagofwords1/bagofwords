"""Profile get_active_data_sources(show_all=True) — the 'Show all' admin view.

Bootstraps an org+admin user, seeds N agents x M connections x T tables
(type=mssql, system_only — the user's case), then times the service call,
counts SQL statements, and times _conn_connector_key / _ds_connector_key.
"""
import os, sys, uuid, asyncio, time, argparse
from datetime import datetime

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app_showall.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy import event, select, insert
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.models  # noqa
import pkgutil, importlib
for _, modname, _ in pkgutil.iter_modules(app.models.__path__):
    if modname == "application":
        continue
    importlib.import_module(f"app.models.{modname}")

from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.connection import Connection
from app.models.domain_connection import domain_connection
from app.models.connection_table import ConnectionTable
from app.models.datasource_table import DataSourceTable
from app.models.data_source_membership import DataSourceMembership
from app.services.organization_service import OrganizationService
from app.schemas.organization_schema import OrganizationCreate
from app.services.data_source_service import DataSourceService
import app.services.data_source_service as dss_mod


def _uid():
    return str(uuid.uuid4())


async def _bulk(db, table, rows, chunk=2000):
    for i in range(0, len(rows), chunk):
        await db.execute(insert(table), rows[i:i + chunk])
    await db.commit()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--conns-per-agent", type=int, default=3)
    ap.add_argument("--tables-per-conn", type=int, default=10)
    args = ap.parse_args()

    u = os.environ["BOW_DATABASE_URL"].replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    engine = create_async_engine(u, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        # bootstrap user + org (org create seeds admin full_admin_access role)
        email = f"sandbox-{_uid()[:8]}@bow.dev"
        user = User(email=email, name="Sandbox")
        if hasattr(user, "hashed_password"):
            user.hashed_password = "x"
        db.add(user)
        await db.commit()
        await db.refresh(user)

        org_schema = await OrganizationService().create_organization(
            db, OrganizationCreate(name=f"Org-{_uid()[:6]}"), user
        )
        org = (await db.execute(select(Organization).where(Organization.id == org_schema.id))).scalars().first()
        org_id = str(org.id)
        uid = str(user.id)
        print(f"org={org_id} user={uid}")

        # seed agents
        agent_ids = [_uid() for _ in range(args.agents)]
        await _bulk(db, DataSource.__table__, [
            {"id": aid, "name": f"agent-{i}", "is_active": True,
             "is_public": False, "organization_id": org_id, "use_llm_sync": False}
            for i, aid in enumerate(agent_ids)
        ])
        await _bulk(db, DataSourceMembership.__table__, [
            {"id": _uid(), "data_source_id": aid, "principal_type": "user", "principal_id": uid}
            for aid in agent_ids
        ])
        conn_rows, link_rows, ctable_rows, dstable_rows = [], [], [], []
        for ai, aid in enumerate(agent_ids):
            for c in range(args.conns_per_agent):
                cid = _uid()
                conn_rows.append({
                    "id": cid, "name": f"conn-{ai}-{c}", "type": "mssql",
                    "config": {"host": "sql.example.com", "port": 1433, "database": f"wh_{ai}",
                               "schema": "dbo", "catalog_key": None},
                    "is_active": True, "auth_policy": "system_only",
                    "allowed_user_auth_modes": None,
                    "last_connection_status": "success",
                    "last_connection_checked_at": datetime.utcnow(),
                    "organization_id": org_id,
                })
                link_rows.append({"data_source_id": aid, "connection_id": cid})
                for t in range(args.tables_per_conn):
                    ctid = _uid()
                    ctable_rows.append({
                        "id": ctid, "name": f"tbl_{ai}_{c}_{t}", "connection_id": cid,
                        "columns": [{"name": "id", "dtype": "int"}], "pks": ["id"], "fks": [], "no_rows": 1000,
                    })
                    dstable_rows.append({
                        "id": _uid(), "name": f"tbl_{ai}_{c}_{t}", "datasource_id": aid,
                        "connection_table_id": ctid, "is_active": True,
                    })
        await _bulk(db, Connection.__table__, conn_rows)
        await _bulk(db, domain_connection, link_rows)
        await _bulk(db, ConnectionTable.__table__, ctable_rows)
        await _bulk(db, DataSourceTable.__table__, dstable_rows)
        print(f"seeded agents={args.agents} conns={len(conn_rows)} tables={len(dstable_rows)}")

    # --- profile in a fresh session ---
    sql_count = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        sql_count["n"] += 1

    # instrument the connector-key helpers
    orig_conn_key = dss_mod._conn_connector_key
    orig_ds_key = dss_mod._ds_connector_key
    timing = {"conn_key_calls": 0, "conn_key_time": 0.0, "ds_key_calls": 0, "ds_key_time": 0.0}

    def wrapped_conn_key(conn):
        t = time.perf_counter()
        r = orig_conn_key(conn)
        timing["conn_key_time"] += time.perf_counter() - t
        timing["conn_key_calls"] += 1
        return r

    def wrapped_ds_key(d):
        t = time.perf_counter()
        r = orig_ds_key(d)
        timing["ds_key_time"] += time.perf_counter() - t
        timing["ds_key_calls"] += 1
        return r

    dss_mod._conn_connector_key = wrapped_conn_key
    dss_mod._ds_connector_key = wrapped_ds_key

    async with Session() as db:
        org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
        user = (await db.execute(select(User).where(User.id == uid))).scalars().first()
        svc = DataSourceService()

        # warm up imports/caches once (not counted)
        await svc.get_active_data_sources(db=db, organization=org, current_user=user,
                                           include_unconnected=True, show_all=True)

        sql_count["n"] = 0
        for k in timing:
            timing[k] = 0.0 if "time" in k else 0
        t0 = time.perf_counter()
        items = await svc.get_active_data_sources(db=db, organization=org, current_user=user,
                                                  include_unconnected=True, show_all=True)
        elapsed = time.perf_counter() - t0

    print("\n===== PROFILE =====")
    print(f"agents returned : {len(items)}")
    print(f"total time      : {elapsed*1000:.1f} ms")
    print(f"SQL statements  : {sql_count['n']}")
    print(f"_conn_connector_key: calls={timing['conn_key_calls']} time={timing['conn_key_time']*1000:.1f} ms")
    print(f"_ds_connector_key  : calls={timing['ds_key_calls']} time={timing['ds_key_time']*1000:.1f} ms")
    ck = timing["conn_key_time"] + timing["ds_key_time"]
    print(f"connector-key TOTAL: {ck*1000:.1f} ms ({100*ck/elapsed:.0f}% of request)")


if __name__ == "__main__":
    asyncio.run(main())
