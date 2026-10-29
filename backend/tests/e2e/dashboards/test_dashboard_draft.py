"""The draft of one dashboard, for its editors (phase 4, part B).

`/d/{id}/draft/...` serves the newest finished version of this dashboard and
the queries it needs (shown, filter options, loaded steps) -- not every query
of its conversation. Data goes through today's policy for the conversation,
so a project member who is not the owner gets what /r gives them.
"""
import uuid

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own


def _get(test_client, corpus, path, reader="owner"):
    return test_client.get(path, headers=as_(corpus, reader))


@pytest.mark.e2e
@pytest.mark.parametrize("key", ["dash_newest_failed", "dash_newest_pending", "dash_newest_stopped",
                                 "dash_deleted_version"])
def test_the_draft_is_this_dashboards_newest_finished_version(corpus, corpus_rows, test_client, key):
    """v2 failed, is still running, stopped, or was deleted: the draft is v1."""
    aid = own(corpus_rows, corpus, f"{key}/dash")
    body = _get(test_client, corpus, f"/api/d/{aid}/draft/content").json()
    meta = _get(test_client, corpus, f"/api/d/{aid}/draft").json()
    assert meta["version_id"] == corpus.ids[f"{key}/dash/v1"]
    assert body["version_id"] == meta["version_id"] and body["mode"] == "page" and body["content"]["code"]


@pytest.mark.e2e
def test_the_draft_serves_only_the_queries_this_dashboard_needs(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    listed = {q["id"] for q in _get(test_client, corpus, f"/api/d/{aid}/draft/queries").json()}
    assert listed == {corpus.ids["conv_three_dash/q_sales"]}
    stock = corpus.ids["conv_three_dash/q_stock"]
    assert _get(test_client, corpus, f"/api/d/{aid}/draft/queries/{stock}/step").status_code == 404
    sales = corpus.ids["conv_three_dash/q_sales"]
    step = _get(test_client, corpus, f"/api/d/{aid}/draft/queries/{sales}/step")
    assert step.status_code == 200 and step.json()["data"]["rows"]

    hidden = own(corpus_rows, corpus, "refs_in_code/dash")
    titles = {q["title"] for q in _get(test_client, corpus, f"/api/d/{hidden}/draft/queries").json()}
    assert {"רשימת אזורים", "נסתרת", "בסיס"} <= titles


@pytest.mark.e2e
def test_a_project_member_gets_the_conversation_policy_not_the_owners_view(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "user_required/dash", project="private")
    query = corpus.ids["user_required/q_sales"]
    path = f"/api/d/{aid}/draft/queries/{query}/step"
    assert _get(test_client, corpus, path, "owner").json()["data"]["rows"]
    member = _get(test_client, corpus, path, "proj_viewer").json()
    assert member["snapshot_withheld"] is True and member["data"] == {} and member["code"] == ""


@pytest.mark.e2e
def test_draft_routes_are_for_editors_only(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "embedded_files/dash")
    corpus_rows.update("artifacts", aid, visibility="public")
    query = corpus.ids["embedded_files/q_sales"]
    paths = [f"/api/d/{aid}/draft/content", f"/api/d/{aid}/draft/queries",
             f"/api/d/{aid}/draft/queries/{query}/step",
             f"/api/d/{aid}/draft/files/{corpus.ids['embedded_files/file_new']}/embed_token"]
    for path in paths:
        assert _get(test_client, corpus, path).status_code == 200, path
        assert _get(test_client, corpus, path, "member").status_code == 404, path
        assert _get(test_client, corpus, path, "anonymous").status_code == 401, path


@pytest.mark.e2e
def test_a_draft_file_opens_only_if_the_draft_holds_it(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "embedded_files/dash")
    new, old = corpus.ids["embedded_files/file_new"], corpus.ids["embedded_files/file_old"]
    assert _get(test_client, corpus, f"/api/d/{aid}/draft/files/{new}/embed_token").status_code == 200
    for fid in (old, str(uuid.uuid4()), "not-an-id"):  # old: only a deleted version holds it
        assert _get(test_client, corpus, f"/api/d/{aid}/draft/files/{fid}/embed_token").status_code == 404, fid
