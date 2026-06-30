"""Seed a LARGE org to reproduce the /agents page slowness at scale.

Creates many agents (DataSource) each with several connections (+ tables), plus
thousands of instructions distributed across agents, a fraction of which carry a
live pending suggestion build (a real "pending review" hunk).

This stresses every hot path behind the agents sidebar:
  * GET /data_sources/active[?show_all=true]  (agents list + embedded connections)
  * GET /api/instructions/counts              (per-agent badge aggregation)
  * GET /api/instructions/pending-changes     (org-wide pending sweep)

Usage:
  python scripts/seed_scale_agents.py \
      --agents 1000 --conns-per-agent 3 --tables-per-conn 5 \
      --instructions 5000 --pending-ratio 0.6

Idempotent-ish: prefixes all seeded rows with a run tag so re-runs add a fresh
batch without colliding on the unique (org,name) connection constraint.
"""
import os
import sys
import uuid
import asyncio
import hashlib
import argparse
from datetime import datetime

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
from app.models.connection import Connection
from app.models.domain_connection import domain_connection
from app.models.connection_table import ConnectionTable
from app.models.datasource_table import DataSourceTable
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.instruction_version import InstructionVersion
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.models.data_source_membership import DataSourceMembership


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _uid() -> str:
    return str(uuid.uuid4())


