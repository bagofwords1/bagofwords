"""Column usage, column cap and column search, end to end (BOW-215).

Agent: the chinook SQLite database through the real data source API. A step's
usage is recorded through ``ProjectManager.emit_step_usage`` (the entry point
the agent calls when a create_data step finishes) with the SQL the step
executed; feedback goes through ``POST /completions/{id}/feedback``; column
counts are read back through ``GET /data_sources/{id}/full_schema``.
"""
import asyncio
import re
from pathlib import Path

import pytest

CHINOOK = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()

# Joins, an alias, unprefixed names, a filter, GROUP BY / ORDER BY.
SQL_TRACKS_BY_ALBUM = (
    "SELECT a.Title, COUNT(*) AS n, SUM(Milliseconds) AS ms FROM Track t "
    "JOIN Album a ON a.AlbumId = t.AlbumId WHERE t.UnitPrice > 0.5 AND Composer IS NOT NULL "
    "GROUP BY a.Title ORDER BY ms DESC LIMIT 10"
)
TRACK_COLS = {"AlbumId", "Milliseconds", "UnitPrice", "Composer"}
ALBUM_COLS = {"AlbumId", "Title"}


def _run(coro):
    return asyncio.run(coro)


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def agent(create_data_source, refresh_schema, update_tables_status_delta, create_report,
          create_user, login_user, whoami, test_client):
    if not CHINOOK.exists():
        pytest.skip(f"SQLite test database missing at {CHINOOK}")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    ds = create_data_source(name="Column usage", type="sqlite", config={"database": str(CHINOOK)},
                            credentials={}, user_token=token, org_id=org_id)
    refresh_schema(data_source_id=ds["id"], user_token=token, org_id=org_id)
    tables = _tables(test_client, ds["id"], token, org_id)
    update_tables_status_delta(data_source_id=ds["id"], activate=[t["id"] for t in tables.values()],
                               user_token=token, org_id=org_id)
    report = create_report(title="cols", user_token=token, org_id=org_id, data_sources=[ds["id"]])
    return {"ds": ds, "token": token, "org_id": org_id, "report": report, "email": user["email"]}


def _tables(test_client, ds_id, token, org_id):
    r = test_client.get(f"/api/data_sources/{ds_id}/full_schema",
                        params={"page": 1, "page_size": 500, "with_stats": "true"}, headers=_hdr(token, org_id))
    assert r.status_code == 200, r.text
    return {t["name"]: t for t in r.json()["tables"]}


def _column_usage(test_client, a, table):
    t = _tables(test_client, a["ds"]["id"], a["token"], a["org_id"])[table]
    return {k: v for k, v in (t.get("column_stats") or {}).items()}


async def _load(a):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.dependencies import async_session_maker
    from app.models.report import Report
    db = async_session_maker()
    report = (await db.execute(select(Report).options(selectinload(Report.data_sources))
                               .where(Report.id == a["report"]["id"]))).scalar_one()
    return db, report


async def _step_with_usage(a, sql, *, success=True, step_id=None):
    """A finished create_data step that executed `sql`, recorded the way the
    agent records it. Returns the step id."""
    from app.project_manager import ProjectManager
    from app.models.step import Step
    pm = ProjectManager()
    db, report = await _load(a)
    try:
        if step_id is None:
            _q, step, _v = await pm.create_query_step_visualization(db, report, "q", create_visualization=False)
        else:
            step = await db.get(Step, step_id)
        await pm.emit_step_usage(db, report, step, executed_queries=[sql], success=success)
        return str(step.id)
    finally:
        await db.close()


@pytest.mark.e2e
def test_step_usage_records_every_column_the_executed_sql_references(test_client, agent):
    _run(_step_with_usage(agent, SQL_TRACKS_BY_ALBUM))
    track, album = _column_usage(test_client, agent, "Track"), _column_usage(test_client, agent, "Album")
    assert set(track) == TRACK_COLS
    assert set(album) == ALBUM_COLS
    assert all(v["usage_count"] == 1 and v["success_count"] == 1 for v in {**track, **album}.values())


