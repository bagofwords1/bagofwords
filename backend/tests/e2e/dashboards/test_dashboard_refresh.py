"""A published dashboard refreshes (phase 6).

Three ways, one run: the schedule (the owner's credentials at run time,
subscribers emailed on success, the owner told on failure), a reader opening
it (at most once per REFRESH_ON_VIEW_MIN_INTERVAL_SECONDS, one at a time),
and the owner's own Refresh. Each runs every copy of the live release with
the owner's credentials and writes all of the new rows at once, or none:
readers never see half a refresh, and a failed one leaves the previous rows.
The copies keep their ids. The draft's own refresh is untouched.
"""
import asyncio
import json
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

STALE = {"rows": [{"חודש": "ישן", "הכנסות": 0}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}
BROKEN = "def generate_df(ds_clients, excel_files):\n    raise ValueError('השאילתה נשברה')\n"


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _months(data) -> set:
    return {r.get("חודש") for r in (_json(data) or {}).get("rows", [])}


def _copies(corpus_rows, release):
    return _rows(corpus_rows, "SELECT q.id, q.title, s.id AS step, s.data FROM queries q JOIN steps s "
                              "ON s.id = q.default_step_id WHERE q.artifact_release_id = :r ORDER BY q.title",
                 r=release)


def _artifact(corpus_rows, aid):
    (row,) = _rows(corpus_rows, "SELECT last_run_at, last_run_status, last_run_error, cron_schedule, "
                                "refresh_on_view, notification_subscribers FROM artifacts WHERE id = :a", a=aid)
    return row


def _published(corpus, corpus_rows, test_client, key="conv_three_dash/dash_sales", visibility="internal"):
    aid = own(corpus_rows, corpus, key)
    release = publish(aid)
    assert share(test_client, corpus, aid, visibility).status_code == 200
    return aid, release


def _scheduled(aid):
    """The scheduled entrypoint as APScheduler fires it, the multi-worker
    claim won (the claim is infrastructure, not behavior)."""
    from app.services import dashboard_refresh

    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=True):
        asyncio.run(dashboard_refresh.scheduled_refresh(aid))


@pytest.fixture
def spawned(monkeypatch):
    """Background work the refresh starts (the thumbnail), not run."""
    jobs = []

    def keep(coro):
        jobs.append(coro.__qualname__)
        coro.close()

    monkeypatch.setattr("app.core.fire_and_forget.spawn", keep)
    return jobs


@pytest.fixture
def sent(monkeypatch):
    """What the refresh told people: subscribers' emails and inbox rows, and
    the owner's failure alerts."""
    from app.services import automation_alerts
    from app.services.inbox_service import inbox_service
    from app.services.notification_service import notification_service

    out = {"email": [], "inbox": [], "owner": []}

    async def email(**kwargs):
        out["email"].append(kwargs)

    async def inbox(db, **kwargs):
        out["inbox"].append(kwargs)

    async def owner(db, **kwargs):
        out["owner"].append(kwargs)

    monkeypatch.setattr(notification_service, "send_scheduled_report_results", email)
    monkeypatch.setattr(inbox_service, "notify_users", inbox)
    monkeypatch.setattr(automation_alerts, "notify_owner_of_failure", owner)
    return out


@pytest.fixture
def jobs():
    """The scheduler's dashboard jobs, removed after the test."""
    from app.core.scheduler import scheduler

    yield scheduler
    for job in scheduler.get_jobs():
        if job.id.startswith("dashboard_") or job.id.startswith("report_phase6_"):
            scheduler.remove_job(job.id)


def _settings(test_client, corpus, aid, **body):
    return test_client.put(f"/api/dashboards/{aid}/settings", json=body, headers=as_(corpus, "owner"))


# ── the schedule's settings ──────────────────────────────────────────────

