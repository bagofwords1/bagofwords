"""MCP app tools serve a reader the same rows the dashboard serves that reader.

`get_visualization` and `get_artifact_data` are the MCP tools the ui:// apps
(and, since tools/call does not enforce app-only visibility, the model) use to
read a report's charts. Every surface that serves step rows must resolve what a
given reader may see through `viewer_data_policy.resolve_step_data` (see the
module docstring there) instead of reading `Step.data` directly.

On an identity-scoped dashboard the `Step.data` snapshot is ONE identity's row
slice (the owner's). A second org member who may open the report must not be
handed it. This pins that the MCP tools agree with the dashboard's own read
path, reader by reader: the owner gets the snapshot, a viewer gets their own
run when they have one, and a withheld viewer gets no rows, no columns, no
generated code and an explicit `withheld` flag, like the web app's
`snapshot_withheld`.

The user-scoped-connection case (no identity params; the source itself
authenticates per user) is in tests/e2e/rbac/test_mcp_app_tools_user_scoped.py,
next to the fixtures it needs; role-gated code, deleted charts and chart order
are in tests/e2e/rbac/test_mcp_app_tools_reader_contract.py. Both go through
/api/mcp.

The tools are called in-process here, not through /api/mcp: the report, its
owner and its viewer are DB-seeded (no endpoint creates a saved query and
artifact without an LLM run), so the readers hold no API key.
"""
import asyncio

import pytest
from sqlalchemy import select

from app.ai.tools.mcp.app_tools import GetArtifactDataMCPTool, GetVisualizationMCPTool
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.visualization import Visualization
from tests.e2e.test_artifact_chat_viewer_data import (
    IDENTITY_PARAMS,
    INPUT_DEFAULT_PARAMS,
    PLAIN_PARAMS,
    _a_member_of,
    _dashboard_rows,
    _owner_of,
    _record_viewer_run,
)
from tests.e2e.test_html_export_offline import _seed

pytestmark = pytest.mark.e2e

# Both ways a query's rows become one identity's slice.
IDENTITY_SCOPED = [IDENTITY_PARAMS, INPUT_DEFAULT_PARAMS]

SEEDED_SQL = "SELECT region, SUM(amount) AS amount FROM sales WHERE owner = :email GROUP BY region"


def _run(coro):
    return asyncio.run(coro)


async def _give_step_code(report_id: str, code: str = SEEDED_SQL) -> None:
    """Put generated code on the dashboard's step.

    Direct write: a step's code is produced by the agent's LLM run, which an
    e2e test cannot do. Without it a "no code for this reader" assertion
    passes on an empty field and proves nothing.
    """
    async with async_session_maker() as db:
        step = (await db.execute(
            select(Step).join(Query, Step.query_id == Query.id).where(Query.report_id == report_id)
        )).scalars().first()
        step.code = code
        await db.commit()


async def _org_of(db, report_id: str) -> Organization:
    report = await db.get(Report, report_id)
    return await db.get(Organization, report.organization_id)


async def _mcp_visualization(report_id: str, reader) -> dict:
    async with async_session_maker() as db:
        viz_id = (await db.execute(
            select(Visualization.id).where(Visualization.report_id == report_id)
        )).scalars().first()
        org = await _org_of(db, report_id)
        out = await GetVisualizationMCPTool().execute(
            {"visualization_id": str(viz_id)}, db, reader, org
        )
        assert "error" not in out, out
        return out


async def _mcp_visualization_rows(report_id: str, reader) -> list[dict]:
    return ((await _mcp_visualization(report_id, reader)).get("data") or {}).get("rows", [])


async def _mcp_artifact(report_id: str, artifact_id: str, reader) -> dict:
    async with async_session_maker() as db:
        org = await _org_of(db, report_id)
        out = await GetArtifactDataMCPTool().execute(
            {"artifact_id": artifact_id}, db, reader, org
        )
        assert "error" not in out, out
        return out


