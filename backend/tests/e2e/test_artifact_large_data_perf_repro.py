"""
Perf reproduction + regression guard: public report/artifact pages
("/r/{id}") were extremely slow when the report's queries hold a lot of data.

Now that the public endpoints (report_service.py) and the permission
decorator use lazyload("*") whitelists, test_public_endpoints_* asserts the
FIXED behavior: metadata endpoints never touch steps.data, and only the
served step row is ever hydrated. test_plain_select_* still documents the
mapper-level hazard (a bare select(Report) cascades) — that is WHY the
endpoints must opt out explicitly.

Reported symptom: on a published report with large datasets, the browser shows
GET /r/{id} ~18s and GET /r/{id}/artifacts ~17s even though both responses are
tiny (~1-2 kB), and the page stays on "Loading..." while each query's /step is
fetched one at a time.

Hypothesis being validated (all backend-side):

  1. CASCADE — every relationship on Report (and Query/Step) is mapper-level
     ``lazy="selectin"`` (app/models/report.py:58-75, query.py, step.py), so a
     plain ``select(Report)`` eagerly loads the ENTIRE report graph: all
     queries -> ALL step versions each carrying the full result rows in the
     ``steps.data`` JSON column, all artifact versions with full content, all
     completions, widgets, visualizations, ...

  2. EVERY PUBLIC ENDPOINT PAYS IT — get_public_report / get_public_artifacts /
     get_public_queries / get_public_step all start with ``select(Report)``
     just to check ``artifact_visibility`` (report_service.py), so even the
     tiny artifacts-list response hydrates every stored dataset. GET /r/{id}
     additionally re-selects the Report a second time for fork eligibility
     (routes/report.py:362) — the cascade runs twice in one request.

  3. VERSION AMPLIFICATION — ``Query.steps`` (lazy="selectin") loads every
     historical step version, each with a full copy of the dataset, although
     only the default step's data is ever served.

No LLM or live data source needed: the report graph is seeded directly.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/e2e/test_artifact_large_data_perf_repro.py -v -s

Tune dataset size with BOW_REPRO_ROWS (rows per step version, default 15000).
"""
import asyncio
import json
import os
import re
import time
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.visualization import Visualization
from app.models.artifact import Artifact


ROWS_PER_STEP = int(os.environ.get("BOW_REPRO_ROWS", "15000"))
N_QUERIES = 4
N_STEP_VERSIONS = 3        # each query was re-run 3 times
N_ARTIFACT_VERSIONS = 3    # dashboard was edited 3 times


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# SQL statement recorder — counts every statement the (single-process) app
# executes, grouped by the first table in FROM.
# ---------------------------------------------------------------------------

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


def _tables_hit(statements):
    counts = {}
    for s in statements:
        m = re.search(r"FROM\s+(\w+)", s)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def _make_rows(n):
    return [
        {
            "order_id": i,
            "customer_name": f"customer_{i % 997}",
            "region": ("north", "south", "east", "west")[i % 4],
            "product": f"product_{i % 251}",
            "quantity": (i * 7) % 40 + 1,
            "unit_price": round(3.5 + (i % 900) * 0.13, 2),
            "revenue": round(((i * 7) % 40 + 1) * (3.5 + (i % 900) * 0.13), 2),
            "order_date": f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
        }
        for i in range(n)
    ]


