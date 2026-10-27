"""A conversation's old API acts on its dashboard (phase 9, approved plan
2026-10-22).

Once a conversation has a dashboard, the dashboard holds its sharing and
schedule; the conversation's old columns are the data migration's backup and
are never written again. The conversation endpoints a customer may still call
(scheduling, publishing, sharing, notifying, reading the conversation) keep
working, answer in their old shape, and act on the conversation's dashboard:
the one its old link leads to (for a schedule, the newest page when that one
is a deck). A conversation with no dashboard (widget era) is served as before.
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


def _report(corpus_rows, rid):
    return _one(corpus_rows, "SELECT * FROM reports WHERE id = :r", r=rid)


def _grants(corpus_rows, aid):
    with corpus_rows.engine.connect() as conn:
        return {(r[0], r[1]) for r in conn.execute(text(
            "SELECT user_id, group_id FROM artifact_shares WHERE artifact_id = :a"), {"a": aid})}


BACKUP = ("artifact_visibility", "status", "cron_schedule", "notification_subscribers", "shared_run_identity",
          "include_data_tab", "artifact_chat_enabled", "refresh_on_view")


def _backup(corpus_rows, rid):
    row = _report(corpus_rows, rid)
    return {k: row[k] for k in BACKUP}


# ── schedule ─────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_scheduling_the_conversation_schedules_its_dashboard(migrated_corpus, corpus_rows, test_client):
    from app.core.scheduler import scheduler

    c = migrated_corpus
    rid = c.reports["cron_job"]
    backup = _backup(corpus_rows, rid)
    subscribers = [{"type": "email", "address": "cfo@corpus.com"}]
    resp = test_client.post(f"/api/reports/{rid}/schedule", headers=as_(c, "owner"),
                            json={"cron_expression": "0 9 * * *", "notification_subscribers": subscribers})
    assert resp.status_code == 200, resp.text
    assert resp.json()["cron_schedule"] == "0 9 * * *"
    dash = _dash_of(corpus_rows, rid)
    assert dash["cron_schedule"] == "0 9 * * *"
    assert _backup(corpus_rows, rid) == backup  # the conversation's columns stay the backup
    try:
        assert scheduler.get_job(f"report_{rid}") is None  # no second, conversation-level run
        assert scheduler.get_job(f"dashboard_{dash['id']}") is not None
    finally:
        for job in (f"report_{rid}", f"dashboard_{dash['id']}"):
            if scheduler.get_job(job):
                scheduler.remove_job(job)

    resp = test_client.post(f"/api/reports/{rid}/schedule", headers=as_(c, "owner"),
                            json={"cron_expression": "None"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["cron_schedule"] is None
    assert _dash_of(corpus_rows, rid)["cron_schedule"] is None


@pytest.mark.e2e
def test_a_schedule_whose_link_shows_a_deck_goes_to_the_newest_page(migrated_corpus, corpus_rows, test_client):
    from app.core.scheduler import scheduler

    c = migrated_corpus
    rid = c.reports["conv_three_dash"]
    # The link shows the deck: the newer page is gone.
    corpus_rows.update("artifacts", c.ids["conv_three_dash/dash_stock"], deleted_at=corpus_rows.clock())
    corpus_rows.update("artifacts", c.ids["conv_three_dash/dash_stock"], legacy_report_id=None)
    corpus_rows.update("artifacts", c.ids["conv_three_dash/deck"], legacy_report_id=rid)
    resp = test_client.post(f"/api/reports/{rid}/schedule", headers=as_(c, "owner"),
                            json={"cron_expression": "0 9 * * *"})
    assert resp.status_code == 200, resp.text
    sales = _one(corpus_rows, "SELECT cron_schedule FROM artifacts WHERE id = :a", a=c.ids["conv_three_dash/dash_sales"])
    assert sales["cron_schedule"] == "0 9 * * *"
    job = f"dashboard_{c.ids['conv_three_dash/dash_sales']}"
    if scheduler.get_job(job):
        scheduler.remove_job(job)


@pytest.mark.e2e
def test_a_widget_era_conversation_is_scheduled_as_before(migrated_corpus, corpus_rows, test_client):
    from app.core.scheduler import scheduler

    c = migrated_corpus
    rid = c.reports["legacy_widgets"]
    resp = test_client.post(f"/api/reports/{rid}/schedule", headers=as_(c, "owner"),
                            json={"cron_expression": "0 9 * * *"})
    assert resp.status_code == 200, resp.text
    assert _report(corpus_rows, rid)["cron_schedule"] == "0 9 * * *"
    try:
        assert scheduler.get_job(f"report_{rid}") is not None
    finally:
        if scheduler.get_job(f"report_{rid}"):
            scheduler.remove_job(f"report_{rid}")


# ── publish / unpublish ──────────────────────────────────────────────────

@pytest.mark.e2e
def test_unpublishing_and_publishing_the_conversation_shares_its_dashboard(migrated_corpus, corpus_rows,
                                                                          test_client):
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    backup = _backup(corpus_rows, rid)
    resp = test_client.post(f"/api/reports/{rid}/publish", headers=as_(c, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft"
    dash = _dash_of(corpus_rows, rid)
    assert dash["visibility"] == "none" and dash["published_release_id"] is not None  # the release stays
    resp = test_client.post(f"/api/reports/{rid}/publish", headers=as_(c, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"
    assert _dash_of(corpus_rows, rid)["visibility"] == "public"
    # The same through PUT /reports/{id} {status}.
    resp = test_client.put(f"/api/reports/{rid}", headers=as_(c, "owner"), json={"status": "draft"})
    assert resp.status_code == 200, resp.text
    assert _dash_of(corpus_rows, rid)["visibility"] == "none"
    assert _backup(corpus_rows, rid) == backup


@pytest.mark.e2e
def test_publishing_an_unpublished_dashboard_publishes_it_and_shares_it_once_ready(migrated_corpus, corpus_rows,
                                                                                  test_client, monkeypatch):
    from app.core import fire_and_forget

    spawned = []
    monkeypatch.setattr(fire_and_forget, "spawn", lambda coro: spawned.append(coro) or coro.close())
    c = migrated_corpus
    rid = c.reports["cron_job_private"]
    assert _dash_of(corpus_rows, rid)["published_release_id"] is None
    resp = test_client.post(f"/api/reports/{rid}/publish", headers=as_(c, "owner"))
    assert resp.status_code == 200, resp.text
    dash = _dash_of(corpus_rows, rid)
    assert dash["visibility"] == "public"  # takes effect when the release is ready (/d serves only that)
    started = _one(corpus_rows, "SELECT status FROM artifact_releases WHERE artifact_id = :a", a=dash["id"])
    assert started["status"] == "publishing" and spawned


def _scoped_owner(corpus_rows, c, rid):
    """Managing a monitoring dashboard needs the monitoring scope: its owner
    has it, so what refuses is the public rule, nothing else."""
    corpus_rows.update("reports", rid, user_id=c.cast.id("bow_scoped"))
    corpus_rows.update("artifacts", _dash_of(corpus_rows, rid)["id"], owner_id=c.cast.id("bow_scoped"))


@pytest.mark.e2e
def test_a_monitoring_dashboard_is_not_made_public_through_the_conversation(migrated_corpus, corpus_rows,
                                                                          test_client):
    c = migrated_corpus
    rid = c.reports["bow_source"]
    _scoped_owner(corpus_rows, c, rid)
    resp = test_client.put(f"/api/reports/{rid}/visibility/artifact", headers=as_(c, "bow_scoped"),
                           json={"visibility": "public"})
    assert resp.status_code == 403, resp.text
    assert _dash_of(corpus_rows, rid)["visibility"] != "public"


@pytest.mark.e2e
def test_an_unpublished_monitoring_dashboard_is_not_made_public_through_the_conversation(
        migrated_corpus, corpus_rows, test_client):
    """With no release to read the source from, the conversation's is read."""
    c = migrated_corpus
    rid = c.reports["bow_source"]
    did = _dash_of(corpus_rows, rid)["id"]
    _scoped_owner(corpus_rows, c, rid)
    corpus_rows.update("artifacts", did, published_release_id=None, visibility="none")
    resp = test_client.put(f"/api/reports/{rid}/visibility/artifact", headers=as_(c, "bow_scoped"),
                           json={"visibility": "public"})
    assert resp.status_code == 403, resp.text
    assert _dash_of(corpus_rows, rid)["visibility"] != "public"


