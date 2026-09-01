"""
Regression guard: the card-level "Run" on a parameterized widget must build
data-source clients when the report has data sources attached.

Reported symptom: a builder typed a value into a widget's param bar, hit Run,
and got the raw SQLAlchemy text back in the card:

    greenlet_spawn has not been called; can't call await_only() here.
    Was IO attempted in an unexpected place?

Root cause: query_service loaded the report graph with
`selectinload(Report.data_sources).options(lazyload("*"))`. The wildcard also
cancels DataSource.connections' own lazy="selectin" default, so
DataSourceService.construct_clients — which reads `data_source.connections`
synchronously — did lazy IO inside a coroutine. run_query_viewer collects that
failure per data source and, when no client survives, re-raises it as a
ParamError, which is how the driver's own words reached the UI.

It only bit a run that MISSED the per-viewer cache (a new param value, or a
step that refreshed since), and only when the report had data sources
attached — reports without them fall back to the org-level query, which loads
with the model's own eager defaults. Both conditions are why it survived
review: the existing param tests seed `data_sources=[]`.

Run:
    cd backend
    uv run pytest tests/e2e/test_query_viewer_run_with_data_source.py -v
"""
import asyncio
import uuid
from datetime import datetime

import pytest

from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.widget import Widget


def _run(coro):
    return asyncio.run(coro)


# Reports how many clients the run was handed, so a run that "succeeds" with
# an empty ds_clients (the silent half of the same bug) still fails the test.
PARAM_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame({
        "ran_for": [params["BillingYear"]],
        "client_count": [len(ds_clients)],
    })
"""

PARAM_SPEC = {
    "name": "BillingYear",
    "type": "string",
    "label": "BillingYear",
    "default": None,
    "required": False,
    "source": "input",
    "identity_binding": None,
    "options": None,
    "options_source": None,
    "strict_options": False,
}


async def _seed_parameterized_query(report_id):
    """Seed the widget/query/step graph an AI-built dashboard produces.

    There is no public CRUD API for it, so it is written directly — the same
    approach as test_report_rerun_params.py.
    """
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)

        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()

        query = Query(
            title="Projects by company",
            report_id=report_id,
            widget_id=widget.id,
            organization_id=report.organization_id,
            user_id=report.user_id,
            parameters=[PARAM_SPEC],
        )
        db.add(query)
        await db.flush()

        step = Step(
            title="Step",
            slug=f"s-{suffix}",
            status="success",
            widget_id=widget.id,
            query_id=query.id,
            code=PARAM_CODE,
            data={"rows": [{"ran_for": "2023", "client_count": 1}],
                  "columns": [{"field": "ran_for"}, {"field": "client_count"}]},
            applied_params={"BillingYear": "2023"},
            created_at=datetime.utcnow(),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        await db.commit()
        return str(query.id)


@pytest.mark.e2e
def test_viewer_run_builds_clients_for_a_report_with_data_sources(
    dynamic_sqlite_db, create_data_source, create_report, create_user,
    login_user, whoami, test_client,
):
    """A viewer run on a report WITH an attached data source must execute."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    ds = create_data_source(
        name=f"sqlite-{uuid.uuid4().hex[:6]}",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    report = create_report(title="Projects by company", user_token=token,
                           org_id=org_id, data_sources=[ds["id"]])
    query_id = _run(_seed_parameterized_query(report["id"]))

    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    # A value the step has never run with, so the per-viewer cache misses and
    # the request reaches client construction — the run that used to fail.
    response = test_client.post(
        f"/api/queries/{query_id}/run",
        json={"mode": "viewer", "params": {"BillingYear": "2024"}},
        headers=headers,
    )

    # Before the fix: 400, detail = "greenlet_spawn has not been called; …".
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "success", body
    assert body["applied_params"] == {"BillingYear": "2024"}

    rows = (body.get("data") or {}).get("rows") or []
    assert rows, body
    assert str(rows[0]["ran_for"]) == "2024"
    # The attached data source produced at least one client. Zero means
    # construct_clients failed and the run just happened not to need it.
    assert rows[0]["client_count"] >= 1, rows