async def _seed(rows_per_step: int):
    """Seed a public report: N_QUERIES queries, each with N_STEP_VERSIONS
    steps holding `rows_per_step` result rows in steps.data, plus
    N_ARTIFACT_VERSIONS artifact versions."""
    suffix = uuid.uuid4().hex[:8]
    columns = [{"field": f} for f in _make_rows(1)[0].keys()]

    async with async_session_maker() as db:
        org = Organization(name=f"Perf Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="Perf User",
            email=f"perf-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        report = Report(
            title=f"Perf Report {suffix}",
            slug=f"perf-report-{suffix}",
            status="published",
            artifact_visibility="public",
            user_id=user.id,
            organization_id=org.id,
        )
        db.add(report)
        await db.flush()

        query_ids, viz_ids = [], []
        for qi in range(N_QUERIES):
            widget = Widget(
                title=f"W{qi} {suffix}",
                slug=f"w{qi}-{suffix}",
                report_id=report.id,
            )
            db.add(widget)
            await db.flush()

            query = Query(
                title=f"Query {qi}",
                report_id=report.id,
                widget_id=widget.id,
                organization_id=org.id,
                user_id=user.id,
            )
            db.add(query)
            await db.flush()

            last_step = None
            for si in range(N_STEP_VERSIONS):
                step = Step(
                    title=f"Step {qi}.{si}",
                    slug=f"s{qi}-{si}-{suffix}",
                    status="success",
                    widget_id=widget.id,
                    query_id=query.id,
                    data={"rows": _make_rows(rows_per_step), "columns": columns},
                    data_model={"type": "bar_chart", "columns": columns},
                    view={"type": "bar_chart"},
                )
                db.add(step)
                last_step = step
            await db.flush()
            query.default_step_id = last_step.id

            viz = Visualization(
                title=f"Viz {qi}",
                status="success",
                report_id=report.id,
                query_id=query.id,
                view={"type": "bar_chart"},
            )
            db.add(viz)
            await db.flush()
            query_ids.append(str(query.id))
            viz_ids.append(str(viz.id))

        # a realistic generated dashboard: ~100 kB of JSX per artifact version
        fake_code = "function Dashboard() {\n" + ("  // chart section filler line of jsx code\n" * 2500) + "}\n"
        for vi in range(N_ARTIFACT_VERSIONS):
            db.add(Artifact(
                report_id=report.id,
                user_id=user.id,
                organization_id=org.id,
                title="Dashboard",
                mode="page",
                version=vi + 1,
                content={"code": fake_code, "visualization_ids": viz_ids},
                status="completed",
            ))

        await db.commit()

        step_bytes = len(json.dumps({"rows": _make_rows(rows_per_step), "columns": columns}))
        return {
            "report_id": str(report.id),
            "query_ids": query_ids,
            "viz_ids": viz_ids,
            "step_bytes": step_bytes,
            "total_step_bytes": step_bytes * N_QUERIES * N_STEP_VERSIONS,
        }


# ---------------------------------------------------------------------------
# Claim 1 + 3: a plain select(Report) hydrates every dataset & every version
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_plain_select_report_cascades_all_step_data():
    async def scenario():
        seeded = await _seed(ROWS_PER_STEP)

        async with async_session_maker() as db:
            with record_sql() as statements:
                t0 = time.perf_counter()
                res = await db.execute(select(Report).where(Report.id == seeded["report_id"]))
                report = res.scalar_one()
                elapsed = time.perf_counter() - t0

        tables = _tables_hit(statements)

        # Everything below was already loaded by the cascade — no further IO.
        loaded_steps = [s for q in report.queries for s in q.steps]
        hydrated_bytes = sum(len(json.dumps(s.data)) for s in loaded_steps if "data" in s.__dict__)
        artifact_bytes = sum(len(json.dumps(a.content)) for a in report.artifacts)

        print(f"\n[cascade] select(Report) issued {len(statements)} SQL statements in {elapsed:.2f}s")
        print(f"[cascade] tables hit: {dict(sorted(tables.items(), key=lambda kv: -kv[1]))}")
        print(f"[cascade] steps hydrated: {len(loaded_steps)} "
              f"(= {N_QUERIES} queries x {N_STEP_VERSIONS} versions; only {N_QUERIES} default steps are ever served)")
        print(f"[cascade] step data hydrated: {hydrated_bytes / 1e6:.1f} MB; "
              f"artifact content: {artifact_bytes / 1e6:.1f} MB")

        # Claim 1: the steps table (with its data column) is pulled by a bare Report select
        assert tables.get("steps", 0) >= 1
        assert len(statements) > 10, "expected a selectin cascade, got a simple query"
        # Claim 3: ALL versions load, not just the default step
        assert len(loaded_steps) == N_QUERIES * N_STEP_VERSIONS
        assert hydrated_bytes >= seeded["total_step_bytes"] * 0.9

    _run(scenario())


# ---------------------------------------------------------------------------
# Claim 2: every public /r endpoint pays the cascade; timings scale with the
# stored dataset size even though the responses stay tiny.
# ---------------------------------------------------------------------------