@pytest.mark.e2e
def test_a_step_counts_once_however_often_its_usage_is_emitted(test_client, agent):
    step_id = _run(_step_with_usage(agent, SQL_TRACKS_BY_ALBUM))
    _run(_step_with_usage(agent, SQL_TRACKS_BY_ALBUM, step_id=step_id))
    _run(_step_with_usage(agent, "SELECT Composer FROM Track", step_id=step_id))
    assert {v["usage_count"] for v in _column_usage(test_client, agent, "Track").values()} == {1}
    # A different step is new usage.
    _run(_step_with_usage(agent, SQL_TRACKS_BY_ALBUM))
    assert {v["usage_count"] for v in _column_usage(test_client, agent, "Track").values()} == {2}


@pytest.mark.e2e
def test_failed_steps_count_as_failures_and_weigh_less(test_client, agent):
    _run(_step_with_usage(agent, "SELECT Bytes FROM Track WHERE Bytes > 0", success=True))
    _run(_step_with_usage(agent, "SELECT Name FROM Track WHERE Name LIKE 'A%'", success=False))
    track = _column_usage(test_client, agent, "Track")
    assert track["Bytes"]["success_count"] == 1 and track["Bytes"]["failure_count"] == 0
    assert track["Name"]["failure_count"] == 1 and track["Name"]["success_count"] == 0

    async def weights():
        from sqlalchemy import select
        from app.dependencies import async_session_maker
        from app.models.column_stats import ColumnStats
        async with async_session_maker() as db:
            rows = (await db.execute(select(ColumnStats).where(ColumnStats.column_name.in_(["Bytes", "Name"])))).scalars().all()
            return {r.column_name: r.weighted_usage_count for r in rows}
    w = _run(weights())
    assert 0 < w["Name"] < w["Bytes"]


@pytest.mark.e2e
def test_thumbs_up_and_down_apply_to_the_columns_the_answer_used(test_client, agent):
    step_id = _run(_step_with_usage(agent, SQL_TRACKS_BY_ALBUM))

    async def completion_for(step):
        # The agent writes the system completion that carries the step; there
        # is no API to create one (tests/AGENTS.md rule 5).
        from app.dependencies import async_session_maker
        from app.models.completion import Completion
        async with async_session_maker() as db:
            c = Completion(report_id=agent["report"]["id"], role="system", status="success", message_type="ai_completion",
                           completion={"content": "answer"}, prompt=None, model="m", turn_index=0, step_id=step)
            db.add(c)
            await db.commit()
            return str(c.id)
    cid = _run(completion_for(step_id))
    r = test_client.post(f"/api/completions/{cid}/feedback", json={"direction": 1},
                         headers=_hdr(agent["token"], agent["org_id"]))
    assert r.status_code == 200, r.text
    track = _column_usage(test_client, agent, "Track")
    assert {k for k, v in track.items() if v["pos_feedback_count"] == 1} == TRACK_COLS
    assert all(v["neg_feedback_count"] == 0 for v in track.values())

    r = test_client.post(f"/api/completions/{cid}/feedback", json={"direction": -1},
                         headers=_hdr(agent["token"], agent["org_id"]))
    assert r.status_code == 200, r.text
    album = _column_usage(test_client, agent, "Album")
    assert {k for k, v in album.items() if v["neg_feedback_count"] == 1} == ALBUM_COLS
    # Columns the answer did not use are untouched.
    assert "Bytes" not in _column_usage(test_client, agent, "Track")


