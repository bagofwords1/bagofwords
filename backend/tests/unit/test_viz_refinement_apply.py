"""Writing a background visualization refinement (AgentV2._apply_viz_refinement).

Against real rows on a real session:

* a refinement lands on the step, the visualization and both the tool result
  and the projection report loads serve, with the pending flag cleared;
* a chart the user changed after the tool ended is never overwritten, and the
  projection keeps the user's chart;
* a tool row that isn't there yet (PostgreSQL's background INSERT) makes the
  write fail before anything changes, never a refined step next to a
  provisional tool result;
* the step query reads chart metadata only, not the stored result rows.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.ai.agent_v2 import AgentV2
from app.ai.viz_refinement import VizRefinement
from app.dependencies import async_session_maker
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.tool_execution import ToolExecution
from app.models.user import User
from app.models.visualization import Visualization
from app.schemas.view_schema import ViewSchema

pytestmark = pytest.mark.db

ROWS = [{"country": c, "revenue": r, "invoices": n} for c, r, n in [("USA", 523.1, 91), ("Canada", 303.9, 56)]]
GRID = {"rows": ROWS, "columns": [{"field": k, "headerName": k} for k in ("country", "revenue", "invoices")],
        "info": {"total_rows": 2, "total_columns": 3}}
PROV_DM = {"type": "bar_chart", "series": [{"name": "invoices", "key": "country", "value": "invoices"}]}
PROV_VIEW = {"version": "v2", "view": {"type": "bar_chart", "x": "country", "y": ["invoices"]}}
REF_DM = {"type": "bar_chart", "series": [{"name": "Revenue", "key": "country", "value": "revenue"}],
          "display": {"format": "currency", "currency": "USD"}}
REF_VIEW = {"version": "v2", "view": {"type": "bar_chart", "x": "country", "y": ["revenue"]}}


def _stored(view):
    return ViewSchema(**view).model_dump(exclude_none=True)


async def _seed():
    async with async_session_maker() as db:
        user = User(name="U", email=f"u-{uuid.uuid4().hex}@example.test", hashed_password="x")
        org = Organization(name=f"org {uuid.uuid4().hex}")
        db.add_all([user, org])
        await db.flush()
        report = Report(title="R", slug=f"r-{uuid.uuid4().hex[:12]}", user_id=str(user.id), organization_id=str(org.id))
        db.add(report)
        await db.flush()
        comp = Completion(report_id=str(report.id), user_id=str(user.id), role="system", status="in_progress",
                          prompt={}, completion={})
        db.add(comp)
        await db.flush()
        execution = AgentExecution(completion_id=str(comp.id), organization_id=str(org.id), user_id=str(user.id),
                                   report_id=str(report.id), status="in_progress")
        query = Query(title="Revenue", report_id=str(report.id), organization_id=str(org.id), user_id=str(user.id))
        db.add_all([execution, query])
        await db.flush()
        step = Step(title="Revenue", slug=f"s-{uuid.uuid4().hex}", status="success", data=GRID,
                    data_model={**PROV_DM, "columns": GRID["columns"]}, query_id=str(query.id))
        viz = Visualization(title="Revenue", status="success", report_id=str(report.id), query_id=str(query.id),
                            view=_stored(PROV_VIEW))
        db.add_all([step, viz])
        await db.flush()
        result = {"success": True, "data_model": PROV_DM, "view": PROV_VIEW, "viz_pending": True}
        te = ToolExecution(agent_execution_id=str(execution.id), tool_name="create_data", arguments_json={},
                           status="success", success=True, created_step_id=str(step.id),
                           result_json=result, context_summary_json={"data_model": PROV_DM, "view": PROV_VIEW})
        db.add(te)
        await db.commit()
        return SimpleNamespace(step=str(step.id), viz=str(viz.id), te=str(te.id))


def _agent() -> AgentV2:
    agent = object.__new__(AgentV2)
    agent._session_maker = async_session_maker
    agent._writes = None
    agent._use_single_write_session = lambda: False   # fresh session per write (PostgreSQL mode)
    agent.system_completion = None                    # no SSE in these tests
    agent.current_execution = None
    return agent


def _record(ids, *, result=None, te_id=None):
    async def _noop():
        return None
    return VizRefinement(
        task=None, tool_execution_id=te_id or ids.te, step_id=ids.step, visualization_id=ids.viz,
        provisional_view=PROV_VIEW, provisional_data_model=PROV_DM,
        result=result if result is not None else {"data_model": REF_DM, "view": REF_VIEW},
    )


async def _load(ids):
    async with async_session_maker() as db:
        step = await db.get(Step, ids.step)
        viz = await db.get(Visualization, ids.viz)
        te = await db.get(ToolExecution, ids.te)
        return step.data_model, viz.view, te.result_json, te.context_summary_json


@pytest.mark.asyncio
async def test_refinement_lands_everywhere_a_reader_looks():
    ids = await _seed()
    await _agent()._apply_viz_refinement(_record(ids))
    step_dm, view, rj, summary = await _load(ids)
    assert step_dm["series"] == REF_DM["series"] and step_dm["display"] == REF_DM["display"]
    assert step_dm["columns"] == GRID["columns"]          # what the run streamed in is kept
    assert view == _stored(REF_VIEW)
    assert "viz_pending" not in rj
    assert rj["data_model"] == REF_DM and rj["view"] == REF_VIEW
    assert summary["data_model"] == REF_DM and summary["view"] == REF_VIEW   # what reloads serve


@pytest.mark.asyncio
async def test_a_chart_the_user_changed_while_pending_is_kept():
    ids = await _seed()
    user_view = _stored({"version": "v2", "view": {"type": "line_chart", "x": "country", "y": ["invoices"]}})
    async with async_session_maker() as db:
        viz = await db.get(Visualization, ids.viz)
        viz.view = user_view
        await db.commit()
    await _agent()._apply_viz_refinement(_record(ids))
    step_dm, view, rj, summary = await _load(ids)
    assert view == user_view                               # not overwritten
    assert step_dm["series"] == PROV_DM["series"]          # refinement dropped for this chart
    assert "viz_pending" not in rj                         # but no longer pending
    assert rj["view"] == user_view and summary["view"] == user_view   # reloads show the user's chart


@pytest.mark.asyncio
async def test_a_missing_tool_row_changes_nothing():
    ids = await _seed()
    with pytest.raises(RuntimeError):
        await _agent()._apply_viz_refinement(_record(ids, te_id=str(uuid.uuid4())))
    step_dm, view, rj, summary = await _load(ids)
    assert step_dm["series"] == PROV_DM["series"]
    assert view == _stored(PROV_VIEW)


@pytest.mark.asyncio
async def test_no_refinement_only_clears_the_pending_flag():
    ids = await _seed()
    rec = _record(ids)
    rec.result = None
    await _agent()._apply_viz_refinement(rec)
    step_dm, view, rj, summary = await _load(ids)
    assert "viz_pending" not in rj and rj["data_model"] == PROV_DM
    assert view == _stored(PROV_VIEW) and step_dm["series"] == PROV_DM["series"]


@pytest.mark.asyncio
async def test_the_step_query_does_not_read_the_result_rows():
    ids = await _seed()
    statements: list[str] = []
    engine = async_session_maker.kw["bind"].sync_engine

    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        await _agent()._apply_viz_refinement(_record(ids))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    step_selects = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "FROM steps" in s]
    assert step_selects, statements
    for s in step_selects:
        assert "steps.data_model" in s
        assert "steps.data," not in s and "steps.data " not in s and not s.rstrip().endswith("steps.data")
