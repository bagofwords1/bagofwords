"""
Reproduction + regression guard: POST /reports/{id}/rerun must refresh the
data actually rendered by the report's artifact dashboards.

Reported symptom: on an artifact-based report (dashboard generated as an
Artifact whose content.visualization_ids reference Visualizations -> Queries),
clicking "Refresh Dashboard" (which POSTs /rerun) returns 200 and a success
toast, but the dashboard keeps showing stale/empty data forever.

Root cause: rerun_report_steps (app/services/report_service.py) only rerans
steps referenced by dashboard-layout `visualization` blocks — a deprecated
mechanism artifact reports never populate (their active layout has 0 blocks) —
and then falls back to rerunning each widget's *newest* step. Artifacts render
each query's *default* step (routes/query.py get_default_step), so the step
the dashboard reads is never re-executed.

The contract asserted here: rerunning a report re-executes and repopulates the
default step of every query referenced by the report's artifacts, and the
response reports how many steps ran/succeeded/failed.

Run:
    cd backend
    uv run pytest tests/e2e/test_report_rerun_artifact.py -v
"""
import asyncio
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.visualization import Visualization
from app.models.artifact import Artifact


def _run(coro):
    return asyncio.run(coro)


# Deterministic step code — no data source needed (ds_clients unused), so the
# loop runs in a clean sandbox with no external boundary to stub.
GOOD_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({"month": ["2024-01", "2024-02"], "revenue": [10, 20]})
"""

FAILING_CODE = """
def generate_df(ds_clients, excel_files):
    raise RuntimeError("simulated upstream failure (e.g. db login failed)")
