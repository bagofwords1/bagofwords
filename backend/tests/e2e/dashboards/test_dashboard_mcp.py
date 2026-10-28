"""The MCP app tools read a published dashboard like /d does (phase 4, part C).

`get_artifact_data` takes a `dashboard_id` and returns the live release's
code and charts; `get_visualization` serves a chart of a live release. Both
open only for who may open the dashboard, and give each reader the rows the
step route gives them. The conversation-keyed calls behave as before.
"""
import json

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share
from tests.e2e.rbac.conftest import assign_role, create_role  # noqa: F401  (pytest fixtures)


@pytest.fixture
def mcp_key(test_client, corpus, create_api_key, enable_mcp):
    admin = corpus.cast.people["admin"]
    enable_mcp(user_token=admin.token, org_id=corpus.cast.org_id)
    keys = {}

    def key(person):
        if person not in keys:
            keys[person] = create_api_key(user_token=corpus.cast.people[person].token,
                                          org_id=corpus.cast.org_id)["key"]
        return keys[person]
    return key


def _call(test_client, api_key, name, arguments):
    resp = test_client.post("/api/mcp", headers={"X-API-Key": api_key}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    try:
        payload = json.loads(result["content"][0]["text"])
    except (ValueError, KeyError, IndexError):
        payload = {}
    return result.get("isError"), payload


def _release_viz(corpus_rows, release):
    with corpus_rows.engine.connect() as conn:
        return conn.execute(text("SELECT id FROM visualizations WHERE artifact_release_id = :r"),
                            {"r": release}).scalars().all()


@pytest.mark.e2e
def test_get_artifact_data_reads_the_live_release_for_its_readers(corpus, corpus_rows, test_client, mcp_key):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    err, data = _call(test_client, mcp_key("member"), "get_artifact_data", {"dashboard_id": aid})
    assert not err and "error" not in data, data
    assert {v["id"] for v in data["visualizations"]} == set(_release_viz(corpus_rows, release))
    assert all(v["rows"] for v in data["visualizations"])
    assert data["mode"] == "page" and data["code"]

    assert share(test_client, corpus, aid, "none").status_code == 200
    err, data = _call(test_client, mcp_key("member"), "get_artifact_data", {"dashboard_id": aid})
    assert err or "error" in data


@pytest.mark.e2e
def test_mcp_gives_each_reader_the_rows_the_step_route_gives(corpus, corpus_rows, test_client, mcp_key):
    aid = own(corpus_rows, corpus, "user_required/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (viz,) = _release_viz(corpus_rows, release)
    _, member = _call(test_client, mcp_key("member"), "get_artifact_data", {"dashboard_id": aid})
    _, owner = _call(test_client, mcp_key("owner"), "get_artifact_data", {"dashboard_id": aid})
    assert [v["rows"] for v in member["visualizations"]] == [[]]
    assert owner["visualizations"][0]["rows"]

    _, chart = _call(test_client, mcp_key("member"), "get_visualization", {"visualization_id": viz})
    assert chart["data"]["rows"] == [] and chart["code"] == ""
    _, chart = _call(test_client, mcp_key("owner"), "get_visualization", {"visualization_id": viz})
    assert chart["data"]["rows"] and chart["code"]


@pytest.mark.e2e
def test_a_chart_of_an_old_release_or_a_closed_dashboard_is_not_found(corpus, corpus_rows, test_client, mcp_key):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    first = publish(aid)
    live = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (live_viz,) = _release_viz(corpus_rows, live)
    err, data = _call(test_client, mcp_key("member"), "get_visualization", {"visualization_id": live_viz})
    assert not err and data.get("id") == live_viz and data["data"]["rows"], data
    (old_viz,) = _release_viz(corpus_rows, first)
    err, data = _call(test_client, mcp_key("member"), "get_visualization", {"visualization_id": old_viz})
    assert err or "error" in data
    other = own(corpus_rows, corpus, "conv_three_dash/dash_stock")
    (closed_viz,) = _release_viz(corpus_rows, publish(other))  # published, but private
    err, data = _call(test_client, mcp_key("member"), "get_visualization", {"visualization_id": closed_viz})
    assert err or "error" in data


@pytest.mark.e2e
def test_mcp_shows_a_charts_code_only_to_readers_whose_role_allows_it(
        corpus, corpus_rows, test_client, mcp_key, create_role, assign_role):
    from tests.e2e.rbac.test_code_visibility import _strip_code_role

    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    (viz,) = _release_viz(corpus_rows, publish(aid))
    assert share(test_client, corpus, aid, "internal").status_code == 200
    _, chart = _call(test_client, mcp_key("member"), "get_visualization", {"visualization_id": viz})
    assert chart["code"] and chart["data"]["rows"]
    admin = {"token": corpus.cast.people["admin"].token, "org_id": corpus.cast.org_id}
    _strip_code_role(test_client, admin, {"user_id": corpus.cast.id("member")}, create_role, assign_role)
    _, chart = _call(test_client, mcp_key("member"), "get_visualization", {"visualization_id": viz})
    assert chart["code"] == "" and chart["data"]["rows"]