# ── sharing and settings ─────────────────────────────────────────────────

@pytest.mark.e2e
def test_sharing_the_conversation_shares_its_dashboard(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    backup = _backup(corpus_rows, rid)
    resp = test_client.put(f"/api/reports/{rid}/visibility/artifact", headers=as_(c, "owner"), json={
        "visibility": "shared", "shared_user_ids": [c.cast.id("direct_share")],
        "include_data_tab": False, "artifact_chat_enabled": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["visibility"] == "shared" and body["shared_user_ids"] == [c.cast.id("direct_share")]
    assert body["include_data_tab"] is False and body["artifact_chat_enabled"] is True
    dash = _dash_of(corpus_rows, rid)
    assert dash["visibility"] == "shared" and not dash["include_data_tab"] and dash["chat_enabled"]
    assert _grants(corpus_rows, dash["id"]) == {(c.cast.id("direct_share"), None)}
    assert _backup(corpus_rows, rid) == backup
    # Read back through the conversation.
    shares = test_client.get(f"/api/reports/{rid}/shares/artifact", headers=as_(c, "owner")).json()
    assert [s.get("user_id") for s in shares if s.get("user_id")] == [c.cast.id("direct_share")]
    report = test_client.get(f"/api/reports/{rid}", headers=as_(c, "owner")).json()
    assert report["artifact_visibility"] == "shared" and report["include_data_tab"] is False
    assert report["artifact_chat_enabled"] is True
    assert report["artifact_shared_user_ids"] == [c.cast.id("direct_share")]


@pytest.mark.e2e
def test_the_conversation_reads_its_dashboards_schedule(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    rid = c.reports["cron_job"]
    corpus_rows.update("artifacts", _dash_of(corpus_rows, rid)["id"], cron_schedule="15 7 * * *",
                       refresh_on_view=False)
    report = test_client.get(f"/api/reports/{rid}", headers=as_(c, "owner")).json()
    assert report["cron_schedule"] == "15 7 * * *" and report["refresh_on_view"] is False


# ── notify ───────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_notifying_from_the_conversation_sends_the_dashboards_link(migrated_corpus, corpus_rows, test_client,
                                                                   monkeypatch):
    from app.services import email_client_resolver
    from app.services.notification_service import notification_service

    sent = []

    async def dispatch(**kwargs):
        sent.append(kwargs)
        from app.schemas.notification_schema import NotifyResponse
        return NotifyResponse(success=True, message="sent", sent_to=kwargs["recipients"])

    async def available(*args, **kwargs):
        return True

    monkeypatch.setattr(notification_service, "dispatch", dispatch)
    monkeypatch.setattr(email_client_resolver, "is_outbound_available", available)
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    resp = test_client.post(f"/api/reports/{rid}/notify", headers=as_(c, "owner"), json={
        "type": "share_dashboard", "channels": ["email"], "recipients": ["cfo@corpus.com"],
        "share_url": f"http://localhost:3000/r/{rid}"})  # as old clients send it
    assert resp.status_code == 200, resp.text
    (mail,) = sent
    assert f"/d/{_dash_of(corpus_rows, rid)['id']}" in mail["share_url"]


# ── the editor's refresh on open ─────────────────────────────────────────

@pytest.mark.e2e
def test_the_editors_refresh_on_open_follows_the_dashboard(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    dash = _dash_of(corpus_rows, rid)
    corpus_rows.update("reports", rid, refresh_on_view=True)  # the backup says on
    corpus_rows.update("artifacts", dash["id"], refresh_on_view=False)  # the dashboard says off
    version = _one(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a", a=dash["id"])["id"]
    resp = test_client.post(f"/api/r/{rid}/rerun?artifact_id={version}", headers=as_(c, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped"] is True


@pytest.mark.e2e
def test_the_editors_refresh_on_open_follows_the_dashboard_open_in_it(migrated_corpus, corpus_rows, test_client):
    """Not the conversation's dashboard: the one the editor has open."""
    c = migrated_corpus
    rid = c.reports["conv_three_dash"]
    corpus_rows.update("artifacts", c.ids["conv_three_dash/dash_stock"], refresh_on_view=True)  # the link's
    corpus_rows.update("artifacts", c.ids["conv_three_dash/dash_sales"], refresh_on_view=False)  # the open one
    with corpus_rows.engine.connect() as conn:
        version = conn.execute(text("SELECT id FROM artifact_versions WHERE artifact_id = :a ORDER BY version DESC"),
                               {"a": c.ids["conv_three_dash/dash_sales"]}).scalars().first()
    resp = test_client.post(f"/api/r/{rid}/rerun?artifact_id={version}", headers=as_(c, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped"] is True


# ── who may call ─────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_only_the_owner_acts_through_the_conversation(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    before = _dash_of(corpus_rows, rid)
    for method, path, body in (("post", f"/api/reports/{rid}/schedule", {"cron_expression": "0 9 * * *"}),
                               ("post", f"/api/reports/{rid}/publish", None),
                               ("put", f"/api/reports/{rid}/visibility/artifact", {"visibility": "public"})):
        resp = getattr(test_client, method)(path, headers=as_(c, "member"), **({"json": body} if body else {}))
        assert resp.status_code in (403, 404), (path, resp.text)
    after = _dash_of(corpus_rows, rid)
    assert (after["visibility"], after["cron_schedule"]) == (before["visibility"], before["cron_schedule"])


# ── a conversation made since: the old API as main ───────────────────────

@pytest.mark.e2e
def test_a_conversation_made_since_answers_its_old_api_as_main(corpus, corpus_rows, test_client):
    """No link recorded (made after the upgrade): its old API writes the
    conversation as main does -- the adapter is for migrated conversations,
    whose columns are the migration's backup (approved 2026-10-22)."""
    from app.core.scheduler import scheduler
    from tests.e2e.dashboards.dashboard_setup import own

    c = corpus
    rid = c.reports["conv_one_dash"]
    aid = own(corpus_rows, c, "conv_one_dash/dash")  # a dashboard of its own, as phase 8 makes them
    try:
        resp = test_client.post(f"/api/reports/{rid}/schedule", headers=as_(c, "owner"),
                                json={"cron_expression": "0 9 * * *"})
        assert resp.status_code == 200, resp.text
        assert _report(corpus_rows, rid)["cron_schedule"] == "0 9 * * *"
        assert scheduler.get_job(f"report_{rid}") is not None
        assert _one(corpus_rows, "SELECT cron_schedule FROM artifacts WHERE id = :a", a=aid)["cron_schedule"] is None
    finally:
        if scheduler.get_job(f"report_{rid}"):
            scheduler.remove_job(f"report_{rid}")
    resp = test_client.put(f"/api/reports/{rid}/visibility/artifact", headers=as_(c, "owner"),
                           json={"visibility": "internal"})
    assert resp.status_code == 200, resp.text
    assert _report(corpus_rows, rid)["artifact_visibility"] == "internal"


# ── a migrated conversation with nothing to act on ───────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("case", ["only a deck", "its dashboard deleted"])
def test_a_migrated_conversation_with_no_dashboard_to_act_on_refuses_the_old_api(
        migrated_corpus, corpus_rows, test_client, case):
    """Its columns are the migration's backup: the old API never falls back
    to writing them (nor to a hidden conversation-level schedule)."""
    from app.core.scheduler import scheduler

    c = migrated_corpus
    if case == "only a deck":
        rid = c.reports["slides_previews"]
        calls = [("post", f"/api/reports/{rid}/schedule", {"cron_expression": "0 9 * * *"})]
    else:
        rid = c.reports["cron_job"]
        corpus_rows.update("artifacts", _dash_of(corpus_rows, rid)["id"], deleted_at=corpus_rows.clock())
        calls = [("post", f"/api/reports/{rid}/schedule", {"cron_expression": "0 9 * * *"}),
                 ("post", f"/api/reports/{rid}/publish", None),
                 ("put", f"/api/reports/{rid}", {"status": "published"}),
                 ("put", f"/api/reports/{rid}/visibility/artifact", {"visibility": "internal"})]
    backup = _backup(corpus_rows, rid)
    try:
        for method, path, body in calls:
            resp = getattr(test_client, method)(path, headers=as_(c, "owner"), **({"json": body} if body else {}))
            assert resp.status_code == 409, (path, resp.text)
            assert resp.json().get("error_code") == "dashboard.conversation_moved", resp.text
        assert _backup(corpus_rows, rid) == backup
        assert scheduler.get_job(f"report_{rid}") is None
    finally:
        if scheduler.get_job(f"report_{rid}"):
            scheduler.remove_job(f"report_{rid}")


@pytest.mark.e2e
def test_a_conversation_job_left_on_a_migrated_conversation_removes_itself(migrated_corpus, corpus_rows,
                                                                          test_client, monkeypatch):
    """One a server still on the old version made during a rolling upgrade:
    it never runs beside the dashboard's own schedule."""
    import asyncio

    from app.core.scheduler import scheduler
    from app.services.report_service import ReportService

    c = migrated_corpus
    rid = c.reports["cron_job"]
    ran = []

    async def rerun(self, *args, **kwargs):
        ran.append(args)
        return {}

    monkeypatch.setattr(ReportService, "rerun_report_steps", rerun)
    scheduler.add_job(func=ReportService().scheduled_rerun_report_steps, trigger="cron", hour=3,
                      id=f"report_{rid}", args=[rid, c.cast.id("owner"), c.cast.org_id], replace_existing=True)
    backup = _backup(corpus_rows, rid)
    try:
        asyncio.run(ReportService().scheduled_rerun_report_steps(rid, c.cast.id("owner"), c.cast.org_id))
        assert ran == []
        assert _backup(corpus_rows, rid) == backup  # the backup is not touched
        assert scheduler.get_job(f"report_{rid}") is None
    finally:
        if scheduler.get_job(f"report_{rid}"):
            scheduler.remove_job(f"report_{rid}")


@pytest.mark.e2e
def test_sharing_waits_for_a_publish_that_can_start(migrated_corpus, corpus_rows, test_client, monkeypatch):
    """The old API's publish of a dashboard with no release: when publishing
    is refused (no finished version, one already running...), nothing is
    shared and nobody is told -- the refusal is the answer."""
    from app.errors import AppError, ErrorCode
    from app.services import dashboard_publish
    from app.services.inbox_service import inbox_service

    told = []

    async def refuse(*args, **kwargs):
        raise AppError.conflict(ErrorCode.DASHBOARD_PUBLISH_IN_PROGRESS, "busy")

    async def notify(*args, **kwargs):
        told.append(kwargs)

    monkeypatch.setattr(dashboard_publish, "start_publish", refuse)
    monkeypatch.setattr(inbox_service, "notify_dashboard_share", notify)
    c = migrated_corpus
    rid = c.reports["cron_job_private"]
    before = _dash_of(corpus_rows, rid)
    resp = test_client.put(f"/api/reports/{rid}/visibility/artifact", headers=as_(c, "owner"), json={
        "visibility": "shared", "shared_user_ids": [c.cast.id("direct_share")]})
    assert resp.status_code == 409, resp.text
    after = _dash_of(corpus_rows, rid)
    assert after["visibility"] == before["visibility"] == "none"
    assert _grants(corpus_rows, after["id"]) == set() and told == []


@pytest.mark.e2e
def test_the_conversation_reads_its_schedule_where_it_is(migrated_corpus, corpus_rows, test_client):
    """The link shows a deck: the schedule lives on the newest page, and the
    conversation reads it from there."""
    from app.core.scheduler import scheduler

    c = migrated_corpus
    rid = c.reports["conv_three_dash"]
    corpus_rows.update("artifacts", c.ids["conv_three_dash/dash_stock"], deleted_at=corpus_rows.clock(),
                       legacy_report_id=None)
    corpus_rows.update("artifacts", c.ids["conv_three_dash/deck"], legacy_report_id=rid)
    try:
        assert test_client.post(f"/api/reports/{rid}/schedule", headers=as_(c, "owner"),
                                json={"cron_expression": "0 9 * * *"}).status_code == 200
        assert test_client.get(f"/api/reports/{rid}", headers=as_(c, "owner")).json()["cron_schedule"] == "0 9 * * *"
    finally:
        job = f"dashboard_{c.ids['conv_three_dash/dash_sales']}"
        if scheduler.get_job(job):
            scheduler.remove_job(job)


# ── round 6 review (approved 2026-10-22) ─────────────────────────────────

def _created(corpus_rows, aid):
    return _one(corpus_rows, "SELECT created_at FROM artifacts WHERE id = :a", a=aid)["created_at"]


def _link_shows_the_deck(corpus_rows, c):
    rid = c.reports["conv_three_dash"]
    corpus_rows.update("artifacts", c.ids["conv_three_dash/dash_stock"], legacy_report_id=None)
    corpus_rows.update("artifacts", c.ids["conv_three_dash/deck"], legacy_report_id=rid)
    pages = sorted((c.ids["conv_three_dash/dash_sales"], c.ids["conv_three_dash/dash_stock"]),
                   key=lambda a: _created(corpus_rows, a))
    return rid, pages  # oldest page first


def _drop_jobs(*aids):
    from app.core.scheduler import scheduler

    for aid in aids:
        if scheduler.get_job(f"dashboard_{aid}"):
            scheduler.remove_job(f"dashboard_{aid}")


@pytest.mark.e2e
def test_a_deck_conversations_schedule_is_read_and_changed_on_the_page_it_runs_on(
        migrated_corpus, corpus_rows, test_client):
    """The migration may have put it on a page that is not the newest made:
    the old API reads and changes that one, never schedules a second."""
    c = migrated_corpus
    rid, (older, newer) = _link_shows_the_deck(corpus_rows, c)
    corpus_rows.update("artifacts", older, cron_schedule="0 7 * * *")
    corpus_rows.update("artifacts", newer, cron_schedule=None)
    try:
        assert test_client.get(f"/api/reports/{rid}", headers=as_(c, "owner")).json()["cron_schedule"] == "0 7 * * *"
        resp = test_client.post(f"/api/reports/{rid}/schedule", headers=as_(c, "owner"),
                                json={"cron_expression": "0 9 * * *"})
        assert resp.status_code == 200, resp.text
        crons = {a: _one(corpus_rows, "SELECT cron_schedule FROM artifacts WHERE id = :a", a=a)["cron_schedule"]
                 for a in (older, newer)}
        assert crons == {older: "0 9 * * *", newer: None}
    finally:
        _drop_jobs(older, newer)


@pytest.mark.e2e
def test_a_deck_conversation_with_no_schedule_schedules_the_page_the_migration_would(
        migrated_corpus, corpus_rows, test_client):
    """No page scheduled yet: the one with the newest version, as the
    migration chooses -- not the newest made."""
    from datetime import datetime

    c = migrated_corpus
    rid, (older, newer) = _link_shows_the_deck(corpus_rows, c)
    for aid in (older, newer):
        corpus_rows.update("artifacts", aid, cron_schedule=None)
    with corpus_rows.engine.begin() as conn:
        conn.execute(text("UPDATE artifact_versions SET created_at = :t WHERE artifact_id = :a"),
                     {"t": datetime(2030, 1, 1), "a": older})
    try:
        resp = test_client.post(f"/api/reports/{rid}/schedule", headers=as_(c, "owner"),
                                json={"cron_expression": "0 9 * * *"})
        assert resp.status_code == 200, resp.text
        crons = {a: _one(corpus_rows, "SELECT cron_schedule FROM artifacts WHERE id = :a", a=a)["cron_schedule"]
                 for a in (older, newer)}
        assert crons == {older: "0 9 * * *", newer: None}
    finally:
        _drop_jobs(older, newer)


@pytest.mark.e2e
def test_its_dashboard_deleted_the_old_api_does_not_act_on_another(migrated_corpus, corpus_rows, test_client):
    """The dashboard its link led to was deleted: the old API refuses rather
    than publish or share one of its other dashboards (never shared)."""
    c = migrated_corpus
    rid = c.reports["conv_three_dash"]
    corpus_rows.update("artifacts", _dash_of(corpus_rows, rid)["id"], deleted_at=corpus_rows.clock())
    others = {a: (r["published_release_id"], r["visibility"]) for a, r in (
        (c.ids[f"conv_three_dash/{k}"], _one(corpus_rows, "SELECT * FROM artifacts WHERE id = :a",
                                             a=c.ids[f"conv_three_dash/{k}"])) for k in ("dash_sales", "deck"))}
    for method, path, body in (("post", f"/api/reports/{rid}/publish", None),
                               ("put", f"/api/reports/{rid}", {"status": "published"}),
                               ("put", f"/api/reports/{rid}/visibility/artifact", {"visibility": "public"})):
        resp = getattr(test_client, method)(path, headers=as_(c, "owner"), **({"json": body} if body else {}))
        assert resp.status_code == 409, (path, resp.text)
        assert resp.json().get("error_code") == "dashboard.conversation_moved", resp.text
    for aid, before in others.items():
        row = _one(corpus_rows, "SELECT published_release_id, visibility FROM artifacts WHERE id = :a", a=aid)
        assert (row["published_release_id"], row["visibility"]) == before


@pytest.mark.e2e
def test_a_refused_sharing_changes_none_of_the_settings_sent_with_it(migrated_corpus, corpus_rows, test_client):
    """Everything is checked before anything is saved: a refused share leaves
    whose credentials readers run with as it was (switching it drops their
    results)."""
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    before = _dash_of(corpus_rows, rid)
    resp = test_client.put(f"/api/reports/{rid}/visibility/artifact", headers=as_(c, "owner"), json={
        "visibility": "shared", "shared_user_ids": [c.cast.id("other_org")], "run_identity": "creator",
        "include_data_tab": not before["include_data_tab"]})
    assert 400 <= resp.status_code < 500, resp.text
    after = _dash_of(corpus_rows, rid)
    assert (after["shared_run_identity"], after["include_data_tab"], after["visibility"]) == \
        (before["shared_run_identity"], before["include_data_tab"], before["visibility"])


@pytest.mark.e2e
def test_a_refused_status_change_saves_nothing_else_sent_with_it(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    rid = c.reports["bow_source"]
    _scoped_owner(corpus_rows, c, rid)
    corpus_rows.update("artifacts", _dash_of(corpus_rows, rid)["id"], published_release_id=None, visibility="none")
    title = _report(corpus_rows, rid)["title"]
    resp = test_client.put(f"/api/reports/{rid}", headers=as_(c, "bow_scoped"),
                           json={"title": "A new name", "status": "published"})
    assert resp.status_code == 403, resp.text
    assert _report(corpus_rows, rid)["title"] == title


@pytest.mark.e2e
def test_only_an_editor_chooses_which_dashboard_governs_refresh_on_open(corpus, corpus_rows, test_client):
    """A conversation made since (main's rule: its own setting) -- a reader
    naming a dashboard of it does not make it refresh."""
    from tests.e2e.dashboards.dashboard_setup import own

    c = corpus
    rid = c.reports["vis_internal"]
    aid = own(corpus_rows, c, "vis_internal/dash")
    corpus_rows.update("reports", rid, refresh_on_view=False)
    corpus_rows.update("artifacts", aid, refresh_on_view=True)
    with corpus_rows.engine.connect() as conn:
        version = conn.execute(text("SELECT id FROM artifact_versions WHERE artifact_id = :a"), {"a": aid}).scalars().first()
    resp = test_client.post(f"/api/r/{rid}/rerun?artifact_id={version}", headers=as_(c, "member"))
    assert resp.status_code == 200, resp.text
    assert "not enabled" in resp.json()["message"]


@pytest.mark.e2e
def test_a_deleted_dashboard_does_not_govern_refresh_on_open(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    rid = c.reports["conv_three_dash"]
    corpus_rows.update("artifacts", c.ids["conv_three_dash/dash_stock"], refresh_on_view=False)  # the link's
    sales = c.ids["conv_three_dash/dash_sales"]
    corpus_rows.update("artifacts", sales, refresh_on_view=True, deleted_at=corpus_rows.clock())
    with corpus_rows.engine.connect() as conn:
        version = conn.execute(text("SELECT id FROM artifact_versions WHERE artifact_id = :a"), {"a": sales}).scalars().first()
    resp = test_client.post(f"/api/r/{rid}/rerun?artifact_id={version}", headers=as_(c, "owner"))
    assert resp.status_code == 200, resp.text
    assert "not enabled" in resp.json()["message"]
