"""The owner deletes a dashboard, not one version of it.

`DELETE /api/artifacts/{dashboard_id}/dashboard`: the owner only (gated on
`delete_reports` and the dashboard's owner capability). Refused while a
publish is in progress, as a conversation delete is. A published dashboard
is unpublished in the same step: its link -- by id or by name -- opens
nothing for anyone, its sharing goes back to private and its schedule is
cleared with its job. The dashboard and every version of it are soft-deleted
and leave the dashboards list.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

DASH = "conv_three_dash/dash_sales"


@pytest.fixture
def jobs():
    """The scheduler's dashboard jobs, removed after the test."""
    from app.core.scheduler import scheduler

    yield scheduler
    for job in scheduler.get_jobs():
        if job.id.startswith("dashboard_"):
            scheduler.remove_job(job.id)


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _artifact(corpus_rows, aid):
    (row,) = _rows(corpus_rows, "SELECT deleted_at, published_release_id, visibility, slug, cron_schedule "
                                "FROM artifacts WHERE id = :a", a=aid)
    return row


def _live_versions(corpus_rows, aid):
    return _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a AND deleted_at IS NULL",
                 a=aid)


def _delete(test_client, corpus, aid, person):
    return test_client.delete(f"/api/artifacts/{aid}/dashboard", headers=as_(corpus, person))


def _listed(test_client, corpus, aid):
    resp = test_client.get("/api/artifacts?filter=my&limit=100", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    return aid in {d["id"] for d in resp.json()["dashboards"]}


@pytest.mark.e2e
def test_the_owner_deletes_a_published_shared_scheduled_dashboard(corpus, corpus_rows, test_client, jobs):
    from app.services.dashboard_refresh import job_id

    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    name = f"sales-{uuid.uuid4().hex[:8]}"
    resp = test_client.put(f"/api/artifacts/{aid}/settings", headers=as_(corpus, "owner"),
                           json={"cron_schedule": "0 8 * * *", "slug": name})
    assert resp.status_code == 200, resp.text
    # Control: before the delete, every door is open -- so what follows can
    # only pass because the delete closed it.
    assert jobs.get_job(job_id(aid)) is not None
    for ref in (aid, name):
        assert test_client.get(f"/api/d/{ref}", headers=as_(corpus, "member")).status_code == 200
    assert _listed(test_client, corpus, aid)
    assert _live_versions(corpus_rows, aid)

    resp = _delete(test_client, corpus, aid, "owner")
    assert resp.status_code == 200, resp.text
    assert resp.json()["was_published"] is True and resp.json()["was_shared"] is True

    row = _artifact(corpus_rows, aid)
    assert row["deleted_at"] is not None
    assert row["published_release_id"] is None and row["visibility"] == "none" and row["cron_schedule"] is None
    assert _live_versions(corpus_rows, aid) == []
    assert jobs.get_job(job_id(aid)) is None
    # The link stops resolving, by id and by name, for the owner as for readers.
    for ref in (aid, name):
        for person in ("owner", "member", "admin"):
            assert test_client.get(f"/api/d/{ref}", headers=as_(corpus, person)).status_code == 404
    assert not _listed(test_client, corpus, aid)
    # Its name stays taken: an old link never opens someone else's dashboard.
    other = own(corpus_rows, corpus, "conv_one_dash/dash")
    taken = test_client.put(f"/api/artifacts/{other}/settings", headers=as_(corpus, "owner"), json={"slug": name})
    assert taken.status_code == 409, taken.text
    # Gone is gone: a second delete finds nothing.
    assert _delete(test_client, corpus, aid, "owner").status_code == 404


@pytest.mark.e2e
def test_the_owner_deletes_a_never_published_dashboard(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "owner")).status_code == 200

    resp = _delete(test_client, corpus, aid, "owner")
    assert resp.status_code == 200, resp.text
    assert resp.json()["was_published"] is False
    assert _artifact(corpus_rows, aid)["deleted_at"] is not None
    assert _live_versions(corpus_rows, aid) == []
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "owner")).status_code == 404
    # The conversation it was made in stays; only the dashboard goes.
    report = corpus.reports["conv_three_dash"]
    assert _rows(corpus_rows, "SELECT status FROM reports WHERE id = :r", r=report)[0]["status"] != "archived"


@pytest.mark.e2e
@pytest.mark.parametrize("person", ["admin", "member", "other_org", "anonymous"])
def test_only_the_owner_deletes_a_dashboard(corpus, corpus_rows, test_client, person):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200  # everyone here can read it

    resp = _delete(test_client, corpus, aid, person)
    assert resp.status_code in (401, 403, 404), resp.text
    row = _artifact(corpus_rows, aid)
    assert row["deleted_at"] is None and row["published_release_id"] is not None
    assert _live_versions(corpus_rows, aid)


@pytest.mark.e2e
def test_an_editor_is_refused_with_403(corpus, corpus_rows, test_client):
    """An editor can see the dashboard, so the refusal says forbidden, not
    missing."""
    aid = own(corpus_rows, corpus, DASH)
    granted = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"),
                              json={"visibility": "none", "editor_user_ids": [corpus.cast.id("member")]})
    assert granted.status_code == 200, granted.text
    # The capability that shows the Delete button in settings: the owner's alone.
    caps = {p: set(test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, p)).json()["capabilities"])
            for p in ("owner", "member")}
    assert "delete" in caps["owner"]
    assert "edit" in caps["member"] and "delete" not in caps["member"]
    assert _delete(test_client, corpus, aid, "member").status_code == 403
    assert _artifact(corpus_rows, aid)["deleted_at"] is None


@pytest.mark.e2e
def test_a_dashboard_is_not_deleted_while_it_publishes(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    release = publish(aid)
    corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id,
                       status="publishing", updated_at=datetime.utcnow())

    resp = _delete(test_client, corpus, aid, "owner")
    assert resp.status_code == 409 and resp.json()["error_code"] == "dashboard.publish_in_progress"
    row = _artifact(corpus_rows, aid)
    assert row["deleted_at"] is None and str(row["published_release_id"]) == release
    assert _live_versions(corpus_rows, aid)


@pytest.mark.e2e
def test_a_publish_that_stopped_beating_does_not_block_the_delete(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id,
                       status="publishing", updated_at=datetime.utcnow() - timedelta(hours=1))

    assert _delete(test_client, corpus, aid, "owner").status_code == 200
    assert _artifact(corpus_rows, aid)["deleted_at"] is not None
    assert _rows(corpus_rows, "SELECT status FROM artifact_releases WHERE artifact_id = :a", a=aid) == [
        {"status": "failed"}]
