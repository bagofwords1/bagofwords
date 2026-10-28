"""A reader runs a published dashboard, or moves its filters (phase 4, part C).

`POST /api/d/{id}/run` re-executes the release's charts for the reader and
`POST /api/d/{id}/queries/{qid}/run` one copy with filter values. Both are
the conversation's viewer runs asked of the dashboard: whose credentials run
it comes from the dashboard's "run on my behalf" (never the owner's over RLS
or monitoring sources, and only for members or people the dashboard is
shared with); identity values always bind the reader; results land in the
reader's own rows and never change the copied snapshot. Only the live
release's copies run.
"""
import uuid

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

STALE = {"rows": [{"חודש": "ישן", "הכנסות": 0}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _published(corpus, corpus_rows, test_client, key, visibility="internal"):
    aid = own(corpus_rows, corpus, key)
    release = publish(aid)
    assert share(test_client, corpus, aid, visibility).status_code == 200
    copies = _rows(corpus_rows, "SELECT q.id, q.title, s.id AS step FROM queries q JOIN steps s "
                                "ON s.id = q.default_step_id WHERE q.artifact_release_id = :r", r=release)
    return aid, release, copies


def _months(body) -> set:
    return {r.get("חודש") for r in (body.get("data") or {}).get("rows", [])}


@pytest.mark.e2e
def test_a_readers_run_is_theirs_and_leaves_the_snapshot_alone(corpus, corpus_rows, test_client):
    aid, older, _ = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    release = publish(aid)  # an older release exists too: only the live one runs
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    corpus_rows.update("steps", copy["step"], data=STALE)  # tell the snapshot from a fresh run
    resp = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["steps_total"], body["steps_succeeded"], body["steps_failed"]) == (1, 1, 0)
    assert body["executed_as"] == "viewer"

    step_path = f"/api/d/{aid}/queries/{copy['id']}/step"
    mine = test_client.get(step_path, headers=as_(corpus, "member")).json()
    assert _months(mine) == {"ינואר", "פברואר"} and mine["viewer_result"]["executed_as"] == "viewer"
    assert _months(test_client.get(step_path, headers=as_(corpus, "owner")).json()) == {"ישן"}
    assert _months(test_client.get(step_path, headers=as_(corpus, "direct_share")).json()) == {"ישן"}

    results = _rows(corpus_rows, "SELECT step_id, user_id, report_id FROM step_user_results WHERE step_id = :s",
                    s=copy["step"])
    assert results == [{"step_id": copy["step"], "user_id": corpus.cast.id("member"), "report_id": None}]
    draft_step = corpus.ids["conv_three_dash/q_sales/step"]
    assert not _rows(corpus_rows, "SELECT id FROM step_user_results WHERE step_id = :s AND user_id = :u",
                     s=draft_step, u=corpus.cast.id("member"))

    # A content without a chart list means all of the release's charts --
    # this release's, not an older one's.
    corpus_rows.update("artifact_releases", release, content={"code": "function App() {}", "visualization_ids": []})
    again = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member")).json()
    assert (again["steps_total"], again["steps_failed"]) == (1, 0)


@pytest.mark.e2e
def test_who_may_run(corpus, corpus_rows, test_client):
    aid, _, (copy,) = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales", "shared")
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    for path in (f"/api/d/{aid}/run", f"/api/d/{aid}/queries/{copy['id']}/run"):
        assert test_client.post(path, json={}, headers=as_(corpus, "anonymous")).status_code == 401, path
        assert test_client.post(path, json={}, headers=as_(corpus, "member")).status_code == 403, path
        assert test_client.post(path, json={}, headers=as_(corpus, "direct_share")).status_code == 200, path
    # The owner refreshes the dashboard itself (Publish and refresh), not as a reader.
    assert test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "owner")).status_code == 400


