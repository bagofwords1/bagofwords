"""Seed the review-hunks perf shape: suggestion builds that SNAPSHOT every
instruction (copy_from_main), so each instruction appears in every pending
build as an unchanged carry-over row.

This is the shape a long-lived org accumulates: every hunk-accept creates a
fresh copy_from_main build, and self-learning keeps adding suggestion builds.
`GET /instructions/{id}/review-hunks` then finds the instruction in EVERY
pending build and (before the fix) pays a base-text query + word diff per
build even though only one build actually changed it.

Usage:
  python scripts/seed_review_hunks_perf.py [n_instructions] [n_builds]

Defaults: 40 instructions, 60 pending suggestion builds. Each build carries
all N instructions; build i changes instruction (i % N) and carries the rest
over unchanged.
"""
import os
import sys
import uuid
import asyncio
import hashlib

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
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.instruction_version import InstructionVersion
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def main():
    n_instr = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    n_builds = int(sys.argv[2]) if len(sys.argv) > 2 else 60

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

        ds = (await db.execute(
            select(DataSource).where(DataSource.organization_id == org_id,
                                     DataSource.name == "Review Perf Agent")
        )).scalars().first()
        if ds is None:
            ds = DataSource(id=str(uuid.uuid4()), name="Review Perf Agent",
                            is_active=True, organization_id=org_id)
            db.add(ds)
            await db.flush()
            db.add(DataSourceMembership(id=str(uuid.uuid4()), data_source_id=str(ds.id),
                                        principal_type="user", principal_id=str(user.id)))
        ds_id = str(ds.id)

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

        # Instructions with a live v1 in main.
        instr_ids, v1_ids = [], []
        for i in range(n_instr):
            iid = str(uuid.uuid4())
            text = (f"Rule {i}: compute occupancy from confirmed reservations only; "
                    f"exclude no-shows and same-day cancellations from the denominator.")
            db.add(Instruction(id=iid, text=text, title=f"Rule {i}", status="published",
                               category="general", kind="instruction", user_id=str(user.id),
                               organization_id=org_id, source_type="user", load_mode="always",
                               thumbs_up=0, is_seen=True, can_user_toggle=True))
            await db.flush()
            await db.execute(instruction_data_source_association.insert().values(
                instruction_id=iid, data_source_id=ds_id))
            v1 = InstructionVersion(id=str(uuid.uuid4()), instruction_id=iid, version_number=1,
                                    text=text, title=f"Rule {i}", status="published",
                                    load_mode="always", content_hash=_hash(text),
                                    created_by_user_id=str(user.id))
            db.add(v1)
            await db.flush()
            db.add(BuildContent(id=str(uuid.uuid4()), build_id=main_build_id,
                                instruction_id=iid, instruction_version_id=str(v1.id)))
            instr_ids.append(iid)
            v1_ids.append(str(v1.id))
        await db.commit()

        # Pending suggestion builds, each snapshotting ALL instructions
        # (copy_from_main carry-over) and actually changing exactly one.
        for b in range(n_builds):
            changed = b % n_instr
            sug = InstructionBuild(id=str(uuid.uuid4()), build_number=next_bn, status="draft",
                                   source="ai", is_main=False, organization_id=org_id,
                                   base_build_id=main_build_id, created_by_user_id=str(user.id),
                                   description=f"AI suggestion batch {b}")
            next_bn += 1
            db.add(sug)
            await db.flush()

            v2_text = (f"Rule {changed}: compute occupancy from confirmed reservations only; "
                       f"exclude no-shows, same-day cancellations AND overbookings (rev {b}).")
            v2 = InstructionVersion(id=str(uuid.uuid4()), instruction_id=instr_ids[changed],
                                    version_number=2 + b, text=v2_text, title=f"Rule {changed}",
                                    status="published", load_mode="always",
                                    content_hash=_hash(v2_text), created_by_user_id=str(user.id))
            db.add(v2)
            await db.flush()

            rows = []
            for i, iid in enumerate(instr_ids):
                rows.append({
                    "id": str(uuid.uuid4()), "build_id": str(sug.id), "instruction_id": iid,
                    "instruction_version_id": (str(v2.id) if i == changed else v1_ids[i]),
                })
            await db.execute(insert(BuildContent), rows)
            await db.commit()

        print(f"seeded {n_instr} instructions, {n_builds} pending builds "
              f"({n_builds * n_instr} build-content rows)")
        print(f"hot instruction (in every build, changed by ~{n_builds // n_instr} of them): {instr_ids[0]}")


if __name__ == "__main__":
    asyncio.run(main())