async def _schema(a):
    """The agent's schema context, built the way the planner builds it."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.user import User
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.services.organization_settings_service import OrganizationSettingsService
    async with async_session_maker() as db:
        org = await db.get(Organization, a["org_id"])
        user = (await db.execute(select(User).where(User.email == a["email"]))).scalar_one()
        settings = await OrganizationSettingsService().get_settings(db, org, user)
        ds = (await db.execute(select(DataSource).options(selectinload(DataSource.connections))
                               .where(DataSource.id == a["ds"]["id"]))).scalar_one()
        return await SchemaContextBuilder(db, [ds], org, None, user=user, organization_settings=settings).build()


def _table_xml(xml, name):
    m = re.search(rf'<table name="{name}"[^>]*>.*?</table>', xml, re.S)
    assert m, f"{name} not rendered"
    return m.group(0)


@pytest.mark.e2e
@pytest.mark.parametrize("budget", [3, 5])
def test_context_caps_columns_and_puts_used_columns_first(test_client, agent, budget, update_organization_settings):
    update_organization_settings({"top_k_schema_columns": {"value": budget}}, user_token=agent["token"], org_id=agent["org_id"])
    # Composer and Bytes are neither keys nor timestamps: only usage can lift them.
    for _ in range(2):
        _run(_step_with_usage(agent, "SELECT Composer, Bytes FROM Track WHERE Bytes > 100"))
    ctx = _run(_schema(agent))
    xml = ctx.render_combined(top_k_per_ds=20, index_limit=50)
    track = _table_xml(xml, "Track")
    names = re.findall(r'<column name="([^"]+)"', track)
    assert len(names) == budget
    assert names[:2] == ["Bytes", "Composer"] or names[:2] == ["Composer", "Bytes"]
    # After usage, key / id columns outrank the plain unused ones (Name,
    # Milliseconds, UnitPrice).
    assert set(names[2:]) <= {"TrackId", "AlbumId", "MediaTypeId", "GenreId"}
    assert f'count="{9 - budget}"' in track and "<more_columns" in track
    snap = {t.name: t for t in ctx.get_usage_snapshot(top_k_per_ds=20).data_sources[0].tables_used}
    assert (snap["Track"].columns_shown, snap["Track"].columns_total) == (budget, 9)
    assert snap["Track"].column_selection_reason == "usage_ranked"


@pytest.mark.e2e
def test_excluded_columns_never_reach_the_context_even_when_most_used(test_client, agent, update_tables_status_delta,
                                                                    update_organization_settings):
    update_organization_settings({"top_k_schema_columns": {"value": 3}}, user_token=agent["token"], org_id=agent["org_id"])
    for _ in range(3):
        _run(_step_with_usage(agent, "SELECT Composer FROM Track WHERE Composer LIKE 'A%'"))
    track_id = _tables(test_client, agent["ds"]["id"], agent["token"], agent["org_id"])["Track"]["id"]
    update_tables_status_delta(data_source_id=agent["ds"]["id"], excluded_columns={track_id: ["Composer"]},
                               user_token=agent["token"], org_id=agent["org_id"])
    ctx = _run(_schema(agent))
    assert 'name="Composer"' not in ctx.render_combined(top_k_per_ds=20, index_limit=50)


async def _describe(a, **tool_input):
    from types import SimpleNamespace
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.user import User
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.ai.tools.implementations.describe_tables import DescribeTablesTool
    async with async_session_maker() as db:
        org = await db.get(Organization, a["org_id"])
        user = (await db.execute(select(User).where(User.email == a["email"]))).scalar_one()
        ds = (await db.execute(select(DataSource).options(selectinload(DataSource.connections))
                               .where(DataSource.id == a["ds"]["id"]))).scalar_one()
        hub = SimpleNamespace(schema_builder=SchemaContextBuilder(db, [ds], org, None, user=user),
                              data_sources=[ds], user=user)
        end = None
        async for ev in DescribeTablesTool().run_stream(tool_input, {"db": db, "organization": org, "context_hub": hub}):
            if ev.type == "tool.end":
                end = ev.payload
        return end


@pytest.mark.e2e
def test_column_search_without_query_returns_only_tables_with_a_match(agent):
    out = _run(_describe(agent, columns=["Milliseconds", "billing*"]))["output"]
    names = {t["name"] for t in out["top_tables"]}
    assert names == {"Track", "Invoice"}  # Milliseconds lives on Track, Billing* on Invoice
    for t in out["top_tables"]:
        cols = {c["name"] for c in t["columns"]}
        assert cols and all(c == "Milliseconds" or c.lower().startswith("billing") for c in cols)
        assert t["columns_matched"] == len(cols) and t["columns_total"] > len(cols)


@pytest.mark.e2e
def test_column_search_with_query_narrows_to_matching_columns(agent):
    end = _run(_describe(agent, query="Track", columns=["id"]))
    track = next(t for t in end["output"]["top_tables"] if t["name"] == "Track")
    assert {c["name"] for c in track["columns"]} == {"TrackId", "AlbumId", "MediaTypeId", "GenreId"}
    assert (track["columns_matched"], track["columns_total"]) == (4, 9)
    assert 'cols_matched="4"' in end["observation"]["schemas_excerpt"]
    assert "showing 4 of 9 columns" in end["observation"]["schemas_excerpt"]


@pytest.mark.e2e
def test_column_search_never_returns_excluded_columns(test_client, agent, update_tables_status_delta):
    track_id = _tables(test_client, agent["ds"]["id"], agent["token"], agent["org_id"])["Track"]["id"]
    update_tables_status_delta(data_source_id=agent["ds"]["id"], excluded_columns={track_id: ["Composer", "Bytes"]},
                               user_token=agent["token"], org_id=agent["org_id"])
    for kwargs in ({"columns": ["Composer", "Bytes"]}, {"query": "Track", "columns": ["o"]}, {"columns": ["*"]}):
        end = _run(_describe(agent, **kwargs))
        excerpt = end["observation"]["schemas_excerpt"]
        cols = {c["name"] for t in end["output"]["top_tables"] for c in t["columns"]}
        assert "Composer" not in cols and "Bytes" not in cols, kwargs
        assert 'name="Composer"' not in excerpt and 'name="Bytes"' not in excerpt, kwargs


@pytest.mark.e2e
def test_describe_without_column_filter_caps_wide_tables_and_points_at_column_search(test_client, agent,
                                                                                    update_organization_settings):
    update_organization_settings({"top_k_schema_columns": {"value": 4}}, user_token=agent["token"], org_id=agent["org_id"])

    async def run():
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from types import SimpleNamespace
        from app.dependencies import async_session_maker
        from app.models.data_source import DataSource
        from app.models.organization import Organization
        from app.models.user import User
        from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
        from app.ai.tools.implementations.describe_tables import DescribeTablesTool
        from app.services.organization_settings_service import OrganizationSettingsService
        async with async_session_maker() as db:
            org = await db.get(Organization, agent["org_id"])
            user = (await db.execute(select(User).where(User.email == agent["email"]))).scalar_one()
            settings = await OrganizationSettingsService().get_settings(db, org, user)
            ds = (await db.execute(select(DataSource).options(selectinload(DataSource.connections))
                                   .where(DataSource.id == agent["ds"]["id"]))).scalar_one()
            hub = SimpleNamespace(schema_builder=SchemaContextBuilder(db, [ds], org, None, user=user,
                                                                      organization_settings=settings),
                                  data_sources=[ds], user=user)
            async for ev in DescribeTablesTool().run_stream({"query": "Track"}, {"db": db, "organization": org, "context_hub": hub}):
                if ev.type == "tool.end":
                    return ev.payload
    end = _run(run())
    track = _table_xml(end["observation"]["schemas_excerpt"], "Track")
    assert track.count("<column ") == 4 and "<more_columns" in track and "columns=" in track


@pytest.mark.e2e
@pytest.mark.parametrize("limit", [1, 2])
def test_stats_read_is_bounded_per_table_and_keeps_the_most_used(test_client, agent, limit):
    for sql, times in (("SELECT Bytes FROM Track", 3), ("SELECT Composer FROM Track", 2), ("SELECT Name FROM Track", 1)):
        for _ in range(times):
            _run(_step_with_usage(agent, sql))
    track_id = _tables(test_client, agent["ds"]["id"], agent["token"], agent["org_id"])["Track"]["id"]

    async def load():
        from app.dependencies import async_session_maker
        from app.services.column_usage_service import ColumnUsageService
        async with async_session_maker() as db:
            return await ColumnUsageService().load_stats(db, [track_id], per_table_limit=limit)
    got = _run(load())[track_id]
    assert set(got) == set(["bytes", "composer", "name"][:limit])
    assert all(row.usage_count >= 1 for row in got.values())