@pytest.mark.e2e
def test_the_schedule_is_a_setting_of_the_published_dashboard(corpus, corpus_rows, test_client, jobs):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    early = _settings(test_client, corpus, aid, cron_schedule="0 8 * * *")
    assert early.status_code == 409 and early.json()["error_code"] == "dashboard.schedule_requires_publish"
    assert jobs.get_job(f"dashboard_{aid}") is None

    publish(aid)
    subscribers = [{"type": "user", "id": corpus.cast.id("member")}, {"type": "email", "address": "cfo@corpus.com"}]
    resp = _settings(test_client, corpus, aid, cron_schedule="0 8 * * 1-5", notification_subscribers=subscribers,
                     refresh_on_view=False)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cron_schedule"] == "0 8 * * 1-5" and body["refresh_on_view"] is False
    assert body["notification_subscribers"] == [{"type": "user", "id": corpus.cast.id("member"), "address": None},
                                                {"type": "email", "id": None, "address": "cfo@corpus.com"}]
    job = jobs.get_job(f"dashboard_{aid}")
    assert job is not None and list(job.args) == [aid]  # the dashboard only: who runs it is read at run time
    assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("hour")]) == "8"
    assert test_client.get(f"/api/dashboards/{aid}/settings",
                           headers=as_(corpus, "owner")).json()["cron_schedule"] == "0 8 * * 1-5"

    for bad_cron in ("every morning", "0 25 * * *"):  # not a cron; five fields, hour out of range
        bad = _settings(test_client, corpus, aid, cron_schedule=bad_cron)
        assert bad.status_code == 400, bad_cron
    bad_subscriber = _settings(test_client, corpus, aid, notification_subscribers=[{"type": "pigeon"}])
    assert bad_subscriber.status_code == 422

    off = _settings(test_client, corpus, aid, cron_schedule="")
    assert off.status_code == 200
    assert jobs.get_job(f"dashboard_{aid}") is None
    row = _artifact(corpus_rows, aid)
    assert row["cron_schedule"] is None and _json(row["notification_subscribers"]) is None
    assert row["refresh_on_view"] in (False, 0)  # a separate setting: unscheduling leaves it


@pytest.mark.e2e
def test_the_schedule_runs_in_the_organizations_timezone_when_it_has_one(corpus, corpus_rows, test_client, jobs):
    """As scheduled prompts do; without one, the server's clock as today."""
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    publish(aid)
    assert _settings(test_client, corpus, aid, cron_schedule="0 8 * * *").status_code == 200
    server = jobs.get_job(f"dashboard_{aid}").trigger.timezone

    (org,) = _rows(corpus_rows, "SELECT id, config FROM organization_settings WHERE organization_id = :o",
                   o=corpus.cast.org_id)
    config = {**(_json(org["config"]) or {}), "timezone": "Pacific/Auckland"}
    corpus_rows.update("organization_settings", org["id"], config=config)
    assert _settings(test_client, corpus, aid, cron_schedule="0 8 * * *").status_code == 200
    assert str(jobs.get_job(f"dashboard_{aid}").trigger.timezone) == "Pacific/Auckland"
    assert str(server) != "Pacific/Auckland"


