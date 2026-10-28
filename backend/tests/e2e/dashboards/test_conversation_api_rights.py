"""A conversation's old API acts on its dashboard only with the caller's
rights on that dashboard.

The old endpoints (sharing, publishing, scheduling, the share list) are
gated by owning the conversation. Since collaborative dashboards the
dashboard's owner can change -- ownership transferred, the previous owner
then removed -- so owning the conversation no longer says what one may do
with its dashboard: each call also needs the same right the dashboard's own
API asks for (share, publish, settings, view).
"""
import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_


def _one(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text(sql), params).mappings()]
    assert len(rows) == 1, rows
    return rows[0]


def _dash_of(corpus_rows, rid):
    return _one(corpus_rows, "SELECT * FROM artifacts WHERE legacy_report_id = :r", r=rid)


def _handed_over(test_client, c, aid, *, removed: bool):
    """The owner makes `member` the owner; `member` then removes the
    previous owner (who stayed an editor) when `removed`."""
    grant = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(c, "owner"),
                            json={"visibility": "internal", "editor_user_ids": [c.cast.id("member")]})
    assert grant.status_code == 200, grant.text
    moved = test_client.post(f"/api/artifacts/{aid}/transfer", json={"user_id": c.cast.id("member")},
                             headers=as_(c, "owner"))
    assert moved.status_code == 200, moved.text
    if removed:
        out = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(c, "member"),
                              json={"visibility": "internal", "editor_user_ids": []})
        assert out.status_code == 200, out.text


@pytest.mark.e2e
def test_the_previous_owner_cannot_change_the_dashboard_through_the_conversation(migrated_corpus, corpus_rows,
                                                                                test_client):
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    aid = _dash_of(corpus_rows, rid)["id"]
    _handed_over(test_client, c, aid, removed=True)
    before = _dash_of(corpus_rows, rid)
    headers = as_(c, "owner")  # still owns the conversation, nothing of the dashboard

    calls = [
        test_client.put(f"/api/reports/{rid}/visibility/artifact", headers=headers, json={"visibility": "none"}),
        test_client.post(f"/api/reports/{rid}/publish", headers=headers),
        test_client.put(f"/api/reports/{rid}", headers=headers, json={"status": "draft"}),
        test_client.post(f"/api/reports/{rid}/schedule", headers=headers, json={
            "cron_expression": "0 7 * * *",
            "notification_subscribers": [{"type": "email", "address": "someone@elsewhere.example"}]}),
        test_client.get(f"/api/reports/{rid}/shares/artifact", headers=headers),
    ]
    assert [r.status_code for r in calls] == [403] * len(calls), [(r.status_code, r.text[:120]) for r in calls]
    after = _dash_of(corpus_rows, rid)
    assert {k: after[k] for k in ("visibility", "cron_schedule", "notification_subscribers")} == \
        {k: before[k] for k in ("visibility", "cron_schedule", "notification_subscribers")}


@pytest.mark.e2e
def test_an_editor_who_owns_the_conversation_still_uses_it(migrated_corpus, corpus_rows, test_client):
    """The previous owner, still an editor: sharing and scheduling through
    the conversation work as before (only making it public is the owner's)."""
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    aid = _dash_of(corpus_rows, rid)["id"]
    _handed_over(test_client, c, aid, removed=False)
    headers = as_(c, "owner")
    resp = test_client.put(f"/api/reports/{rid}/visibility/artifact", headers=headers, json={"visibility": "none"})
    assert resp.status_code == 200, resp.text
    assert _dash_of(corpus_rows, rid)["visibility"] == "none"
    assert test_client.get(f"/api/reports/{rid}/shares/artifact", headers=headers).status_code == 200
    resp = test_client.post(f"/api/reports/{rid}/schedule", headers=headers, json={"cron_expression": "0 7 * * *"})
    assert resp.status_code == 200, resp.text
    assert _dash_of(corpus_rows, rid)["cron_schedule"] == "0 7 * * *"
    from app.core.scheduler import scheduler
    if scheduler.get_job(f"dashboard_{aid}"):
        scheduler.remove_job(f"dashboard_{aid}")