async def _bulk(db, table, rows, chunk=2000):
    """Insert rows in chunks via a core INSERT (fast path, no ORM unit-of-work)."""
    for i in range(0, len(rows), chunk):
        await db.execute(insert(table), rows[i:i + chunk])
    await db.commit()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=1000)
    ap.add_argument("--conns-per-agent", type=int, default=3)
    ap.add_argument("--tables-per-conn", type=int, default=5)
    ap.add_argument("--instructions", type=int, default=5000)
    ap.add_argument("--pending-ratio", type=float, default=0.6)
    ap.add_argument("--user-required", action="store_true",
                    help="make connections user_required+oauth (OBO) to exercise the per-connection auth path")
    args = ap.parse_args()

    tag = uuid.uuid4().hex[:6]
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
        uid = str(user.id)

        auth_policy = "user_required" if args.user_required else "system_only"
        auth_modes = ["oauth"] if args.user_required else None

        print(f"[seed:{tag}] agents={args.agents} conns/agent={args.conns_per_agent} "
              f"tables/conn={args.tables_per_conn} instructions={args.instructions} "
              f"pending_ratio={args.pending_ratio} auth_policy={auth_policy}")

        # ---- agents (data sources) + memberships ------------------------------
        agent_ids = [_uid() for _ in range(args.agents)]
        await _bulk(db, DataSource.__table__, [
            {"id": aid, "name": f"agent-{tag}-{i}", "is_active": True,
             "is_public": False, "organization_id": org_id, "use_llm_sync": False}
            for i, aid in enumerate(agent_ids)
        ])
        await _bulk(db, DataSourceMembership.__table__, [
            {"id": _uid(), "data_source_id": aid, "principal_type": "user", "principal_id": uid}
            for aid in agent_ids
        ])
        print(f"  agents+memberships done ({len(agent_ids)})")

        # ---- connections + domain links + tables ------------------------------
        conn_rows, link_rows, ctable_rows, dstable_rows = [], [], [], []
        for ai, aid in enumerate(agent_ids):
            for c in range(args.conns_per_agent):
                cid = _uid()
                conn_rows.append({
                    "id": cid, "name": f"conn-{tag}-{ai}-{c}", "type": "postgresql",
                    "config": {"host": "db.example.com", "port": 5432, "database": f"warehouse_{ai}",
                               "schema": "public", "catalog_key": None},
                    "is_active": True, "auth_policy": auth_policy,
                    "allowed_user_auth_modes": auth_modes,
                    "last_connection_status": "success",
                    "last_connection_checked_at": datetime.utcnow(),
                    "organization_id": org_id,
                })
                link_rows.append({"data_source_id": aid, "connection_id": cid})
                for t in range(args.tables_per_conn):
                    ctid = _uid()
                    ctable_rows.append({
                        "id": ctid, "name": f"tbl_{ai}_{c}_{t}", "connection_id": cid,
                        "columns": [{"name": "id", "dtype": "int"}, {"name": "amount", "dtype": "float"}],
                        "pks": ["id"], "fks": [], "no_rows": 1000,
                    })
                    dstable_rows.append({
                        "id": _uid(), "name": f"tbl_{ai}_{c}_{t}", "datasource_id": aid,
                        "connection_table_id": ctid, "is_active": True,
                    })
        await _bulk(db, Connection.__table__, conn_rows)
        await _bulk(db, domain_connection, link_rows)
        await _bulk(db, ConnectionTable.__table__, ctable_rows)
        await _bulk(db, DataSourceTable.__table__, dstable_rows)
        print(f"  connections={len(conn_rows)} tables={len(dstable_rows)} done")

        # ---- main build -------------------------------------------------------
        main_build = (await db.execute(
            select(InstructionBuild).where(
                InstructionBuild.organization_id == org_id, InstructionBuild.is_main == True  # noqa: E712
            )
        )).scalars().first()
        next_bn = ((await db.execute(
            select(InstructionBuild.build_number).where(InstructionBuild.organization_id == org_id)
            .order_by(InstructionBuild.build_number.desc()).limit(1)
        )).scalar() or 0) + 1
        if main_build is None:
            mb_id = _uid()
            await _bulk(db, InstructionBuild.__table__, [{
                "id": mb_id, "build_number": next_bn, "status": "approved", "source": "user",
                "is_main": True, "organization_id": org_id, "created_by_user_id": uid,
            }])
            next_bn += 1
        else:
            mb_id = str(main_build.id)

        # ---- instructions (+ v1, main build content) + pending builds ---------
        inst_rows, assoc_rows, v1_rows, mbc_rows = [], [], [], []
        v2_rows, sug_rows, sbc_rows = [], [], []
        n_pending = 0
        n = args.instructions
        for i in range(n):
            iid = _uid()
            aid = agent_ids[i % len(agent_ids)]  # spread across agents
            base_text = (f"Instruction {tag}-{i}: exclude refunded orders; use completion date; "
                         f"join orders to customers on customer_id; filter to active accounts.")
            inst_rows.append({
                "id": iid, "text": base_text, "title": f"Instruction {i}", "status": "published",
                "category": "general", "kind": "instruction", "user_id": uid,
                "organization_id": org_id, "source_type": "user", "load_mode": "always",
                "thumbs_up": 0, "is_seen": True, "can_user_toggle": True,
            })
            assoc_rows.append({"instruction_id": iid, "data_source_id": aid})
            v1id = _uid()
            v1_rows.append({
                "id": v1id, "instruction_id": iid, "version_number": 1, "text": base_text,
                "title": f"Instruction {i}", "status": "published", "load_mode": "always",
                "content_hash": _hash(base_text), "created_by_user_id": uid,
            })
            mbc_rows.append({"id": _uid(), "build_id": mb_id, "instruction_id": iid,
                             "instruction_version_id": v1id})
            if i < int(n * args.pending_ratio):
                proposed = base_text.replace("active accounts", "active, non-trial accounts") + \
                    " Cap lookback to trailing 24 months."
                v2id = _uid()
                v2_rows.append({
                    "id": v2id, "instruction_id": iid, "version_number": 2, "text": proposed,
                    "title": f"Instruction {i}", "status": "published", "load_mode": "always",
                    "content_hash": _hash(proposed), "created_by_user_id": uid,
                })
                sb_id = _uid()
                sug_rows.append({
                    "id": sb_id, "build_number": next_bn, "status": "draft", "source": "ai",
                    "is_main": False, "organization_id": org_id, "base_build_id": mb_id,
                    "created_by_user_id": uid, "description": f"AI suggestion for instruction {i}",
                })
                next_bn += 1
                sbc_rows.append({"id": _uid(), "build_id": sb_id, "instruction_id": iid,
                                 "instruction_version_id": v2id})
                n_pending += 1

        await _bulk(db, Instruction.__table__, inst_rows)
        await _bulk(db, instruction_data_source_association, assoc_rows)
        await _bulk(db, InstructionVersion.__table__, v1_rows)
        if v2_rows:
            await _bulk(db, InstructionVersion.__table__, v2_rows)
            await _bulk(db, InstructionBuild.__table__, sug_rows)
        await _bulk(db, BuildContent.__table__, mbc_rows)
        if sbc_rows:
            await _bulk(db, BuildContent.__table__, sbc_rows)

        print(f"  instructions={len(inst_rows)} pending={n_pending} done")
        print(f"[seed:{tag}] DONE  org={org_id}")


if __name__ == "__main__":
    asyncio.run(main())
