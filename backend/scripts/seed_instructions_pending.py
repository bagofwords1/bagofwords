"""Seed many instructions + versions + a main build + per-instruction PENDING
suggestion builds, to reproduce the /agents tree-pane slowness.

This recreates exactly the data shape that `GET /api/instructions/pending-changes`
walks: for every instruction there is one is_main build (live v1) plus a draft,
non-main suggestion build (proposed v2) whose text differs from main — i.e. a
live "pending review" hunk. The endpoint's per-instruction `review_hunks` loop
then has real work to do for each row.

Usage:
  python scripts/seed_instructions_pending.py <n_instructions> [pending_ratio]

  n_instructions : how many instructions to create (default 300)
  pending_ratio  : fraction that also get a pending suggestion build (default 1.0)

All rows attach to a single data source ("Perf Agent") so they show under one
agent in the tree pane. Idempotent-ish: re-running adds a fresh batch.
"""
import os
import sys
import uuid
import asyncio
import hashlib
from datetime import datetime

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select

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


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    ratio = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    engine = create_async_engine("sqlite+aiosqlite:///db/app.db", future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        assert user and org, "run signup first (sandbox@bow.dev) so user+org exist"
        org_id = str(org.id)

        # One data source so instructions render under a single agent in the tree.
        ds = (await db.execute(
            select(DataSource).where(DataSource.organization_id == org_id, DataSource.name == "Perf Agent")
        )).scalars().first()
        if ds is None:
            ds = DataSource(id=str(uuid.uuid4()), name="Perf Agent",
                            is_active=True, organization_id=org_id)
            db.add(ds)
            await db.flush()
        ds_id = str(ds.id)

        # Make the admin a member so the DS's instructions are visible in the list/tree.
        existing_member = (await db.execute(
            select(DataSourceMembership).where(
                DataSourceMembership.data_source_id == ds_id,
                DataSourceMembership.principal_id == str(user.id),
            )
        )).scalars().first()
        if existing_member is None:
            db.add(DataSourceMembership(id=str(uuid.uuid4()), data_source_id=ds_id,
                                        principal_type="user", principal_id=str(user.id)))
            await db.flush()

        # Get or create the org's single is_main build.
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
            main_build = InstructionBuild(id=str(uuid.uuid4()), build_number=next_bn, status="approved",
                                          source="user", is_main=True, organization_id=org_id,
                                          created_by_user_id=str(user.id))
            db.add(main_build)
            await db.flush()
            next_bn += 1
        main_build_id = str(main_build.id)

        n_pending = 0
        for i in range(n):
            iid = str(uuid.uuid4())
            base_text = (
                f"Instruction {i}: when computing revenue, always exclude refunded "
                f"orders and use the order completion date as the reporting date. "
                f"Join orders to customers on customer_id and filter to active accounts."
            )
            inst = Instruction(id=iid, text=base_text, title=f"Instruction {i}",
                               status="published", category="general", kind="instruction",
                               user_id=str(user.id), organization_id=org_id, source_type="user",
                               load_mode="always", thumbs_up=0, is_seen=True, can_user_toggle=True)
            db.add(inst)
            await db.flush()
            await db.execute(instruction_data_source_association.insert().values(
                instruction_id=iid, data_source_id=ds_id))

            # v1 = live text, recorded in the main build.
            v1 = InstructionVersion(id=str(uuid.uuid4()), instruction_id=iid, version_number=1,
                                    text=base_text, title=f"Instruction {i}", status="published",
                                    load_mode="always", content_hash=_hash(base_text),
                                    created_by_user_id=str(user.id))
            db.add(v1)
            await db.flush()
            inst.current_version_id = v1.id
            db.add(BuildContent(id=str(uuid.uuid4()), build_id=main_build_id,
                                instruction_id=iid, instruction_version_id=v1.id))

            # Per-instruction pending suggestion build: proposed v2 differs from main.
            if i < int(n * ratio):
                proposed_text = base_text.replace("active accounts", "active, non-trial accounts") \
                    + " Also cap the lookback window to the trailing 24 months."
                v2 = InstructionVersion(id=str(uuid.uuid4()), instruction_id=iid, version_number=2,
                                        text=proposed_text, title=f"Instruction {i}", status="published",
                                        load_mode="always", content_hash=_hash(proposed_text),
                                        created_by_user_id=str(user.id))
                db.add(v2)
                await db.flush()
                sug = InstructionBuild(id=str(uuid.uuid4()), build_number=next_bn, status="draft",
                                       source="ai", is_main=False, organization_id=org_id,
                                       base_build_id=main_build_id, created_by_user_id=str(user.id),
                                       description=f"AI suggestion for instruction {i}")
                next_bn += 1
                db.add(sug)
                await db.flush()
                db.add(BuildContent(id=str(uuid.uuid4()), build_id=str(sug.id),
                                    instruction_id=iid, instruction_version_id=v2.id))
                n_pending += 1

            if (i + 1) % 50 == 0:
                await db.commit()
                print(f"  ...{i+1}/{n}")

        await db.commit()
        print(f"seeded {n} instructions ({n_pending} with pending suggestion builds)")
        print(f"org={org_id} ds={ds_id} main_build={main_build_id}")


if __name__ == "__main__":
    asyncio.run(main())
