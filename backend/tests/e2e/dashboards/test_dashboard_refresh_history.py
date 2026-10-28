"""A dashboard's refresh history (decided 2026-10-25).

Every refresh that ran leaves one row: when, what started it (the schedule,
someone opening the dashboard, a person's Refresh), how it went (success,
partial -- only a draft's run can be, a published one is all or nothing --
failed, or skipped with why), whose credentials it ran with, and the
error. A scheduled run that could not run says why; a view that did not
refresh (fresh, off, already running) is not a refresh and leaves nothing.

Both engines record: the published release's (dashboard_refresh) and the
draft's (the conversation's rerun). The owner and the dashboard's editors
read the history; nobody else, the organization's admin included. Rows go
after 30 days. Recording never breaks a refresh.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

STALE = {"rows": [{"חודש": "ישן", "הכנסות": 0}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}
BROKEN = "def generate_df(ds_clients, excel_files):\n    raise ValueError('השאילתה נשברה')\n"
CONV = "conv_three_dash"
DASH = f"{CONV}/dash_sales"


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _db_runs(corpus_rows, aid):
    return _rows(corpus_rows, "SELECT * FROM dashboard_refresh_runs WHERE artifact_id = :a ORDER BY started_at",
                 a=aid)


def _when(value) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _history(test_client, corpus, aid, person="owner", **params):
    return test_client.get(f"/api/artifacts/{aid}/refresh-runs", params=params, headers=as_(corpus, person))


def _runs(test_client, corpus, aid, person="owner", **params):
    resp = _history(test_client, corpus, aid, person, **params)
    assert resp.status_code == 200, resp.text
    return resp.json()["runs"]


def _copies(corpus_rows, release):
    return _rows(corpus_rows, "SELECT q.id, q.title, s.id AS step FROM queries q JOIN steps s "
                              "ON s.id = q.default_step_id WHERE q.artifact_release_id = :r ORDER BY q.title",
                 r=release)


def _draft_steps(corpus_rows, report_id):
    return {r["title"]: r["step"] for r in _rows(
        corpus_rows, "SELECT q.title, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                     "WHERE q.report_id = :r AND q.artifact_release_id IS NULL", r=report_id)}


def _published(corpus, corpus_rows, test_client, key=DASH, visibility="internal"):
    aid = own(corpus_rows, corpus, key)
    release = publish(aid)
    assert share(test_client, corpus, aid, visibility).status_code == 200
    return aid, release


def _scheduled(aid):
    from app.services import dashboard_refresh

    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=True):
        asyncio.run(dashboard_refresh.scheduled_refresh(aid))


def _grant_editor(test_client, corpus, aid, *people):
    visibility = test_client.get(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner")).json()["visibility"]
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"), json={
        "visibility": visibility, "editor_user_ids": [corpus.cast.id(p) for p in people]})
    assert resp.status_code == 200, resp.text


@pytest.fixture
def quiet(monkeypatch):
    """No thumbnails, no mail: what a refresh starts besides itself."""
    from app.services import automation_alerts
    from app.services.inbox_service import inbox_service
    from app.services.notification_service import notification_service
    from app.services.thumbnail_service import ThumbnailService

    def drop(coro):
        coro.close()

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr("app.core.fire_and_forget.spawn", drop)
    monkeypatch.setattr(ThumbnailService, "regenerate_for_report", nothing)
    for target, name in ((notification_service, "send_scheduled_report_results"),
                         (notification_service, "send_automation_failure"),
                         (inbox_service, "notify_users"), (automation_alerts, "notify_owner_of_failure")):
        monkeypatch.setattr(target, name, nothing)


# ── the published dashboard ──────────────────────────────────────────────

@pytest.mark.e2e
def test_each_way_a_published_dashboard_refreshes_leaves_one_row(corpus, corpus_rows, test_client, quiet):
    aid, _ = _published(corpus, corpus_rows, test_client)
    owner = corpus.cast.id("owner")

    assert test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner")).json()["status"] == \
        "success"
    corpus_rows.update("artifacts", aid, last_run_at=datetime(2026, 1, 1))  # stale again
    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=True):
        assert test_client.post(f"/api/d/{aid}/refresh", headers=as_(corpus, "member")).json()["skipped"] is False
    corpus_rows.update("artifacts", aid, cron_schedule="0 8 * * *")
    _scheduled(aid)

    runs = _runs(test_client, corpus, aid)
    assert [r["trigger"] for r in runs] == ["schedule", "view", "manual"]  # newest first
    for run in runs:
        assert run["status"] == "success" and run["error"] is None
        assert run["steps_total"] == run["steps_succeeded"] == 1 and run["steps_failed"] == 0
        assert run["run_as"]["id"] == owner and run["run_as"]["name"]
        assert run["started_at"] and run["finished_at"]
    # Who pressed Refresh is kept; a schedule or a reader opening it is nobody's.
    assert runs[2]["triggered_by"]["id"] == owner
    assert runs[0]["triggered_by"] is None and runs[1]["triggered_by"] is None


@pytest.mark.e2e
def test_a_failed_refresh_says_why(corpus, corpus_rows, test_client, quiet):
    aid, release = _published(corpus, corpus_rows, test_client)
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("steps", copy["step"], code=BROKEN)

    assert test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner")).json()["status"] == \
        "failed"
    (run,) = _runs(test_client, corpus, aid)
    assert run["status"] == "failed" and run["trigger"] == "manual" and run["kind"] == "published"
    # Which query, and why -- apart, so the page can show the name as a name.
    assert copy["title"] in run["error_source"] and "השאילתה נשברה" in run["error"]
    assert copy["title"] not in run["error"]
    # A published run stops at its first failure: it counted nothing.
    assert run["steps_total"] is None and run["steps_failed"] is None


@pytest.mark.e2e
def test_a_refresh_a_publish_replaced_meanwhile_is_recorded_as_skipped(corpus, corpus_rows, test_client,
                                                                       monkeypatch, quiet):
    from app.services.step_service import StepService

    aid, older = _published(corpus, corpus_rows, test_client)
    newer = publish(aid)
    corpus_rows.update("artifacts", aid, published_release_id=older)
    run = StepService._execute_step_code

    async def published_meanwhile(self, db, step, ctx, **kwargs):
        corpus_rows.update("artifacts", aid, published_release_id=newer)
        return await run(self, db, step, ctx, **kwargs)

    monkeypatch.setattr(StepService, "_execute_step_code", published_meanwhile)
    assert test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner")).json()["status"] == \
        "skipped"
    (row,) = _runs(test_client, corpus, aid)
    assert row["status"] == "skipped" and row["skip_reason"] == "replaced"


@pytest.mark.e2e
def test_opening_the_dashboard_without_a_refresh_leaves_no_row(corpus, corpus_rows, test_client, quiet):
    aid, _ = _published(corpus, corpus_rows, test_client)
    path = f"/api/d/{aid}/refresh"
    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=True):
        corpus_rows.update("artifacts", aid, last_run_at=datetime.utcnow())
        assert test_client.post(path, headers=as_(corpus, "member")).json()["reason"] == "fresh"
        corpus_rows.update("artifacts", aid, last_run_at=None, refresh_on_view=False)
        assert test_client.post(path, headers=as_(corpus, "member")).json()["reason"] == "disabled"
    corpus_rows.update("artifacts", aid, refresh_on_view=True)
    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=False):
        assert test_client.post(path, headers=as_(corpus, "member")).json()["reason"] == "running"
    assert _db_runs(corpus_rows, aid) == []


@pytest.mark.e2e
def test_a_schedule_that_could_not_run_says_why(corpus, corpus_rows, test_client, quiet):
    aid, _ = _published(corpus, corpus_rows, test_client)
    corpus_rows.update("artifacts", aid, cron_schedule="0 8 * * *")
    publishing = corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id,
                                    status="publishing", updated_at=datetime.utcnow())  # a publish is running
    _scheduled(aid)
    (busy,) = _db_runs(corpus_rows, aid)
    assert (busy["trigger"], busy["status"], busy["skip_reason"]) == ("schedule", "skipped", "publishing")

    corpus_rows.update("artifact_releases", publishing, status="failed")
    membership = corpus.cast.people["owner"].membership_id
    assert test_client.delete(f"/api/organizations/{corpus.cast.org_id}/members/{membership}",
                              headers=as_(corpus, "admin")).status_code == 204
    _scheduled(aid)
    newest = _db_runs(corpus_rows, aid)[-1]
    assert (newest["trigger"], newest["status"], newest["skip_reason"]) == ("schedule", "skipped", "no_runner")
    assert newest["run_as_user_id"] is None


# ── the draft ────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_refresh_in_the_editor_is_recorded_on_its_dashboard(corpus, corpus_rows, test_client, quiet):
    aid = own(corpus_rows, corpus, DASH)
    conv = corpus.reports[CONV]
    version = corpus.ids[f"{DASH}/v1"]

    resp = test_client.post(f"/api/reports/{conv}/rerun", params={"artifact_id": version},
                            headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["steps_succeeded"] == 1, resp.text
    (run,) = _db_runs(corpus_rows, aid)
    assert (run["trigger"], run["status"], run["report_id"]) == ("manual", "success", conv)
    assert run["release_id"] is None and run["triggered_by_user_id"] == corpus.cast.id("owner")
    # Another dashboard of the same conversation was not refreshed.
    assert _db_runs(corpus_rows, corpus.ids[f"{CONV}/dash_stock"]) == []


@pytest.mark.e2e
def test_a_draft_refresh_where_some_queries_fail_is_partial(corpus, corpus_rows, test_client, quiet):
    aid = own(corpus_rows, corpus, "options_sources/dash")
    conv = corpus.reports["options_sources"]
    steps = _draft_steps(corpus_rows, conv)
    broken_title = "אלבומים לפי ז'אנר"
    corpus_rows.update("steps", steps[broken_title], code=BROKEN)

    resp = test_client.post(f"/api/reports/{conv}/rerun",
                            params={"artifact_id": corpus.ids["options_sources/dash/v1"]},
                            headers=as_(corpus, "owner"))
    body = resp.json()
    assert body["steps_failed"] == 1 and body["steps_succeeded"] >= 1, body
    (run,) = _runs(test_client, corpus, aid)
    assert run["status"] == "partial" and run["kind"] == "draft"
    assert (run["steps_total"], run["steps_succeeded"], run["steps_failed"]) == \
        (body["steps_total"], body["steps_succeeded"], 1)
    assert run["error_source"] == broken_title and "השאילתה נשברה" in run["error"]


@pytest.mark.e2e
def test_a_draft_refresh_where_every_query_fails_is_failed(corpus, corpus_rows, test_client, quiet):
    aid = own(corpus_rows, corpus, DASH)
    conv = corpus.reports[CONV]
    corpus_rows.update("steps", _draft_steps(corpus_rows, conv)["הכנסות לפי חודש"], code=BROKEN)
    test_client.post(f"/api/reports/{conv}/rerun", params={"artifact_id": corpus.ids[f"{DASH}/v1"]},
                     headers=as_(corpus, "owner"))
    (run,) = _runs(test_client, corpus, aid)
    assert run["status"] == "failed" and "השאילתה נשברה" in run["error"]


@pytest.mark.e2e
def test_an_unpublished_dashboards_schedule_is_recorded(corpus, corpus_rows, test_client, quiet):
    aid = own(corpus_rows, corpus, DASH)
    corpus_rows.update("artifacts", aid, cron_schedule="0 8 * * *")
    _scheduled(aid)
    (run,) = _runs(test_client, corpus, aid)
    assert (run["trigger"], run["status"]) == ("schedule", "success")
    assert run["run_as"]["id"] == corpus.cast.id("owner") and run["triggered_by"] is None


@pytest.mark.e2e
def test_opening_the_draft_in_the_editor_is_recorded_as_a_view(corpus, corpus_rows, test_client, quiet):
    # A conversation with one dashboard: opening refreshes the conversation's
    # newest dashboard, which the row names (dashboard_of_rerun).
    aid = own(corpus_rows, corpus, "options_sources/dash")
    conv = corpus.reports["options_sources"]
    with patch("app.services.report_service.claim_scheduled_run", return_value=True):
        resp = test_client.post(f"/api/r/{conv}/rerun", params={"artifact_id": corpus.ids["options_sources/dash/v1"]},
                                headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["skipped"] is False, resp.text
    (run,) = _db_runs(corpus_rows, aid)
    assert (run["trigger"], run["status"], run["triggered_by_user_id"]) == ("view", "success", None), run["error"]


# ── who reads it ─────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_the_owner_and_editors_read_the_history_and_nobody_else(corpus, corpus_rows, test_client, quiet):
    aid, _ = _published(corpus, corpus_rows, test_client)
    _grant_editor(test_client, corpus, aid, "member")
    assert test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner")).status_code == 200

    assert len(_runs(test_client, corpus, aid, "owner")) == 1
    assert len(_runs(test_client, corpus, aid, "member")) == 1  # an editor
    for person, codes in (("admin", (403,)), ("direct_share", (403,)), ("anonymous", (401,)),
                          ("other_org", (403, 404))):
        assert _history(test_client, corpus, aid, person).status_code in codes, person


@pytest.mark.e2e
def test_a_dashboards_history_holds_only_its_own_runs(corpus, corpus_rows, test_client, quiet):
    aid, _ = _published(corpus, corpus_rows, test_client)
    other, _ = _published(corpus, corpus_rows, test_client, key=f"{CONV}/dash_stock")
    test_client.post(f"/api/artifacts/{other}/refresh", headers=as_(corpus, "owner"))
    assert _runs(test_client, corpus, aid) == []
    assert len(_runs(test_client, corpus, other)) == 1


@pytest.mark.e2e
def test_the_history_pages_newest_first(corpus, corpus_rows, test_client, quiet):
    aid, _ = _published(corpus, corpus_rows, test_client)
    for _ in range(3):
        assert test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner")).status_code == 200
    every = _runs(test_client, corpus, aid)
    assert len(every) == 3
    starts = [r["started_at"] for r in every]
    assert starts == sorted(starts, reverse=True)

    first = _history(test_client, corpus, aid, limit=2).json()
    assert [r["id"] for r in first["runs"]] == [r["id"] for r in every[:2]] and first["has_more"] is True
    rest = _history(test_client, corpus, aid, limit=2, before=first["next_before"]).json()
    assert [r["id"] for r in rest["runs"]] == [every[2]["id"]] and rest["has_more"] is False
    assert rest["next_before"] is None

    # The row the cursor came from is gone (the daily purge): the next page
    # still starts after it, never at the top again.
    with corpus_rows.engine.begin() as conn:
        conn.execute(text("DELETE FROM dashboard_refresh_runs WHERE id = :i"), {"i": every[1]["id"]})
    after_purge = _history(test_client, corpus, aid, limit=2, before=first["next_before"]).json()
    assert [r["id"] for r in after_purge["runs"]] == [every[2]["id"]]
    # A cursor that does not parse returns nothing, not the first page --
    # nor one with a timezone, which no page ever hands out.
    assert _history(test_client, corpus, aid, before="not-a-cursor").json()["runs"] == []
    aware = _history(test_client, corpus, aid, before="2099-01-01T00:00:00+00:00|zzzz")  # after every row
    assert aware.status_code == 200 and aware.json()["runs"] == []


@pytest.mark.e2e
def test_a_run_that_never_finished_reads_as_interrupted(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    corpus_rows.insert("dashboard_refresh_runs", artifact_id=aid, organization_id=corpus.cast.org_id,
                       trigger="schedule", status="running", steps_total=0, steps_succeeded=0, steps_failed=0,
                       started_at=datetime.utcnow() - timedelta(hours=2))
    corpus_rows.insert("dashboard_refresh_runs", artifact_id=aid, organization_id=corpus.cast.org_id,
                       trigger="manual", status="running", steps_total=0, steps_succeeded=0, steps_failed=0,
                       started_at=datetime.utcnow())
    by_trigger = {r["trigger"]: r["status"] for r in _runs(test_client, corpus, aid)}
    assert by_trigger == {"schedule": "interrupted", "manual": "running"}


# ── it never gets in the way, and it does not keep forever ───────────────

@pytest.mark.e2e
def test_recording_never_breaks_a_refresh(corpus, corpus_rows, test_client, monkeypatch, quiet):
    from app.services import dashboard_refresh_history

    async def broken(*args, **kwargs):
        raise RuntimeError("the history table is gone")

    monkeypatch.setattr(dashboard_refresh_history, "_write", broken)
    aid, _ = _published(corpus, corpus_rows, test_client)
    assert test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner")).json()["status"] == \
        "success"
    conv = corpus.reports[CONV]
    resp = test_client.post(f"/api/reports/{conv}/rerun", params={"artifact_id": corpus.ids[f"{DASH}/v1"]},
                            headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["steps_succeeded"] == 1


@pytest.mark.e2e
def test_runs_older_than_thirty_days_are_deleted(corpus, corpus_rows, test_client):
    from app.services import dashboard_refresh_history

    aid = own(corpus_rows, corpus, DASH)
    now = datetime.utcnow()
    for age in (31, 29, 0):
        corpus_rows.insert("dashboard_refresh_runs", artifact_id=aid, organization_id=corpus.cast.org_id,
                           trigger="schedule", status="success", steps_total=1, steps_succeeded=1, steps_failed=0,
                           started_at=now - timedelta(days=age), finished_at=now - timedelta(days=age))
    asyncio.run(dashboard_refresh_history.purge_old_runs())
    kept = sorted((now - _when(r["started_at"])).days for r in _db_runs(corpus_rows, aid))
    assert kept == [0, 29]


# ── what the row says (review round 1, 2026-10-25) ───────────────────────

def _conversation_source_name(corpus_rows, report_id) -> str:
    (row,) = _rows(corpus_rows, "SELECT d.name FROM data_sources d JOIN report_data_source_association a "
                                "ON a.data_source_id = d.id WHERE a.report_id = :r LIMIT 1", r=report_id)
    return row["name"]


@pytest.mark.e2e
def test_a_broken_connection_is_blamed_only_for_a_query_that_uses_it(corpus, corpus_rows, test_client, quiet,
                                                                      monkeypatch):
    from app.services.data_source_service import DataSourceService

    async def expired(self, db, data_source, **kwargs):
        raise RuntimeError("the token expired")

    monkeypatch.setattr(DataSourceService, "construct_clients", expired)
    aid = own(corpus_rows, corpus, DASH)
    conv = corpus.reports[CONV]
    step = _draft_steps(corpus_rows, conv)["הכנסות לפי חודש"]
    params = {"artifact_id": corpus.ids[f"{DASH}/v1"]}

    # The query names no connection: its own failure is the reason.
    corpus_rows.update("steps", step, code=BROKEN)
    test_client.post(f"/api/reports/{conv}/rerun", params=params, headers=as_(corpus, "owner"))
    (own_failure,) = _runs(test_client, corpus, aid)
    assert own_failure["error_source"] == "הכנסות לפי חודש" and "השאילתה נשברה" in own_failure["error"]
    assert "token expired" not in own_failure["error"]

    name = _conversation_source_name(corpus_rows, conv)
    # Its name is only a word in the query (a table, a comment), not a client
    # it addresses: still the query's own failure.
    corpus_rows.update("steps", step, code=(
        "def generate_df(ds_clients, excel_files):\n"
        f"    # SELECT * FROM {name}\n"
        "    raise ValueError('השאילתה נשברה')\n"))
    test_client.post(f"/api/reports/{conv}/rerun", params=params, headers=as_(corpus, "owner"))
    word_only = _runs(test_client, corpus, aid)[0]
    assert word_only["error_source"] == "הכנסות לפי חודש" and "השאילתה נשברה" in word_only["error"]

    # It addresses the broken connection's client: the connection is why.
    corpus_rows.update("steps", step, code=(
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients['{name}:main'].execute_query('SELECT 1')\n"))
    test_client.post(f"/api/reports/{conv}/rerun", params=params, headers=as_(corpus, "owner"))
    newest = _runs(test_client, corpus, aid)[0]
    assert newest["error_source"] == name and "token expired" in newest["error"]


@pytest.mark.e2e
def test_an_error_outside_the_queries_names_no_query(corpus, corpus_rows, test_client, monkeypatch, quiet):
    from app.services import dashboard_runs

    async def broken(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(dashboard_runs, "in_dependency_order", broken)
    aid, _ = _published(corpus, corpus_rows, test_client)
    test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner"))
    (run,) = _runs(test_client, corpus, aid)
    assert run["status"] == "failed" and run["error_source"] is None
    assert run["error"].startswith("Refresh failed") and "the database went away" in run["error"]


@pytest.mark.e2e
def test_a_skipped_run_says_neither_draft_nor_published_nor_counts(corpus, corpus_rows, test_client, quiet):
    aid, _ = _published(corpus, corpus_rows, test_client)
    corpus_rows.update("artifacts", aid, cron_schedule="0 8 * * *")
    membership = corpus.cast.people["owner"].membership_id
    assert test_client.delete(f"/api/organizations/{corpus.cast.org_id}/members/{membership}",
                              headers=as_(corpus, "admin")).status_code == 204
    _scheduled(aid)
    (row,) = _db_runs(corpus_rows, aid)
    assert row["status"] == "skipped" and row["finished_at"] is not None
    assert row["release_id"] is None and row["report_id"] is None
    assert (row["steps_total"], row["steps_succeeded"], row["steps_failed"]) == (0, 0, 0)


@pytest.mark.e2e
def test_a_skipped_run_is_written_once_and_final(corpus, corpus_rows, test_client, monkeypatch, quiet):
    """A second write that fails must not leave a skipped run reading as one
    still running (or interrupted): the row goes in final, in one write."""
    from app.services import dashboard_refresh_history

    write = dashboard_refresh_history._write
    calls = []

    async def only_the_first(work):
        calls.append(work)
        if len(calls) > 1:
            raise RuntimeError("the second write failed")
        return await write(work)

    aid, _ = _published(corpus, corpus_rows, test_client)
    corpus_rows.update("artifacts", aid, cron_schedule="0 8 * * *")
    corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id,
                       status="publishing", updated_at=datetime.utcnow())  # a publish is running
    monkeypatch.setattr(dashboard_refresh_history, "_write", only_the_first)
    _scheduled(aid)
    (row,) = _db_runs(corpus_rows, aid)
    assert (row["status"], row["skip_reason"]) == ("skipped", "publishing")


@pytest.mark.e2e
def test_a_conversation_schedule_whose_user_cannot_act_is_recorded(corpus, corpus_rows, test_client, monkeypatch,
                                                                    quiet):
    from app.services.report_service import ReportService

    aid = own(corpus_rows, corpus, "options_sources/dash")
    conv = corpus.reports["options_sources"]

    async def cannot_act(*args, **kwargs):
        return False

    monkeypatch.setattr("app.core.permission_resolver.principal_can_act_in_org", cannot_act)
    with patch("app.services.report_service.claim_scheduled_run", return_value=True):
        asyncio.run(ReportService().scheduled_rerun_report_steps(conv, corpus.cast.id("owner"),
                                                                 corpus.cast.org_id))
    (row,) = _db_runs(corpus_rows, aid)
    assert (row["trigger"], row["status"], row["skip_reason"], row["report_id"]) == \
        ("schedule", "skipped", "no_runner", conv)


@pytest.mark.e2e
def test_the_row_names_the_dashboard_the_rerun_ran(corpus, corpus_rows, test_client, quiet):
    """The rerun picks the conversation's newest dashboard by its live
    versions; the row names that same dashboard, whatever its own
    deleted_at says."""
    aid = own(corpus_rows, corpus, "options_sources/dash")
    conv = corpus.reports["options_sources"]
    corpus_rows.update("artifacts", aid, deleted_at=datetime.utcnow())
    resp = test_client.post(f"/api/reports/{conv}/rerun", headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["steps_total"] > 0, resp.text
    (row,) = _db_runs(corpus_rows, aid)
    assert row["trigger"] == "manual"


@pytest.mark.e2e
def test_a_refresh_whose_failure_could_not_be_recorded_does_not_stay_running(corpus, corpus_rows, test_client,
                                                                                monkeypatch, quiet):
    from app.services import dashboard_refresh

    async def broken(*args, **kwargs):
        raise RuntimeError("the database went away while recording")

    aid, release = _published(corpus, corpus_rows, test_client)
    (copy,) = _copies(corpus_rows, release)
    corpus_rows.update("steps", copy["step"], code=BROKEN)
    monkeypatch.setattr(dashboard_refresh, "_record_failure", broken)
    try:  # the route fails either way (raised through the client, or a 500)
        assert test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner")).status_code == 500
    except RuntimeError:
        pass
    (row,) = _db_runs(corpus_rows, aid)
    assert row["status"] == "failed" and row["finished_at"] is not None


@pytest.mark.e2e
def test_only_the_worker_that_claims_the_purge_deletes(corpus, corpus_rows, test_client):
    from app.services import dashboard_refresh_history

    aid = own(corpus_rows, corpus, DASH)
    old = datetime.utcnow() - timedelta(days=40)
    corpus_rows.insert("dashboard_refresh_runs", artifact_id=aid, organization_id=corpus.cast.org_id,
                       trigger="schedule", status="success", steps_total=1, steps_succeeded=1, steps_failed=0,
                       started_at=old, finished_at=old)
    with patch("app.core.scheduler.claim_scheduled_run", return_value=False):
        assert asyncio.run(dashboard_refresh_history.purge_old_runs()) == 0
    assert len(_db_runs(corpus_rows, aid)) == 1
