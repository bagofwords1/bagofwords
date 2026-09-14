"""run_query executes an EXISTING query with supplied parameter values.

Contract asserted here:
  1. the saved code runs with the requested values, and the rows returned are
     that slice — not the query's stored snapshot;
  2. it creates NO new Step and never repoints default_step_id, so the shared
     dashboard and every other viewer are untouched;
  3. each value combination gets its own per-viewer cached result, and a
     repeat call is served from it (cached=True);
  4. a required parameter with no value returns structured `missing_params`
     instead of running or guessing;
  5. unknown names, undeclared params, and identity-locked values are refused
     — and a failed run NEVER falls back to the stored snapshot, which answers
     a different question.

Run:
    cd backend && uv run pytest tests/e2e/test_run_query_params.py -v
"""
import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.step_user_result import StepUserResult
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget


def _run(coro):
    return asyncio.run(coro)


# Mirrors what the agent generates: params is a real argument and the value
# drives the rows, so a wrong/ignored value is visible in the output.
CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    region = params.get("region")
    rows = [
        {"region": "US", "revenue": 100},
        {"region": "DE", "revenue": 40},
        {"region": "FR", "revenue": 25},
    ]
    if region is not None:
        rows = [r for r in rows if r["region"] == region]
    return pd.DataFrame(rows)
"""

REQUIRED_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame([{"region": params["region"], "revenue": 1}])
"""


def _spec(name="region", required=False, default=None, source="input",
          identity_binding=None, options=None):
    return {
        "name": name, "type": "string", "label": name.title(),
        "default": default, "required": required, "source": source,
        "identity_binding": identity_binding, "options": options,
        "options_source": None, "strict_options": False,
    }


async def _seed_query(report_id, specs, code=CODE, snapshot_region="US"):
    """Seed one parameterized query + step + visualization on the report.

    Queries/steps are produced by the AI flow in production and have no public
    CRUD API, so the graph is seeded directly (as test_report_rerun_params does).
    """
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()

        query = Query(
            title="Revenue by region",
            report_id=report_id,
            widget_id=widget.id,
            organization_id=report.organization_id,
            user_id=report.user_id,
            parameters=specs,
        )
        db.add(query)
        await db.flush()

        step = Step(
            title="Revenue by region",
            slug=f"s-{suffix}",
            status="success",
            widget_id=widget.id,
            query_id=query.id,
            code=code,
            # The stored snapshot answers snapshot_region — a run for another
            # value must never serve these rows.
            data={"rows": [{"region": snapshot_region, "revenue": 100}],
                  "columns": [{"field": "region"}, {"field": "revenue"}]},
            applied_params={"region": snapshot_region},
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id

        viz = Visualization(title="Viz", status="success", report_id=report_id,
                            query_id=query.id, view={"type": "table"})
        db.add(viz)
        await db.flush()
        await db.commit()
        return {"query_id": str(query.id), "step_id": str(step.id),
                "widget_id": str(widget.id), "viz_id": str(viz.id)}


async def _call(org_id, user_id, report_id, tool_input):
    from app.ai.tools.implementations.run_query import RunQueryTool

    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        report = await db.get(Report, report_id)
        settings = await org.get_settings(db)
        events = []
        async for evt in RunQueryTool().run_stream(
            tool_input,
            {"db": db, "organization": org, "user": user, "report": report,
             "settings": settings},
        ):
            events.append(evt)
        return events[-1].payload


async def _step_count(widget_id):
    async with async_session_maker() as db:
        return await db.scalar(
            select(func.count()).select_from(Step).where(Step.widget_id == widget_id)
        )


async def _query_state(query_id):
    async with async_session_maker() as db:
        q = await db.get(Query, query_id)
        step = await db.get(Step, str(q.default_step_id))
        return {"default_step_id": str(q.default_step_id),
                "rows": (step.data or {}).get("rows"),
                "applied_params": step.applied_params}


async def _user_results(step_id):
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(StepUserResult).where(StepUserResult.step_id == step_id)
        )).scalars().all()
        return [(r.params_fingerprint, r.applied_params, (r.data or {}).get("rows")) for r in rows]


