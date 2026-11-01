"""The data migration (phase 9, dashfc08): every existing dashboard as its
conversation left it.

What a customer could open before the upgrade, they open after it: the
dashboard a conversation's /r link shows becomes that link's dashboard, with
the conversation's owner, project, settings, sharing and schedule; a
conversation others could open publishes it (a release copied from the rows
it shows, nothing run). Nothing existing changes or goes; the migration only
fills the dashboard's columns and adds rows. Decisions: the phase-9 research
document (2026-10-19), numbered here as there.
"""
import contextlib
import hashlib
import importlib.util
import json
import pathlib
import pickle

import pytest
from sqlalchemy import text

from tests.fixtures.dashboard_corpus.alembic_at import PRE_MIGRATION_REVISION, alembic_pointed_at, current_revisions
from tests.fixtures.dashboard_corpus.rows import sync_test_url

MIGRATION = "dashfc08"


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _one(corpus_rows, sql, **params):
    (row,) = _rows(corpus_rows, sql, **params)
    return row


def _dash(corpus_rows, corpus, name):
    return _one(corpus_rows, "SELECT * FROM artifacts WHERE id = :a", a=corpus.ids[name])


def _release(corpus_rows, dash):
    return _one(corpus_rows, "SELECT * FROM artifact_releases WHERE id = :r", r=dash["published_release_id"])


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _copies(corpus_rows, release_id):
    return _rows(corpus_rows, "SELECT q.id, q.title, s.id AS step, s.data FROM queries q "
                              "JOIN steps s ON s.id = q.default_step_id WHERE q.artifact_release_id = :r "
                              "ORDER BY q.title", r=release_id)


def _jobs(corpus_rows):
    return {r["id"] for r in _rows(corpus_rows, "SELECT id FROM apscheduler_jobs")}


def _published(corpus_rows, corpus, name):
    dash = _dash(corpus_rows, corpus, name)
    assert dash["published_release_id"], f"{name} was not published"
    release = _release(corpus_rows, dash)
    assert release["status"] == "ready" and release["deleted_at"] is None, name
    return dash, release


def _unpublished(corpus_rows, corpus, name):
    dash = _dash(corpus_rows, corpus, name)
    assert dash["published_release_id"] is None, f"{name} was published"
    return dash


# ── every dashboard: owner, project, settings ────────────────────────────

@pytest.mark.e2e
def test_every_dashboard_gets_its_conversations_owner_and_project(migrated_corpus, corpus_rows):
    missing = _rows(corpus_rows, """
        SELECT a.id FROM artifacts a JOIN reports r ON r.id = a.report_id
        WHERE r.report_type = 'regular' AND (a.owner_id IS NULL OR a.owner_id <> r.user_id
              OR COALESCE(a.project_id, '') <> COALESCE(r.project_id, ''))""")
    assert missing == []
    # The conversation's owner, not whoever created the dashboard row.
    dash = _dash(corpus_rows, migrated_corpus, "created_by_other/dash")
    assert str(dash["owner_id"]) == migrated_corpus.cast.id("owner")
    assert str(dash["created_by"]) == migrated_corpus.cast.id("member")
    inside = _dash(corpus_rows, migrated_corpus, "in_project_private/dash")
    assert str(inside["project_id"]) == migrated_corpus.cast.projects["private"]


@pytest.mark.e2e
def test_every_setting_reaches_the_dashboard_as_it_was(migrated_corpus, corpus_rows):
    dash = _dash(corpus_rows, migrated_corpus, "settings_set/dash")
    assert dash["include_data_tab"] is False or dash["include_data_tab"] == 0  # off stays off: no data exposed
    assert dash["shared_run_identity"] == "creator"
    assert not dash["refresh_on_view"]
    assert dash["chat_enabled"] and dash["chat_model_id"] == "org-default"
    _, release = _published(corpus_rows, migrated_corpus, "settings_set/dash")
    assert release["theme_name"] == "dark" and _json(release["theme_overrides"]) == {"accent": "#2457d6"}
    chat = _dash(corpus_rows, migrated_corpus, "chat_enabled/dash")
    assert chat["chat_enabled"] and _json(chat["chat_data_source_ids"]) == [migrated_corpus.cast.data_sources["chinook"]]
    cron = _dash(corpus_rows, migrated_corpus, "cron/dash")
    assert cron["last_run_at"] is not None


# ── which dashboard a conversation's link becomes (decisions 2-4) ────────

