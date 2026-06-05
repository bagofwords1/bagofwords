"""End-to-end reproduction of the "metric broken down by a dimension renders
as a single line" bug, against the committed Chinook demo and a real
Anthropic model.

Symptom (from the field): asking for a metric in dynamics broken down by a
dimension (e.g. "GMV by category") produces SQL that correctly GROUPs BY the
dimension, but the chart shows a single total line instead of one line per
category.

This test drives the *real* agent through the HTTP surface — same path the
product uses — then inspects the chart that was actually persisted on the
widget (``last_step.data_model`` + ``last_step.view``) and asserts the
breakdown dimension survived into the visualization config.

Gating
------
Requires ``ANTHROPIC_API_KEY_TEST`` (skipped otherwise) and the committed
``demo-datasources/chinook.sqlite``. Runs against the default sqlite test DB,
so no Docker/Postgres is needed in the sandbox.

Run it:
    cd backend
    ANTHROPIC_API_KEY_TEST=sk-ant-... \
      python -m pytest tests/e2e/test_repro_viz_breakdown.py -s -m e2e

See ``REPRO-viz-breakdown.md`` for the full feedback loop.
"""

import json
import os

import pytest

# Mirror the customer's "metric broken down by category" ask, using a Chinook
# dimension (genre) that requires the agent to GROUP BY a categorical column.
# The prompt is deliberately prescriptive so the SQL reliably returns granular,
# tall (month x genre) rows — which is exactly the shape that triggers the bug.
REPRO_PROMPT = (
    "Plot total sales over time broken down by music genre, as a LINE chart "
    "with one line per genre. Aggregate sales by month. "
    "Join Invoice -> InvoiceLine -> Track -> Genre and use the sum of "
    "InvoiceLine.UnitPrice * InvoiceLine.Quantity as the sales metric. "
    "I want to compare genres against each other over time, not a single total line."
)

CARTESIAN_TYPES = {"line_chart", "bar_chart", "area_chart"}

# Model used for the repro. Override with BOW_REPRO_MODEL. Defaults to Haiku
# (cheap + fast) — the bug is model-independent (it's a schema/validation
# mismatch in create_data's viz inference, not a planning failure).
REPRO_MODEL_ID = os.getenv("BOW_REPRO_MODEL", "claude-haiku-4-5-20251001")