"""


@contextmanager
def record_sql():
    statements = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _before)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", _before)


def _steps_selects(statements):
    return [s for s in statements
            if s.lstrip().upper().startswith("SELECT") and re.search(r"FROM\s+steps\b", s)]


async def _seed_artifact_graph(
    report_id: str,
    n_queries: int = 2,
    default_step_code: str = GOOD_CODE,
    default_step_data=None,
    newer_failed_attempt: bool = True,
    extra_success_versions: int = 0,
    rows_per_version: int = 0,
    failing_query_indexes: tuple = (),
):
    """Attach an artifact dashboard graph to an API-created report.

    Queries/visualizations/artifacts are produced by the AI completion flow in
    production; there is no public CRUD API that creates them, so the graph is
    seeded directly (mirrors tests/e2e/test_artifact_large_data_perf_repro.py).

    Each query gets:
      - `extra_success_versions` old success steps carrying `rows_per_version`
        rows (historical re-runs whose payloads are still stored),
      - a default step whose stored data is `default_step_data` (None ==
        payload purged by the retention job) and whose code is deterministic,
      - optionally a NEWER failed attempt step (a later re-run that errored),
        so "rerun the newest step" is observably different from "rerun the
        default step the artifact renders".
    """
    suffix = uuid.uuid4().hex[:8]
    now = datetime.utcnow()

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org_id, user_id = report.organization_id, report.user_id

        query_ids, viz_ids, default_step_ids = [], [], []
        for qi in range(n_queries):
            widget = Widget(title=f"W{qi} {suffix}", slug=f"w{qi}-{suffix}", report_id=report_id)
            db.add(widget)
            await db.flush()

            query = Query(
                title=f"Query {qi}",
                report_id=report_id,
                widget_id=widget.id,
                organization_id=org_id,
                user_id=user_id,
            )
            db.add(query)
            await db.flush()

            for vi in range(extra_success_versions):
                rows = [{"month": f"2023-{(r % 12) + 1:02d}", "revenue": r} for r in range(rows_per_version)]
                db.add(Step(
                    title=f"Old {qi}.{vi}",
                    slug=f"old-{qi}-{vi}-{suffix}",
                    status="success",
                    widget_id=widget.id,
                    query_id=query.id,
                    code=GOOD_CODE,
                    data={"rows": rows, "columns": [{"field": "month"}, {"field": "revenue"}]},
                    created_at=now - timedelta(hours=3 + vi),
                ))

            code = FAILING_CODE if qi in failing_query_indexes else default_step_code
            default_step = Step(
                title=f"Default {qi}",
                slug=f"default-{qi}-{suffix}",
                status="success",
                widget_id=widget.id,
                query_id=query.id,
                code=code,
                data=default_step_data,
                created_at=now - timedelta(hours=2),
            )
            db.add(default_step)
            await db.flush()
            query.default_step_id = default_step.id
            default_step_ids.append(str(default_step.id))

            if newer_failed_attempt:
                db.add(Step(
                    title=f"Failed attempt {qi}",
                    slug=f"failed-{qi}-{suffix}",
                    status="error",
                    widget_id=widget.id,
                    query_id=query.id,
                    code=FAILING_CODE,
                    data=None,
                    created_at=now - timedelta(hours=1),
                ))

            viz = Visualization(
                title=f"Viz {qi}",
                status="success",
                report_id=report_id,
                query_id=query.id,
                view={"type": "bar_chart"},
            )
            db.add(viz)
            await db.flush()
            query_ids.append(str(query.id))
            viz_ids.append(str(viz.id))

        db.add(Artifact(
            report_id=report_id,
            user_id=user_id,
            organization_id=org_id,
            title="Dashboard",
            mode="page",
            version=1,
            content={"code": "function App() {}", "visualization_ids": viz_ids},
            status="completed",
        ))
        await db.commit()

    return {"query_ids": query_ids, "viz_ids": viz_ids, "default_step_ids": default_step_ids}


def _get_default_step(test_client, query_id, user_token, org_id):
    resp = test_client.get(
        f"/api/queries/{query_id}/default_step",
        headers={"Authorization": f"Bearer {user_token}", "X-Organization-Id": str(org_id)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["step"]


@pytest.mark.e2e
def test_rerun_refreshes_default_steps_behind_report_artifacts(
    create_report, create_user, login_user, whoami, rerun_report, get_report, test_client
):
    """Rerun must re-execute the default step of every artifact-referenced
    query — the step the dashboard actually renders — even when its stored
    payload was purged and a newer (failed) step version exists."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="Artifact report", user_token=user_token, org_id=org_id, data_sources=[])
    seeded = _run(_seed_artifact_graph(report["id"], n_queries=2, default_step_data=None))

    # Before: what the dashboard reads (default step) has no data — this is
    # exactly the state the retention job leaves a stale draft report in.
    for qid in seeded["query_ids"]:
        step = _get_default_step(test_client, qid, user_token, org_id)
        assert not (step.get("data") or {}).get("rows")

    body = rerun_report(report["id"], user_token=user_token, org_id=org_id)

    # After: every artifact query's default step is re-executed and holds the
    # code's current output, and it is still the same step the artifact reads.
    for qid, sid in zip(seeded["query_ids"], seeded["default_step_ids"]):
        step = _get_default_step(test_client, qid, user_token, org_id)
        assert step["id"] == sid
        rows = (step.get("data") or {}).get("rows")
        assert rows, f"default step of query {qid} was not refreshed by rerun"
        assert {r["month"] for r in rows} == {"2024-01", "2024-02"}
        assert {r["revenue"] for r in rows} == {10, 20}

    # The response must state what actually happened — not a full report dump.
    assert body["steps_total"] == 2
    assert body["steps_succeeded"] == 2
    assert body["steps_failed"] == 0

    refreshed = get_report(report["id"], user_token=user_token, org_id=org_id)
    assert refreshed["last_run_at"] is not None


@pytest.mark.e2e
def test_rerun_reports_partial_failures_and_still_refreshes_the_rest(
    create_report, create_user, login_user, whoami, rerun_report, test_client
):
    """A failing step must be counted as failed in the response — not silently
    swallowed behind a success answer — and must not block other queries."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="Partly broken", user_token=user_token, org_id=org_id, data_sources=[])
    seeded = _run(_seed_artifact_graph(
        report["id"], n_queries=3, default_step_data=None,
        newer_failed_attempt=False, failing_query_indexes=(1,),
    ))

    body = rerun_report(report["id"], user_token=user_token, org_id=org_id)

    assert body["steps_total"] == 3
    assert body["steps_succeeded"] == 2
    assert body["steps_failed"] == 1

    healthy = [qid for i, qid in enumerate(seeded["query_ids"]) if i != 1]
    for qid in healthy:
        step = _get_default_step(test_client, qid, user_token, org_id)
        assert (step.get("data") or {}).get("rows")


@pytest.mark.e2e
def test_rerun_targets_only_the_latest_artifact_version(
    create_report, create_user, login_user, whoami, rerun_report, test_client
):
    """Artifact edits create a new row per version; superseded versions stay
    non-deleted. A rerun must refresh what the report renders NOW — the
    latest artifact — not re-execute (and count failures for) queries only
    referenced by old versions."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="Versioned artifact", user_token=user_token, org_id=org_id, data_sources=[])
    # v1 (superseded): one healthy query + one whose code raises
    superseded = _run(_seed_artifact_graph(
        report["id"], n_queries=2, default_step_data=None,
        newer_failed_attempt=False, failing_query_indexes=(1,),
    ))
    # v2 (latest): two healthy queries
    latest = _run(_seed_artifact_graph(
        report["id"], n_queries=2, default_step_data=None, newer_failed_attempt=False,
    ))

    body = rerun_report(report["id"], user_token=user_token, org_id=org_id)

    # Only the latest artifact's queries ran — and none of them failed.
    assert body["steps_total"] == 2
    assert body["steps_succeeded"] == 2
    assert body["steps_failed"] == 0

    for qid in latest["query_ids"]:
        step = _get_default_step(test_client, qid, user_token, org_id)
        assert (step.get("data") or {}).get("rows")
    for qid in superseded["query_ids"]:
        step = _get_default_step(test_client, qid, user_token, org_id)
        assert not (step.get("data") or {}).get("rows"), (
            "a query referenced only by a superseded artifact version was re-executed")