@pytest.mark.e2e
@pytest.mark.parametrize("source,column,target,report_key,duplicates", [
    ("report_data_source_association", "data_source_id",
     "artifact_release_data_source_association", "multi_source", 3),
    ("report_file_association", "file_id",
     "artifact_release_file_association", "embedded_files", 0),
])
def test_published_release_preserves_each_distinct_resource_link(
        corpus, corpus_rows, source, column, target, report_key, duplicates):
    report_id = corpus.reports[report_key]
    links = _rows(corpus_rows, f"SELECT {column} FROM {source} WHERE report_id = :r", r=report_id)
    expected = {row[column] for row in links}
    assert len(expected) > 1  # deduplication must also preserve different resources
    # Legacy data-source associations permit repeated links; today's API
    # cannot reliably produce that state, so seed it before upgrading.
    # File associations already have a composite primary key in Alembic.
    for row in links:
        for _ in range(duplicates):
            corpus_rows.insert(source, report_id=report_id, **row)
    before = _rows(corpus_rows, f"SELECT * FROM {source} WHERE report_id = :r", r=report_id)

    _upgrade(MIGRATION)

    _, release = _published(corpus_rows, corpus, f"{report_key}/dash")
    copied = _rows(corpus_rows, f"SELECT {column} FROM {target} WHERE artifact_release_id = :r",
                   r=release["id"])
    assert {row[column] for row in copied} == expected
    assert len(copied) == len(expected)
    after = _rows(corpus_rows, f"SELECT * FROM {source} WHERE report_id = :r", r=report_id)
    assert sorted(map(str, after)) == sorted(map(str, before))


@pytest.mark.e2e
def test_the_shared_conversations_shown_dashboard_is_published_from_what_it_shows(migrated_corpus, corpus_rows):
    c = migrated_corpus
    dash, release = _published(corpus_rows, c, "vis_internal/dash")
    assert dash["visibility"] == "internal" and str(dash["legacy_report_id"]) == c.reports["vis_internal"]
    assert str(release["source_version_id"]) == c.ids["vis_internal/dash/v1"]
    assert str(release["published_by"]) == c.cast.id("owner")
    (copy,) = _copies(corpus_rows, release["id"])
    draft = _one(corpus_rows, "SELECT data FROM steps WHERE id = :s", s=c.ids["vis_internal/q_sales/step"])
    assert copy["data"] == draft["data"], "the copy holds exactly the rows the link showed"
    # The draft is untouched and still the conversation's.
    assert _one(corpus_rows, "SELECT artifact_release_id FROM queries WHERE id = :q",
                q=c.ids["vis_internal/q_sales"])["artifact_release_id"] is None


@pytest.mark.e2e
def test_only_the_dashboard_the_link_shows_is_published(migrated_corpus, corpus_rows):
    c = migrated_corpus
    # /r shows the newest dashboard or deck, never a doc saved after it.
    _published(corpus_rows, c, "conv_three_dash/dash_stock")
    for other in ("conv_three_dash/dash_sales", "conv_three_dash/deck"):
        dash = _unpublished(corpus_rows, c, other)
        assert dash["visibility"] == "none" and dash["legacy_report_id"] is None, other
    _published(corpus_rows, c, "doc_after_page/dash")
    _unpublished(corpus_rows, c, "doc_after_page/doc")
    _published(corpus_rows, c, "doc_only/doc")  # no page: the doc is what the link shows


@pytest.mark.e2e
def test_the_newest_completed_version_is_published(migrated_corpus, corpus_rows):
    c = migrated_corpus
    for key in ("dash_newest_failed", "dash_newest_pending", "dash_newest_stopped"):
        _, release = _published(corpus_rows, c, f"{key}/dash")
        assert str(release["source_version_id"]) == c.ids[f"{key}/dash/v1"], key
    _, release = _published(corpus_rows, c, "dash_deleted_version/dash")
    assert str(release["source_version_id"]) == c.ids["dash_deleted_version/dash/v1"]


@pytest.mark.e2e
def test_old_versions_with_no_dashboard_tie_publish_only_the_newest(migrated_corpus, corpus_rows):
    c = migrated_corpus
    _published(corpus_rows, c, "artver01_chain/legacy_3")
    for older in ("artver01_chain/legacy_1", "artver01_chain/legacy_2"):
        assert _unpublished(corpus_rows, c, older)["legacy_report_id"] is None, older
    _, release = _published(corpus_rows, c, "legacy_grown/old")
    assert str(release["source_version_id"]) == c.ids["legacy_grown/old/v2"]


@pytest.mark.e2e
def test_a_private_or_project_conversation_publishes_nothing(migrated_corpus, corpus_rows):
    c = migrated_corpus
    for key in ("vis_none", "in_project_private", "in_project_org", "cron_job_private"):
        dash = _unpublished(corpus_rows, c, f"{key}/dash")
        assert dash["visibility"] == "none", key
        # The old link still leads to it (its editors open the draft).
        assert str(dash["legacy_report_id"]) == c.reports[key], key


@pytest.mark.e2e
def test_archived_conversations(migrated_corpus, corpus_rows):
    """Decision 6: shared ones stay served (published, detached as phase 7
    leaves them); private ones go with their conversation."""
    c = migrated_corpus
    for key in ("archived_public", "archived_shared", "report_deleted_at"):
        dash, _ = _published(corpus_rows, c, f"{key}/dash")
        assert dash["deleted_at"] is None, key
    for key in ("archived_private", "fork_retired"):
        assert _dash(corpus_rows, c, f"{key}/dash")["deleted_at"] is not None, key
    # The conversations themselves are left as they were.
    assert _one(corpus_rows, "SELECT status FROM reports WHERE id = :r",
                r=c.reports["archived_private"])["status"] == "archived"