def _all_rows(payload) -> list[list[dict]]:
    """The rows of every query a payload carries, whatever the nesting, so a
    leak assertion does not depend on the payload shape."""
    rows: list[list[dict]] = []

    def walk(node):
        if isinstance(node, dict):
            if "rows" in node and isinstance(node["rows"], list):
                rows.append(node["rows"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return rows


async def _mcp_artifact_rows(report_id: str, artifact_id: str, reader) -> list[list[dict]]:
    return _all_rows(await _mcp_artifact(report_id, artifact_id, reader))


def test_dashboard_withholds_the_creator_snapshot_from_another_viewer():
    """Baseline: the dashboard's own read path withholds the owner's slice."""
    report_id, _ = _run(_seed(parameters=IDENTITY_PARAMS, artifact_visibility="internal"))
    viewer = _run(_a_member_of(report_id))
    assert all(rows == [] for rows in _run(_dashboard_rows(report_id, viewer)))


@pytest.mark.parametrize("params", IDENTITY_SCOPED)
def test_mcp_get_visualization_withholds_the_creator_snapshot_from_another_viewer(params):
    report_id, _ = _run(_seed(parameters=params, artifact_visibility="internal"))
    _run(_give_step_code(report_id))
    owner = _run(_owner_of(report_id))
    viewer = _run(_a_member_of(report_id))

    mine = _run(_mcp_visualization(report_id, owner))
    assert mine["data"]["rows"], "the owner should see their rows"
    assert mine["code"] == SEEDED_SQL, "the owner should see their step's code"
    assert not mine.get("withheld")

    out = _run(_mcp_visualization(report_id, viewer))
    assert out["data"]["rows"] == [], f"viewer was served the owner's identity-scoped rows: {out}"
    assert out["data"]["columns"] == []
    # The SQL names the tables and the identity filter behind the slice.
    assert not out["code"], f"viewer was served the code behind withheld rows: {out['code']!r}"
    assert out.get("withheld") is True


@pytest.mark.parametrize("params", IDENTITY_SCOPED)
def test_mcp_get_artifact_data_withholds_the_creator_snapshot_from_another_viewer(params):
    report_id, artifact_id = _run(_seed(parameters=params, artifact_visibility="internal"))
    owner = _run(_owner_of(report_id))
    viewer = _run(_a_member_of(report_id))

    mine = _run(_mcp_artifact(report_id, artifact_id, owner))
    assert any(_all_rows(mine)), "the owner should see rows"
    assert not any(v.get("withheld") for v in mine["visualizations"])

    out = _run(_mcp_artifact(report_id, artifact_id, viewer))
    leaked = [r for r in _all_rows(out) if r]
    assert leaked == [], f"viewer was served the owner's identity-scoped rows: {leaked}"
    assert out["visualizations"], "the dashboard's charts should still be described"
    assert all(v["columns"] == [] for v in out["visualizations"])
    assert all(v.get("withheld") is True for v in out["visualizations"])
    # The dashboard itself still loads: it is the step rows that are withheld.
    assert out["code"]


@pytest.mark.parametrize("params", IDENTITY_SCOPED + [PLAIN_PARAMS])
def test_mcp_rows_are_exactly_what_the_dashboard_serves_the_same_reader(params):
    """The invariant, for every reader and every dashboard shape."""
    report_id, artifact_id = _run(_seed(parameters=params, artifact_visibility="internal"))
    owner = _run(_owner_of(report_id))
    viewer = _run(_a_member_of(report_id))

    for reader in (owner, viewer):
        dashboard = _run(_dashboard_rows(report_id, reader))
        assert [_run(_mcp_visualization_rows(report_id, reader))] == dashboard, \
            f"get_visualization drifted for {reader.name}"
        assert _run(_mcp_artifact_rows(report_id, artifact_id, reader)) == dashboard, \
            f"get_artifact_data drifted for {reader.name}"


def test_mcp_serves_a_viewer_their_own_run_not_the_creators():
    """Once the viewer has run it themselves, MCP answers from THEIR slice."""
    report_id, artifact_id = _run(_seed(parameters=IDENTITY_PARAMS, artifact_visibility="internal"))
    viewer = _run(_a_member_of(report_id))
    theirs = [{"region": "NL", "amount": 7}, {"region": "BE", "amount": 3}]
    _run(_record_viewer_run(report_id, viewer, theirs))

    viz = _run(_mcp_visualization(report_id, viewer))
    assert viz["data"]["rows"] == theirs
    assert viz["data"]["columns"]
    assert not viz.get("withheld")

    art = _run(_mcp_artifact(report_id, artifact_id, viewer))
    assert [v["rows"] for v in art["visualizations"]] == [theirs]
    assert not any(v.get("withheld") for v in art["visualizations"])


def test_mcp_keeps_serving_a_plain_dashboard_to_its_viewers():
    """Control: no identity in play, so sharing still shares the numbers."""
    report_id, artifact_id = _run(_seed(parameters=PLAIN_PARAMS, artifact_visibility="internal"))
    _run(_owner_of(report_id))
    viewer = _run(_a_member_of(report_id))
    _run(_give_step_code(report_id))
    viz = _run(_mcp_visualization(report_id, viewer))
    assert viz["data"]["rows"]
    # A member's default role holds view_code: nothing is withheld here.
    assert viz["code"] == SEEDED_SQL
    assert not viz.get("withheld")
    assert any(_run(_mcp_artifact_rows(report_id, artifact_id, viewer)))
