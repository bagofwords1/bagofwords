"""Seed an agent (DataSource) with N instructions all pinned to a table.

Reproduces the /agents 3->0 scenario: counts.by_agent shows N but the per-agent
flat Instructions node shows 0 because every instruction has a datasource_table
reference (excluded by !hasTableRef in listForAgent).

Run with: uv run python scripts/seed_table_pinned.py [N]
"""
import os, sys, asyncio, uuid
os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app_3to0.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

import app.models  # noqa
import pkgutil, importlib
for _, modname, _ in pkgutil.iter_modules(app.models.__path__):
    if modname == "application":
        continue
    importlib.import_module(f"app.models.{modname}")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
from app.services.instruction_service import InstructionService
from app.schemas.instruction_schema import InstructionCreate
from app.schemas.instruction_reference_schema import InstructionReferenceCreate


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    orphan = "--orphan" in sys.argv  # case (c): bad object_id that matches no table

    url = os.environ["BOW_DATABASE_URL"].replace("sqlite://", "sqlite+aiosqlite://")
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        org = (await db.execute(select(Organization))).scalars().first()
        user = (await db.execute(select(User))).scalars().first()
        print(f"org={org.id} user={user.id} ({user.email})")

        # Agent (DataSource)
        ds = DataSource(
            name="Pinned Agent",
            organization_id=org.id,
            is_public=False,
        )
        db.add(ds)
        await db.commit()
        await db.refresh(ds)

        # Admin is a member of the agent
        db.add(DataSourceMembership(
            data_source_id=ds.id,
            principal_type=PRINCIPAL_TYPE_USER,
            principal_id=user.id,
            config={"role": "owner"},
        ))

        # A table belonging to the agent
        tbl = DataSourceTable(
            name="orders",
            datasource_id=ds.id,
            is_active=True,
            columns=[],
            pks=[],
            fks=[],
        )
        db.add(tbl)
        await db.commit()
        await db.refresh(tbl)
        print(f"data_source={ds.id} table={tbl.id} (name={tbl.name})")

        svc = InstructionService()
        ref_object_id = ("nonexistent-" + str(uuid.uuid4())) if orphan else tbl.id
        created = []
        for i in range(n):
            payload = InstructionCreate(
                text=f"Pinned instruction #{i} for orders table",
                title=f"Pinned #{i}",
                category="general",
                status="published",
                data_source_ids=[ds.id],
                references=[InstructionReferenceCreate(
                    object_type="datasource_table",
                    object_id=ref_object_id,
                    display_text="orders",
                )],
            )
            schema = await svc.create_instruction(
                db, payload, current_user=user, organization=org,
            )
            created.append(schema.id)
        print(f"created {len(created)} instructions pinned to object_id={ref_object_id}")
        print(f"AGENT_ID={ds.id}")
        print(f"TABLE_ID={tbl.id}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