@pytest.fixture
def report(create_report, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    info = whoami(token)
    rep = create_report(title="Regions", user_token=token,
                        org_id=info["organizations"][0]["id"])
    return {"report_id": rep["id"], "org_id": info["organizations"][0]["id"],
            "user_id": info["id"], "token": token}


@pytest.mark.e2e
def test_runs_the_saved_code_for_the_requested_values(report):
    seeded = _run(_seed_query(report["report_id"], [_spec()]))
    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         {"query_id": seeded["query_id"], "params": {"region": "DE"}}))
    out, obs = payload["output"], payload["observation"]

    assert out["success"], out.get("error")
    assert out["data"]["rows"] == [{"region": "DE", "revenue": 40}]
    assert out["applied_params"] == {"region": "DE"}
    assert out["cached"] is False
    # The planner reads the same budgeted preview create_data emits.
    assert obs["data_preview"]["rows"] == out["data"]["rows"]
    # Provenance: the summary states the values these rows answer.
    assert "region='DE'" in obs["summary"]
    assert obs["applied_params"] == {"region": "DE"}
    assert [p["name"] for p in obs["parameters"]] == ["region"]


@pytest.mark.e2e
def test_creates_no_step_and_leaves_the_shared_snapshot_alone(report):
    seeded = _run(_seed_query(report["report_id"], [_spec()]))
    before_steps = _run(_step_count(seeded["widget_id"]))
    before = _run(_query_state(seeded["query_id"]))

    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         {"query_id": seeded["query_id"], "params": {"region": "FR"}}))
    assert payload["output"]["success"]

    assert _run(_step_count(seeded["widget_id"])) == before_steps, "run_query must not create a Step"
    after = _run(_query_state(seeded["query_id"]))
    assert after == before, "default step, its rows and its applied_params must be untouched"


@pytest.mark.e2e
def test_each_value_set_caches_separately_and_repeats_are_served_from_cache(report):
    seeded = _run(_seed_query(report["report_id"], [_spec()]))
    args = {"query_id": seeded["query_id"], "params": {"region": "DE"}}

    first = _run(_call(report["org_id"], report["user_id"], report["report_id"], args))
    assert first["output"]["cached"] is False

    second = _run(_call(report["org_id"], report["user_id"], report["report_id"], args))
    assert second["output"]["cached"] is True
    assert second["output"]["data"]["rows"] == first["output"]["data"]["rows"]
    assert "from cache" in second["observation"]["summary"]

    _run(_call(report["org_id"], report["user_id"], report["report_id"],
               {"query_id": seeded["query_id"], "params": {"region": "FR"}}))

    results = _run(_user_results(seeded["step_id"]))
    assert len({fp for fp, _, _ in results}) == 2, "one cache row per value combination"
    by_region = {(ap or {}).get("region"): rows for _, ap, rows in results}
    assert by_region["DE"] == [{"region": "DE", "revenue": 40}]
    assert by_region["FR"] == [{"region": "FR", "revenue": 25}]


@pytest.mark.e2e
def test_missing_required_value_asks_instead_of_running(report):
    seeded = _run(_seed_query(report["report_id"],
                              [_spec(required=True)], code=REQUIRED_CODE))
    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         {"query_id": seeded["query_id"]}))
    out, obs = payload["output"], payload["observation"]

    assert out["success"] is False
    assert [p["name"] for p in out["missing_params"]] == ["region"]
    assert out["missing_params"][0]["required"] is True
    assert out["missing_params"][0]["type"] == "string"
    # Structured, so the planner can hand it to clarify without parsing prose.
    assert obs["missing_params"][0]["name"] == "region"
    assert obs["error"]["type"] == "missing_parameter"
    assert not (out.get("data") or {}).get("rows")
    assert _run(_user_results(seeded["step_id"])) == [], "nothing was executed"


