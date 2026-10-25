"""A published dashboard outlives its conversation (phase 7).

Deleting a conversation still archives it, as today. Its published
dashboards leave it and stay live -- link, sharing, schedule and chat as
they were -- and its never-published ones are deleted with it; a published
dashboard's unpublished changes stay behind in the archived conversation.
`GET /api/reports/{id}/delete_preview` says beforehand which of the three
each dashboard is. Bulk delete and project delete follow the same rule. A
conversation is not deleted while one of its dashboards publishes, and a
dashboard without a live conversation does not publish (there is no draft:
edit it first).
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

CONV = "conv_three_dash"


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _artifact(corpus_rows, aid):
    (row,) = _rows(corpus_rows, "SELECT report_id, project_id, deleted_at, published_release_id FROM artifacts "
                                "WHERE id = :a", a=aid)
    return row


def _three(corpus, corpus_rows, test_client):
    """dash_sales published as is, dash_stock published from an older
    version (it has unpublished changes), the deck never published."""
    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    stock = own(corpus_rows, corpus, f"{CONV}/dash_stock")
    deck = own(corpus_rows, corpus, f"{CONV}/deck")
    publish(sales)
    older = publish(stock)
    corpus_rows.update("artifact_releases", older, source_version_id=None)  # not the newest version any more
    assert share(test_client, corpus, sales, "internal").status_code == 200
    return sales, stock, deck


@pytest.mark.e2e
def test_the_preview_says_what_happens_to_each_dashboard(corpus, corpus_rows, test_client):
    sales, stock, deck = _three(corpus, corpus_rows, test_client)
    report = corpus.reports[CONV]
    resp = test_client.get(f"/api/reports/{report}/delete_preview", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    outcomes = {d["id"]: d["outcome"] for d in resp.json()["dashboards"]}
    assert outcomes == {sales: "survives", stock: "loses_changes", deck: "deleted"}
    assert test_client.get(f"/api/reports/{report}/delete_preview",
                           headers=as_(corpus, "member")).status_code in (403, 404)
    # Asking changes nothing.
    assert _artifact(corpus_rows, deck)["deleted_at"] is None
    assert _rows(corpus_rows, "SELECT status FROM reports WHERE id = :r", r=report)[0]["status"] != "archived"


@pytest.mark.e2e
def test_deleting_the_conversation_keeps_its_published_dashboards(corpus, corpus_rows, test_client):
    sales, stock, deck = _three(corpus, corpus_rows, test_client)
    corpus_rows.update("artifacts", sales, cron_schedule="0 8 * * *")
    report = corpus.reports[CONV]
    assert test_client.delete(f"/api/reports/{report}", headers=as_(corpus, "owner")).status_code == 200
    assert _rows(corpus_rows, "SELECT status FROM reports WHERE id = :r", r=report) == [{"status": "archived"}]

    for aid in (sales, stock):
        row = _artifact(corpus_rows, aid)
        assert row["report_id"] is None and row["deleted_at"] is None and row["published_release_id"]
    assert _artifact(corpus_rows, deck)["deleted_at"] is not None

    # Readers keep the dashboard: the same link, sharing and schedule.
    assert test_client.get(f"/api/d/{sales}", headers=as_(corpus, "member")).status_code == 200
    assert test_client.get(f"/api/d/{sales}/queries", headers=as_(corpus, "member")).json()
    assert _rows(corpus_rows, "SELECT cron_schedule FROM artifacts WHERE id = :a", a=sales) == [
        {"cron_schedule": "0 8 * * *"}]
    assert test_client.get(f"/api/d/{deck}", headers=as_(corpus, "owner")).status_code == 404

    # No draft any more: the draft door is closed and Publish asks for an edit first.
    assert test_client.get(f"/api/d/{sales}/draft", headers=as_(corpus, "owner")).status_code == 404
    refused = test_client.post(f"/api/artifacts/{sales}/publish", headers=as_(corpus, "owner"))
    assert refused.status_code == 409 and refused.json()["error_code"] == "dashboard.edit_first"


@pytest.mark.e2e
def test_a_dashboard_whose_conversation_was_archived_before_does_not_publish_either(corpus, corpus_rows,
                                                                                    test_client):
    """Archived before this phase, so still attached: the same rule."""
    aid = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    publish(aid)
    corpus_rows.update("reports", corpus.reports[CONV], status="archived")
    refused = test_client.post(f"/api/artifacts/{aid}/publish", headers=as_(corpus, "owner"))
    assert refused.status_code == 409 and refused.json()["error_code"] == "dashboard.edit_first"


@pytest.mark.e2e
def test_bulk_delete_settles_dashboards_the_same_way(corpus, corpus_rows, test_client):
    published = own(corpus_rows, corpus, "conv_one_dash/dash")
    publish(published)
    never = own(corpus_rows, corpus, "conv_three_dash/deck")
    resp = test_client.post("/api/reports/bulk/archive", headers=as_(corpus, "owner"),
                            json=[corpus.reports["conv_one_dash"], corpus.reports[CONV]])
    assert resp.status_code == 200, resp.text
    assert _artifact(corpus_rows, published)["report_id"] is None
    assert _artifact(corpus_rows, published)["deleted_at"] is None
    assert _artifact(corpus_rows, never)["deleted_at"] is not None


@pytest.mark.e2e
def test_deleting_a_project_settles_its_dashboards_and_takes_them_out_of_it(corpus, corpus_rows, test_client):
    project = corpus.cast.projects["org_wide"]
    aid = own(corpus_rows, corpus, "in_project_org/dash", project="org_wide")
    publish(aid)
    resp = test_client.delete(f"/api/projects/{project}", headers=as_(corpus, "proj_owner"))
    assert resp.status_code in (200, 204), resp.text
    row = _artifact(corpus_rows, aid)
    assert row["report_id"] is None and row["deleted_at"] is None and row["project_id"] is None


@pytest.mark.e2e
def test_a_conversation_is_not_deleted_while_one_of_its_dashboards_publishes(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id,
                       status="publishing", updated_at=datetime.utcnow())
    report = corpus.reports[CONV]
    resp = test_client.delete(f"/api/reports/{report}", headers=as_(corpus, "owner"))
    assert resp.status_code == 409 and resp.json()["error_code"] == "dashboard.publish_in_progress"
    assert _rows(corpus_rows, "SELECT status FROM reports WHERE id = :r", r=report)[0]["status"] != "archived"
    assert _artifact(corpus_rows, aid)["report_id"] == report


# ── "Edit" and Fork from the published release (part B) ─────────────────

def _copies(corpus_rows, release):
    return _rows(corpus_rows, "SELECT q.id, q.title, s.id AS step, s.data, s.code FROM queries q JOIN steps s "
                              "ON s.id = q.default_step_id WHERE q.artifact_release_id = :r ORDER BY q.title",
                 r=release)


def _conversation_queries(corpus_rows, report_id):
    return _rows(corpus_rows, "SELECT q.id, q.title, q.user_id, s.data, s.code, s.status FROM queries q "
                              "JOIN steps s ON s.id = q.default_step_id WHERE q.report_id = :r ORDER BY q.title",
                 r=report_id)


@pytest.fixture
def spawned(monkeypatch):
    jobs = []

    def keep(coro):
        jobs.append(coro.__qualname__)
        coro.close()

    monkeypatch.setattr("app.core.fire_and_forget.spawn", keep)
    return jobs


@pytest.mark.e2e
def test_edit_opens_the_live_conversation_without_copying(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    publish(aid)
    before = _rows(corpus_rows, "SELECT count(*) AS n FROM reports")[0]["n"]
    resp = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"report_id": corpus.reports[CONV], "created": False, "rerunning": False}
    assert _rows(corpus_rows, "SELECT count(*) AS n FROM reports")[0]["n"] == before


@pytest.mark.e2e
def test_edit_after_the_conversation_was_deleted_starts_one_from_the_release(corpus, corpus_rows, test_client,
                                                                             spawned):
    aid = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _copies(corpus_rows, release)
    marker = {"rows": [{"חודש": "מפורסם", "הכנסות": 7}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}
    corpus_rows.update("steps", copy["step"], data=marker)
    old = corpus.reports[CONV]
    assert test_client.delete(f"/api/reports/{old}", headers=as_(corpus, "owner")).status_code == 200

    resp = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    new = body["report_id"]
    assert body["created"] is True and new != old
    assert _artifact(corpus_rows, aid)["report_id"] == new  # the same dashboard, now edited there
    (report,) = _rows(corpus_rows, "SELECT user_id, status, report_type FROM reports WHERE id = :r", r=new)
    assert report == {"user_id": corpus.cast.id("owner"), "status": "draft", "report_type": "regular"}

    # A full copy of the release: its query, its rows (the owner's own), its sources.
    (query,) = _conversation_queries(corpus_rows, new)
    data = json.loads(query["data"]) if isinstance(query["data"], str) else query["data"]
    assert query["title"] == copy["title"] and data == marker
    assert _rows(corpus_rows, "SELECT data_source_id FROM report_data_source_association WHERE report_id = :r",
                 r=new) == _rows(corpus_rows, "SELECT data_source_id FROM artifact_release_data_source_association "
                                          "WHERE artifact_release_id = :r", r=release)
    # A new draft version of the same dashboard, in the new conversation: the editor lists it there.
    listed = test_client.get(f"/api/artifacts/report/{new}", headers=as_(corpus, "owner")).json()
    assert aid in {a.get("artifact_id") or a.get("id") for a in listed} or any(
        str(a.get("artifact_id")) == aid for a in listed)

    # Readers did not notice; the draft door and Publish are back.
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "member")).status_code == 200
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "owner")).status_code == 200
    assert test_client.post(f"/api/artifacts/{aid}/publish", headers=as_(corpus, "owner")).status_code == 202

    # A second Edit opens that conversation.
    again = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "owner")).json()
    assert again == {"report_id": new, "created": False, "rerunning": False}


@pytest.mark.e2e
def test_only_the_owner_edits(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    for person, code in (("anonymous", 401), ("member", 403), ("admin", 403)):
        assert test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, person)).status_code == code


@pytest.mark.e2e
def test_a_reader_forks_the_published_dashboard_into_their_own(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    resp = test_client.post(f"/api/d/{aid}/fork", json={}, headers=as_(corpus, "member"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    fork_report, fork_artifact = body["report_id"], body["artifact_id"]
    assert fork_artifact != aid
    (row,) = _rows(corpus_rows, "SELECT owner_id, report_id, published_release_id, visibility FROM artifacts "
                                "WHERE id = :a", a=fork_artifact)
    assert row == {"owner_id": corpus.cast.id("member"), "report_id": fork_report, "published_release_id": None,
                   "visibility": "none"}
    (query,) = _conversation_queries(corpus_rows, fork_report)
    assert query["user_id"] == corpus.cast.id("member")
    # The original is untouched.
    assert _artifact(corpus_rows, aid)["report_id"] == corpus.reports[CONV]
    assert len(_copies(corpus_rows, release)) == 1


@pytest.mark.e2e
def test_a_fork_never_copies_rows_that_are_one_identitys(corpus, corpus_rows, test_client, spawned):
    """Over row-level security the published rows are the owner's slice: the
    fork gets the query, not the rows -- Fork's rule."""
    aid = own(corpus_rows, corpus, "rls/dash")
    release = publish(aid)
    corpus_rows.update("artifacts", aid, visibility="internal")
    resp = test_client.post(f"/api/d/{aid}/fork", json={}, headers=as_(corpus, "admin"))
    assert resp.status_code == 200, resp.text
    (query,) = _conversation_queries(corpus_rows, resp.json()["report_id"])
    assert query["data"] in (None, "{}", {}) and query["code"]
    assert _copies(corpus_rows, release)[0]["data"] not in (None, "{}", {})


