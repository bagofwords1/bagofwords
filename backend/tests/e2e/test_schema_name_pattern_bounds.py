"""Schema context name filters must stay bounded on model-written regex.

describe_tables and create_widget hand the model's table queries to
SchemaContextBuilder.build(name_patterns=...) verbatim when they look like a
regex. Matching ran synchronously on the event loop with stdlib `re`, so one
backtracking term ("(\\w+_?)+#") against an agent's table names froze the single
web worker — health checks timed out. The timeout mark is a hang guard, not a
speed assertion.
"""
import asyncio
import sqlite3
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
from app.dependencies import async_session_maker
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.user import User


def _wide_sqlite(path, n, stem):
    con = sqlite3.connect(path)
    for i in range(n):
        con.execute(f"create table t{i:04d}_{stem} (id integer, amount real)")
    con.commit()
    con.close()


@pytest.fixture
def wide_agent(tmp_path, create_data_source, refresh_schema, bulk_update_tables,
               create_user, login_user, whoami):
    stem = f"{uuid.uuid4().hex[:6]}_sales_fact_region"
    db_path = tmp_path / "wide.sqlite"
    _wide_sqlite(db_path, 120, stem)
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    ds = create_data_source(
        name="Wide warehouse", type="sqlite", config={"database": str(db_path)},
        credentials={}, user_token=token, org_id=org_id,
    )
    refresh_schema(data_source_id=ds["id"], user_token=token, org_id=org_id)
    bulk_update_tables(data_source_id=ds["id"], action="activate", user_token=token, org_id=org_id)
    return {"ds_id": ds["id"], "org_id": org_id, "user_id": me["id"], "stem": stem}


def _matched_table_names(agent, name_patterns):
    async def go():
        async with async_session_maker() as db:
            ds = (await db.execute(
                select(DataSource).options(selectinload(DataSource.connections))
                .where(DataSource.id == agent["ds_id"])
            )).scalar_one()
            org = await db.get(Organization, agent["org_id"])
            user = await db.get(User, agent["user_id"])
            ctx = await SchemaContextBuilder(
                db=db, data_sources=[ds], organization=org, report=None, user=user,
            ).build(with_stats=False, name_patterns=name_patterns)
            return {t.name for d in ctx.data_sources for t in (d.tables or [])}
    return asyncio.run(go())


@pytest.mark.e2e
@pytest.mark.timeout(60)
@pytest.mark.parametrize("runaway", [r"(?i)(\w+_?)+#", r"([a-z0-9_]+)*#", r"(\w*)*\d!"])
def test_backtracking_pattern_does_not_hang_and_other_patterns_still_filter(wide_agent, runaway):
    stem = wide_agent["stem"]
    names = _matched_table_names(wide_agent, [runaway, rf"(?i)^t00(0|1)\d_{stem}$"])
    assert names == {f"t{i:04d}_{stem}" for i in range(20)}


@pytest.mark.e2e
@pytest.mark.parametrize("pattern,expected", [
    (r"(?i)^T011\d_", set(range(110, 120))),
    (r"t000[0-4]_|t0099_", {0, 1, 2, 3, 4, 99}),
])
def test_regex_name_patterns_select_matching_tables(wide_agent, pattern, expected):
    stem = wide_agent["stem"]
    assert _matched_table_names(wide_agent, [pattern]) == {f"t{i:04d}_{stem}" for i in expected}
