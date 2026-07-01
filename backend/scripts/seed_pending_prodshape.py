"""Seed a PROD-SHAPE dataset for the pending-sweep perf bug.

Goal: reproduce production, where an org has only a HANDFUL of genuinely-live
pending changes but has accumulated MANY non-main builds + build_contents over
months (every AI suggestion / edit / git sync creates builds and build-contents).

Shape produced:
  - N instructions, each with a v1 recorded in the single is_main build.
  - Each instruction also carries MANY *accumulated* non-main builds across the
    lifecycle statuses (draft / pending_approval / approved / rejected) and all
    sources (user / ai / git). To make them realistic "history that is NOT
    pending", their proposed text is identical to main (no live hunk) OR their
    status/source is outside the pending selector. This bloats instruction_builds
    and build_contents massively while contributing ZERO to the pending set.
  - Only PENDING_LIVE instructions additionally get a draft/ai suggestion build
    whose text DIFFERS from main -> a genuine live pending hunk.

Usage:
  python scripts/seed_pending_prodshape.py [n_instructions] [builds_per_instruction] [n_pending_live]

Defaults: 3000 instructions, 8 history builds each, 7 live pending.
"""
import os
import sys
import uuid
import asyncio
import hashlib
from datetime import datetime, timedelta

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app_sweep.db")
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
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.instruction_version import InstructionVersion
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.models.data_source_membership import DataSourceMembership


def _uid():
    return str(uuid.uuid4())


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


async def _bulk(db, table, rows, chunk=2000):
    for i in range(0, len(rows), chunk):
        await db.execute(insert(table), rows[i:i + chunk])
    await db.commit()