@pytest.mark.e2e
def test_a_fork_never_copies_rows_an_identity_parameter_scoped_to_the_owner(corpus, corpus_rows, test_client,
                                                                           spawned):
    """A query bound to the viewer's email (no RLS, a shared connection):
    the published rows are still the owner's slice -- the page withholds
    them from readers, so a fork must not hand them over either."""
    aid = own(corpus_rows, corpus, "identity_param/dash")
    release = publish(aid)
    corpus_rows.update("artifacts", aid, visibility="internal")
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("steps", copy["step"], applied_params={"UserEmail": "owner@corpus.com"})
    resp = test_client.post(f"/api/d/{aid}/fork", json={}, headers=as_(corpus, "member"))
    assert resp.status_code == 200, resp.text
    (query,) = _conversation_queries(corpus_rows, resp.json()["report_id"])
    assert query["data"] in (None, "{}", {}) and query["code"]
    assert "owner@corpus.com" not in json.dumps(query, default=str)
    assert _copies(corpus_rows, release)[0]["data"] not in (None, "{}", {})


@pytest.mark.e2e
def test_who_may_fork_a_published_dashboard(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    publish(aid)
    path = f"/api/d/{aid}/fork"
    assert test_client.post(path, json={}, headers=as_(corpus, "member")).status_code == 404  # cannot see it
    assert share(test_client, corpus, aid, "public").status_code == 200
    assert test_client.post(path, json={}, headers=as_(corpus, "anonymous")).status_code == 401
    assert test_client.post(path, json={}, headers=as_(corpus, "other_org")).status_code == 403


# ── review fixes ─────────────────────────────────────────────────────────

def _published_shared(corpus, corpus_rows, test_client, key, visibility="internal"):
    aid = own(corpus_rows, corpus, key)
    release = publish(aid)
    assert share(test_client, corpus, aid, visibility).status_code == 200
    return aid, release


def _fork(test_client, corpus, aid, person="member"):
    resp = test_client.post(f"/api/d/{aid}/fork", json={}, headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _files_of(corpus_rows, report_id):
    return {r["file_id"] for r in _rows(corpus_rows, "SELECT file_id FROM report_file_association WHERE report_id = :r",
                                        r=report_id)}


@pytest.mark.e2e
def test_a_fork_links_no_file_and_an_edit_keeps_them(corpus, corpus_rows, test_client, spawned):
    """A file linked to a conversation is readable by whoever views it: a
    Fork links none (as the conversation's Fork); the owner's Edit keeps
    the release's."""
    aid, release = _published_shared(corpus, corpus_rows, test_client, "embedded_files/dash")
    files = {r["file_id"] for r in _rows(corpus_rows, "SELECT file_id FROM artifact_release_file_association "
                                                      "WHERE artifact_release_id = :r", r=release)}
    assert files
    assert _files_of(corpus_rows, _fork(test_client, corpus, aid)["report_id"]) == set()
    assert test_client.delete(f"/api/reports/{corpus.reports['embedded_files']}",
                              headers=as_(corpus, "owner")).status_code == 200
    edited = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "owner")).json()
    assert _files_of(corpus_rows, edited["report_id"]) == files