def _install_anthropic_haiku(test_client, *, user_token: str, org_id: str) -> None:
    """Install an Anthropic provider with the repro model as the org default.

    Uses ``ANTHROPIC_API_KEY_TEST``. Sets the chosen model as both default and
    small-default so the planner, viz-inference pass, and judge all run on it.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY_TEST", "")
    assert api_key, "ANTHROPIC_API_KEY_TEST is not set"
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": "anthropic provider",
            "provider_type": "anthropic",
            "credentials": {"api_key": str(api_key)},
            "models": [
                {
                    "model_id": REPRO_MODEL_ID,
                    "name": REPRO_MODEL_ID,
                    "is_custom": False,
                    "is_default": True,
                }
            ],
        },
        headers={
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        },
    )
    assert resp.status_code == 200, resp.text


def _looks_numeric(values) -> bool:
    seen = False
    for v in values:
        if v is None:
            continue
        seen = True
        if isinstance(v, bool):
            return False
        if not isinstance(v, (int, float)):
            return False
    return seen


def _analyze_chart(step: dict) -> dict:
    """Pull the breakdown-relevant fields out of a persisted step."""
    dm = step.get("data_model") or {}
    view = step.get("view") or {}
    inner_view = view.get("view") if isinstance(view.get("view"), dict) else view
    data = step.get("data") or {}

    columns = []
    for c in data.get("columns") or []:
        if isinstance(c, dict):
            f = c.get("field") or c.get("headerName")
            if f:
                columns.append(f)
        elif isinstance(c, str):
            columns.append(c)

    rows = data.get("rows") or []
    info = data.get("info") or {}
    total_rows = info.get("total_rows")
    if not isinstance(total_rows, int):
        total_rows = len(rows)

    series = dm.get("series") or []
    x_key = series[0].get("key") if series and isinstance(series[0], dict) else None
    value_keys = {
        s.get("value") for s in series if isinstance(s, dict) and s.get("value")
    }

    group_by = dm.get("group_by")
    view_group_by = inner_view.get("groupBy") if isinstance(inner_view, dict) else None
    y = inner_view.get("y") if isinstance(inner_view, dict) else None
    multi_measure = isinstance(y, list) and len(y) > 1

    # A candidate breakdown column = a non-x, non-measure column. For a tall
    # (month, genre, total) result that's the 'genre' column.
    dim_cols = [c for c in columns if c != x_key and c not in value_keys]
    # Trim obvious measure columns from the sample to reduce false positives.
    if rows:
        dim_cols = [
            c
            for c in dim_cols
            if not _looks_numeric([r.get(c) for r in rows[:25] if isinstance(r, dict)])
        ]

    distinct_x = None
    if x_key and rows:
        distinct_x = len({r.get(x_key) for r in rows if isinstance(r, dict)})

    breakdown_dim_present = bool(dim_cols) or (
        distinct_x is not None and total_rows > distinct_x
    )
    breakdown_applied = bool(group_by) or bool(view_group_by) or len(series) > 1 or multi_measure

    return {
        "title": step.get("title"),
        "type": dm.get("type"),
        "columns": columns,
        "total_rows": total_rows,
        "x_key": x_key,
        "value_keys": sorted(v for v in value_keys if v),
        "series_count": len(series),
        "group_by": group_by,
        "view_group_by": view_group_by,
        "view_y": y,
        "dim_cols": dim_cols,
        "distinct_x": distinct_x,
        "breakdown_dim_present": breakdown_dim_present,
        "breakdown_applied": breakdown_applied,
    }


@pytest.mark.e2e
def test_metric_breakdown_survives_to_chart(
    create_user,
    login_user,
    whoami,
    install_demo_data_source,
    create_report,
    create_completion,
    test_client,
):
    if not os.getenv("ANTHROPIC_API_KEY_TEST"):
        pytest.skip("ANTHROPIC_API_KEY_TEST is not set")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    _install_anthropic_haiku(test_client, user_token=token, org_id=org_id)
    print(f"[repro] using model: {REPRO_MODEL_ID}", flush=True)
    ds = install_demo_data_source(demo_id="chinook", user_token=token, org_id=org_id)
    ds_id = ds["data_source_id"]

    report = create_report(
        title="Repro: metric breakdown by category",
        user_token=token,
        org_id=org_id,
        data_sources=[ds_id],
    )

    # Drive the real agent (synchronous foreground run).
    create_completion(
        report_id=report["id"],
        prompt=REPRO_PROMPT,
        user_token=token,
        org_id=org_id,
        background=False,
    )

    # Inspect what got persisted on the report's widgets.
    resp = test_client.get(f"/api/reports/{report['id']}/widgets", headers=headers)
    assert resp.status_code == 200, resp.text
    widgets = resp.json()

    analyses = []
    for w in widgets:
        step = w.get("last_step")
        if isinstance(step, dict):
            analyses.append(_analyze_chart(step))

    print("\n========== REPRO DIAGNOSTIC: viz breakdown ==========", flush=True)
    print(f"prompt: {REPRO_PROMPT}", flush=True)
    print(f"widgets produced: {len(widgets)}", flush=True)
    for a in analyses:
        print(json.dumps(a, indent=2, default=str), flush=True)
    print("=====================================================\n", flush=True)

    charts = [a for a in analyses if a["type"] in CARTESIAN_TYPES]
    assert charts, (
        "Agent did not produce a line/bar/area chart widget; cannot evaluate the "
        f"breakdown. Produced types: {[a['type'] for a in analyses]}"
    )

    # The bug: a chart whose underlying data carries a breakdown dimension, but
    # whose visualization config applies no grouping (single line).
    broken = [
        a
        for a in charts
        if a["breakdown_dim_present"] and not a["breakdown_applied"]
    ]
    assert not broken, (
        "Metric breakdown was lost on the chart — the data is granular along a "
        "category dimension, but the chart applies no group_by / multi-series, so "
        "it renders a single total line. Offending chart(s):\n"
        + json.dumps(broken, indent=2, default=str)
    )

    # Positive confirmation that at least one chart actually applied the breakdown.
    assert any(a["breakdown_applied"] for a in charts), (
        "No chart applied a breakdown (group_by / multi-series). Charts:\n"
        + json.dumps(charts, indent=2, default=str)
    )

    # The frontend renders from the Visualization.view (not Step.view), so
    # assert the breakdown actually reached the rendered view: a valid
    # CartesianView with x/y and a groupBy. This is the layer the customer sees.
    import asyncio as _asyncio
    from sqlalchemy import select as _select
    from app.dependencies import async_session_maker as _sm
    from app.models.visualization import Visualization as _Viz

    async def _viz_views():
        async with _sm() as s:
            rows = (await s.execute(
                _select(_Viz.view).where(_Viz.report_id == str(report["id"]))
            )).all()
            return [r[0] for r in rows if isinstance(r[0], dict)]

    viz_views = _asyncio.run(_viz_views())
    print("VIZ VIEWS:", json.dumps(viz_views, default=str), flush=True)

    def _view_group_by(v: dict):
        inner = v.get("view") if isinstance(v.get("view"), dict) else v
        if not isinstance(inner, dict):
            return None
        gb = inner.get("groupBy")
        if gb:
            return gb
        enc = inner.get("encoding") if isinstance(inner.get("encoding"), dict) else None
        if enc and (enc.get("groupBy") or enc.get("series")):
            return enc.get("groupBy") or "multi-series"
        return None

    cartesian_views = [
        v for v in viz_views
        if isinstance((v.get("view") or v), dict)
        and ((v.get("view") or v).get("type") in CARTESIAN_TYPES)
    ]
    assert cartesian_views, f"No cartesian visualization view persisted: {viz_views}"
    assert any(_view_group_by(v) for v in cartesian_views), (
        "Rendered visualization view has no breakdown (groupBy/multi-series); "
        "the chart would draw a single line. Views:\n"
        + json.dumps(viz_views, indent=2, default=str)
    )