DS_CLIENT_CODE = """
def generate_df(ds_clients, excel_files):
    client = ds_clients[next(iter(ds_clients))]
    return client.execute_query("SELECT Name AS genre FROM Genre ORDER BY GenreId LIMIT 3")
"""


@pytest.mark.e2e
def test_rerun_executes_step_code_against_report_data_sources(
    create_report, create_user, login_user, whoami, rerun_report,
    install_demo_data_source, test_client
):
    """Rerun must build working data-source clients for the report's data
    sources and hand them to the step code (the production shape: saved SQL
    re-executed against the attached source)."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    demo = install_demo_data_source(demo_id="chinook", user_token=user_token, org_id=org_id)
    report = create_report(title="DS rerun", user_token=user_token, org_id=org_id,
                           data_sources=[demo["data_source_id"]])
    seeded = _run(_seed_artifact_graph(
        report["id"], n_queries=1, default_step_code=DS_CLIENT_CODE,
        default_step_data=None, newer_failed_attempt=False,
    ))

    body = rerun_report(report["id"], user_token=user_token, org_id=org_id)
    assert body["steps_total"] == 1
    assert body["steps_succeeded"] == 1

    step = _get_default_step(test_client, seeded["query_ids"][0], user_token, org_id)
    rows = (step.get("data") or {}).get("rows")
    assert rows and len(rows) == 3
    assert "Rock" in {r["genre"] for r in rows}


@pytest.mark.e2e
def test_rerun_cost_does_not_scale_with_stored_step_history(
    create_report, create_user, login_user, whoami, rerun_report
):
    """Rerun must only touch the steps it re-executes. Historical step
    versions (each holding a full stored dataset) must not be hydrated, so
    latency and SQL traffic must not scale with stored history size."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    def _measure(rows_per_version):
        report = create_report(title=f"hist {rows_per_version}", user_token=user_token, org_id=org_id, data_sources=[])
        _run(_seed_artifact_graph(
            report["id"], n_queries=2, default_step_data=None, newer_failed_attempt=False,
            extra_success_versions=3, rows_per_version=rows_per_version,
        ))
        with record_sql() as statements:
            t0 = time.perf_counter()
            rerun_report(report["id"], user_token=user_token, org_id=org_id)
            elapsed = time.perf_counter() - t0
        return elapsed, _steps_selects(statements)

    n_queries = 2
    small_dt, small_steps_sql = _measure(rows_per_version=10)
    large_dt, large_steps_sql = _measure(rows_per_version=8000)

    print(f"\n[rerun-perf] small={small_dt*1000:.0f}ms ({len(small_steps_sql)} steps SELECTs), "
          f"large={large_dt*1000:.0f}ms ({len(large_steps_sql)} steps SELECTs)")

    # SQL traffic against steps is bounded by the number of rerun targets,
    # not by how many historical versions exist (the mapper-wide selectin
    # cascade issues extra SELECTs per widget and hydrates every version's
    # data). Identical counts across history sizes plus a per-target bound
    # (resolution + execution + refresh is a handful per rerun step) catch a
    # reintroduced cascade without pinning the exact query plan.
    assert len(large_steps_sql) == len(small_steps_sql), (
        "steps SELECT count grew with stored history — full-graph hydration is back")
    assert len(large_steps_sql) <= n_queries * 4, (
        f"{len(large_steps_sql)} steps SELECTs for {n_queries} rerun targets — "
        f"the rerun is hydrating steps beyond the ones it re-executes")