@pytest.mark.e2e
def test_a_fork_carries_the_slides_only_when_its_reader_may_see_them(corpus, corpus_rows, test_client, spawned):
    aid, release = _published_shared(corpus, corpus_rows, test_client, "slides_previews/deck")

    def version_of(fork):
        (row,) = _rows(corpus_rows, "SELECT content, pptx_path, thumbnail_path FROM artifact_versions "
                                    "WHERE artifact_id = :a", a=fork["artifact_id"])
        return json.loads(row["content"]) if isinstance(row["content"], str) else row["content"], row

    content, _ = version_of(_fork(test_client, corpus, aid))
    assert content.get("preview_images")  # the reader sees them on /d, so the fork has them

    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("queries", copy["id"], parameters=[{"name": "UserEmail", "type": "string",
                                                           "source": "identity"}])
    content, row = version_of(_fork(test_client, corpus, aid))
    assert not content.get("preview_images") and row["pptx_path"] is None and row["thumbnail_path"] is None


@pytest.mark.e2e
def test_fork_refusals(corpus, corpus_rows, test_client, monkeypatch):
    from app.core import permission_resolver

    aid, _ = _published_shared(corpus, corpus_rows, test_client, f"{CONV}/dash_sales")
    path = f"/api/d/{aid}/fork"
    real = permission_resolver.resolve_permissions

    async def no_create(db, user_id, org_id):
        resolved = await real(db, user_id, org_id)
        resolved.org_permissions = {p for p in resolved.org_permissions if p not in ("create_reports",
                                                                                     "full_admin_access")}
        return resolved

    monkeypatch.setattr(permission_resolver, "resolve_permissions", no_create)
    denied = test_client.post(path, json={}, headers=as_(corpus, "member"))
    assert denied.status_code == 403 and "create reports" in denied.text
    monkeypatch.setattr(permission_resolver, "resolve_permissions", real)

    assert test_client.put("/api/organization/settings", headers=as_(corpus, "admin"),
                           json={"config": {"allow_forks": {"value": False}}}).status_code == 200
    disabled = test_client.post(path, json={}, headers=as_(corpus, "member"))
    assert disabled.status_code == 403 and "forks_disabled" in disabled.text
    assert test_client.post(path, json={"title": 7}, headers=as_(corpus, "member")).status_code == 422