@pytest.mark.e2e
def test_conversations_without_a_dashboard_are_left_as_they_are(migrated_corpus, corpus_rows):
    c = migrated_corpus
    for key in ("conv_no_dash", "shared_no_dash", "legacy_widgets", "widgets_cron"):
        assert _rows(corpus_rows, "SELECT id FROM artifacts WHERE legacy_report_id = :r", r=c.reports[key]) == [], key


# ── sharing (decision 11) ────────────────────────────────────────────────

def _grants(corpus_rows, dash_id):
    return {(str(r["user_id"]) if r["user_id"] else None, str(r["group_id"]) if r["group_id"] else None)
            for r in _rows(corpus_rows, "SELECT user_id, group_id FROM artifact_shares WHERE artifact_id = :a",
                           a=dash_id)}


@pytest.mark.e2e
def test_shares_move_to_the_dashboard_only_while_they_still_mean_something(migrated_corpus, corpus_rows):
    c = migrated_corpus
    dash, _ = _published(corpus_rows, c, "vis_shared_user/dash")
    assert dash["visibility"] == "shared" and _grants(corpus_rows, dash["id"]) == {(c.cast.id("direct_share"), None)}
    dash, _ = _published(corpus_rows, c, "vis_shared_group/dash")
    assert _grants(corpus_rows, dash["id"]) == {(None, c.cast.groups["analysts"])}
    # Someone who left, a deleted group: not copied.
    dash, _ = _published(corpus_rows, c, "shares_gone/dash")
    assert _grants(corpus_rows, dash["id"]) == {(c.cast.id("direct_share"), None)}
    # Grants left on a conversation that is no longer 'shared' open nothing later.
    dash, _ = _published(corpus_rows, c, "stale_shares/dash")
    assert dash["visibility"] == "internal" and _grants(corpus_rows, dash["id"]) == set()
    # Sharing the conversation (the chat) is not sharing its dashboard.
    assert _unpublished(corpus_rows, c, "conv_shared_user/dash")["visibility"] == "none"


@pytest.mark.e2e
def test_monitoring_access_goes_with_the_release(migrated_corpus, corpus_rows):
    _, release = _published(corpus_rows, migrated_corpus, "bow_source/dash")
    assert _json(release["bow_source_access"])["source_id"] == "builtin:bow"


# ── data, copied as it is ────────────────────────────────────────────────

@pytest.mark.e2e
def test_encrypted_rows_are_copied_byte_for_byte(migrated_corpus, corpus_rows):
    c = migrated_corpus
    _, release = _published(corpus_rows, c, "encrypted/dash")
    sealed = _one(corpus_rows, "SELECT data FROM steps WHERE id = :s", s=c.ids["encrypted/q_sealed/step"])["data"]
    assert "__bow_enc__" in str(sealed)
    copies = {r["title"]: r for r in _copies(corpus_rows, release["id"])}
    assert copies["מוצפנת"]["data"] == sealed, "re-encrypted, decrypted or lost"


@pytest.mark.e2e
def test_readers_own_results_are_copied_to_the_published_copies(migrated_corpus, corpus_rows):
    """Decision 9: a reader of a personal dashboard opens it after the upgrade
    on the result they had, without running again."""
    c = migrated_corpus
    for key, qkey in (("user_required", "q_sales"), ("run_viewer_results", "q_sales"), ("encrypted", "q_sealed")):
        _, release = _published(corpus_rows, c, f"{key}/dash")
        before = _rows(corpus_rows, "SELECT user_id, params_fingerprint, data, status FROM step_user_results "
                                    "WHERE step_id = :s ORDER BY user_id, params_fingerprint",
                       s=c.ids[f"{key}/{qkey}/step"])
        copy = next(r for r in _copies(corpus_rows, release["id"]) if r["title"] == _one(
            corpus_rows, "SELECT title FROM queries WHERE id = :q", q=c.ids[f"{key}/{qkey}"])["title"])
        after = _rows(corpus_rows, "SELECT user_id, params_fingerprint, data, status FROM step_user_results "
                                   "WHERE step_id = :s ORDER BY user_id, params_fingerprint", s=copy["step"])
        assert before and after == before, key


# ── schedules (decisions 7, 8) ───────────────────────────────────────────

@pytest.mark.e2e
def test_a_conversations_schedule_moves_to_its_dashboard_and_its_old_job_goes(migrated_corpus, corpus_rows):
    c = migrated_corpus
    dash = _dash(corpus_rows, c, "cron_job/dash")
    assert dash["cron_schedule"] == "0 8 * * *"
    assert _json(dash["notification_subscribers"]) == [{"type": "user", "id": c.cast.id("member")}]
    assert _dash(corpus_rows, c, "cron_job_private/dash")["cron_schedule"] == "0 9 * * *"
    assert _dash(corpus_rows, c, "cron_job_by_admin/dash")["cron_schedule"] == "30 6 * * 1"
    for key in ("cron_legacy_empty", "cron_legacy_none", "vis_internal"):
        assert _dash(corpus_rows, c, f"{key}/dash")["cron_schedule"] is None, key
    jobs = _jobs(corpus_rows)
    for key in ("cron_job", "cron_job_by_admin", "cron_job_private", "cron_job_orphan", "widgets_cron"):
        assert f"report_{c.reports[key]}" not in jobs, f"{key}'s old job would run beside the dashboard's"
    # Decision 7: the widget-era schedule is removed with its job.
    assert _one(corpus_rows, "SELECT cron_schedule FROM reports WHERE id = :r",
                r=c.reports["widgets_cron"])["cron_schedule"] is None
    # A moved schedule stays on its conversation too (decision 14), unread.
    assert _one(corpus_rows, "SELECT cron_schedule FROM reports WHERE id = :r",
                r=c.reports["cron_job"])["cron_schedule"]


