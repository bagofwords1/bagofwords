"""Deleting a conversation never deletes someone else's dashboard.

A never-published dashboard is deleted only once no conversation edits it
any more (collab01), and deleting a dashboard is the owner's (or the org
admin's) to do. So when an editor deletes the last conversation editing an
unpublished dashboard, the dashboard stays the owner's, with no draft: the
owner opens Edit to go on from its newest version, or deletes it. When the
owner's own conversation is the last one, it goes with it, as before.
"""
import asyncio

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own

CONV = "conv_three_dash"
DASH = f"{CONV}/dash_sales"


@pytest.fixture
def spawned(monkeypatch):
    jobs = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", jobs.append)
    return jobs


def _finish(spawned):
    while spawned:
        job = spawned.pop(0)
        if asyncio.iscoroutine(job):
            asyncio.run(job)


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _deleted_at(corpus_rows, aid):
    return _rows(corpus_rows, "SELECT deleted_at FROM artifacts WHERE id = :a", a=aid)[0]["deleted_at"]


def _grant_and_join(test_client, corpus, aid, spawned, person="member"):
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"),
                           json={"visibility": "none", "editor_user_ids": [corpus.cast.id(person)]})
    assert resp.status_code == 200, resp.text
    joined = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, person))
    assert joined.status_code == 200, joined.text
    _finish(spawned)
    return joined.json()["report_id"]


def _preview(test_client, corpus, report_id, person):
    resp = test_client.get(f"/api/reports/{report_id}/delete_preview", headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return {d["id"]: d["outcome"] for d in resp.json()["dashboards"]}


def _delete(test_client, corpus, report_id, person):
    resp = test_client.delete(f"/api/reports/{report_id}", headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text


@pytest.mark.e2e
def test_an_editor_deleting_the_last_conversation_leaves_the_dashboard_to_its_owner(
        corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)  # never published
    avis = _grant_and_join(test_client, corpus, aid, spawned)
    _delete(test_client, corpus, corpus.reports[CONV], "owner")  # Avi still edits it: it stays
    assert _deleted_at(corpus_rows, aid) is None

    assert _preview(test_client, corpus, avis, "member")[aid] == "kept_for_owner"
    _delete(test_client, corpus, avis, "member")
    assert _deleted_at(corpus_rows, aid) is None
    # Still the owner's, in her list; Edit goes on from its newest version.
    mine = test_client.get("/api/artifacts?filter=my", headers=as_(corpus, "owner")).json()["dashboards"]
    assert aid in {d["id"] for d in mine}
    resumed = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "owner"))
    assert resumed.status_code == 200, resumed.text
    new = resumed.json()["report_id"]
    assert new not in (avis, corpus.reports[CONV])
    versions = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a AND report_id = :r "
                                  "AND deleted_at IS NULL", a=aid, r=new)
    assert versions


@pytest.mark.e2e
def test_the_owner_deleting_the_last_conversation_deletes_it_as_before(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    avis = _grant_and_join(test_client, corpus, aid, spawned)
    _delete(test_client, corpus, avis, "member")  # the owner's conversation still edits it
    assert _deleted_at(corpus_rows, aid) is None

    assert _preview(test_client, corpus, corpus.reports[CONV], "owner")[aid] == "deleted"
    _delete(test_client, corpus, corpus.reports[CONV], "owner")
    assert _deleted_at(corpus_rows, aid) is not None


@pytest.mark.e2e
def test_with_no_editors_deleting_the_conversation_deletes_it_as_before(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    assert _preview(test_client, corpus, corpus.reports[CONV], "owner")[aid] == "deleted"
    _delete(test_client, corpus, corpus.reports[CONV], "owner")
    assert _deleted_at(corpus_rows, aid) is not None