@pytest.mark.e2e
def test_unknown_parameter_names_the_declared_ones(report):
    seeded = _run(_seed_query(report["report_id"], [_spec()]))
    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         {"query_id": seeded["query_id"], "params": {"regionn": "DE"}}))
    out, obs = payload["output"], payload["observation"]

    assert out["success"] is False
    assert "regionn" in out["error"]
    assert "region" in obs["error"]["message"]
    assert not (out.get("data") or {}).get("rows"), \
        "must not fall back to the stored snapshot, which answers other values"


@pytest.mark.e2e
def test_query_without_parameters_refuses_values(report):
    seeded = _run(_seed_query(report["report_id"], []))
    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         {"query_id": seeded["query_id"], "params": {"region": "DE"}}))
    out = payload["output"]

    assert out["success"] is False
    assert "no parameters" in (out["error"] or "").lower()
    assert "add_parameter" in payload["observation"]["summary"]


@pytest.mark.e2e
def test_identity_locked_values_are_refused(report):
    seeded = _run(_seed_query(
        report["report_id"],
        [_spec(name="viewer_email", source="identity", identity_binding="viewer.email")],
    ))
    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         {"query_id": seeded["query_id"],
                          "params": {"viewer_email": "someone@else.com"}}))
    out = payload["output"]

    assert out["success"] is False
    assert "identity" in (out["error"] or "").lower()
    assert not (out.get("data") or {}).get("rows")


@pytest.mark.e2e
def test_execution_failure_does_not_serve_the_stored_snapshot(report):
    BROKEN = """
def generate_df(ds_clients, excel_files, params):
    raise RuntimeError("boom: " + str(params.get("region")))
"""
    seeded = _run(_seed_query(report["report_id"], [_spec()], code=BROKEN))
    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         {"query_id": seeded["query_id"], "params": {"region": "DE"}}))
    out, obs = payload["output"], payload["observation"]

    assert out["success"] is False
    assert not (out.get("data") or {}).get("rows")
    assert "NOT substituted" in obs["summary"]
    # The stored snapshot is still intact for the dashboard.
    assert _run(_query_state(seeded["query_id"]))["rows"] == [{"region": "US", "revenue": 100}]


@pytest.mark.e2e
def test_query_from_another_report_is_not_addressable(report, create_report):
    """A query id is addressable only from the report that owns it. Same user,
    same org, different report — still not reachable."""
    seeded = _run(_seed_query(report["report_id"], [_spec()]))
    other = create_report(title="Other report", user_token=report["token"],
                          org_id=report["org_id"])

    payload = _run(_call(report["org_id"], report["user_id"], other["id"],
                         {"query_id": seeded["query_id"], "params": {"region": "DE"}}))
    out = payload["output"]
    assert out["success"] is False
    assert "not found" in (out["error"] or "").lower()
    assert not (out.get("data") or {}).get("rows")


@pytest.mark.e2e
def test_a_visualization_id_resolves_to_its_query(report):
    """create_data reports both query_id and viz_id, read_query accepts either,
    and the planner passes whichever is nearest in context — live, it passed
    the viz_id. Both handles must resolve, in query_id or visualization_id."""
    seeded = _run(_seed_query(report["report_id"], [_spec()]))

    via_field = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                           {"visualization_id": seeded["viz_id"], "params": {"region": "DE"}}))
    assert via_field["output"]["success"], via_field["output"].get("error")
    assert via_field["output"]["data"]["rows"] == [{"region": "DE", "revenue": 40}]
    assert via_field["output"]["query_id"] == seeded["query_id"]
    assert via_field["output"]["visualization_id"] == seeded["viz_id"]

    # The real-world shape: a viz id handed to query_id.
    via_query_id = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                              {"query_id": seeded["viz_id"], "params": {"region": "FR"}}))
    assert via_query_id["output"]["success"], via_query_id["output"].get("error")
    assert via_query_id["output"]["data"]["rows"] == [{"region": "FR", "revenue": 25}]
    assert via_query_id["output"]["query_id"] == seeded["query_id"]


@pytest.mark.e2e
def test_no_id_at_all_is_a_validation_error(report):
    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         {"params": {"region": "DE"}}))
    assert payload["output"]["success"] is False
    assert payload["observation"]["error"]["type"] == "validation_error"