# ── viewer chats ─────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_readers_chats_point_at_the_dashboard(migrated_corpus, corpus_rows):
    c = migrated_corpus
    dash, _ = _published(corpus_rows, c, "chat_enabled/dash")
    for viewer in ("chat_viewer", "member"):
        thread = _one(corpus_rows, "SELECT source_artifact_id FROM reports WHERE id = :r",
                      r=c.reports[f"chat_enabled/thread_{viewer}"])
        assert str(thread["source_artifact_id"]) == str(dash["id"]), viewer


# ── nothing existing changes; downgrade; re-run ──────────────────────────

# Rows the migration must leave exactly as they were (the columns it fills
# on artifacts and reports.source_artifact_id are compared apart).
UNTOUCHED = {
    "reports": "SELECT * FROM reports",
    "report_shares": "SELECT * FROM report_shares",
    "artifact_versions": "SELECT * FROM artifact_versions",
    "conversation_queries": "SELECT * FROM queries WHERE artifact_release_id IS NULL",
    "steps": "SELECT s.* FROM steps s LEFT JOIN queries q ON q.id = s.query_id "
             "WHERE q.artifact_release_id IS NULL OR q.id IS NULL",
    "visualizations": "SELECT * FROM visualizations WHERE artifact_release_id IS NULL",
    "step_user_results": "SELECT u.* FROM step_user_results u JOIN steps s ON s.id = u.step_id "
                         "LEFT JOIN queries q ON q.id = s.query_id WHERE q.artifact_release_id IS NULL",
    "artifacts_before_columns": "SELECT id, report_id, organization_id, created_by, mode, title, created_at "
                                "FROM artifacts",
}


def _digest(corpus_rows, sql, drop=()):
    rows = _rows(corpus_rows, sql)
    for r in rows:
        for column in drop:
            r.pop(column, None)
        for column, value in r.items():  # Postgres hands bytea back as a memoryview
            if isinstance(value, (bytes, memoryview)):
                r[column] = bytes(value).hex()
    blob = json.dumps(sorted(json.dumps(r, sort_keys=True, default=str) for r in rows))
    return hashlib.sha256(blob.encode()).hexdigest(), len(rows)


def _snapshot(corpus_rows):
    out = {name: _digest(corpus_rows, sql, drop=("source_artifact_id", "updated_at", "cron_schedule"))
           for name, sql in UNTOUCHED.items()}
    out["artifacts_all"] = _digest(corpus_rows, "SELECT * FROM artifacts")
    out["jobs"] = _digest(corpus_rows, "SELECT id, job_state FROM apscheduler_jobs")
    out["schedules"] = {str(r["id"]): r["cron_schedule"] for r in _rows(
        corpus_rows, "SELECT id, cron_schedule FROM reports WHERE cron_schedule IS NOT NULL")}
    # Readers' chats point at their dashboard after the upgrade; the
    # downgrade puts them back as they were.
    out["chats"] = {str(r["id"]): r["source_artifact_id"] for r in _rows(
        corpus_rows, "SELECT id, source_artifact_id FROM reports WHERE report_type = 'artifact_chat'")}
    # What the migration recorded (a second run adds none).
    with corpus_rows.engine.connect() as conn:
        from sqlalchemy import inspect
        out["undo"] = (_digest(corpus_rows, "SELECT table_name, row_id, action FROM dashfc08_undo")
                       if "dashfc08_undo" in inspect(conn).get_table_names() else None)
    return out


def _upgrade(target):
    from alembic import command

    with alembic_pointed_at(sync_test_url()) as cfg:
        command.upgrade(cfg, target)


def _downgrade(target):
    from alembic import command

    with alembic_pointed_at(sync_test_url()) as cfg:
        command.downgrade(cfg, target)


@pytest.mark.e2e
def test_the_migration_changes_no_existing_row_and_downgrades_exactly(corpus, corpus_rows):
    url = sync_test_url()
    assert current_revisions(url) == (PRE_MIGRATION_REVISION,)
    before = _snapshot(corpus_rows)
    _upgrade(MIGRATION)
    after = _snapshot(corpus_rows)
    for name in UNTOUCHED:
        assert after[name] == before[name], f"the migration changed {name}"
    # Only the widget-era schedule it removed (decision 7) leaves its conversation.
    gone = {rid for rid in before["schedules"] if rid not in after["schedules"]}
    assert len(gone) == 1 and {k: v for k, v in before["schedules"].items() if k not in gone} == after["schedules"]
    assert after["artifacts_all"] != before["artifacts_all"]  # it did fill the dashboards
    _downgrade(PRE_MIGRATION_REVISION)
    restored = _snapshot(corpus_rows)
    assert restored == before, f"the downgrade did not restore: {sorted(k for k in before if restored[k] != before[k])}"
    assert _rows(corpus_rows, "SELECT id FROM artifact_releases") == []