@pytest.mark.e2e
def test_run_on_my_behalf_runs_with_the_owners_credentials_for_members_only(corpus, corpus_rows, test_client):
    aid, _, (copy,) = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales", "public")
    assert test_client.put(f"/api/artifacts/{aid}/settings", headers=as_(corpus, "owner"),
                           json={"shared_run_identity": "creator"}).status_code == 200
    resp = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member"))
    assert resp.status_code == 200 and resp.json()["executed_as"] == "creator"
    stamped = _rows(corpus_rows, "SELECT executed_as FROM step_user_results WHERE step_id = :s AND user_id = :u",
                    s=copy["step"], u=corpus.cast.id("member"))
    assert stamped == [{"executed_as": "creator"}]
    # A signed-in stranger on a public dashboard does not get to spend the owner's credentials.
    assert test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "other_org")).status_code == 403


@pytest.mark.e2e
@pytest.mark.parametrize("key", ["rls/dash", "bow_source/dash"])
def test_rls_and_monitoring_sources_always_run_as_the_reader(corpus, corpus_rows, test_client, key):
    """Even if "run on my behalf" is on (the settings refuse it over RLS; a
    source can gain RLS later), these run with the reader's own access."""
    owner = "bow_scoped" if key.startswith("bow") else "owner"
    aid = own(corpus_rows, corpus, key, owner=owner)
    publish(aid)
    corpus_rows.update("artifacts", aid, visibility="internal", shared_run_identity="creator")
    reader = "admin"
    resp = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, reader))
    assert resp.status_code == 200, resp.text
    assert resp.json()["executed_as"] == "viewer"


@pytest.mark.e2e
def test_a_filter_run_is_cached_per_reader_and_values(corpus, corpus_rows, test_client):
    aid, release, copies = _published(corpus, corpus_rows, test_client, "options_sources/dash")
    by_id = next(c for c in copies if c["title"] == "שירים לפי ז'אנר")
    path = f"/api/d/{aid}/queries/{by_id['id']}/run"
    first = test_client.post(path, json={"params": {"genre": "ינואר"}}, headers=as_(corpus, "member"))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "success" and first.json()["cached"] is False
    assert first.json()["step_id"] == by_id["step"] and _months(first.json())
    again = test_client.post(path, json={"params": {"genre": "ינואר"}}, headers=as_(corpus, "member"))
    assert again.json()["cached"] is True
    other = test_client.post(path, json={"params": {"genre": "פברואר"}}, headers=as_(corpus, "member"))
    assert other.json()["cached"] is False
    rows = _rows(corpus_rows, "SELECT user_id, report_id FROM step_user_results WHERE step_id = :s", s=by_id["step"])
    assert len(rows) == 2 and {r["report_id"] for r in rows} == {None}
    assert {r["user_id"] for r in rows} == {corpus.cast.id("member")}
    # The copied snapshot never changes.
    assert _rows(corpus_rows, "SELECT status FROM steps WHERE id = :s", s=by_id["step"]) == [{"status": "success"}]


@pytest.mark.e2e
def test_only_the_live_releases_copies_run(corpus, corpus_rows, test_client):
    aid, first, (old,) = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    publish(aid)
    draft = corpus.ids["conv_three_dash/q_sales"]
    for qid in (old["id"], draft, str(uuid.uuid4()), "not-an-id"):
        resp = test_client.post(f"/api/d/{aid}/queries/{qid}/run", json={}, headers=as_(corpus, "member"))
        # A malformed id is answered by main's guard before the route, with a
        # plain 404 that names nothing; a well-formed one by the route.
        assert resp.status_code == 404, qid
        if qid != "not-an-id":
            assert resp.json()["error_code"] == "artifact.not_found", qid


@pytest.mark.e2e
def test_identity_values_bind_the_reader_and_cannot_be_supplied(corpus, corpus_rows, test_client):
    aid, _, (copy,) = _published(corpus, corpus_rows, test_client, "identity_param/dash")
    path = f"/api/d/{aid}/queries/{copy['id']}/run"
    forged = test_client.post(path, json={"params": {"UserEmail": "owner@corpus.com"}},
                              headers=as_(corpus, "member"))
    assert forged.status_code == 400
    preview = test_client.post(path, json={"run_as_user_id": corpus.cast.id("owner")},
                               headers=as_(corpus, "member"))
    assert preview.status_code == 400  # "view as" belongs to the editor
    body = test_client.post(path, json={}, headers=as_(corpus, "member")).json()
    assert body["applied_params"].get("UserEmail") == "member@corpus.com"