@pytest.mark.e2e
def test_a_fork_over_a_personal_connection_runs_again_as_the_forker(corpus, corpus_rows, test_client, spawned):
    """The code is held back and hydrate_fork runs it as the forker; the
    fork names no conversation of the owner's, and refreshing it on view
    waits for that hydration all the same."""
    aid, _ = _published_shared(corpus, corpus_rows, test_client, "user_required/dash")
    fork = _fork(test_client, corpus, aid, person="owner")
    assert spawned == ["ForkService.hydrate_fork"]
    (row,) = _rows(corpus_rows, "SELECT forked_from_id FROM reports WHERE id = :r", r=fork["report_id"])
    assert row["forked_from_id"] is None
    (query,) = _conversation_queries(corpus_rows, fork["report_id"])
    assert query["code"] == "" and query["status"] == "pending"
    resp = test_client.post(f"/api/r/{fork['report_id']}/rerun", headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["skipped"] is True and "hydrating" in str(resp.json())


@pytest.mark.e2e
def test_deleting_a_project_also_settles_dashboards_already_out_of_its_conversations(corpus, corpus_rows,
                                                                                    test_client):
    project = corpus.cast.projects["org_wide"]
    detached = own(corpus_rows, corpus, "in_project_org/dash", project="org_wide")
    publish(detached)
    assert test_client.delete(f"/api/reports/{corpus.reports['in_project_org']}",
                              headers=as_(corpus, "owner")).status_code == 200
    assert _artifact(corpus_rows, detached)["project_id"] == project  # still in it
    assert test_client.delete(f"/api/projects/{project}", headers=as_(corpus, "proj_owner")).status_code in (200, 204)
    assert _artifact(corpus_rows, detached)["project_id"] is None and _artifact(corpus_rows, detached)["deleted_at"] is None


@pytest.mark.e2e
def test_two_edits_at_once_give_one_conversation(corpus, corpus_rows, test_client, monkeypatch, spawned):
    """Postgres only: the other Edit commits from a second connection while
    this one runs, which SQLite's single writer cannot interleave."""
    from app.services import dashboard_lifecycle

    if corpus_rows.engine.dialect.name != "postgresql":
        pytest.skip("needs two concurrent writers")

    aid, _ = _published_shared(corpus, corpus_rows, test_client, f"{CONV}/dash_sales")
    assert test_client.delete(f"/api/reports/{corpus.reports[CONV]}", headers=as_(corpus, "owner")).status_code == 200
    winner = corpus.reports["conv_one_dash"]  # the other Edit's new conversation, say
    copy_it = dashboard_lifecycle.release_to_conversation

    async def raced(*args, **kwargs):
        made = await copy_it(*args, **kwargs)
        corpus_rows.update("artifacts", aid, report_id=winner)  # the other Edit committed first
        return made

    monkeypatch.setattr(dashboard_lifecycle, "release_to_conversation", raced)
    body = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "owner")).json()
    assert body == {"report_id": winner, "created": False, "rerunning": False}
    assert _rows(corpus_rows, "SELECT id FROM reports WHERE slug LIKE 'edit-%' AND user_id = :u",
                 u=corpus.cast.id("owner")) == []  # this Edit's copy was rolled back


