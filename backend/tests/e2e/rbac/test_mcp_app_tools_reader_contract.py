"""MCP app tools follow the dashboard's reader contract beyond the rows.

`get_visualization` and `get_artifact_data` (called through /api/mcp) must
serve a reader what the dashboard serves that reader:

- generated code only to a role that holds `view_code` (as /r does through
  set_code_visibility), even when the rows themselves are shared;
- nothing for a chart whose query was deleted;
- the dashboard's charts in the artifact's own `visualization_ids` order,
  which its code indexes them by.

The row-withholding cases are in test_mcp_app_tools_user_scoped.py and
tests/e2e/test_mcp_app_tools_viewer_data.py.
"""
import asyncio
import json

import pytest

from tests.e2e.rbac.test_code_visibility import _strip_code_role
from tests.e2e.rbac.test_mcp_app_tools_user_scoped import _artifact_version_of, _call
from tests.e2e.rbac.test_viewer_run_shared_artifacts import (
    GOOD_CODE,
    _headers,
    _shared_report,
)

pytestmark = pytest.mark.e2e


def _run(coro):
    return asyncio.run(coro)


def _call_payload(test_client, api_key, name, arguments) -> dict:
    """The tool's payload, error or not."""
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def test_mcp_withholds_code_from_a_reader_whose_role_lacks_view_code(
    test_client, create_report, bootstrap_admin, invite_user_to_org, enable_mcp, create_api_key,
    create_role, assign_role,
):
    # A plain dashboard (no per-user source, no identity params): its rows are
    # shared with every reader, so only the role decides the code.
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org, visibility="internal",
    )
    enable_mcp(user_token=admin["token"], org_id=admin["org_id"])
    keys = {who["token"]: create_api_key(user_token=who["token"], org_id=admin["org_id"])["key"]
            for who in (viewer, admin)}
    _strip_code_role(test_client, admin, viewer, create_role, assign_role)
    viz_id = seeded["viz_ids"][0]

    viz = _call(test_client, keys[viewer["token"]], "get_visualization", {"visualization_id": viz_id})
    assert {r["month"] for r in viz["data"]["rows"]} == {"stale"}, "the rows are shared"
    assert not viz.get("withheld")
    assert not viz["code"], "a role without view_code was served the step's code"

    # An org admin holds view_code (full_admin_access): rows and code.
    viz = _call(test_client, keys[admin["token"]], "get_visualization", {"visualization_id": viz_id})
    assert {r["month"] for r in viz["data"]["rows"]} == {"stale"}
    assert viz["code"] == GOOD_CODE


def test_mcp_stops_serving_a_chart_whose_query_was_deleted(
    test_client, create_report, bootstrap_admin, invite_user_to_org, enable_mcp, create_api_key,
):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )
    enable_mcp(user_token=admin["token"], org_id=admin["org_id"])
    artifact_id = _run(_artifact_version_of(report["id"]))
    gone, kept = seeded["viz_ids"]

    resp = test_client.delete(f"/api/queries/{seeded['query_ids'][0]}",
                              headers=_headers(owner["token"], admin["org_id"]))
    assert resp.status_code == 200, resp.text

    for who in (owner, viewer):
        key = create_api_key(user_token=who["token"], org_id=admin["org_id"])["key"]
        out = _call_payload(test_client, key, "get_visualization", {"visualization_id": gone})
        assert "error" in out and "data" not in out, f"a deleted chart was served: {out}"
        assert "error" not in _call_payload(test_client, key, "get_visualization",
                                            {"visualization_id": kept})

        # The artifact still lists the deleted chart's id; its data must not
        # come back with it.
        art = _call(test_client, key, "get_artifact_data", {"artifact_id": artifact_id})
        assert [v["id"] for v in art["visualizations"]] == [kept]


def test_mcp_get_artifact_data_serves_charts_in_the_dashboards_order(
    test_client, create_report, bootstrap_admin, invite_user_to_org, enable_mcp, create_api_key,
):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=3,
    )
    enable_mcp(user_token=admin["token"], org_id=admin["org_id"])
    artifact_id = _run(_artifact_version_of(report["id"]))
    # Not the order the queries were created in: the code's viz[0] is the last.
    order = [seeded["viz_ids"][i] for i in (2, 0, 1)]
    owner_headers = _headers(owner["token"], admin["org_id"])
    current = test_client.get(f"/api/artifacts/{artifact_id}", headers=owner_headers)
    assert current.status_code == 200, current.text
    reordered = test_client.patch(
        f"/api/artifacts/{artifact_id}",
        json={"content": {**current.json()["content"], "visualization_ids": order}},
        headers=owner_headers,
    )
    assert reordered.status_code == 200, reordered.text

    for who in (owner, viewer):
        key = create_api_key(user_token=who["token"], org_id=admin["org_id"])["key"]
        art = _call(test_client, key, "get_artifact_data", {"artifact_id": artifact_id})
        assert [v["id"] for v in art["visualizations"]] == order