def _migration_module():
    path = next(pathlib.Path("alembic/versions").glob(f"{MIGRATION}_*.py"))
    spec = importlib.util.spec_from_file_location(MIGRATION, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.e2e
def test_running_the_migration_again_adds_nothing(migrated_corpus, corpus_rows):
    """Run again over a migrated database (a second upgrade attempt): no row
    is added or changed, the undo record included. (A run cannot stop
    halfway: it is one transaction.)"""
    from sqlalchemy import create_engine

    first = _snapshot(corpus_rows)
    engine = create_engine(sync_test_url())
    try:
        with engine.begin() as conn:
            _migration_module().migrate(conn)
    finally:
        engine.dispose()
    assert _snapshot(corpus_rows) == first


@pytest.mark.e2e
def test_the_migration_reports_what_it_did(migrated_corpus, corpus_rows, caplog):
    """The counts support reads from a customer's log (decision 12)."""
    from sqlalchemy import create_engine

    _downgrade(PRE_MIGRATION_REVISION)
    engine = create_engine(sync_test_url())
    try:
        with engine.begin() as conn:
            report = _migration_module().migrate(conn)
    finally:
        engine.dispose()
    for key in ("published", "drafts", "deleted", "shares_copied", "shares_dropped", "schedules_moved",
                "schedules_removed", "jobs_removed", "widget_schedules_removed", "reader_results_copied"):
        assert key in report, key
    assert report["shares_dropped"] == 2  # the departed user and the deleted group
    assert report["widget_schedules_removed"] == 1
    # The counts are what the database now holds: published dashboards, and
    # live unpublished ones (drafts) -- not ones that were already deleted.
    count = lambda sql: _one(corpus_rows, sql)["n"]  # noqa: E731
    assert report["published"] == count("SELECT count(*) AS n FROM artifacts WHERE published_release_id IS NOT NULL")
    assert report["drafts"] == count("SELECT count(*) AS n FROM artifacts WHERE deleted_at IS NULL "
                                     "AND published_release_id IS NULL AND owner_id IS NOT NULL")
    scheduled_by_other = report["schedules_set_by_someone_else"]
    assert migrated_corpus.reports["cron_job_by_admin"] in scheduled_by_other
    # Set by the conversation's owner: not someone else.
    assert migrated_corpus.reports["cron_job"] not in scheduled_by_other
    assert migrated_corpus.reports["cron_job_private"] not in scheduled_by_other
    # The job states are APScheduler's pickles; the migration reads them
    # without importing the app.
    assert pickle.dumps  # noqa: B018 -- documents the dependency


# ── dashboards made by the new code (a database that ran a build between) ──

@pytest.mark.e2e
def test_a_dashboard_the_new_code_made_is_left_exactly_as_it_is(corpus, corpus_rows):
    """Phases 8 and 9 ship together, so a customer has none; a database that
    ran a build in between (ours) does. Such a dashboard already has its
    owner, settings and release: the migration leaves it as it is, and
    chooses among the conversation's old dashboards only."""
    from tests.e2e.dashboards.dashboard_setup import own, publish

    c = corpus
    # conv_three_dash (shared with the organization): dash_stock, the newest,
    # was made and published by the new code, with settings of its own.
    stock = own(corpus_rows, c, "conv_three_dash/dash_stock")
    corpus_rows.update("artifacts", stock, include_data_tab=False, chat_enabled=True, refresh_on_view=False)
    release = publish(stock)
    before = _rows(corpus_rows, "SELECT * FROM artifacts WHERE id = :a", a=stock)

    _upgrade(MIGRATION)
    assert _rows(corpus_rows, "SELECT * FROM artifacts WHERE id = :a", a=stock) == before
    assert [r["id"] for r in _rows(corpus_rows, "SELECT id FROM artifact_releases WHERE artifact_id = :a",
                                   a=stock)] == [release]
    # The link goes to what /r showed among the old ones: the deck, newer
    # than dash_sales.
    deck = _dash(corpus_rows, c, "conv_three_dash/deck")
    assert deck["legacy_report_id"] == c.reports["conv_three_dash"]
    assert deck["published_release_id"] is not None

    # And it stays so on a second run, and the downgrade leaves it too.
    from sqlalchemy import create_engine

    engine = create_engine(sync_test_url())
    try:
        with engine.begin() as conn:
            _migration_module().migrate(conn)
    finally:
        engine.dispose()
    assert _rows(corpus_rows, "SELECT * FROM artifacts WHERE id = :a", a=stock) == before
    _downgrade(PRE_MIGRATION_REVISION)
    assert _rows(corpus_rows, "SELECT * FROM artifacts WHERE id = :a", a=stock) == before


@pytest.mark.e2e
def test_reading_a_conversations_job_imports_no_app_code():
    """A job's state names the service its method belongs to; the migration
    reads who it runs as without importing it (decision 12)."""
    import sys
    import types

    module = _migration_module()
    gone = types.ModuleType("dashfc08_gone_module")

    class Service:
        pass

    Service.__module__, Service.__qualname__ = gone.__name__, "Service"
    gone.Service = Service
    sys.modules[gone.__name__] = gone
    try:
        raw = pickle.dumps({"args": (Service(), "rid-1", "user-1", "org-1"), "kwargs": {}})
    finally:
        del sys.modules[gone.__name__]
    assert module._scheduled_by(raw, "rid-1") == "user-1"
    assert module._scheduled_by(memoryview(raw), "rid-1") == "user-1"  # as Postgres hands it back
    assert module._scheduled_by(b"not a pickle", "rid-1") is None
    assert module._scheduled_by(raw, "another") is None


@pytest.mark.e2e
def test_a_schedule_whose_dashboards_were_all_deleted_is_removed(corpus, corpus_rows):
    """Decision 7 for a conversation with no live dashboard (all deleted):
    its job goes, so its schedule goes too -- nothing says it still runs."""
    c = corpus
    corpus_rows.update("artifacts", c.ids["cron_job_private/dash"], deleted_at=corpus_rows.clock())
    _upgrade(MIGRATION)
    rid = c.reports["cron_job_private"]
    assert _one(corpus_rows, "SELECT cron_schedule FROM reports WHERE id = :r", r=rid)["cron_schedule"] is None
    assert f"report_{rid}" not in _jobs(corpus_rows)
    _downgrade(PRE_MIGRATION_REVISION)
    assert _one(corpus_rows, "SELECT cron_schedule FROM reports WHERE id = :r", r=rid)["cron_schedule"] == "0 9 * * *"


@pytest.mark.e2e
def test_a_monitoring_dashboards_release_gets_no_thumbnail(corpus, corpus_rows):
    """As Publish draws none (phase 5): the thumbnail path is served without
    a user, and a monitoring dashboard's rows are not everyone's."""
    module = _migration_module()
    c = corpus
    # One identity's rows (2026-10-21 review), as Publish draws none for them:
    # a personal connection read as the viewer, row-level security, and a
    # query with an identity parameter.
    corpus_rows.update("queries", c.ids["thumbnail/q_sales"],
                       parameters=[{"name": "UserEmail", "type": "string", "source": "identity"}])
    corpus_rows.update("reports", c.reports["thumbnail"], artifact_visibility="internal")
    # A conversation with no source of its own (chat-made): it ran on the
    # organization's, and the organization has a personal one.
    with corpus_rows.engine.begin() as conn:
        conn.execute(text("DELETE FROM report_data_source_association WHERE report_id = :r"),
                     {"r": c.reports["vis_internal"]})
    pictures = {}
    for key in ("bow_source", "multi_source", "vis_internal", "user_required", "rls", "thumbnail"):
        (version,) = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a",
                           a=c.ids[f"{key}/dash"])
        path = module.UPLOADS / "thumbnails" / f"{version['id']}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG owner's rows")
        corpus_rows.update("artifact_versions", version["id"], thumbnail_path=f"thumbnails/{version['id']}.png")
        pictures[key] = path
    try:
        _upgrade(MIGRATION)
        # vis_internal has no source of its own: it ran on the organization's,
        # one of which is personal -- as Publish judges it (all of them).
        for key in ("bow_source", "user_required", "rls", "thumbnail", "vis_internal"):
            assert _release(corpus_rows, _dash(corpus_rows, c, f"{key}/dash"))["thumbnail_path"] is None, key
        copied = _release(corpus_rows, _dash(corpus_rows, c, "multi_source/dash"))["thumbnail_path"]
        assert copied and copied.startswith("thumbnails/release-")
    finally:
        for path in pictures.values():
            path.unlink(missing_ok=True)
        for leftover in (module.UPLOADS / "thumbnails").glob("release-*.png"):
            with contextlib.suppress(OSError):  # other tests remove theirs meanwhile
                if leftover.read_bytes() == b"\x89PNG owner's rows":
                    leftover.unlink()