@pytest.mark.e2e
def test_right_after_edit_nothing_is_unpublished_and_old_versions_do_not_publish(corpus, corpus_rows, test_client,
                                                                                spawned):
    aid, _ = _published_shared(corpus, corpus_rows, test_client, f"{CONV}/dash_sales")
    old_version = corpus.ids[f"{CONV}/dash_sales/v1"]
    assert test_client.delete(f"/api/reports/{corpus.reports[CONV]}", headers=as_(corpus, "owner")).status_code == 200
    new = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "owner")).json()["report_id"]
    status = test_client.get(f"/api/artifacts/{aid}/publish", headers=as_(corpus, "owner")).json()
    assert status["has_unpublished_changes"] is False
    preview = test_client.get(f"/api/reports/{new}/delete_preview", headers=as_(corpus, "owner")).json()
    assert preview["dashboards"][0]["outcome"] == "survives"
    old = test_client.post(f"/api/artifacts/{aid}/publish", json={"version_id": old_version},
                           headers=as_(corpus, "owner"))
    assert old.status_code == 409 and old.json()["error_code"] == "dashboard.version_not_publishable"


@pytest.mark.e2e
def test_a_dashboard_deleted_while_publishing_does_not_go_live(corpus, corpus_rows, test_client, monkeypatch):
    import asyncio

    jobs = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", jobs.append)
    aid = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    attempt = test_client.post(f"/api/artifacts/{aid}/publish", headers=as_(corpus, "owner")).json()["latest"]["id"]
    corpus_rows.update("artifacts", aid, deleted_at=datetime(2026, 3, 1))
    while jobs:
        asyncio.run(jobs.pop(0))
    assert _rows(corpus_rows, "SELECT status FROM artifact_releases WHERE id = :r", r=attempt) == [
        {"status": "failed"}]
    assert _artifact(corpus_rows, aid)["published_release_id"] is None


