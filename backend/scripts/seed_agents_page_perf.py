"""Seed the /agents page perf shape: many connections × many tables × few agents.

Reproduces the reported deployment (~50 connections, ~1,500 tables each,
3 agents) that makes the agents page take 40s+:

  * N_CONN system_only connections, each with N_TABLES ConnectionTable rows
  * one COMPLETED ConnectionIndexing row per connection carrying N_EVENTS
    event-log entries (what the list endpoints serialize per connection)
  * N_AGENTS data sources; connections are distributed round-robin across
    them, and every agent gets an ACTIVE DataSourceTable row mirroring each
    table of each of its connections (what refresh_schema's domain sync does)
  * the sandbox admin (sandbox@bow.dev) is a member of every agent

Usage:
  python scripts/seed_agents_page_perf.py [n_conn] [n_tables] [n_agents]

Defaults: 50 connections × 1500 tables × 3 agents. Idempotent-ish: re-running
adds a fresh batch (names are suffixed with a run nonce).
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
    n_tables = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    n_agents = int(sys.argv[3]) if len(sys.argv) > 3 else 3
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

        # Agents
        agent_ids = []
        for a in range(n_agents):
            ds = DataSource(id=str(uuid.uuid4()), name=f"Perf Agent {a} {nonce}",
                            is_active=True, is_public=False, organization_id=org_id)
            db.add(ds)
            await db.flush()
            db.add(DataSourceMembership(id=str(uuid.uuid4()), data_source_id=str(ds.id),
                                        principal_type="user", principal_id=str(user.id)))
            agent_ids.append(str(ds.id))
        await db.commit()

        for c in range(n_conn):
            conn_id = str(uuid.uuid4())
            db.add(Connection(
                id=conn_id, name=f"Hotel DB {c:02d} {nonce}", type="postgres",
                config={"host": "db.example.com", "port": 5432, "database": f"hotel_{c}"},
                credentials=None, is_active=True, auth_policy="system_only",
                last_synced_at=now, last_connection_status="success",
                last_connection_checked_at=now, organization_id=org_id,
            ))
            await db.flush()

            # Completed indexing run with a full event log (what lists serialize).
            db.add(ConnectionIndexing(
                id=str(uuid.uuid4()), connection_id=conn_id, status="completed",
                progress_done=n_tables, progress_total=n_tables,
                started_at=now - timedelta(minutes=5), finished_at=now,
                stats_json={"table_count": n_tables, "synced_domains": 1, "elapsed_s": 38.9},
                events_json=_events(),
            ))

            # Catalog tables for this connection (bulk insert).
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

            # Link to one agent (round-robin) and mirror every table as an
            # ACTIVE DataSourceTable — what sync_domain_tables_from_connection
            # produces after each reindex.
            ds_id = agent_ids[c % n_agents]
            await db.execute(insert(domain_connection).values(
                data_source_id=ds_id, connection_id=conn_id))
            dst_rows = [
                {
                    "id": str(uuid.uuid4()), "name": r["name"],
                    "datasource_id": ds_id, "connection_table_id": r["id"],
                    "is_active": True, "created_at": now, "updated_at": now,
                }
                for r in ct_rows
            ]
            await db.execute(insert(DataSourceTable), dst_rows)
            await db.commit()
            if (c + 1) % 10 == 0:
                print(f"  ...{c+1}/{n_conn} connections")

        print(f"seeded {n_conn} connections × {n_tables} tables, {n_agents} agents")
        print(f"org={org_id} agents={agent_ids}")


if __name__ == "__main__":
    asyncio.run(main())