@pytest.fixture
def credentials_used(monkeypatch):
    """Whose credentials each run built its clients with. Client
    construction is where the data-source drivers start; the run itself is
    real."""
    from app.services.data_source_service import DataSourceService

    seen = []

    async def record(self, db, data_source, current_user=None, **kwargs):
        seen.append(str(current_user.id) if current_user is not None else None)
        return {}

    monkeypatch.setattr(DataSourceService, "construct_clients", record)
    return seen


@pytest.mark.e2e
def test_the_owners_credentials_run_only_in_their_mode(corpus, corpus_rows, test_client, credentials_used):
    aid, _, (copy,) = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    member, owner = corpus.cast.id("member"), corpus.cast.id("owner")
    filter_path = f"/api/d/{aid}/queries/{copy['id']}/run"
    assert test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member")).status_code == 200
    assert test_client.post(filter_path, json={"force_refresh": True}, headers=as_(corpus, "member")).status_code == 200
    assert set(credentials_used) == {member}
    credentials_used.clear()
    assert test_client.put(f"/api/artifacts/{aid}/settings", headers=as_(corpus, "owner"),
                           json={"shared_run_identity": "creator"}).status_code == 200
    assert test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member")).status_code == 200
    assert test_client.post(filter_path, json={"force_refresh": True}, headers=as_(corpus, "member")).status_code == 200
    assert set(credentials_used) == {owner}


@pytest.mark.e2e
def test_an_auto_release_over_an_rls_source_runs_as_the_reader(corpus, corpus_rows, test_client, credentials_used):
    """An "Auto" dashboard has no sources of its own: it runs over the
    organization's, which here include an RLS source, so "run on my behalf"
    is refused and never takes effect."""
    aid, _, _ = _published(corpus, corpus_rows, test_client, "auto_conv/dash")
    refused = test_client.put(f"/api/artifacts/{aid}/settings", headers=as_(corpus, "owner"),
                              json={"shared_run_identity": "creator"})
    assert refused.status_code == 400 and refused.json()["error_code"] == "dashboard.rls_requires_viewer"
    corpus_rows.update("artifacts", aid, shared_run_identity="creator")  # a source gained RLS later
    resp = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member"))
    assert resp.status_code == 200 and resp.json()["executed_as"] == "viewer"
    assert set(credentials_used) <= {corpus.cast.id("member")}


@pytest.mark.e2e
def test_filters_are_for_members_of_the_organization(corpus, corpus_rows, test_client):
    aid, _, (copy,) = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales", "public")
    path = f"/api/d/{aid}/queries/{copy['id']}/run"
    assert test_client.post(path, json={}, headers=as_(corpus, "other_org")).status_code == 403
    assert test_client.post(path, json={}, headers=as_(corpus, "member")).status_code == 200
    # The whole-dashboard run replays stored values, open to every reader as on /r.
    assert test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "other_org")).status_code == 200


@pytest.mark.e2e
def test_a_chart_that_loads_an_entity_runs_with_filters_too(corpus, corpus_rows, test_client):
    corpus_rows.update("steps", corpus.ids["conv_three_dash/q_sales/step"], code=(
        "def generate_df(ds_clients, excel_files, load_entity):\n"
        "    return load_entity('מודל הכנסות')\n"))
    aid, _, (copy,) = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    # The entity is the owner's (a member may not load it, by today's entity
    # rules), so the owner moves the filter.
    body = test_client.post(f"/api/d/{aid}/queries/{copy['id']}/run", json={},
                            headers=as_(corpus, "owner")).json()
    assert body["status"] == "success", body
    assert body["data"]["rows"]