# ── the run ──────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_refresh_replaces_every_row_at_once_and_keeps_the_ids(corpus, corpus_rows, test_client, spawned):
    aid, release = _published(corpus, corpus_rows, test_client)
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("steps", copy["step"], data=STALE)
    assert test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member")).status_code == 200
    draft_step = corpus.ids["conv_three_dash/q_sales/step"]
    corpus_rows.update("steps", draft_step, data={**STALE, "rows": [{"חודש": "טיוטה", "הכנסות": 0}]})

    resp = test_client.post(f"/api/dashboards/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success" and resp.json()["error"] is None

    (after,) = _copies(corpus_rows, release)
    assert (after["id"], after["step"]) == (copy["id"], copy["step"])
    assert _months(after["data"]) == {"ינואר", "פברואר"}
    assert _rows(corpus_rows, "SELECT id FROM step_user_results WHERE step_id = :s", s=copy["step"]) == []
    assert _months(_rows(corpus_rows, "SELECT data FROM steps WHERE id = :s", s=draft_step)[0]["data"]) == {"טיוטה"}
    row = _artifact(corpus_rows, aid)
    assert row["last_run_at"] is not None and row["last_run_status"] == "success" and row["last_run_error"] is None
    last = test_client.get(f"/api/dashboards/{aid}/publish", headers=as_(corpus, "owner")).json()["last_run"]
    assert last["status"] == "success" and last["at"]
    assert spawned == ["ThumbnailService.regenerate_for_release"]


@pytest.mark.e2e
def test_a_refresh_that_fails_changes_no_row(corpus, corpus_rows, test_client, spawned):
    aid, release = _published(corpus, corpus_rows, test_client, "options_sources/dash")
    copies = _copies(corpus_rows, release)
    assert len(copies) == 3
    for c in copies:
        corpus_rows.update("steps", c["step"], data=STALE)
    corpus_rows.update("steps", copies[-1]["step"], code=BROKEN)  # the others run first, and succeed
    assert test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member")).status_code == 200
    kept = _rows(corpus_rows, "SELECT id FROM step_user_results WHERE step_id = :s", s=copies[0]["step"])

    resp = test_client.post(f"/api/dashboards/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed" and "השאילתה נשברה" in resp.json()["error"]
    assert all(_months(c["data"]) == {"ישן"} for c in _copies(corpus_rows, release))
    assert _rows(corpus_rows, "SELECT id FROM step_user_results WHERE step_id = :s", s=copies[0]["step"]) == kept
    row = _artifact(corpus_rows, aid)
    assert row["last_run_status"] == "failed" and "השאילתה נשברה" in row["last_run_error"]
    assert row["last_run_at"] is None  # a failed run does not count as fresh
    assert spawned == []


@pytest.mark.e2e
def test_what_a_copy_loads_runs_first(corpus, corpus_rows, test_client, spawned):
    corpus_rows.update("steps", corpus.ids["load_step/q_derived/step"], code=(
        "def generate_df(ds_clients, excel_files, load_step):\n"
        "    return load_step('בסיס · ריצה')\n"))
    aid, release = _published(corpus, corpus_rows, test_client, "load_step/dash")
    by_title = {c["title"]: c for c in _copies(corpus_rows, release)}
    corpus_rows.update("queries", by_title["נגזרת"]["id"], created_at=datetime(2020, 1, 1))
    assert test_client.post(f"/api/dashboards/{aid}/refresh", headers=as_(corpus, "owner")).json()["status"] == \
        "success"
    by_title = {c["title"]: c for c in _copies(corpus_rows, release)}
    assert _months(by_title["נגזרת"]["data"]) == {"ינואר", "פברואר"}


@pytest.mark.e2e
def test_a_refresh_never_writes_a_release_that_was_replaced_meanwhile(corpus, corpus_rows, test_client,
                                                                        monkeypatch, spawned):
    from app.services.step_service import StepService

    aid, older = _published(corpus, corpus_rows, test_client)
    newer = publish(aid)
    corpus_rows.update("artifacts", aid, published_release_id=older)
    (copy,) = _copies(corpus_rows, older)
    corpus_rows.update("steps", copy["step"], data=STALE)
    run = StepService._execute_step_code

    async def published_meanwhile(self, db, step, ctx, **kwargs):
        corpus_rows.update("artifacts", aid, published_release_id=newer)
        return await run(self, db, step, ctx, **kwargs)

    monkeypatch.setattr(StepService, "_execute_step_code", published_meanwhile)
    resp = test_client.post(f"/api/dashboards/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["status"] == "skipped"
    assert _months(_copies(corpus_rows, older)[0]["data"]) == {"ישן"}


@pytest.mark.e2e
def test_only_the_owner_refreshes_and_only_a_live_release(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    path = f"/api/dashboards/{aid}/refresh"
    early = test_client.post(path, headers=as_(corpus, "owner"))
    assert early.status_code == 409 and early.json()["error_code"] == "dashboard.not_published"
    publish(aid)
    for person, code in (("anonymous", 401), ("member", 403), ("admin", 403)):
        assert test_client.post(path, headers=as_(corpus, person)).status_code == code, person
    corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id, status="publishing",
                       updated_at=datetime.utcnow())  # beating
    busy = test_client.post(path, headers=as_(corpus, "owner"))
    assert busy.status_code == 409 and busy.json()["error_code"] == "dashboard.publish_in_progress"


@pytest.mark.e2e
def test_a_personal_connection_refreshes_as_today(corpus, corpus_rows, test_client, monkeypatch, spawned):
    """A connection each person signs in to: the refresh runs with the
    owner's sign-in and clears readers' own results, as the conversation's
    refresh does."""
    from app.services.data_source_service import DataSourceService

    seen = []

    async def record(self, db, data_source, current_user=None, **kwargs):
        seen.append(str(current_user.id))
        return {}

    monkeypatch.setattr(DataSourceService, "construct_clients", record)
    aid, release = _published(corpus, corpus_rows, test_client, "user_required/dash")
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.insert("step_user_results", step_id=copy["step"], user_id=corpus.cast.id("member"),
                       organization_id=corpus.cast.org_id, status="success", data=STALE, executed_as="viewer",
                       params_fingerprint="", last_run_at=datetime(2026, 1, 1))
    assert test_client.post(f"/api/dashboards/{aid}/refresh", headers=as_(corpus, "owner")).json()["status"] == \
        "success"
    assert set(seen) == {corpus.cast.id("owner")}
    assert _rows(corpus_rows, "SELECT id FROM step_user_results WHERE step_id = :s", s=copy["step"]) == []


# ── scheduled ────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_scheduled_refresh_tells_the_subscribers(corpus, corpus_rows, test_client, spawned, sent):
    aid, release = _published(corpus, corpus_rows, test_client)
    subscribers = [{"type": "user", "id": corpus.cast.id("member")}, {"type": "email", "address": "cfo@corpus.com"}]
    corpus_rows.update("artifacts", aid, cron_schedule="0 8 * * *", notification_subscribers=subscribers)
    _scheduled(aid)
    assert _artifact(corpus_rows, aid)["last_run_status"] == "success"
    (email,) = sent["email"]
    assert email["report_url"].endswith(f"/d/{aid}") and email["subscribers"] == subscribers
    assert email["organization_id"] == corpus.cast.org_id
    (inbox,) = sent["inbox"]
    assert inbox["link"] == f"/d/{aid}" and inbox["user_ids"] == [corpus.cast.id("member")]
    assert sent["owner"] == []
    assert spawned == ["ThumbnailService.regenerate_for_release"]


@pytest.mark.e2e
def test_a_scheduled_refresh_that_fails_tells_the_owner_only(corpus, corpus_rows, test_client, spawned, sent):
    aid, release = _published(corpus, corpus_rows, test_client)
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("steps", copy["step"], code=BROKEN, data=STALE)
    corpus_rows.update("artifacts", aid, cron_schedule="0 8 * * *",
                       notification_subscribers=[{"type": "email", "address": "cfo@corpus.com"}])
    _scheduled(aid)
    assert sent["email"] == [] and sent["inbox"] == []
    (alert,) = sent["owner"]
    assert str(alert["owner"].id) == corpus.cast.id("owner") and alert["link"] == f"/d/{aid}"
    assert "השאילתה נשברה" in alert["outcome"].error_message
    assert _months(_copies(corpus_rows, release)[0]["data"]) == {"ישן"}


@pytest.mark.e2e
def test_a_schedule_runs_as_the_owner_while_they_are_in_the_organization(corpus, corpus_rows, test_client,
                                                                           spawned, sent):
    aid, release = _published(corpus, corpus_rows, test_client)
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("steps", copy["step"], data=STALE)
    corpus_rows.update("artifacts", aid, cron_schedule="0 8 * * *")
    membership = corpus.cast.people["owner"].membership_id
    assert test_client.delete(f"/api/organizations/{corpus.cast.org_id}/members/{membership}",
                              headers=as_(corpus, "admin")).status_code == 204
    _scheduled(aid)
    row = _artifact(corpus_rows, aid)
    assert row["last_run_status"] == "failed" and "owner" in row["last_run_error"]
    assert _months(_copies(corpus_rows, release)[0]["data"]) == {"ישן"}
    assert sent["email"] == []


@pytest.mark.e2e
def test_a_schedule_of_a_dashboard_that_is_gone_removes_itself(corpus, corpus_rows, test_client, jobs, sent):
    from app.services import dashboard_refresh

    aid, _ = _published(corpus, corpus_rows, test_client)
    assert _settings(test_client, corpus, aid, cron_schedule="0 8 * * *").status_code == 200
    assert jobs.get_job(f"dashboard_{aid}") is not None
    corpus_rows.update("artifacts", aid, deleted_at=datetime(2026, 3, 1))
    _scheduled(aid)
    assert jobs.get_job(f"dashboard_{aid}") is None
    assert dashboard_refresh.job_id(aid) == f"dashboard_{aid}"


@pytest.mark.e2e
def test_a_schedule_waits_for_a_publish_that_is_running(corpus, corpus_rows, test_client, spawned, sent):
    aid, release = _published(corpus, corpus_rows, test_client)
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("steps", copy["step"], data=STALE)
    corpus_rows.update("artifacts", aid, cron_schedule="0 8 * * *")
    corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id, status="publishing",
                       updated_at=datetime.utcnow())  # beating
    _scheduled(aid)
    assert _months(_copies(corpus_rows, release)[0]["data"]) == {"ישן"}
    assert _artifact(corpus_rows, aid)["last_run_status"] is None and sent["email"] == []


@pytest.mark.e2e
def test_startup_registers_exactly_the_dashboard_schedules(corpus, corpus_rows, test_client, jobs):
    """The leader rebuilds the dashboards' jobs from the table: one per
    published, scheduled dashboard; a job left for a dashboard without a
    schedule goes; the conversations' jobs are not touched. Twice gives the
    same."""
    from app.services import dashboard_refresh

    scheduled = []
    for key in ("conv_three_dash/dash_sales", "conv_three_dash/dash_stock"):
        aid = own(corpus_rows, corpus, key)
        publish(aid)
        corpus_rows.update("artifacts", aid, cron_schedule="30 6 * * *")
        scheduled.append(aid)
    unpublished = own(corpus_rows, corpus, "conv_one_dash/dash")
    corpus_rows.update("artifacts", unpublished, cron_schedule="30 6 * * *")
    stray = str(uuid.uuid4())
    jobs.add_job(dashboard_refresh.scheduled_refresh, "cron", id=f"dashboard_{stray}", args=[stray], hour=6)
    jobs.add_job(dashboard_refresh.scheduled_refresh, "cron", id="report_phase6_keep", args=[stray], hour=6)

    for _ in range(2):
        asyncio.run(dashboard_refresh.register_all_jobs())
        mine = {j.id for j in jobs.get_jobs() if j.id.startswith("dashboard_")}
        assert {f"dashboard_{a}" for a in scheduled} <= mine
        assert f"dashboard_{stray}" not in mine and f"dashboard_{unpublished}" not in mine
        assert jobs.get_job("report_phase6_keep") is not None


# ── on view ──────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_opening_the_dashboard_refreshes_it_at_most_every_interval(corpus, corpus_rows, test_client, spawned,
                                                                   sent):
    aid, release = _published(corpus, corpus_rows, test_client, visibility="public")
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("steps", copy["step"], data=STALE)
    path = f"/api/d/{aid}/refresh"
    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=True):
        first = test_client.post(path, headers=as_(corpus, "anonymous"))
        assert first.status_code == 200 and first.json()["skipped"] is False, first.text
        assert _months(_copies(corpus_rows, release)[0]["data"]) == {"ינואר", "פברואר"}
        corpus_rows.update("steps", copy["step"], data=STALE)
        again = test_client.post(path, headers=as_(corpus, "member")).json()
        assert again["skipped"] is True and again["reason"] == "fresh"
        assert _months(_copies(corpus_rows, release)[0]["data"]) == {"ישן"}
    assert spawned == [] and sent["email"] == []  # no thumbnail, no email on view

    corpus_rows.update("artifacts", aid, last_run_at=datetime(2026, 1, 1))
    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=False):
        busy = test_client.post(path, headers=as_(corpus, "member")).json()
    assert busy["skipped"] is True and busy["reason"] == "running"


