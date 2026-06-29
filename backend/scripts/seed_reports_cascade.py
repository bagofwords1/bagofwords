"""Seed reports with a deep object graph (widgets -> queries -> steps,
completions, visualizations) to reproduce the GET /api/reports?filter=my list
cascade — the Report model's lazy="selectin" relationships hydrate this whole
graph for every report in the list.

Usage: python scripts/seed_reports_cascade.py [n_reports]
Reads BOW_DATABASE_URL from env (postgres or sqlite).
"""
import os, sys, uuid, asyncio, json
from datetime import datetime

os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select

import app.models, pkgutil, importlib
for _, m, _ in pkgutil.iter_modules(app.models.__path__):
    if m != "application":
        importlib.import_module(f"app.models.{m}")

from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.report import Report
from app.models.report_data_source_association import report_data_source_association
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.completion import Completion
from app.models.visualization import Visualization


def _url():
    u = os.environ["BOW_DATABASE_URL"]
    if u.startswith("postgresql://"):
        return u.replace("postgresql://", "postgresql+asyncpg://", 1)
    if u.startswith("sqlite:///"):
        return u.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return u


_BLOB = int(os.environ.get("SEED_BLOB", "40"))
_NCOMP = int(os.environ.get("SEED_COMPLETIONS", "6"))
_NWIDG = int(os.environ.get("SEED_WIDGETS", "3"))
BIGJSON = {"rows": [{"c": i, "v": "x" * 80} for i in range(_BLOB)]}  # ~payload weight per step/completion


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    engine = create_async_engine(_url(), future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        assert user and org, "signup sandbox@bow.dev first"
        oid = str(org.id); uid = str(user.id)

        ds = (await db.execute(select(DataSource).where(DataSource.organization_id == oid))).scalars().first()
        if ds is None:
            ds = DataSource(id=str(uuid.uuid4()), name="Perf Agent", is_active=True, organization_id=oid)
            db.add(ds); await db.flush()
        dsid = str(ds.id)

        for i in range(n):
            rid = str(uuid.uuid4())
            db.add(Report(id=rid, title=f"Report {i}", slug=f"report-{i}-{uuid.uuid4().hex[:6]}",
                          status="published", user_id=uid, organization_id=oid))
            await db.flush()
            await db.execute(report_data_source_association.insert().values(report_id=rid, data_source_id=dsid))

            # 3 widgets, each with a query + 2 steps
            for w in range(_NWIDG):
                wid = str(uuid.uuid4())
                db.add(Widget(id=wid, report_id=rid, title=f"w{i}-{w}", slug=f"w{i}-{w}-{uuid.uuid4().hex[:6]}",
                              status="published", x=0, y=0, width=6, height=4))
                await db.flush()
                qid = str(uuid.uuid4())
                db.add(Query(id=qid, report_id=rid, widget_id=wid, title=f"q{i}-{w}",
                             organization_id=oid, user_id=uid))
                await db.flush()
                for s in range(2):
                    db.add(Step(id=str(uuid.uuid4()), query_id=qid, widget_id=wid,
                                title=f"s{i}-{w}-{s}", slug=f"s{i}-{w}-{s}-{uuid.uuid4().hex[:6]}",
                                status="published", prompt="compute", code="SELECT 1",
                                data=BIGJSON, data_model=BIGJSON))
                db.add(Visualization(id=str(uuid.uuid4()), report_id=rid, query_id=qid,
                                     title=f"v{i}-{w}", status="published"))

            # 6 completions (conversation turns) with sizeable JSON
            for c in range(_NCOMP):
                db.add(Completion(id=str(uuid.uuid4()), report_id=rid,
                                  prompt={"content": "show me " + "y" * 60}, completion=BIGJSON,
                                  status="success", model="x", turn_index=c,
                                  message_type="ai_completion", role="user", main_router="x"))

            if (i + 1) % 10 == 0:
                await db.commit(); print(f"  ...{i+1}/{n}")

        await db.commit()
        print(f"seeded {n} reports (3 widgets/queries, 6 steps, 3 viz, 6 completions each)")
        print(f"org={oid} ds={dsid}")


if __name__ == "__main__":
    asyncio.run(main())
