"""GET /d/{ref}/queries_with_steps and /d/{ref}/draft/queries_with_steps.

The shared page reads every query with its step in one request instead of a
list + one step per query. Each bundled `default_step` must be exactly what
the per-query step route returns for the same reader (null where that route
404s), so a reader never gets rows through the bundle the step route withholds;
and the bundle is closed to whoever the list route is closed to.
"""
import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share


def _get(test_client, corpus, path, reader):
    return test_client.get(path, headers=as_(corpus, reader))


def _assert_matches_per_query(test_client, corpus, base, reader, suffix=""):
    """The bundle == list route + per-query step route, for this reader."""
    bundle = _get(test_client, corpus, f"{base}/queries_with_steps{suffix}", reader)
    assert bundle.status_code == 200, bundle.text
    listed = _get(test_client, corpus, f"{base}/queries{suffix}", reader).json()
    body = bundle.json()
    assert [{k: v for k, v in q.items() if k != "default_step"} for q in body] == listed
    for q in body:
        step = _get(test_client, corpus, f"{base}/queries/{q['id']}/step{suffix}", reader)
        if step.status_code == 404:
            assert q["default_step"] is None
        else:
            assert step.status_code == 200
            assert q["default_step"] == step.json(), (reader, q["id"])
    return body


def _published(corpus, corpus_rows, test_client, key, visibility="internal"):
    aid = own(corpus_rows, corpus, key)
    publish(aid)
    assert share(test_client, corpus, aid, visibility).status_code == 200
    return aid


@pytest.mark.e2e
@pytest.mark.parametrize("key", ["conv_three_dash/dash_sales", "refs_in_code/dash"])
def test_published_bundle_is_the_list_plus_each_step(corpus, corpus_rows, test_client, key):
    aid = _published(corpus, corpus_rows, test_client, key)
    for reader in ("owner", "member"):
        body = _assert_matches_per_query(test_client, corpus, f"/api/d/{aid}", reader)
        assert body and any(q["default_step"] and q["default_step"]["data"].get("rows") for q in body)


@pytest.mark.e2e
@pytest.mark.parametrize("key", ["user_required/dash", "rls/dash", "identity_param/dash"])
def test_published_bundle_withholds_what_the_step_route_withholds(corpus, corpus_rows, test_client, key):
    aid = _published(corpus, corpus_rows, test_client, key)
    if key == "identity_param/dash":
        rel = test_client.get(f"/api/d/{aid}/queries", headers=as_(corpus, "owner")).json()
        for q in rel:
            step_id = _get(test_client, corpus, f"/api/d/{aid}/queries/{q['id']}/step", "owner").json()["id"]
            corpus_rows.update("steps", step_id, applied_params={"UserEmail": "owner@corpus.com"})
    member = _assert_matches_per_query(test_client, corpus, f"/api/d/{aid}", "member")
    assert member and all(q["default_step"]["snapshot_withheld"] is True for q in member)
    assert all(q["default_step"]["data"] == {} and q["default_step"]["code"] == "" for q in member)
    assert "owner@corpus.com" not in str(member)
    owner = _assert_matches_per_query(test_client, corpus, f"/api/d/{aid}", "owner")
    assert all(q["default_step"]["data"]["rows"] for q in owner)


@pytest.mark.e2e
def test_published_bundle_is_closed_to_who_cannot_open_the_dashboard(corpus, corpus_rows, test_client):
    aid = _published(corpus, corpus_rows, test_client, "embedded_files/dash", visibility="shared")
    for reader, code in (("member", 403), ("anonymous", 401), ("other_org", 403)):
        resp = _get(test_client, corpus, f"/api/d/{aid}/queries_with_steps", reader)
        assert resp.status_code == code, (reader, resp.status_code)
        assert "הכנסות" not in resp.text


@pytest.mark.e2e
def test_draft_bundle_is_the_draft_list_plus_each_step(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    body = _assert_matches_per_query(test_client, corpus, f"/api/d/{aid}/draft", "owner")
    assert [q["id"] for q in body] == [corpus.ids["conv_three_dash/q_sales"]]
    assert body[0]["default_step"]["data"]["rows"]
    _assert_matches_per_query(test_client, corpus, f"/api/d/{aid}/draft", "owner", suffix="?version=1")


@pytest.mark.e2e
def test_draft_bundle_gives_a_project_member_the_conversation_policy(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "user_required/dash", project="private")
    member = _assert_matches_per_query(test_client, corpus, f"/api/d/{aid}/draft", "proj_viewer")
    assert member and all(q["default_step"]["snapshot_withheld"] is True and q["default_step"]["data"] == {}
                          and q["default_step"]["code"] == "" for q in member)
    owner = _assert_matches_per_query(test_client, corpus, f"/api/d/{aid}/draft", "owner")
    assert all(q["default_step"]["data"]["rows"] for q in owner)


@pytest.mark.e2e
def test_draft_bundle_is_for_editors_only(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "embedded_files/dash")
    corpus_rows.update("artifacts", aid, visibility="public")
    path = f"/api/d/{aid}/draft/queries_with_steps"
    assert _get(test_client, corpus, path, "owner").status_code == 200
    assert _get(test_client, corpus, path, "member").status_code == 404
    assert _get(test_client, corpus, path, "anonymous").status_code == 401


@pytest.mark.e2e
def test_content_carries_the_precompiled_page_code_when_cached(corpus, corpus_rows, test_client, monkeypatch):
    """/d content (published and draft) carries compiled_js/compiled_js_key
    as the artifact read routes do, keyed on the code it serves."""
    from app.services import artifact_precompile

    monkeypatch.setattr(artifact_precompile, "get_cached", lambda code: "/*compiled*/" if code else None)
    aid = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    for path in (f"/api/d/{aid}/content", f"/api/d/{aid}/draft/content"):
        body = _get(test_client, corpus, path, "owner").json()
        assert body["compiled_js"] == "/*compiled*/", path
        assert body["compiled_js_key"] == artifact_precompile.source_key(body["content"]["code"])
    monkeypatch.setattr(artifact_precompile, "get_cached", lambda code: None)
    assert "compiled_js" not in _get(test_client, corpus, f"/api/d/{aid}/content", "owner").json()