@pytest.mark.e2e
def test_opening_the_dashboard_refreshes_only_when_it_may(corpus, corpus_rows, test_client):
    aid, release = _published(corpus, corpus_rows, test_client, visibility="internal")
    path = f"/api/d/{aid}/refresh"
    assert test_client.post(path, headers=as_(corpus, "anonymous")).status_code == 401
    assert test_client.post(path, headers=as_(corpus, "other_org")).status_code in (403, 404)
    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=True):
        corpus_rows.update("artifacts", aid, refresh_on_view=False)
        off = test_client.post(path, headers=as_(corpus, "member")).json()
        assert off["skipped"] is True and off["reason"] == "disabled"
        corpus_rows.update("artifacts", aid, refresh_on_view=True)
        corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id,
                           status="publishing", updated_at=datetime.utcnow())  # beating
    claims = []
    with patch("app.services.dashboard_refresh.claim_scheduled_run",
               side_effect=lambda *a, **k: claims.append(a) or True):
        busy = test_client.post(path, headers=as_(corpus, "member")).json()
    assert busy["skipped"] is True and busy["reason"] == "publishing"
    assert claims == []  # the interval's claim is not spent on a skipped view
    unpublished = own(corpus_rows, corpus, "conv_three_dash/dash_stock")
    early = test_client.post(f"/api/d/{unpublished}/refresh", headers=as_(corpus, "owner"))
    assert early.status_code == 404 and early.json()["error_code"] == "dashboard.not_published"  # as /d