# ── phase 8: a fork from /d says where it came from ──────────────────────

@pytest.mark.e2e
def test_a_fork_from_d_names_its_source_dashboard(corpus, corpus_rows, test_client, spawned):
    """The conversation's Fork shows "Forked from <conversation>" and waits
    for its hydration; a fork from /d names the dashboard instead (never the
    owner's conversation), so the editor shows the same banner and the same
    waiting."""
    aid, _ = _published_shared(corpus, corpus_rows, test_client, f"{CONV}/dash_sales")
    fork = _fork(test_client, corpus, aid)
    (row,) = _rows(corpus_rows, "SELECT forked_from_id, forked_from_artifact_id FROM reports WHERE id = :r",
                   r=fork["report_id"])
    assert row == {"forked_from_id": None, "forked_from_artifact_id": aid}
    body = test_client.get(f"/api/reports/{fork['report_id']}", headers=as_(corpus, "member")).json()
    assert body["forked_from_artifact_id"] == aid and body["forked_from_id"] is None
    assert body["forked_from_title"] == "לוח מכירות"
    assert body["forked_from_user_name"] == "corpus owner"


@pytest.mark.e2e
def test_a_refused_delete_saves_nothing_even_after_failing_a_stale_publish(corpus, corpus_rows, test_client,
                                                                           monkeypatch):
    """One dashboard's publish stopped beating, another's is running: the
    delete is refused and none of it is saved -- the conversation is not
    archived, the stale publish is not failed half-way (PR #1455 review)."""
    from datetime import timedelta

    from app.services import dashboard_lifecycle

    stale_aid = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    busy_aid = own(corpus_rows, corpus, f"{CONV}/dash_stock")
    stale = corpus_rows.insert("artifact_releases", artifact_id=stale_aid, organization_id=corpus.cast.org_id,
                               status="publishing", updated_at=datetime.utcnow() - timedelta(hours=1))
    corpus_rows.insert("artifact_releases", artifact_id=busy_aid, organization_id=corpus.cast.org_id,
                       status="publishing", updated_at=datetime.utcnow())
    listed = dashboard_lifecycle._dashboards

    async def stale_first(db, report_ids, *, lock=False):
        return sorted(await listed(db, report_ids, lock=lock), key=lambda a: str(a.id) != stale_aid)

    monkeypatch.setattr(dashboard_lifecycle, "_dashboards", stale_first)
    report = corpus.reports[CONV]
    resp = test_client.delete(f"/api/reports/{report}", headers=as_(corpus, "owner"))
    assert resp.status_code == 409 and resp.json()["error_code"] == "dashboard.publish_in_progress"
    assert _rows(corpus_rows, "SELECT status FROM reports WHERE id = :r", r=report)[0]["status"] != "archived"
    assert _rows(corpus_rows, "SELECT status FROM artifact_releases WHERE id = :r", r=stale)[0]["status"] == \
        "publishing"