@pytest.mark.e2e
def test_a_migration_that_fails_leaves_no_copied_files(corpus, corpus_rows, monkeypatch):
    """The database rolls back; the files it copied for releases that no
    longer exist go too (a retry would otherwise add another set)."""
    from sqlalchemy import create_engine

    module = _migration_module()
    (version,) = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a",
                       a=corpus.ids["multi_source/dash"])
    source = module.UPLOADS / "thumbnails" / f"{version['id']}.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"\x89PNG rows")
    corpus_rows.update("artifact_versions", version["id"], thumbnail_path=f"thumbnails/{version['id']}.png")
    written = []
    real_copy = module.Migration._copy_file

    def copy_and_note(self, src, target, relative):
        out = real_copy(self, src, target, relative)
        if out:
            written.append(target)
        return out

    def broken(self, *args, **kwargs):
        raise RuntimeError("the upgrade stopped")

    monkeypatch.setattr(module.Migration, "_copy_file", copy_and_note)
    monkeypatch.setattr(module.Migration, "_jobs", broken)
    engine = create_engine(sync_test_url())
    try:
        with pytest.raises(RuntimeError):
            with engine.begin() as conn:
                module.migrate(conn)
    finally:
        engine.dispose()
        source.unlink(missing_ok=True)
    assert written, "the test copied nothing"
    assert [p for p in written if p.exists()] == []
    assert _rows(corpus_rows, "SELECT id FROM artifact_releases") == []