@pytest.mark.e2e
def test_a_deck_does_not_refresh(corpus, corpus_rows, test_client, monkeypatch, spawned):
    """A published deck is frozen (phase 5): nothing runs, nothing changes."""
    from app.services.step_service import StepService

    async def never(*args, **kwargs):
        raise AssertionError("a published deck does not run")

    monkeypatch.setattr(StepService, "_execute_step_code", never)
    aid, _ = _published(corpus, corpus_rows, test_client, "slides_previews/deck")
    body = test_client.post(f"/api/dashboards/{aid}/refresh", headers=as_(corpus, "owner")).json()
    assert body["status"] == "skipped" and body["reason"] == "frozen"
    assert _artifact(corpus_rows, aid)["last_run_status"] is None and spawned == []


# ── review fixes ─────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_monitoring_data_a_refresh_reads_extends_the_releases_lineage(corpus, corpus_rows, test_client, spawned):
    """The refresh writes the shared rows, so what they now hold is recorded
    on the release, as a conversation's refresh records it on the
    conversation."""
    aid = own(corpus_rows, corpus, "bow_source/dash", owner="bow_scoped")
    release = publish(aid)
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("steps", copy["step"], code=(
        "def generate_df(ds_clients, excel_files):\n"
        "    return ds_clients['bow'].execute_query({'dataset': 'runs', "
        "'metrics': [{'op': 'count', 'name': 'runs'}]})\n"))
    before = _rows(corpus_rows, "SELECT source_refs FROM queries WHERE id = :q", q=copy["id"])
    assert "builtin:bow" not in str(before)
    resp = test_client.post(f"/api/dashboards/{aid}/refresh", headers=as_(corpus, "bow_scoped"))
    assert resp.json()["status"] == "success", resp.text
    after = _rows(corpus_rows, "SELECT source_refs FROM queries WHERE id = :q", q=copy["id"])
    assert "builtin:bow" in str(after)


