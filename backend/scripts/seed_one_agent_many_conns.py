"""Seed the "one agent owns every connection" shape: N connections × N tables
all attached to a SINGLE agent, plus one empty agent. Only a handful of tables
are ACTIVE (selected); the rest are the unselected catalog.

Reproduces the reported deployment (50 connections × ~3,000 tables each on ONE
agent — ~150k DataSourceTable rows — with only ~100 tables active across all
50 connections; a second agent unused) where /agents takes forever and the
whole system degrades. Differs from seed_agents_page_perf.py in four ways that
matter for this loop:

  * every connection joins the SAME data source (no round-robin), so the
    per-agent volumes (50 connections, 75k DataSourceTable rows) match the
    report;
  * connections are seeded with type="postgresql" (the registry name) and a
    config pointing at HOST:PORT (default 127.0.0.1:55445 — run
    scripts/slow_tcp_stub.py there), so ConnectionService.test_connection
    builds a real PostgresqlClient and actually dials the address — required
    to reproduce the stale-connection retest sweep in
    DataSourceService.get_data_source;
  * DataSourceTable rows carry `columns` (like the rows
    sync_domain_tables_from_connection writes in production), so the
    /full_schema legacy path serializes full Table objects instead of
    crashing on `columns=None`;
  * only N_ACTIVE tables per connection get is_active=True — the reported org
    has ~100 active tables across 50 connections, yet the row-volume costs are
    driven by the ~150k INACTIVE catalog rows.

Usage:
  python scripts/seed_one_agent_many_conns.py [n_conn] [n_tables] [n_active_per_conn] [host] [port]

Defaults: 50 connections × 3000 tables, 2 active per connection (=100 org-wide),
stub at 127.0.0.1:55445. Requires the sandbox admin (sandbox@bow.dev) to exist.
Re-running adds a fresh batch.
"""
import os
import sys
import uuid
import asyncio
from datetime import datetime, timedelta

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, insert

import app.models  # noqa
import pkgutil, importlib
for _, modname, _ in pkgutil.iter_modules(app.models.__path__):
    if modname == "application":
        continue
    importlib.import_module(f"app.models.{modname}")

from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.data_source_membership import DataSourceMembership
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.connection_indexing import ConnectionIndexing
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection

N_EVENTS = 200  # matches ConnectionIndexingService._EVENT_LOG_MAX


def _columns():
    return [{"name": f"col_{i}", "dtype": "varchar"} for i in range(8)]


def _events(n=N_EVENTS):
    base = datetime.utcnow() - timedelta(hours=1)
    return [
        {
            "ts": (base + timedelta(seconds=i)).isoformat() + "Z",
            "level": "info",
            "phase": "schema",
            "message": f"Discovered table dbo.some_table_{i} (8 columns)",
            "done": i,
            "total": n,
        }
        for i in range(n)
    ]


async def main():
    n_conn = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    n_tables = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    n_active = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    host = sys.argv[4] if len(sys.argv) > 4 else "127.0.0.1"
    port = int(sys.argv[5]) if len(sys.argv) > 5 else 55445
    nonce = uuid.uuid4().hex[:6]

    _u = os.environ["BOW_DATABASE_URL"]
    _u = (_u.replace("postgresql://", "postgresql+asyncpg://", 1) if _u.startswith("postgresql://")
          else _u.replace("sqlite:///", "sqlite+aiosqlite:///", 1) if _u.startswith("sqlite:///") else _u)
    engine = create_async_engine(_u, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        assert user and org, "run signup first (sandbox@bow.dev) so user+org exist"
        org_id = str(org.id)
        now = datetime.utcnow()

        # Agent 0 gets every connection; agent 1 stays empty.
        agent_ids = []
        for name in (f"Hub Agent {nonce}", f"Empty Agent {nonce}"):
            ds = DataSource(id=str(uuid.uuid4()), name=name,
                            is_active=True, is_public=False, organization_id=org_id)
            db.add(ds)
            await db.flush()
            db.add(DataSourceMembership(id=str(uuid.uuid4()), data_source_id=str(ds.id),
                                        principal_type="user", principal_id=str(user.id)))
            agent_ids.append(str(ds.id))
        await db.commit()
        hub_id = agent_ids[0]

        for c in range(n_conn):
            conn_id = str(uuid.uuid4())
            db.add(Connection(
                id=conn_id, name=f"Hotel DB {c:02d} {nonce}", type="postgresql",
                config={"host": host, "port": port, "database": f"hotel_{c}", "user": "bow"},
                credentials=None, is_active=True, auth_policy="system_only",
                last_synced_at=now, last_connection_status="success",
                last_connection_checked_at=now, organization_id=org_id,
            ))
            await db.flush()

            db.add(ConnectionIndexing(
                id=str(uuid.uuid4()), connection_id=conn_id, status="completed",
                progress_done=n_tables, progress_total=n_tables,
                started_at=now - timedelta(minutes=5), finished_at=now,
                stats_json={"table_count": n_tables, "synced_domains": 1, "elapsed_s": 38.9},
                events_json=_events(),
            ))

            ct_rows = [
                {
                    "id": str(uuid.uuid4()), "name": f"dbo.table_{c:02d}_{t}",
                    "connection_id": conn_id, "columns": _columns(),
                    "pks": [], "fks": [], "no_rows": 0,
                    "created_at": now, "updated_at": now,
                }
                for t in range(n_tables)
            ]
            await db.execute(insert(ConnectionTable), ct_rows)

            await db.execute(insert(domain_connection).values(
                data_source_id=hub_id, connection_id=conn_id))
            # Mirror what sync_domain_tables_from_connection writes: rows WITH
            # columns/pks/fks (/full_schema serializes these fields). Only the
            # first n_active per connection are selected (is_active=True); the
            # rest are the unselected catalog the org never uses.
            dst_rows = [
                {
                    "id": str(uuid.uuid4()), "name": r["name"],
                    "datasource_id": hub_id, "connection_table_id": r["id"],
                    "columns": r["columns"], "pks": [], "fks": [],
                    "is_active": t < n_active, "created_at": now, "updated_at": now,
                }
                for t, r in enumerate(ct_rows)
            ]
            await db.execute(insert(DataSourceTable), dst_rows)
            await db.commit()
            if (c + 1) % 10 == 0:
                print(f"  ...{c+1}/{n_conn} connections")

        print(f"seeded {n_conn} connections × {n_tables} tables "
              f"({n_active} active each → {n_conn * n_active} org-wide) → 1 hub agent (+1 empty)")
        print(f"org={org_id} hub_agent={hub_id} empty_agent={agent_ids[1]}")


if __name__ == "__main__":
    asyncio.run(main())
