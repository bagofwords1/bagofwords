"""Transitive identity taint — graph analysis contracts.

Uses an in-memory async SQLite with the app's models to build small
dependency chains and assert taint + freshness propagation.
"""

import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.entity import Entity
from app.services.identity_taint import (
    analyze_code_taint,
    entity_identity_scope,
    step_identity_scope,
)

IDENTITY_PARAM = [{"name": "email", "type": "string", "source": "identity"}]
INPUT_PARAM = [{"name": "region", "type": "string", "source": "input"}]


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed(db, *, entity_params=None, entity_code="def generate_df(c,e):\n    return None"):
    org = Organization(name="o")
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add_all([org, user])
    await db.flush()
    report = Report(title="r", user_id=str(user.id), organization_id=str(org.id), slug="r1", status="published")
    db.add(report)
    await db.flush()
    ent = Entity(
        organization_id=str(org.id), owner_id=str(user.id), type="model",
        title="My Tasks Entity", slug="my-tasks-entity", code=entity_code,
        parameters=entity_params, status="published",
        last_refreshed_at=datetime(2026, 1, 1),
    )
    db.add(ent)
    await db.flush()
    return org, user, report, ent


def _mk_step(report, query, code, title="s"):
    w = Widget(title=title, slug=f"w-{title}", report_id=str(report.id), status="draft")
    return w, code


@pytest.mark.asyncio
async def test_entity_ref_taints_consuming_code(db):
    org, user, report, ent = await _seed(db, entity_params=IDENTITY_PARAM)
    code = "def generate_df(ds, ef, load_entity):\n    return load_entity('my-tasks-entity')"
    t = await analyze_code_taint(db, code, organization_id=str(org.id), report_id=str(report.id))
    assert t.identity is True
    assert t.latest_upstream_refresh == datetime(2026, 1, 1)


@pytest.mark.asyncio
async def test_input_param_entity_does_not_taint(db):
    org, user, report, ent = await _seed(db, entity_params=INPUT_PARAM)
    code = "def generate_df(ds, ef, load_entity):\n    return load_entity('my-tasks-entity')"
    t = await analyze_code_taint(db, code, organization_id=str(org.id), report_id=str(report.id))
    assert t.identity is False
    assert t.latest_upstream_refresh == datetime(2026, 1, 1)


@pytest.mark.asyncio
async def test_step_chain_taints_two_levels(db):
    org, user, report, ent = await _seed(db, entity_params=None)
    # Step B: identity-parameterized query
    wb = Widget(title="B", slug="w-b", report_id=str(report.id), status="draft")
    db.add(wb)
    await db.flush()
    qb = Query(title="B", report_id=str(report.id), widget_id=str(wb.id),
               organization_id=str(org.id), parameters=IDENTITY_PARAM)
    db.add(qb)
    await db.flush()
    sb = Step(title="B", slug="step-b", status="success", type="table",
              code="def generate_df(c,e,params):\n    return None",
              widget_id=str(wb.id), query_id=str(qb.id))
    db.add(sb)
    await db.flush()
    # Step A consumes B via load_step
    wa = Widget(title="A", slug="w-a", report_id=str(report.id), status="draft")
    db.add(wa)
    await db.flush()
    qa = Query(title="A", report_id=str(report.id), widget_id=str(wa.id), organization_id=str(org.id))
    db.add(qa)
    await db.flush()
    sa = Step(title="A", slug="step-a", status="success", type="table",
              code="def generate_df(c,e,load_step):\n    return load_step('step-b')",
              widget_id=str(wa.id), query_id=str(qa.id))
    db.add(sa)
    await db.flush()

    tainted, _ = await step_identity_scope(db, sa)
    assert tainted is True
    # A step consuming only non-identity things is clean
    tainted_b_own, _ = await step_identity_scope(db, sb)
    assert tainted_b_own is True  # declared, not inherited


@pytest.mark.asyncio
async def test_entity_scope_own_and_clean(db):
    org, user, report, ent = await _seed(db, entity_params=IDENTITY_PARAM)
    t, _ = await entity_identity_scope(db, ent)
    assert t is True
    ent2 = Entity(
        organization_id=str(org.id), owner_id=str(user.id), type="model",
        title="Plain", slug="plain", code="def generate_df(c,e):\n    return None",
        status="published",
    )
    db.add(ent2)
    await db.flush()
    t2, _ = await entity_identity_scope(db, ent2)
    assert t2 is False


@pytest.mark.asyncio
async def test_cycle_and_depth_guard(db):
    org, user, report, ent = await _seed(
        db,
        entity_params=None,
        entity_code="def generate_df(c,e,load_entity):\n    return load_entity('my-tasks-entity')",
    )
    # Self-referential entity: must terminate, not recurse forever.
    t = await analyze_code_taint(
        db, "def generate_df(c,e,load_entity):\n    return load_entity('my-tasks-entity')",
        organization_id=str(org.id), report_id=None,
    )
    assert t.identity is False