@pytest.mark.e2e
def test_the_subscribers_email_goes_through_the_organizations_relay(corpus, corpus_rows, test_client, monkeypatch):
    from app.services.notification_service import notification_service

    routed = []

    async def send(recipients, subject, html, **kwargs):
        routed.append(kwargs.get("organization_id"))

    monkeypatch.setattr(notification_service, "_resolved_send", send)
    asyncio.run(notification_service.send_scheduled_report_results(
        report_id=str(uuid.uuid4()), report_title="מכירות", subscribers=[{"type": "email", "address": "a@b.com"}],
        report_url="http://x/d/1", organization_id=corpus.cast.org_id))
    assert routed == [corpus.cast.org_id]


@pytest.mark.e2e
def test_a_publish_during_a_failing_refresh_keeps_its_own_outcome(corpus, corpus_rows, test_client, monkeypatch,
                                                                   spawned, sent):
    from app.services.step_service import StepService

    aid, older = _published(corpus, corpus_rows, test_client)
    newer = publish(aid)
    corpus_rows.update("artifacts", aid, published_release_id=older, cron_schedule="0 8 * * *",
                       last_run_status="success")

    async def replaced_then_broke(self, db, step, ctx, **kwargs):
        corpus_rows.update("artifacts", aid, published_release_id=newer)
        raise ValueError("the old copies are gone")

    monkeypatch.setattr(StepService, "_execute_step_code", replaced_then_broke)
    _scheduled(aid)
    row = _artifact(corpus_rows, aid)
    assert row["last_run_status"] == "success" and row["last_run_error"] is None
    assert sent["owner"] == []