# ── a schedule whose link shows a deck; a deleted shared conversation ────

def _schedule_conversation(corpus_rows, corpus, key):
    """A conversation schedule as main kept it: the column and its job."""
    import sqlalchemy as sa
    from tests.fixtures.dashboard_corpus.kinds import _report_job

    corpus_rows.update("reports", corpus.reports[key], cron_schedule="0 8 * * *",
                       notification_subscribers=[{"type": "email", "address": "cfo@corpus.com"}])

    class Seeder:  # what _report_job reads
        pass

    s = Seeder()
    s.corpus, s.cast, s.rows = corpus, corpus.cast, corpus_rows
    _report_job(s, key, "owner")


@pytest.mark.e2e
def test_a_schedule_whose_link_shows_a_deck_moves_to_the_newest_page(corpus, corpus_rows):
    """A deck never refreshes: the schedule goes to the conversation's newest
    page dashboard, which main's schedule refreshed with it, and its
    subscribers keep their email (decided 2026-10-21)."""
    c = corpus
    corpus_rows.update("artifacts", c.ids["conv_three_dash/dash_stock"], deleted_at=corpus_rows.clock())
    _schedule_conversation(corpus_rows, c, "conv_three_dash")
    _upgrade(MIGRATION)
    deck, sales = _dash(corpus_rows, c, "conv_three_dash/deck"), _dash(corpus_rows, c, "conv_three_dash/dash_sales")
    assert deck["legacy_report_id"] == c.reports["conv_three_dash"]  # the link still shows the deck
    assert deck["cron_schedule"] is None
    assert sales["cron_schedule"] == "0 8 * * *"
    assert _json(sales["notification_subscribers"]) == [{"type": "email", "address": "cfo@corpus.com"}]
    assert f"report_{c.reports['conv_three_dash']}" not in _jobs(corpus_rows)


@pytest.mark.e2e
def test_a_schedule_of_a_conversation_with_only_a_deck_is_removed(corpus, corpus_rows):
    c = corpus
    _schedule_conversation(corpus_rows, c, "slides_previews")
    _upgrade(MIGRATION)
    rid = c.reports["slides_previews"]
    assert _dash(corpus_rows, c, "slides_previews/deck")["cron_schedule"] is None
    assert _one(corpus_rows, "SELECT cron_schedule FROM reports WHERE id = :r", r=rid)["cron_schedule"] is None
    assert f"report_{rid}" not in _jobs(corpus_rows)
    _downgrade(PRE_MIGRATION_REVISION)
    assert _one(corpus_rows, "SELECT cron_schedule FROM reports WHERE id = :r", r=rid)["cron_schedule"] == "0 8 * * *"


@pytest.mark.e2e
def test_a_deleted_shared_conversation_keeps_only_its_published_dashboard(corpus, corpus_rows):
    """As deleting a conversation does since phase 7: the dashboard its link
    shows is published and stays; its drafts -- reachable by no one, the
    conversation being gone -- are deleted (decided 2026-10-21)."""
    c = corpus
    corpus_rows.update("reports", c.reports["conv_three_dash"], status="archived")
    _upgrade(MIGRATION)
    stock = _dash(corpus_rows, c, "conv_three_dash/dash_stock")
    assert stock["published_release_id"] is not None and stock["deleted_at"] is None
    for other in ("dash_sales", "deck"):
        assert _dash(corpus_rows, c, f"conv_three_dash/{other}")["deleted_at"] is not None, other
    _downgrade(PRE_MIGRATION_REVISION)
    for other in ("dash_sales", "deck"):
        assert _dash(corpus_rows, c, f"conv_three_dash/{other}")["deleted_at"] is None, other


@pytest.mark.e2e
def test_the_undo_table_is_indexed_for_its_lookups(migrated_corpus, corpus_rows):
    """The migration asks the undo table, row by row, whether it already did
    something: without an index every question reads the whole table -- a
    large installation's upgrade would grow with the square of its rows."""
    sql = ("SELECT sql FROM sqlite_master WHERE tbl_name = 'dashfc08_undo' AND type = 'index'"
           if corpus_rows.engine.dialect.name == "sqlite"
           else "SELECT indexdef FROM pg_indexes WHERE tablename = 'dashfc08_undo'")
    with corpus_rows.engine.connect() as conn:
        defs = [str(r[0]) for r in conn.execute(text(sql)) if r[0]]
    assert any("table_name" in d and "row_id" in d and "action" in d for d in defs), defs


