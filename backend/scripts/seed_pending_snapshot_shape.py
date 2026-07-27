"""Seed the REAL prod shape for the /agents pending-changes stall.

Unlike scripts/seed_pending_prodshape.py (which gives each accumulated build a
single build_content row), this mirrors what `BuildService.create_build` actually
does: **every** build copies ALL of main's contents (`copy_from_main=True`). So
`build_contents` grows as `builds x instructions`, and a long-lived org that has
run many training sessions / AI suggestions accumulates hundreds of non-main
draft builds that each snapshot the entire instruction set.

That is the shape where the org has only a HANDFUL of genuinely-live pending
changes (the "21 pending" in the report) yet the pending sweep has to grind over
hundreds of thousands of carry-over rows.

Shape produced:
  - N instructions, each with v1 recorded in the single is_main build.
  - M non-main builds in the pending selector (status draft/pending_approval,
    source user/ai/git), each carrying a FULL copy of main's contents at the
    SAME version ids -> pure carry-over, contributing zero pending changes.
  - P of those builds additionally point ONE instruction at a new, differing
    version -> a genuine live pending hunk.

Usage:
  python scripts/seed_pending_snapshot_shape.py [n_instructions] [n_builds] [n_pending_live]

Defaults: 600 instructions, 250 pending-selector builds, 21 live pending.
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


async def _bulk(db, table, rows, chunk=5000):
    for i in range(0, len(rows), chunk):
        await db.execute(insert(table), rows[i:i + chunk])
    await db.commit()


# Builds inside the pending selector; the sweep must consider every one of them.
SELECTOR_COMBOS = [
    ("draft", "ai"),
    ("draft", "user"),
    ("pending_approval", "ai"),
    ("draft", "git"),
]


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    n_builds = int(sys.argv[2]) if len(sys.argv) > 2 else 250
    n_pending = int(sys.argv[3]) if len(sys.argv) > 3 else 21

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
            select(DataSource).where(DataSource.organization_id == org_id, DataSource.name == "Project Agent")
        )).scalars().first()
        if ds is None:
            ds_id = _uid()
            await _bulk(db, DataSource.__table__, [{
                "id": ds_id, "name": "Project Agent", "is_active": True,
                "is_public": False, "organization_id": org_id, "use_llm_sync": False}])
            await _bulk(db, DataSourceMembership.__table__, [{
                "id": _uid(), "data_source_id": ds_id, "principal_type": "user", "principal_id": uid}])
        else:
            ds_id = str(ds.id)

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

        now = datetime.utcnow()
        inst_rows, assoc_rows, v_rows, build_rows, bc_rows = [], [], [], [], []

        # --- main build: N instructions at v1 --------------------------------
        main_version_of = {}
        for i in range(n):
            iid = _uid()
            v1_id = _uid()
            base_text = (f"Instruction {i}: exclude refunded orders; use completion date; "
                         f"join orders to customers on customer_id; filter to active accounts.")
            main_version_of[iid] = (v1_id, base_text)
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

        inst_ids = list(main_version_of.keys())

        # --- M non-main builds, each a FULL snapshot of main -----------------
        for b in range(n_builds):
            status, source = SELECTOR_COMBOS[b % len(SELECTOR_COMBOS)]
            bld_id = _uid()
            build_rows.append({
                "id": bld_id, "build_number": bn, "status": status, "source": source,
                "is_main": False, "organization_id": org_id, "base_build_id": mb_id,
                "created_by_user_id": uid, "description": f"session {b}",
                "created_at": now - timedelta(days=(n_builds - b))})
            bn += 1

            # This build changes ONE instruction (only for the first n_pending
            # builds); every other instruction is copied from main verbatim.
            changed_iid = inst_ids[b] if b < n_pending else None
            for iid in inst_ids:
                v1_id, base_text = main_version_of[iid]
                if iid == changed_iid:
                    proposed = (base_text.replace("active accounts", "active, non-trial accounts")
                                + " Also cap the lookback to trailing 24 months.")
                    vp_id = _uid()
                    v_rows.append({
                        "id": vp_id, "instruction_id": iid, "version_number": 100 + b,
                        "text": proposed, "title": f"Instruction {b}", "status": "published",
                        "load_mode": "always", "content_hash": _hash(proposed),
                        "created_by_user_id": uid})
                    bc_rows.append({"id": _uid(), "build_id": bld_id,
                                    "instruction_id": iid, "instruction_version_id": vp_id})
                else:
                    # Pure carry-over: same version id as main (what
                    # _copy_build_contents actually writes).
                    bc_rows.append({"id": _uid(), "build_id": bld_id,
                                    "instruction_id": iid, "instruction_version_id": v1_id})

        await _bulk(db, Instruction.__table__, inst_rows)
        await _bulk(db, instruction_data_source_association, assoc_rows)
        await _bulk(db, InstructionVersion.__table__, v_rows)
        await _bulk(db, InstructionBuild.__table__, build_rows)
        await _bulk(db, BuildContent.__table__, bc_rows)

        print(f"seeded instructions={n} pending-selector builds={n_builds} live_pending={n_pending}")
        print(f"  instruction_builds ~= {len(build_rows)+1}   build_contents ~= {len(bc_rows)}")
        print(f"  org={org_id} ds={ds_id} main_build={mb_id}")


if __name__ == "__main__":
    asyncio.run(main())


async def add_git_builds():
    """Append G git-sync-shaped builds: source='git', status='draft', and
    base_build_id=NULL (what git_service creates with copy_from_main=False).

    Each carries a FULL set of instruction contents at their OWN version rows
    whose text equals main -> zero genuine pending changes, but the carry-over
    NOT EXISTS prune can never match them (no base build), so every row survives
    into the Python diff pass.

    Usage: python -c "import asyncio,scripts.seed_pending_snapshot_shape as s; asyncio.run(s.add_git_builds())"
    """
    g = int(os.environ.get("N_GIT_BUILDS", "40"))
    _u = os.environ["BOW_DATABASE_URL"]
    _u = (_u.replace("postgresql://", "postgresql+asyncpg://", 1) if _u.startswith("postgresql://")
          else _u.replace("sqlite:///", "sqlite+aiosqlite:///", 1) if _u.startswith("sqlite:///") else _u)
    engine = create_async_engine(_u, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        org_id, uid = str(org.id), str(user.id)
        mb = (await db.execute(select(InstructionBuild).where(
            InstructionBuild.organization_id == org_id, InstructionBuild.is_main == True))).scalars().first()  # noqa: E712
        main_rows = (await db.execute(
            select(BuildContent.instruction_id, InstructionVersion.text)
            .join(InstructionVersion, InstructionVersion.id == BuildContent.instruction_version_id)
            .where(BuildContent.build_id == str(mb.id))
        )).all()
        bn = ((await db.execute(
            select(InstructionBuild.build_number).where(InstructionBuild.organization_id == org_id)
            .order_by(InstructionBuild.build_number.desc()).limit(1)
        )).scalar() or 0) + 1

        now = datetime.utcnow()
        v_rows, build_rows, bc_rows = [], [], []
        for b in range(g):
            bld_id = _uid()
            build_rows.append({
                "id": bld_id, "build_number": bn, "status": "draft", "source": "git",
                "is_main": False, "organization_id": org_id, "base_build_id": None,
                "created_by_user_id": uid, "description": f"git sync {b}",
                "commit_sha": _hash(f"sync{b}")[:40], "branch": "main",
                "created_at": now - timedelta(days=(g - b))})
            bn += 1
            for iid, txt in main_rows:
                vg_id = _uid()
                v_rows.append({
                    "id": vg_id, "instruction_id": str(iid), "version_number": 500 + b,
                    "text": txt, "title": None, "status": "published", "load_mode": "always",
                    "content_hash": _hash((txt or "") + str(b)), "created_by_user_id": uid})
                bc_rows.append({"id": _uid(), "build_id": bld_id,
                                "instruction_id": str(iid), "instruction_version_id": vg_id})

        await _bulk(db, InstructionVersion.__table__, v_rows)
        await _bulk(db, InstructionBuild.__table__, build_rows)
        await _bulk(db, BuildContent.__table__, bc_rows)
        print(f"added {g} git draft builds (base_build_id=NULL), {len(bc_rows)} build_contents")