@pytest.mark.e2e
def test_a_publish_that_stopped_does_not_hold_refreshes_back(corpus, corpus_rows, test_client, spawned):
    aid, _ = _published(corpus, corpus_rows, test_client)
    corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id, status="publishing",
                       updated_at=datetime(2026, 1, 1))
    resp = test_client.post(f"/api/dashboards/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["status"] == "success", resp.text


@pytest.mark.e2e
def test_an_error_outside_the_queries_is_recorded_as_a_failure(corpus, corpus_rows, test_client, monkeypatch,
                                                               spawned):
    from app.services import dashboard_runs

    async def broken(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(dashboard_runs, "in_dependency_order", broken)
    aid, _ = _published(corpus, corpus_rows, test_client)
    resp = test_client.post(f"/api/dashboards/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["status"] == "failed"
    assert "the database went away" in _artifact(corpus_rows, aid)["last_run_error"]


@pytest.mark.e2e
def test_a_deck_cannot_be_scheduled(corpus, corpus_rows, test_client, jobs):
    aid = own(corpus_rows, corpus, "slides_previews/deck")
    publish(aid)
    assert _settings(test_client, corpus, aid, cron_schedule="0 8 * * *").status_code == 400
    assert jobs.get_job(f"dashboard_{aid}") is None


@pytest.mark.e2e
def test_opening_a_dashboard_whose_owner_left_does_not_refresh_it(corpus, corpus_rows, test_client):
    aid, _ = _published(corpus, corpus_rows, test_client)
    membership = corpus.cast.people["owner"].membership_id
    assert test_client.delete(f"/api/organizations/{corpus.cast.org_id}/members/{membership}",
                              headers=as_(corpus, "admin")).status_code == 204
    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=True):
        body = test_client.post(f"/api/d/{aid}/refresh", headers=as_(corpus, "member")).json()
    assert body["skipped"] is True and body["reason"] == "owner"


@pytest.mark.e2e
def test_the_owners_alert_links_to_the_dashboard(corpus, monkeypatch):
    """automation_alerts.notify_owner_of_failure takes a link for what is
    not a conversation; without one it links as before."""
    from app.services import automation_alerts
    from app.services.inbox_service import inbox_service
    from app.services.notification_service import notification_service

    links = []

    async def inbox(db, **kwargs):
        links.append(kwargs["link"])

    async def email(**kwargs):
        links.append(kwargs["link"])

    monkeypatch.setattr(inbox_service, "notify_users", inbox)
    monkeypatch.setattr(notification_service, "send_automation_failure", email)

    class Who:
        id, email = "u1", "owner@corpus.com"

    class Org:
        id, settings = corpus.cast.org_id, None

    outcome = automation_alerts.RunOutcome(ok=False, error_message="boom")
    for link, report_id in (("/d/abc", None), (None, "r1"), (None, None)):
        asyncio.run(automation_alerts.notify_owner_of_failure(
            None, kind="dashboard", automation_id="abc", automation_name="מכירות", owner=Who(), organization=Org(),
            outcome=outcome, report_id=report_id, link=link))
    assert links[0] == "/d/abc" and links[1].endswith("/d/abc")
    assert links[2] == "/reports/r1" and links[4] == "/automations"