# History builds cycle across all lifecycle statuses/sources so the candidate
# selector (status in draft/pending_approval, source in user/ai/git) sees a big
# haystack but most rows are filtered out or produce no live hunk.
# Realistic prod history: the overwhelming majority of accumulated builds are
# terminal (approved / rejected) across all sources — they are OUT of the pending
# selector (status in draft/pending_approval). A tiny minority are stale in-selector
# rows whose text == main (no live hunk). This is the shape where the composite
# index wins: it lets the planner SEEK the few in-selector rows instead of scanning
# every non-main build for the org.
HIST_COMBOS = [
    ("approved", "ai"),
    ("rejected", "ai"),
    ("approved", "user"),
    ("rejected", "user"),
    ("approved", "git"),
    ("rejected", "git"),
    ("approved", "ai"),
    ("rejected", "user"),
]


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    builds_per = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    n_pending = int(sys.argv[3]) if len(sys.argv) > 3 else 7

    _u = os.environ["BOW_DATABASE_URL"]
    _u = (_u.replace("postgresql://", "postgresql+asyncpg://", 1) if _u.startswith("postgresql://")
          else _u.replace("sqlite:///", "sqlite+aiosqlite:///", 1) if _u.startswith("sqlite:///") else _u)
    engine = create_async_engine(_u, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        assert user and org, "run signup first (sandbox@bow.dev)"
        org_id = str(org.id)
        uid = str(user.id)

        ds = (await db.execute(
            select(DataSource).where(DataSource.organization_id == org_id, DataSource.name == "Perf Agent")
        )).scalars().first()
        if ds is None:
            ds_id = _uid()
            await _bulk(db, DataSource.__table__, [{
                "id": ds_id, "name": "Perf Agent", "is_active": True,
                "is_public": False, "organization_id": org_id, "use_llm_sync": False}])
            await _bulk(db, DataSourceMembership.__table__, [{
                "id": _uid(), "data_source_id": ds_id, "principal_type": "user", "principal_id": uid}])
        else:
            ds_id = str(ds.id)

        # Single is_main build for the org.
        main_build = (await db.execute(
            select(InstructionBuild).where(
                InstructionBuild.organization_id == org_id, InstructionBuild.is_main == True  # noqa: E712
            )
        )).scalars().first()
        bn = ((await db.execute(
            select(InstructionBuild.build_number).where(InstructionBuild.organization_id == org_id)
            .order_by(InstructionBuild.build_number.desc()).limit(1)
        )).scalar() or 0) + 1
        if main_build is None:
            mb_id = _uid()
            await _bulk(db, InstructionBuild.__table__, [{
                "id": mb_id, "build_number": bn, "status": "approved", "source": "user",
                "is_main": True, "organization_id": org_id, "created_by_user_id": uid}])
            bn += 1
        else:
            mb_id = str(main_build.id)

        inst_rows, assoc_rows, v_rows, build_rows, bc_rows = [], [], [], [], []
        now = datetime.utcnow()

        for i in range(n):
            iid = _uid()
            base_text = (f"Instruction {i}: exclude refunded orders; use completion date; "
                         f"join orders to customers on customer_id; filter to active accounts.")
            v1_id = _uid()
            inst_rows.append({
                "id": iid, "text": base_text, "title": f"Instruction {i}", "status": "published",
                "category": "general", "kind": "instruction", "user_id": uid,
                "organization_id": org_id, "source_type": "user", "load_mode": "always",
                "thumbs_up": 0, "is_seen": True, "can_user_toggle": True,
                "current_version_id": v1_id,
            })
            assoc_rows.append({"instruction_id": iid, "data_source_id": ds_id})
            v_rows.append({
                "id": v1_id, "instruction_id": iid, "version_number": 1, "text": base_text,
                "title": f"Instruction {i}", "status": "published", "load_mode": "always",
                "content_hash": _hash(base_text), "created_by_user_id": uid})
            bc_rows.append({"id": _uid(), "build_id": mb_id,
                            "instruction_id": iid, "instruction_version_id": v1_id})

            # Accumulated history builds (bloat) — each with its own version+content.
            for b in range(builds_per):
                status, source = HIST_COMBOS[b % len(HIST_COMBOS)]
                bld_id = _uid()
                vh_id = _uid()
                # History text == main so even in-selector rows yield no live hunk.
                htext = base_text
                v_rows.append({
                    "id": vh_id, "instruction_id": iid, "version_number": 2 + b, "text": htext,
                    "title": f"Instruction {i}", "status": "published", "load_mode": "always",
                    "content_hash": _hash(htext + str(b)), "created_by_user_id": uid})
                build_rows.append({
                    "id": bld_id, "build_number": bn, "status": status, "source": source,
                    "is_main": False, "organization_id": org_id, "base_build_id": mb_id,
                    "created_by_user_id": uid, "description": f"history {status}/{source} {i}-{b}",
                    "created_at": now - timedelta(days=(builds_per - b))})
                bn += 1
                bc_rows.append({"id": _uid(), "build_id": bld_id,
                                "instruction_id": iid, "instruction_version_id": vh_id})

            # Genuine live pending for the first n_pending instructions.
            if i < n_pending:
                proposed = base_text.replace("active accounts", "active, non-trial accounts") + \
                    " Also cap the lookback to trailing 24 months."
                vp_id = _uid()
                v_rows.append({
                    "id": vp_id, "instruction_id": iid, "version_number": 100, "text": proposed,
                    "title": f"Instruction {i}", "status": "published", "load_mode": "always",
                    "content_hash": _hash(proposed), "created_by_user_id": uid})
                pb_id = _uid()
                build_rows.append({
                    "id": pb_id, "build_number": bn, "status": "draft", "source": "ai",
                    "is_main": False, "organization_id": org_id, "base_build_id": mb_id,
                    "created_by_user_id": uid, "description": f"LIVE pending {i}",
                    "created_at": now})
                bn += 1
                bc_rows.append({"id": _uid(), "build_id": pb_id,
                                "instruction_id": iid, "instruction_version_id": vp_id})

        await _bulk(db, Instruction.__table__, inst_rows)
        await _bulk(db, instruction_data_source_association, assoc_rows)
        await _bulk(db, InstructionVersion.__table__, v_rows)
        await _bulk(db, InstructionBuild.__table__, build_rows)
        await _bulk(db, BuildContent.__table__, bc_rows)

        print(f"seeded instructions={n} history_builds/inst={builds_per} live_pending={n_pending}")
        print(f"  instruction_builds total ~= {len(build_rows)+1}  build_contents total ~= {len(bc_rows)}")
        print(f"  org={org_id} ds={ds_id} main_build={mb_id}")


if __name__ == "__main__":
    asyncio.run(main())