def _fetch_public_page(client, report_id, query_ids):
    """Replicate the browser waterfall of frontend/pages/r/[id]/index.vue and
    return {endpoint: (seconds, response_bytes, n_sql, steps_sql)}."""
    timings = {}

    def timed(label, url):
        with record_sql() as stmts:
            t0 = time.perf_counter()
            resp = client.get(url)
            dt = time.perf_counter() - t0
        assert resp.status_code == 200, f"{url} -> {resp.status_code}: {resp.text[:200]}"
        timings[label] = (dt, len(resp.content), len(stmts), _tables_hit(stmts).get("steps", 0))
        return resp

    timed("GET /r/{id}", f"/api/r/{report_id}")
    artifacts = timed("GET /r/{id}/artifacts", f"/api/r/{report_id}/artifacts").json()
    artifact_id = artifacts[0]["id"]
    timed("GET /r/{id}/artifacts/{aid}", f"/api/r/{report_id}/artifacts/{artifact_id}")
    timed("GET /r/{id}/queries", f"/api/r/{report_id}/queries?artifact_id={artifact_id}")
    # the frontend awaits each step sequentially (for-loop in loadVisualizationData)
    t0 = time.perf_counter()
    n_sql = steps_sql = size = 0
    with record_sql() as stmts:
        for qid in query_ids:
            r = client.get(f"/api/r/{report_id}/queries/{qid}/step")
            assert r.status_code == 200
            size += len(r.content)
    timings[f"{len(query_ids)} serial /step calls"] = (
        time.perf_counter() - t0, size, len(stmts), _tables_hit(stmts).get("steps", 0))
    return timings


@pytest.mark.e2e
def test_public_endpoints_scale_with_stored_step_data(test_client):
    small = _run(_seed(rows_per_step=10))
    large = _run(_seed(rows_per_step=ROWS_PER_STEP))

    small_t = _fetch_public_page(test_client, small["report_id"], small["query_ids"])
    large_t = _fetch_public_page(test_client, large["report_id"], large["query_ids"])

    print(f"\n[endpoints] per-step dataset: small={small['step_bytes']/1e3:.0f} kB, "
          f"large={large['step_bytes']/1e6:.1f} MB "
          f"({N_QUERIES} queries x {N_STEP_VERSIONS} step versions stored)")
    header = f"{'endpoint':38} {'small':>9} {'large':>9} {'ratio':>7} {'resp(large)':>12} {'SQL':>5} {'steps-SQL':>9}"
    print("[endpoints] " + header)
    for label in small_t:
        s_dt, _, _, _ = small_t[label]
        l_dt, l_size, l_sql, l_steps_sql = large_t[label]
        ratio = l_dt / s_dt if s_dt > 0 else float("inf")
        print(f"[endpoints] {label:38} {s_dt*1000:7.0f}ms {l_dt*1000:7.0f}ms {ratio:6.1f}x "
              f"{l_size/1e3:10.1f}kB {l_sql:5d} {l_steps_sql:9d}")

    total_small = sum(v[0] for v in small_t.values())
    total_large = sum(v[0] for v in large_t.values())
    print(f"[endpoints] full page waterfall: small={total_small*1000:.0f}ms large={total_large*1000:.0f}ms")

    # --- REGRESSION GUARDS (post-fix behavior) ---------------------------
    # The public endpoints use lazyload("*") whitelists (report_service.py),
    # so metadata endpoints must never hydrate steps.data.
    for label in ("GET /r/{id}", "GET /r/{id}/artifacts",
                  "GET /r/{id}/artifacts/{aid}", "GET /r/{id}/queries"):
        assert large_t[label][3] == 0, (
            f"{label} queried the steps table {large_t[label][3]}x — the "
            f"lazy='selectin' cascade is back; it must not load step data")

    # The step endpoint serves exactly ONE step per call (the default one),
    # never every historical version of every query.
    step_label = f"{len(large['query_ids'])} serial /step calls"
    assert large_t[step_label][3] == len(large["query_ids"]), (
        f"expected 1 steps SELECT per /step call, got {large_t[step_label][3]}")

    # The artifacts LIST returns a tiny payload and its latency must no
    # longer scale with the stored step data.
    l_dt, l_size, l_sql, _ = large_t["GET /r/{id}/artifacts"]
    s_dt = small_t["GET /r/{id}/artifacts"][0]
    assert l_size < 5_000, "artifacts list response should be tiny"
    assert l_dt < max(s_dt * 3, 0.5), (
        f"artifacts list latency scales with stored data again "
        f"(small={s_dt*1000:.0f}ms large={l_dt*1000:.0f}ms) — cascade regression")