@pytest.mark.e2e
def test_the_losing_edit_leaves_no_copied_files(corpus, corpus_rows, test_client, monkeypatch, spawned):
    """Two Edits at once: the one that loses removes the files it copied
    from the release -- they hold the release's data (PR #1455 review).
    Postgres only, as the race above."""
    from pathlib import Path

    from app.services import dashboard_lifecycle
    from app.services.thumbnail_service import ThumbnailService
    from tests.fixtures.dashboard_corpus.kinds import TINY_PNG

    if corpus_rows.engine.dialect.name != "postgresql":
        pytest.skip("needs two concurrent writers")

    aid, release = _published_shared(corpus, corpus_rows, test_client, f"{CONV}/dash_sales")
    thumbs = Path(ThumbnailService.UPLOADS_DIR)
    (thumbs / f"release-{release}.png").write_bytes(TINY_PNG)
    corpus_rows.update("artifact_releases", release, thumbnail_path=f"thumbnails/release-{release}.png")
    assert test_client.delete(f"/api/reports/{corpus.reports[CONV]}", headers=as_(corpus, "owner")).status_code == 200
    before = set(thumbs.glob("*.png"))
    winner = corpus.reports["conv_one_dash"]
    copy_it = dashboard_lifecycle.release_to_conversation

    async def raced(*args, **kwargs):
        made = await copy_it(*args, **kwargs)
        corpus_rows.update("artifacts", aid, report_id=winner)  # the other Edit committed first
        return made

    monkeypatch.setattr(dashboard_lifecycle, "release_to_conversation", raced)
    body = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "owner")).json()
    assert body == {"report_id": winner, "created": False, "rerunning": False}
    assert set(thumbs.glob("*.png")) - before == set()