@pytest.mark.e2e
def test_an_upgrade_that_fails_after_the_migration_leaves_no_copied_files(corpus, corpus_rows):
    """One transaction runs every revision of an upgrade: a later revision
    (or the commit) failing rolls this migration back too, and its copied
    files must go with it."""
    from sqlalchemy import create_engine

    module = _migration_module()
    (version,) = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a",
                       a=corpus.ids["multi_source/dash"])
    source = module.UPLOADS / "thumbnails" / f"{version['id']}.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"\x89PNG rows")
    corpus_rows.update("artifact_versions", version["id"], thumbnail_path=f"thumbnails/{version['id']}.png")
    engine = create_engine(sync_test_url())
    copied = []
    try:
        with pytest.raises(RuntimeError):
            with engine.begin() as conn:
                module.migrate(conn)
                copied = [module.UPLOADS / str(r[0]) for r in conn.execute(text(
                    "SELECT thumbnail_path FROM artifact_releases WHERE thumbnail_path IS NOT NULL"))]
                raise RuntimeError("a later revision failed")
    finally:
        engine.dispose()
        source.unlink(missing_ok=True)
    assert copied, "the test copied nothing"
    assert [p for p in copied if p.exists()] == []


@pytest.mark.e2e
@pytest.mark.parametrize("ends", ["rolled back", "committed"])
def test_a_downgrade_leaves_the_release_files(migrated_corpus, corpus_rows, ends):
    """A downgrade never deletes files: deleting them before the database's
    commit is known to have succeeded could leave restored releases pointing
    at nothing (2026-10-22 review). Unused files with random names harm no
    one; the copies are removed by hand if wanted."""
    from sqlalchemy import create_engine

    module = _migration_module()
    paths = [module.UPLOADS / str(r["thumbnail_path"]) for r in _rows(
        corpus_rows, "SELECT thumbnail_path FROM artifact_releases WHERE thumbnail_path IS NOT NULL")]
    assert paths and all(p.exists() for p in paths), "the corpus published no picture"
    engine = create_engine(sync_test_url())
    try:
        if ends == "rolled back":
            with pytest.raises(RuntimeError):
                with engine.begin() as conn:
                    module.restore(conn)
                    raise RuntimeError("an older revision refused")
            assert len(_rows(corpus_rows, "SELECT id FROM artifact_releases")) > 0
        else:
            with engine.begin() as conn:
                module.restore(conn)
        assert all(p.exists() for p in paths)
    finally:
        engine.dispose()
        for path in paths:  # this test's own clean-up
            path.unlink(missing_ok=True)


@pytest.mark.e2e
def test_a_downgrade_after_the_app_ran_lets_go_of_what_points_at_the_copies(migrated_corpus, corpus_rows):
    """After the upgrade the app runs: readers run the released copies,
    usage is recorded on them, a schedule gets its dashboard job. The
    downgrade still restores -- what points at the copies lets go first, as
    the app's own release deletion does (Postgres enforces those keys)."""
    c = migrated_corpus
    dash = _dash(corpus_rows, c, "vis_internal/dash")
    (copy,) = _copies(corpus_rows, dash["published_release_id"])
    corpus_rows.insert("step_user_results", step_id=copy["step"], user_id=c.cast.id("member"), status="success",
                       organization_id=c.cast.org_id)
    corpus_rows.insert("table_usage_events", org_id=c.cast.org_id, step_id=copy["step"], table_fqn="t.orders",
                       source_type="sql", success=True, used_at=corpus_rows.clock())
    corpus_rows.insert("apscheduler_jobs", id=f"dashboard_{_dash(corpus_rows, c, 'cron_job/dash')['id']}",
                       next_run_time=1.0, job_state=b"x")
    message = corpus_rows.insert("completions", report_id=c.reports["vis_internal"], step_id=copy["step"])
    _downgrade(PRE_MIGRATION_REVISION)
    assert _rows(corpus_rows, "SELECT id FROM steps WHERE id = :s", s=copy["step"]) == []
    # A message that pointed at a copy stays, pointing at nothing.
    assert _rows(corpus_rows, "SELECT step_id FROM completions WHERE id = :m", m=message) == [{"step_id": None}]
    assert _rows(corpus_rows, "SELECT id FROM table_usage_events WHERE step_id = :s", s=copy["step"]) == []
    assert _rows(corpus_rows, "SELECT id FROM step_user_results WHERE step_id = :s", s=copy["step"]) == []
    # The migrated schedule's dashboard job goes with the schedule.
    assert _rows(corpus_rows, "SELECT id FROM apscheduler_jobs WHERE id LIKE 'dashboard_%'") == []


@pytest.mark.e2e
def test_a_shared_dashboard_that_cannot_be_published_stays_private(corpus, corpus_rows):
    """No finished version to publish: its sharing is not moved half-way (a
    visibility without its grants would open it to the wrong people when it
    is published later)."""
    c = corpus
    aid = c.ids["vis_shared_user/dash"]
    with corpus_rows.engine.begin() as conn:
        conn.execute(text("UPDATE artifact_versions SET status = 'failed' WHERE artifact_id = :a"), {"a": aid})
    _upgrade(MIGRATION)
    dash = _dash(corpus_rows, c, "vis_shared_user/dash")
    assert dash["published_release_id"] is None
    assert dash["visibility"] == "none"
    assert _grants(corpus_rows, aid) == set()
