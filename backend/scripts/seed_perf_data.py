"""Seed heavy data: reports + widgets + queries + steps + completions linked
to data sources, plus instructions and entities linked to data sources.

Usage: python scripts/seed_perf_data.py <reports_per_ds> <instructions_per_ds>
"""
import os
import sys
import uuid
import asyncio
from datetime import datetime

os.environ["BOW_DATABASE_URL"] = "sqlite:///db/app.db"
os.environ["BOW_SMTP_PASSWORD"] = "dummy"

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select

# Import models
import app.models  # noqa
import pkgutil, importlib
for _, modname, _ in pkgutil.iter_modules(app.models.__path__):
    if modname == "application":
        continue
    importlib.import_module(f"app.models.{modname}")

from app.models.data_source import DataSource
from app.models.user import User
from app.models.report import Report
from app.models.report_data_source_association import report_data_source_association
from app.models.widget import Widget
from app.models.completion import Completion
from app.models.query import Query
from app.models.step import Step
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.entity import Entity, entity_data_source_association


async def main():
    n_reports = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    n_instructions = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    engine = create_async_engine("sqlite+aiosqlite:///db/app.db", future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        data_sources = (await db.execute(select(DataSource))).scalars().all()
        assert user and data_sources, "DS or user missing — run sandbox setup first"
        primary_ds = data_sources[0]

        # Reports linked to first DS
        for i in range(n_reports):
            report = Report(
                id=str(uuid.uuid4()),
                title=f"seed-{i}",
                slug=f"seed-{i}-{uuid.uuid4().hex[:6]}",
                status="published",
                user_id=user.id,
                organization_id=primary_ds.organization_id,
            )
            db.add(report)
            await db.flush()
            await db.execute(report_data_source_association.insert().values(
                report_id=report.id, data_source_id=primary_ds.id,
            ))
            for w in range(2):
                db.add(Widget(id=str(uuid.uuid4()), report_id=report.id,
                              title=f"w-{i}-{w}",
                              slug=f"w-{i}-{w}-{uuid.uuid4().hex[:6]}"))
            for c in range(3):
                db.add(Completion(
                    id=str(uuid.uuid4()), report_id=report.id,
                    prompt={}, completion={}, role="user", turn_index=c,
                    sigkill=datetime.utcnow(),
                ))
            await db.flush()
            first_widget = (await db.execute(
                select(Widget).where(Widget.report_id == report.id).limit(1)
            )).scalars().first()
            qid = str(uuid.uuid4())
            db.add(Query(
                id=qid, report_id=report.id, widget_id=first_widget.id,
                title=f"q-{i}", organization_id=primary_ds.organization_id,
                user_id=user.id,
            ))
            await db.flush()
            for s in range(2):
                db.add(Step(
                    id=str(uuid.uuid4()), query_id=qid,
                    title=f"s-{i}-{s}", slug=f"s-{i}-{s}-{uuid.uuid4().hex[:6]}",
                    data={}, data_model={}, code="",
                ))

        # Instructions linked to all data sources
        for i in range(n_instructions):
            inst_id = str(uuid.uuid4())
            inst = Instruction(
                id=inst_id,
                text=f"Test instruction {i}: do something important when you encounter X.",
                title=f"Instruction {i}",
                status="published",
                category="general",
                user_id=user.id,
                organization_id=primary_ds.organization_id,
                source_type="user",
            )
            db.add(inst)
            await db.flush()
            # Link to all DSes
            for ds in data_sources:
                await db.execute(instruction_data_source_association.insert().values(
                    instruction_id=inst_id, data_source_id=ds.id,
                ))

        # Entities linked to first DS
        for i in range(20):
            ent_id = str(uuid.uuid4())
            ent = Entity(
                id=ent_id,
                title=f"Entity-{i}",
                slug=f"entity-{i}-{uuid.uuid4().hex[:6]}",
                type="metric",
                code="SELECT 1",
                description=f"desc-{i}",
                organization_id=primary_ds.organization_id,
                owner_id=user.id,
            )
            db.add(ent)
            await db.flush()
            await db.execute(entity_data_source_association.insert().values(
                entity_id=ent_id, data_source_id=primary_ds.id,
            ))

        await db.commit()
        print(f"seeded {n_reports} reports, {n_instructions} instructions, 20 entities")
        print(f"primary DS: {primary_ds.id}")


if __name__ == "__main__":
    asyncio.run(main())
