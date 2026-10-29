"""Tool-side context reads: table resolution from the run's schema context,
and load_step discovery from step summaries (no result data loaded)."""

# Mapper registration intentionally runs before the app-model imports below.
# ruff: noqa: E402

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select
from sqlalchemy.orm import lazyload
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 — test-only, mirrors alembic/env.py

from app.ai.code_execution.loadables import LoadablesResolver
from app.ai.tools.implementations.create_data import CreateDataTool
from app.models.base import Base
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.widget import Widget


def _static(sections):
    return NS(data_sources=[NS(info=NS(id=ds_id), tables=[NS(name=n) for n in names]) for ds_id, names in sections])


def _patterns(names):
    return [f"(?i)(?:^|[./]){re.escape(n)}$" for n in names]


@pytest.mark.parametrize("requested, ds_scoped, expected", [
    (["Invoice"], True, [{"data_source_id": "ds1", "tables": ["Invoice"]}]),
    (["invoice", "CUSTOMER"], True, [{"data_source_id": "ds1", "tables": ["Customer", "Invoice"]}]),
    (["Invoice"], False, [{"data_source_id": "ds1", "tables": ["Invoice"]}, {"data_source_id": "ds2", "tables": ["sales.Invoice"]}]),
    (["Orders"], False, [{"data_source_id": "ds2", "tables": ["Orders"]}]),
])
def test_resolution_from_static_schema(requested, ds_scoped, expected):
    static = _static([("ds1", ["Customer", "Invoice", "Track"]), ("ds2", ["sales.Invoice", "Orders"])])
    got = CreateDataTool._resolve_group_from_static(static, "ds1" if ds_scoped else None, _patterns(requested))
    assert got == expected


@pytest.mark.parametrize("requested", [["Invoice", "NoSuchTable"], ["Track"]])
def test_resolution_falls_back_when_any_table_is_missing_from_static(requested):
    # The static context is top-k capped: a miss means "not in memory", not
    # "doesn't exist", so the caller must build from the DB.
    static = _static([("ds1", ["Customer", "Invoice"])])
    assert CreateDataTool._resolve_group_from_static(static, "ds1", _patterns(requested)) is None
    assert CreateDataTool._resolve_group_from_static(None, "ds1", _patterns(requested)) is None


@pytest_asyncio.fixture
async def report_with_steps(tmp_path):
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'loadables.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # Direct model writes: a report's successful default steps are produced by
    # agent runs, which the HTTP API can't drive without a live LLM.
    async with maker() as db:
        user = User(name="L", email=f"l-{uuid.uuid4()}@example.com", hashed_password="x")
        org = Organization(name=f"L {uuid.uuid4()}")
        db.add_all([user, org])
        await db.flush()
        report = Report(title="r", slug=f"r-{uuid.uuid4()}", user_id=str(user.id), organization_id=str(org.id))
        db.add(report)
        await db.flush()
        for i, rows in enumerate([3, 40, 0]):
            w = Widget(title=f"w{i}", slug=f"w-{uuid.uuid4()}", report_id=str(report.id))
            db.add(w)
            await db.flush()
            q = Query(title=f"Query {i}", report_id=str(report.id), widget_id=str(w.id), organization_id=str(org.id), user_id=str(user.id))
            db.add(q)
            await db.flush()
            data = {"columns": [{"field": "a"}, {"field": f"col{i}"}, {"headerName": "nofield"}],
                    "rows": [{"a": r} for r in range(rows)], "info": {"total_rows": rows}}
            s = Step(title=f"Step {i}", slug=f"s-{uuid.uuid4()}", type="table", widget_id=str(w.id), query_id=str(q.id),
                     code="", data=data, data_model={}, status="success")
            db.add(s)
            await db.flush()
            q.default_step_id = str(s.id)
            if i == 2:
                s.context_summary_json = None  # a step written before summaries existed
        await db.commit()
        ids = NS(report=str(report.id), org=str(org.id), user=str(user.id))
    yield NS(maker=maker, ids=ids)
    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_uses_summaries_without_loading_result_data(report_with_steps):
    ids = report_with_steps.ids
    async with report_with_steps.maker() as db:
        # Load the report the way the agent does (no eager graph), so the
        # identity map only holds what discovery itself loads.
        report = (await db.execute(select(Report).options(lazyload("*")).where(Report.id == ids.report))).scalar_one()
        org = (await db.execute(select(Organization).options(lazyload("*")).where(Organization.id == ids.org))).scalar_one()
        user = (await db.execute(select(User).options(lazyload("*")).where(User.id == ids.user))).scalar_one()
        resolver = LoadablesResolver(db, org, report, user)
        section = await resolver.list_for_discovery(limit=25)
        by_title = {item.title: item for item in section.items}
        loaded_with_data = [
            s for s in db.sync_session.identity_map.values()
            if isinstance(s, Step) and "data" not in inspect(s).unloaded
        ]

    assert set(by_title) == {"Step 0", "Step 1", "Step 2"}
    for i, rows in enumerate([3, 40, 0]):
        item = by_title[f"Step {i}"]
        assert item.row_count == rows
        assert item.columns == ["a", f"col{i}"]
    # No step row was hydrated with its result data (the summary-less one is
    # read through a column query, not the ORM row).
    assert loaded_with_data == []
