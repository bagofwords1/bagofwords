"""add_parameter resolves its target from either handle the system hands out.

The report context lists each query with its visualizations (titled alike),
and create_data reports both `query_id` and `viz_id`; the planner passes
whichever is nearest — observed live, the viz id. Contract asserted here:
  1. a query id OR a visualization id in `query_id` reaches the same query,
     and the parameter is declared on that query in place;
  2. the output names the real query id, so the planner learns the right handle;
  3. an id from another report — query or visualization — is not addressable
     and leaves that report's query untouched.

The code rewrite is an LLM call; only the provider boundary is faked.

Run:
    cd backend && uv run pytest tests/e2e/test_add_parameter_target.py -v
"""
import asyncio
import uuid

import pytest

from app.ai.llm.types import TextDeltaEvent
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget


def _run(coro):
    return asyncio.run(coro)


CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame([
        {"country": "US", "sales": 100},
        {"country": "DE", "sales": 40},
    ])
"""

# What the rewrite model returns: the filter bound through the :country
# placeholder, applied to the rows.
REWRITTEN = '''```python
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    # WHERE (:country IS NULL OR country = :country)
    country = params.get("country")
    rows = [
        {"country": "US", "sales": 100},
        {"country": "DE", "sales": 40},
    ]
    if country is not None:
        rows = [r for r in rows if r["country"] == country]
    return pd.DataFrame(rows)
```'''


class _FakeLLM:
    def __init__(self, *args, **kwargs):
        pass

    async def inference_stream_v2(self, **kwargs):
        yield TextDeltaEvent(text=REWRITTEN)


@pytest.fixture(autouse=True)
def fake_rewrite_model(monkeypatch):
    monkeypatch.setattr("app.ai.tools.implementations.add_parameter.LLM", _FakeLLM)


def _country_param():
    return {
        "name": "country", "type": "string", "label": "Country",
        "default": None, "required": False, "source": "input",
        "identity_binding": None, "options": None,
        "options_source": None, "strict_options": False,
    }


async def _seed_query(report_id):
    """Seed one unparameterized query + step + visualization on the report.

    Queries/steps are produced by the AI flow in production and have no public
    CRUD API, so the graph is seeded directly (as test_run_query_params does).
    """
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()

        query = Query(
            title=f"Sales {suffix}",
            report_id=report_id,
            widget_id=widget.id,
            organization_id=report.organization_id,
            user_id=report.user_id,
            parameters=[],
        )
        db.add(query)
        await db.flush()

        step = Step(
            title=f"Sales {suffix}",
            slug=f"s-{suffix}",
            status="success",
            widget_id=widget.id,
            query_id=query.id,
            code=CODE,
            data={"rows": [{"country": "US", "sales": 100}, {"country": "DE", "sales": 40}],
                  "columns": [{"field": "country"}, {"field": "sales"}]},
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id

        # Same title as the query, as create_data produces it.
        viz = Visualization(title=f"Sales {suffix}", status="success", report_id=report_id,
                            query_id=query.id, view={"type": "table"})
        db.add(viz)
        await db.flush()
        await db.commit()
        return {"query_id": str(query.id), "viz_id": str(viz.id)}


async def _call(org_id, user_id, report_id, tool_input):
    from app.ai.tools.implementations.add_parameter import AddParameterTool

    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        report = await db.get(Report, report_id)
        events = []
        async for evt in AddParameterTool().run_stream(
            tool_input,
            {"db": db, "organization": org, "user": user, "report": report},
        ):
            events.append(evt)
        return events[-1].payload


async def _declared_names(query_id):
    async with async_session_maker() as db:
        q = await db.get(Query, query_id)
        return [p.get("name") for p in (q.parameters or [])]


def _input(target_id):
    return {"query_id": target_id, "parameter": _country_param(), "column": "country"}


@pytest.fixture
def report(create_report, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    rep = create_report(title="Sales", user_token=token, org_id=org_id)
    other = create_report(title="Other", user_token=token, org_id=org_id)
    return {"report_id": rep["id"], "other_report_id": other["id"],
            "org_id": org_id, "user_id": info["id"]}


@pytest.mark.e2e
@pytest.mark.parametrize("handle", ["query_id", "viz_id"])
def test_either_handle_parameterizes_the_query_in_place(report, handle):
    seeded = _run(_seed_query(report["report_id"]))

    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         _input(seeded[handle])))
    out = payload["output"]

    assert out["success"] is True, out.get("error")
    assert out["query_id"] == seeded["query_id"]
    assert "country" in [p["name"] for p in out["parameters"]]
    assert _run(_declared_names(seeded["query_id"])) == ["country"]


@pytest.mark.e2e
@pytest.mark.parametrize("handle", ["query_id", "viz_id"])
def test_a_handle_from_another_report_is_not_addressable(report, handle):
    elsewhere = _run(_seed_query(report["other_report_id"]))

    payload = _run(_call(report["org_id"], report["user_id"], report["report_id"],
                         _input(elsewhere[handle])))
    out = payload["output"]

    assert out["success"] is False
    assert payload["observation"]["error"]["type"] == "not_found"
    assert _run(_declared_names(elsewhere["query_id"])) == []
