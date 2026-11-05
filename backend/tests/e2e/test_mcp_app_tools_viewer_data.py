"""MCP app tools serve a reader the same rows the dashboard serves that reader.

`get_visualization` and `get_artifact_data` are the MCP tools the ui:// apps
(and, since tools/call does not enforce app-only visibility, the model) use to
read a report's charts. Every surface that serves step rows must resolve what a
given reader may see through `viewer_data_policy.resolve_step_data` (see the
module docstring there) instead of reading `Step.data` directly.

On an identity-scoped dashboard the `Step.data` snapshot is ONE identity's row
slice (the owner's). A second org member who may open the report must not be
handed it. This pins that the MCP tools agree with the dashboard's own read
path, reader by reader.
"""
import asyncio

import pytest
from sqlalchemy import select

from app.ai.tools.mcp.app_tools import GetArtifactDataMCPTool, GetVisualizationMCPTool
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.visualization import Visualization
from tests.e2e.test_artifact_chat_viewer_data import (
    IDENTITY_PARAMS,
    PLAIN_PARAMS,
    _a_member_of,
    _dashboard_rows,
    _owner_of,
)
from tests.e2e.test_html_export_offline import _seed

pytestmark = pytest.mark.e2e


def _run(coro):
    return asyncio.run(coro)


async def _org_of(db, report_id: str) -> Organization:
    report = await db.get(Report, report_id)
    return await db.get(Organization, report.organization_id)


async def _mcp_visualization_rows(report_id: str, reader) -> list[dict]:
    async with async_session_maker() as db:
        viz_id = (await db.execute(
            select(Visualization.id).where(Visualization.report_id == report_id)
        )).scalars().first()
        org = await _org_of(db, report_id)
        out = await GetVisualizationMCPTool().execute(
            {"visualization_id": str(viz_id)}, db, reader, org
        )
        assert "error" not in out, out
        return (out.get("data") or {}).get("rows", [])


async def _mcp_artifact_rows(report_id: str, artifact_id: str, reader) -> list[list[dict]]:
    async with async_session_maker() as db:
        org = await _org_of(db, report_id)
        out = await GetArtifactDataMCPTool().execute(
            {"artifact_id": artifact_id}, db, reader, org
        )
        assert "error" not in out, out
        # Collect the rows of every query the artifact data carries, whatever
        # the nesting, so the assertion does not depend on the payload shape.
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

        walk(out)
        return rows


def test_dashboard_withholds_the_creator_snapshot_from_another_viewer():
    """Baseline: the dashboard's own read path withholds the owner's slice."""
    report_id, _ = _run(_seed(parameters=IDENTITY_PARAMS, artifact_visibility="internal"))
    viewer = _run(_a_member_of(report_id))
    assert all(rows == [] for rows in _run(_dashboard_rows(report_id, viewer)))


def test_mcp_get_visualization_withholds_the_creator_snapshot_from_another_viewer():
    report_id, _ = _run(_seed(parameters=IDENTITY_PARAMS, artifact_visibility="internal"))
    owner = _run(_owner_of(report_id))
    viewer = _run(_a_member_of(report_id))

    assert _run(_mcp_visualization_rows(report_id, owner)), "the owner should see their rows"
    leaked = _run(_mcp_visualization_rows(report_id, viewer))
    assert leaked == [], f"viewer was served the owner's identity-scoped rows: {leaked}"


def test_mcp_get_artifact_data_withholds_the_creator_snapshot_from_another_viewer():
    report_id, artifact_id = _run(_seed(parameters=IDENTITY_PARAMS, artifact_visibility="internal"))
    owner = _run(_owner_of(report_id))
    viewer = _run(_a_member_of(report_id))

    assert any(_run(_mcp_artifact_rows(report_id, artifact_id, owner))), "the owner should see rows"
    leaked = [r for r in _run(_mcp_artifact_rows(report_id, artifact_id, viewer)) if r]
    assert leaked == [], f"viewer was served the owner's identity-scoped rows: {leaked}"


def test_mcp_keeps_serving_a_plain_dashboard_to_its_viewers():
    """Control: no identity in play, so sharing still shares the numbers."""
    report_id, _ = _run(_seed(parameters=PLAIN_PARAMS, artifact_visibility="internal"))
    _run(_owner_of(report_id))
    viewer = _run(_a_member_of(report_id))
    assert _run(_mcp_visualization_rows(report_id, viewer))
