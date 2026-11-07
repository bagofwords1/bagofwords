"""MCP app tools on a report whose source authenticates every user separately.

Viewer-identity mode on a user-scoped (non system_only) connection: the shared
`Step.data` snapshot is the owner's credential view, so the web app withholds
it from other readers (see test_viewer_run_shared_artifacts.py,
`test_viewer_identity_mode_withholds_creator_snapshot_on_user_scoped_sources`).
`get_visualization` and `get_artifact_data`, called through /api/mcp, must
withhold it the same way: no rows, no columns, no generated code, and an
explicit `withheld` flag. Creator mode, where the owner opts to share their
view, serves it again. The identity-parameter cases are in
tests/e2e/test_mcp_app_tools_viewer_data.py.
"""
import asyncio
import json

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.artifact import ArtifactVersion
from tests.e2e.rbac.test_viewer_run_shared_artifacts import (
    GOOD_CODE,
    _attach_source_with_connection,
    _set_artifact_visibility,
    _shared_report,
)


def _run(coro):
    return asyncio.run(coro)


def _call(test_client, api_key, name, arguments) -> dict:
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result["isError"], result
    payload = json.loads(result["content"][0]["text"])
    assert "error" not in payload, payload
    return payload


async def _artifact_version_of(report_id: str) -> str:
    async with async_session_maker() as db:
        return str((await db.execute(
            select(ArtifactVersion.id).where(ArtifactVersion.report_id == report_id)
        )).scalars().first())


@pytest.mark.e2e
def test_mcp_withholds_a_user_scoped_snapshot_in_viewer_identity_mode(
    test_client, create_report, bootstrap_admin, invite_user_to_org, enable_mcp, create_api_key,
):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org, visibility="internal",
    )
    _run(_attach_source_with_connection(report["id"], "user_required"))
    enable_mcp(user_token=admin["token"], org_id=admin["org_id"])
    keys = {who["token"]: create_api_key(user_token=who["token"], org_id=admin["org_id"])["key"]
            for who in (owner, viewer, admin)}
    viz_id = seeded["viz_ids"][0]
    artifact_id = _run(_artifact_version_of(report["id"]))

    def fetch(who):
        key = keys[who["token"]]
        viz = _call(test_client, key, "get_visualization", {"visualization_id": viz_id})
        art = _call(test_client, key, "get_artifact_data", {"artifact_id": artifact_id})
        return viz, art

    viz, art = fetch(owner)
    assert {r["month"] for r in viz["data"]["rows"]} == {"stale"}
    assert viz["code"] == GOOD_CODE
    assert not viz.get("withheld")
    assert [{r["month"] for r in v["rows"]} for v in art["visualizations"]] == [{"stale"}]

    # A plain member, and an org admin: running the org does not make the
    # owner's credential view theirs.
    for reader in (viewer, admin):
        viz, art = fetch(reader)
        assert viz["data"] == {"rows": [], "columns": []}, "a reader got the owner's rows"
        assert art["visualizations"]
        assert all(v["rows"] == [] and v["columns"] == [] for v in art["visualizations"])
        # The step has code (GOOD_CODE); the SQL behind a withheld chart must
        # not reach the reader, as on /r and in read_query.
        assert GOOD_CODE and not viz["code"]
        assert viz.get("withheld") is True
        assert all(v.get("withheld") is True for v in art["visualizations"])

    # Creator mode: the same readers, with no run of their own, are served
    # the snapshot again.
    _set_artifact_visibility(test_client, report["id"], owner, "internal", run_identity="creator")
    for reader in (viewer, admin):
        viz, art = fetch(reader)
        assert {r["month"] for r in viz["data"]["rows"]} == {"stale"}
        assert viz["code"] == GOOD_CODE
        assert not viz.get("withheld")
        assert not any(v.get("withheld") for v in art["visualizations"])
