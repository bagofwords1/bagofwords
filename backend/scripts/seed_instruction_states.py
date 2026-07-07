"""Seed 4 instructions covering every (status × pending) combination, under one
agent, so the KnowledgeExplorer tree shows: active, inactive, active+pending,
inactive+pending. Modeled on backend/scripts/seed_instructions_pending.py.

Run: cd backend && BOW_DATABASE_URL=sqlite:///db/agent.db uv run python scripts/seed_instruction_states.py
"""
import os
import uuid
import asyncio
import hashlib

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/agent.db")
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


ROWS = [
    # (title, status, has_pending)
    ("Revenue definitions", "published", False),
    ("Churn playbook (retired)", "draft", False),
    ("Discount policy", "published", True),
    ("Legacy pricing rules", "draft", True),
]


async def main():
    _u = os.environ["BOW_DATABASE_URL"]
    _u = (_u.replace("postgresql://", "postgresql+asyncpg://", 1) if _u.startswith("postgresql://")
          else _u.replace("sqlite:///", "sqlite+aiosqlite:///", 1) if _u.startswith("sqlite:///") else _u)
    engine = create_async_engine(_u, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "admin@example.com"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        assert user and org, "run seed_org.py first so admin@example.com + org exist"
        org_id = str(org.id)

        ds = (await db.execute(
            select(DataSource).where(DataSource.organization_id == org_id, DataSource.name == "Sales Agent")
        )).scalars().first()
        if ds is None:
            ds = DataSource(id=str(uuid.uuid4()), name="Sales Agent",
                            is_active=True, organization_id=org_id)
            db.add(ds)
            await db.flush()
        ds_id = str(ds.id)

        member = (await db.execute(
            select(DataSourceMembership).where(
                DataSourceMembership.data_source_id == ds_id,
                DataSourceMembership.principal_id == str(user.id),
            )
        )).scalars().first()
        if member is None:
            db.add(DataSourceMembership(id=str(uuid.uuid4()), data_source_id=ds_id,
                                        principal_type="user", principal_id=str(user.id)))
            await db.flush()

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

        for title, status, has_pending in ROWS:
            base_text = (
                f"{title}: when computing metrics, always exclude refunded orders and "
                f"use the order completion date as the reporting date."
            )
            iid = str(uuid.uuid4())
            inst = Instruction(id=iid, text=base_text, title=title,
                               status=status, category="general", kind="instruction",
                               user_id=str(user.id), organization_id=org_id, source_type="user",
                               load_mode="always", thumbs_up=0, is_seen=True, can_user_toggle=True)
            db.add(inst)
            await db.flush()
            await db.execute(instruction_data_source_association.insert().values(
                instruction_id=iid, data_source_id=ds_id))

            v1 = InstructionVersion(id=str(uuid.uuid4()), instruction_id=iid, version_number=1,
                                    text=base_text, title=title, status=status,
                                    load_mode="always", content_hash=_hash(base_text),
                                    created_by_user_id=str(user.id))
            db.add(v1)
            await db.flush()
            inst.current_version_id = v1.id
            db.add(BuildContent(id=str(uuid.uuid4()), build_id=main_build_id,
                                instruction_id=iid, instruction_version_id=v1.id))

            if has_pending:
                proposed_text = base_text + " Also cap the lookback window to the trailing 24 months."
                v2 = InstructionVersion(id=str(uuid.uuid4()), instruction_id=iid, version_number=2,
                                        text=proposed_text, title=title, status=status,
                                        load_mode="always", content_hash=_hash(proposed_text),
                                        created_by_user_id=str(user.id))
                db.add(v2)
                await db.flush()
                sug = InstructionBuild(id=str(uuid.uuid4()), build_number=next_bn, status="draft",
                                       source="ai", is_main=False, organization_id=org_id,
                                       base_build_id=main_build_id, created_by_user_id=str(user.id),
                                       description=f"AI suggestion for {title}")
                next_bn += 1
                db.add(sug)
                await db.flush()
                db.add(BuildContent(id=str(uuid.uuid4()), build_id=str(sug.id),
                                    instruction_id=iid, instruction_version_id=v2.id))
            print(f"seeded: {title} status={status} pending={has_pending} id={iid}")

        await db.commit()
        print(f"org={org_id} ds={ds_id}")


if __name__ == "__main__":
    asyncio.run(main())
