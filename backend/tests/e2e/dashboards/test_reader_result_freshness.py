"""A reader's stored run of a published dashboard is served while what it read holds.

The release's step read and its filter run serve the reader's own stored
result. When the row policy of a relation its sources hold changes, that
result was filtered by the old policy: it is not served, and the filter run
executes again. (The other changes -- an extract refresh, a connection's
auth policy, the reader's groups -- are covered on the conversation's
surfaces in tests/e2e/rbac/test_viewer_result_freshness.py; both go through
the same check.)
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share
from tests.e2e.dashboards.test_dashboard_exports import no_vendor_libs, pdf_pages  # noqa: F401 (fixtures)
from tests.fixtures.identity_race import GROUPS_PARAM, group_joined_after_bind  # noqa: F401 (fixture)
from tests.fixtures.viewer_result_stamp import stamp_now

STALE = {"rows": [{"חודש": "ישן", "הכנסות": 0}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


@pytest.fixture
def unreachable_source_clients(monkeypatch):
    """The corpus source has no reachable driver: client construction (where
    the data-source drivers start, the boundary these tests stub, as
    test_dashboard_runs.py does) returns no clients. The run itself is real."""
    from app.services.data_source_service import DataSourceService

    async def none(self, db, data_source, current_user=None, **kwargs):
        return {}

    monkeypatch.setattr(DataSourceService, "construct_clients", none)


def _months(body) -> set:
    return {r.get("חודש") for r in (body.get("data") or {}).get("rows", [])}


YEAR = {"name": "Year", "type": "string", "label": "Year", "default": None, "required": False,
        "source": "input", "identity_binding": None, "options": None, "options_source": None,
        "strict_options": False}
# The copy's rows name the Year it ran with, so the values a run took show.
YEAR_CODE = ("def generate_df(ds_clients, excel_files, params=None):\n"
             "    import pandas as pd\n"
             "    return pd.DataFrame({'חודש': ['שנה ' + str((params or {}).get('Year'))], 'הכנסות': [1]})\n")


def _stored(corpus_rows, step_id, user_id) -> list:
    """Per stored result of the reader: (data is SQL NULL, applied_params).
    The NULL is asked in SQL, so neither decryption nor a JSON null hides
    rows; the values are read through the model (they may be encrypted)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.step_user_result import StepUserResult

    with Session(corpus_rows.engine) as session:
        return [(bool(cleared), params) for cleared, params in session.execute(
            select(StepUserResult.data.is_(None), StepUserResult.applied_params)
            .where(StepUserResult.step_id == step_id, StepUserResult.user_id == user_id)).all()]


@pytest.mark.e2e
def test_a_row_policy_edit_stops_serving_the_readers_stored_run(corpus, corpus_rows, test_client,
                                                                unreachable_source_clients):
    aid = own(corpus_rows, corpus, "rls/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    corpus_rows.update("steps", copy["step"], data=STALE, code=YEAR_CODE)  # tell the snapshot from a reader's run
    # A filter the reader sets (the corpus dashboard declares none; no route edits a release's copy).
    corpus_rows.update("queries", copy["id"], parameters=[YEAR])
    (relation,) = _rows(corpus_rows, "SELECT ct.id, ct.connection_id FROM connection_tables ct "
                                     "JOIN datasource_tables dt ON dt.connection_table_id = ct.id "
                                     "WHERE dt.datasource_id = :d", d=corpus.cast.data_sources["מחסן RLS"])

    step_path = f"/api/d/{aid}/queries/{copy['id']}/step"
    filter_path = f"/api/d/{aid}/queries/{copy['id']}/run"
    chosen = {"params": {"Year": "2024"}}
    first = test_client.post(filter_path, json=chosen, headers=as_(corpus, "member"))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "success" and first.json()["cached"] is False, first.text
    ran = _months(first.json())
    assert ran == {"שנה 2024"}
    assert test_client.post(filter_path, json=chosen, headers=as_(corpus, "member")).json()["cached"] is True
    assert _months(test_client.get(step_path, headers=as_(corpus, "member")).json()) == ran

    admin = as_(corpus, "admin")
    assert test_client.put("/api/organization/settings", headers=admin,
                           json={"config": {"enable_custom_queries": {"value": True}}}).status_code == 200
    off = test_client.put(f"/api/connections/{relation['connection_id']}/custom-queries/{relation['id']}/rls",
                          json={"rls_enabled": False}, headers=admin)
    assert off.status_code == 200, off.text

    # The reader's run stays as their chosen values, without the rows the
    # old policy filtered for them: those are not kept at rest.
    member = corpus.cast.id("member")
    assert _stored(corpus_rows, copy["step"], member) == [(True, {"Year": "2024"})]
    # Without the row policy every reader shares the snapshot; the cleared
    # run is not reported as a result (or an error) of theirs.
    read = test_client.get(step_path, headers=as_(corpus, "member")).json()
    assert _months(read) == {"ישן"}
    assert read.get("viewer_result") is None, read

    # Their next run of the dashboard takes the values they chose and
    # replaces the cleared result.
    rerun = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member"))
    assert rerun.status_code == 200, rerun.text
    assert _stored(corpus_rows, copy["step"], member) == [(False, {"Year": "2024"})]
    assert _months(test_client.get(step_path, headers=as_(corpus, "member")).json()) == {"שנה 2024"}
    again = test_client.post(filter_path, json=chosen, headers=as_(corpus, "member"))
    assert again.status_code == 200, again.text
    assert again.json()["cached"] is True and _months(again.json()) == {"שנה 2024"}


@pytest.mark.e2e
def test_a_readers_run_stores_results_stamped_before_an_auth_policy_switch(corpus, corpus_rows, test_client,
                                                                          monkeypatch):
    """The connection's auth policy changes right after the run built its
    clients, before it stored the copy's result (in the run's own
    transaction, as a change landing in between would look to it). What it
    stored ran under the old policy: it is not served as the reader's."""
    from sqlalchemy import update

    from app.models.connection import Connection
    from app.services.data_source_service import DataSourceService

    aid = own(corpus_rows, corpus, "rls/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    run_code = ("def generate_df(ds_clients, excel_files, params=None):\n"
                "    import pandas as pd\n"
                "    return pd.DataFrame({'חודש': ['ריצה'], 'הכנסות': [1]})\n")
    corpus_rows.update("steps", copy["step"], data=STALE, code=run_code)
    (conn,) = _rows(corpus_rows, "SELECT connection_id FROM domain_connection WHERE data_source_id = :d",
                    d=corpus.cast.data_sources["מחסן RLS"])
    switched: list = []

    async def build_then_switch(self, db, data_source, current_user=None, **kwargs):
        # No reachable driver (as unreachable_source_clients): no clients.
        if not switched:
            await db.execute(update(Connection).where(Connection.id == conn["connection_id"])
                             .values(auth_policy="user_required").execution_options(synchronize_session=False))
            switched.append(1)
        return {}

    monkeypatch.setattr(DataSourceService, "construct_clients", build_then_switch)
    rerun = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member"))
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["steps_succeeded"] >= 1, rerun.json()
    assert switched, "the policy switched during the run"
    assert _rows(corpus_rows, "SELECT COUNT(*) AS n FROM step_user_results WHERE step_id = :s AND user_id = :u "
                              "AND status = 'success'", s=copy["step"], u=corpus.cast.id("member")) == [{"n": 1}]

    read = test_client.get(f"/api/d/{aid}/queries/{copy['id']}/step", headers=as_(corpus, "member")).json()
    assert read.get("viewer_result") is None, read
    assert _months(read) != {"ריצה"}, read


@pytest.mark.e2e
def test_a_readers_run_stamped_only_after_its_clients_were_built_stores_its_result_cleared(
        corpus, corpus_rows, test_client, unreachable_source_clients, monkeypatch):
    """Every stamp before the release's clients are built fails, so each
    copy's run takes it after: the check before the store cannot cover the
    time the clients were built, and the result is stored as a change during
    the run leaves it (its values, no rows), not served."""
    import app.services.dashboard_runs as dashboard_runs

    aid = own(corpus_rows, corpus, "rls/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    run_code = ("def generate_df(ds_clients, excel_files, params=None):\n"
                "    import pandas as pd\n"
                "    return pd.DataFrame({'חודש': ['ריצה'], 'הכנסות': [1]})\n")
    corpus_rows.update("steps", copy["step"], data=STALE, code=run_code)
    stamped: list = []

    async def none_stamped(*args, **kwargs):
        stamped.append(1)
        return {}

    monkeypatch.setattr(dashboard_runs, "_copy_stamps", none_stamped)
    rerun = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member"))
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["steps_succeeded"] >= 1, rerun.json()
    assert stamped, "the run asked for its stamps before its clients"

    member = corpus.cast.id("member")
    assert [cleared for cleared, _ in _stored(corpus_rows, copy["step"], member)] == [True]
    read = test_client.get(f"/api/d/{aid}/queries/{copy['id']}/step", headers=as_(corpus, "member")).json()
    assert read.get("viewer_result") is None, read
    assert _months(read) != {"ריצה"}, read


@pytest.mark.e2e
def test_a_readers_filter_run_stamps_the_identity_its_params_were_bound_from(
        corpus, corpus_rows, test_client, unreachable_source_clients, group_joined_after_bind):
    """The reader joins a group after their filter run bound their groups
    (none) into the copy's params, before it stored the result. The release's
    step read serves the reader's latest result whatever values it ran with:
    the stored rows hold the old binding, so they are not served as theirs."""
    aid = own(corpus_rows, corpus, "rls/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    groups_code = ("def generate_df(ds_clients, excel_files, params=None):\n"
                   "    import pandas as pd\n"
                   "    names = ','.join((params or {}).get('Groups') or []) or 'אין'\n"
                   "    return pd.DataFrame({'חודש': [names], 'הכנסות': [1]})\n")
    corpus_rows.update("steps", copy["step"], data=STALE, code=groups_code)
    corpus_rows.update("queries", copy["id"], parameters=[GROUPS_PARAM])
    group = corpus_rows.insert("groups", organization_id=corpus.cast.org_id, name="מכירות")

    group_joined_after_bind.update(group=group, user=corpus.cast.id("member"))
    run = test_client.post(f"/api/d/{aid}/queries/{copy['id']}/run", json={}, headers=as_(corpus, "member"))
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "success" and _months(run.json()) == {"אין"}, run.text
    assert not group_joined_after_bind, "the reader joined the group during the run"

    read = test_client.get(f"/api/d/{aid}/queries/{copy['id']}/step", headers=as_(corpus, "member")).json()
    assert read.get("viewer_result") is None, read
    assert _months(read) != {"אין"}, read


@pytest.mark.e2e
def test_an_export_does_not_count_a_run_that_read_an_older_set_of_relations(
        corpus, corpus_rows, test_client, no_vendor_libs, pdf_pages):
    """A withheld reader exports their own run. Once the dashboard's source
    gains an accelerated relation their run never read, that run is no longer
    theirs to export: they run the dashboard first."""
    aid = own(corpus_rows, corpus, "user_required/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    reader = corpus.cast.id("has_results")
    # A reader's run of a personal connection needs the provider: the row it
    # leaves (with the stamp it stores) is written directly.
    corpus_rows.insert("step_user_results", step_id=copy["step"], user_id=reader,
                       organization_id=corpus.cast.org_id, report_id=None, status="success",
                       executed_as="viewer", params_fingerprint="",
                       data={"rows": [{"מי": "has_results"}], "columns": [{"field": "מי"}]},
                       **stamp_now(copy["step"], reader))
    path = f"/api/d/{aid}/export/html"
    assert test_client.get(path, headers=as_(corpus, "has_results")).status_code == 200

    # An accelerated relation activated on the dashboard's source. No route
    # activates one without a reachable warehouse; written as the corpus does.
    ds = corpus.cast.data_sources["מחסן אישי"]
    (conn,) = _rows(corpus_rows, "SELECT connection_id FROM domain_connection WHERE data_source_id = :d", d=ds)
    ct = corpus_rows.insert("connection_tables", connection_id=conn["connection_id"], name="הזמנות",
                            kind="bow", columns=[{"name": "מי"}], pks=[], fks=[])
    corpus_rows.insert("datasource_tables", name="הזמנות", datasource_id=ds, connection_table_id=ct,
                       is_active=True)

    resp = test_client.get(path, headers=as_(corpus, "has_results"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "dashboard.export_needs_run"


async def _counted_own_run_check(release_id: str, user_id: str) -> tuple:
    """The export's own-run check for this reader: (its answer, the
    statements it ran)."""
    from sqlalchemy import event

    from app.dependencies import async_session_maker
    from app.models.artifact_release import ArtifactRelease
    from app.models.user import User
    from app.routes.dashboard import _has_own_run

    counts = {"statements": 0}

    def statement(*args, **kwargs):
        counts["statements"] += 1

    async with async_session_maker() as db:
        release = await db.get(ArtifactRelease, release_id)
        user = await db.get(User, user_id)
        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", statement)
        try:
            answer = await _has_own_run(db, release, user)
        finally:
            event.remove(engine, "before_cursor_execute", statement)
    return answer, counts["statements"]


@pytest.mark.e2e
def test_the_exports_own_run_check_judges_many_slices_of_one_copy_at_the_cost_of_one(
        corpus, corpus_rows, test_client, record_property):
    """A withheld reader holds many stored slices (parameter sets) of one
    copy. What the copy's run reads now is stamped once for all of them, not
    once per slice: ten times the slices take no more statements."""
    import asyncio

    aid = own(corpus_rows, corpus, "user_required/dash")
    release = publish(aid)
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    reader = corpus.cast.id("has_results")
    stamp = stamp_now(copy["step"], reader)

    def slices(start: int, n: int):
        for i in range(start, start + n):
            corpus_rows.insert("step_user_results", step_id=copy["step"], user_id=reader,
                               organization_id=corpus.cast.org_id, report_id=None, status="success",
                               executed_as="viewer", params_fingerprint=f"slice{i}",
                               data={"rows": [{"מי": "has_results"}], "columns": [{"field": "מי"}]}, **stamp)

    slices(0, 3)
    few_answer, few = asyncio.run(_counted_own_run_check(release, reader))
    slices(3, 27)
    many_answer, many = asyncio.run(_counted_own_run_check(release, reader))

    record_property("statements", {"3 slices": few, "30 slices": many})
    assert (few_answer, many_answer) == (True, True)
    assert many <= few, f"statements: {few} for 3 slices, {many} for 30"


@pytest.mark.e2e
def test_an_export_does_not_count_a_run_a_row_policy_edit_cleared(
        corpus, corpus_rows, test_client, unreachable_source_clients, enterprise_license,
        no_vendor_libs, pdf_pages):
    """A withheld reader exports their own run. A row policy edit clears that
    run's rows (it keeps only their values): nothing of theirs is left to
    export until they run the dashboard again."""
    aid = own(corpus_rows, corpus, "rls/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    (relation,) = _rows(corpus_rows, "SELECT ct.id, ct.connection_id FROM connection_tables ct "
                                     "JOIN datasource_tables dt ON dt.connection_table_id = ct.id "
                                     "WHERE dt.datasource_id = :d", d=corpus.cast.data_sources["מחסן RLS"])
    member = as_(corpus, "member")
    export = f"/api/d/{aid}/export/html"
    run = test_client.post(f"/api/d/{aid}/queries/{copy['id']}/run", json={}, headers=member)
    assert run.status_code == 200 and run.json()["status"] == "success", run.text
    assert test_client.get(export, headers=member).status_code == 200

    admin = as_(corpus, "admin")
    assert test_client.put("/api/organization/settings", headers=admin,
                           json={"config": {"enable_custom_queries": {"value": True}}}).status_code == 200
    enterprise_license.features.append("rls")  # the corpus's license stub lists features; authoring a policy is one
    # A policy filters on a plain column name; the corpus relation has only a Hebrew one, and no
    # route edits a relation's columns without a reachable warehouse.
    corpus_rows.update("connection_tables", relation["id"], columns=[{"name": "חודש"}, {"name": "owner_email"}])
    edit = test_client.put(
        f"/api/connections/{relation['connection_id']}/custom-queries/{relation['id']}/rls",
        json={"rls_enabled": True, "rls_mode": "attribute", "rls_default_deny": True,
              "rls_policy": {"column": "owner_email", "source": "user.email", "op": "in"}},
        headers=admin)
    assert edit.status_code == 200, edit.text

    assert [cleared for cleared, _ in _stored(corpus_rows, copy["step"], corpus.cast.id("member"))] == [True]
    resp = test_client.get(export, headers=member)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "dashboard.export_needs_run"

    assert test_client.post(f"/api/d/{aid}/run", headers=member).status_code == 200
    assert test_client.get(export, headers=member).status_code == 200


# ── readers' results the upgrade copied (dashfc08, decision 9) ───────────
#
# The upgrade copies a published conversation's steps into its release, and
# each reader's result along with them. Copying a step is not a change to
# what it holds: a copied result is judged against the time its step last
# changed, as it was on the conversation before the upgrade -- served if it
# was current then, refused if the step had changed since its run -- and a
# copy that changes after the upgrade outdates it.

def _copy_of(test_client, corpus, corpus_rows, conversation, qkey) -> dict:
    """The release copy of a corpus conversation's query: its dashboard,
    query and step ids, and when the step was copied."""
    link = test_client.get(f"/api/r/{corpus.reports[conversation]}/dashboard")
    assert link.status_code == 200, link.text
    did = link.json()["dashboard_id"]
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step, s.created_at FROM queries q "
                                 "JOIN steps s ON s.id = q.default_step_id "
                                 "JOIN artifacts a ON a.published_release_id = q.artifact_release_id "
                                 "JOIN queries src ON src.title = q.title AND src.id = :src "
                                 "WHERE a.id = :d", d=did, src=corpus.ids[f"{conversation}/{qkey}"])
    return {"did": did, **copy}


def _own_rows(test_client, corpus, copy, reader):
    """The rows of the reader's own result /d serves on the copy, or None."""
    read = test_client.get(f"/api/d/{copy['did']}/queries/{copy['id']}/step", headers=as_(corpus, reader))
    assert read.status_code == 200, read.text
    body = read.json()
    if not body.get("viewer_result"):
        return None
    return [r.get("חודש") for r in (body.get("data") or {}).get("rows") or []]


def _when(value):
    """A timestamp column as a datetime (SQLite hands back the stored text)."""
    from datetime import datetime

    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _changed_at(corpus_rows, step_id, when):
    """The step's content changed at `when` (a refresh or an edit)."""
    corpus_rows.update("steps", step_id, updated_at=when)


@pytest.mark.e2e
def test_the_upgrade_serves_each_reader_the_result_that_was_current_before_it(corpus, corpus_rows, test_client):
    """Before the upgrade the conversation's step changed after one reader's
    runs and before another's: after it, the copy serves the later run and
    refuses the earlier ones, as the conversation did."""
    from tests.fixtures.dashboard_corpus.alembic_at import upgrade_test_db_to_head

    source = corpus.ids["run_viewer_results/q_sales/step"]
    ran = {r["user_id"]: _when(r["last_run_at"]) for r in _rows(
        corpus_rows, "SELECT user_id, MAX(last_run_at) AS last_run_at FROM step_user_results "
                     "WHERE step_id = :s GROUP BY user_id", s=source)}
    earlier, later = ran[corpus.cast.id("has_results")], ran[corpus.cast.id("direct_share")]
    assert earlier < later
    _changed_at(corpus_rows, source, earlier + (later - earlier) / 2)

    upgrade_test_db_to_head()

    copy = _copy_of(test_client, corpus, corpus_rows, "run_viewer_results", "q_sales")
    assert _own_rows(test_client, corpus, copy, "direct_share") == ["של direct_share 1"]
    assert _own_rows(test_client, corpus, copy, "has_results") is None


@pytest.mark.e2e
def test_the_upgrade_does_not_serve_a_copied_result_once_its_copy_changed(migrated_corpus, corpus_rows,
                                                                           test_client):
    """A copy that changed after the upgrade copied it -- before the
    revisions after the copy ran, or after them -- outdates the readers'
    copied results; an unchanged copy keeps serving them."""
    from alembic import command

    from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, upgrade_test_db_to_head
    from tests.fixtures.dashboard_corpus.rows import sync_test_url

    c = migrated_corpus
    sales = _copy_of(test_client, c, corpus_rows, "run_viewer_results", "q_sales")
    sealed = _copy_of(test_client, c, corpus_rows, "encrypted", "q_sealed")

    # Changed between the copy (dashfc08) and the revisions after vrstamp01:
    # a database that ran a release with dashfc08 and refreshed the copy.
    with alembic_pointed_at(sync_test_url()) as cfg:
        command.downgrade(cfg, "vrstamp01")
    _changed_at(corpus_rows, sealed["step"], _when(sealed["created_at"]) + timedelta(minutes=1))
    upgrade_test_db_to_head()

    assert _own_rows(test_client, c, sales, "has_results") == ["של has_results 1"]
    assert _own_rows(test_client, c, sales, "direct_share") == ["של direct_share 1"]
    assert _own_rows(test_client, c, sealed, "has_results") is None

    # Changed after the upgrade.
    _changed_at(corpus_rows, sales["step"], datetime.utcnow())
    assert _own_rows(test_client, c, sales, "has_results") is None
    assert _own_rows(test_client, c, sales, "direct_share") is None


def _copied_between_readers(corpus, corpus_rows) -> str:
    """The conversation's step changed after has_results ran and before
    direct_share did (as in the test above); its id."""
    source = corpus.ids["run_viewer_results/q_sales/step"]
    ran = {r["user_id"]: _when(r["last_run_at"]) for r in _rows(
        corpus_rows, "SELECT user_id, MAX(last_run_at) AS last_run_at FROM step_user_results "
                     "WHERE step_id = :s GROUP BY user_id", s=source)}
    earlier, later = ran[corpus.cast.id("has_results")], ran[corpus.cast.id("direct_share")]
    assert earlier < later
    _changed_at(corpus_rows, source, earlier + (later - earlier) / 2)
    return source


def _upgraded_with(test_client, corpus, corpus_rows, between) -> dict:
    """The upgrade, with `between` done after the copy (dashfc08, up to
    vrstamp01) and before the revisions after it: a database that ran a
    release with dashfc08 for a while. The copy of q_sales."""
    from tests.fixtures.dashboard_corpus.alembic_at import upgrade_test_db_to_head

    upgrade_test_db_to_head("vrstamp01")
    copy = _copy_of(test_client, corpus, corpus_rows, "run_viewer_results", "q_sales")
    between(copy)
    upgrade_test_db_to_head()
    return copy


@pytest.mark.e2e
def test_the_upgrade_serves_a_copied_result_after_the_conversation_was_deleted(corpus, corpus_rows, test_client):
    """Deleting the conversation of a published dashboard unlinks it from
    the dashboard. The copies still came from that conversation: each reader
    keeps the result that was current before the upgrade."""
    _copied_between_readers(corpus, corpus_rows)

    def delete_the_conversation(copy):
        resp = test_client.delete(f"/api/reports/{corpus.reports['run_viewer_results']}",
                                  headers=as_(corpus, "owner"))
        assert resp.status_code == 200, resp.text
        assert _rows(corpus_rows, "SELECT report_id FROM artifacts WHERE id = :d",
                     d=copy["did"]) == [{"report_id": None}]

    copy = _upgraded_with(test_client, corpus, corpus_rows, delete_the_conversation)
    assert _own_rows(test_client, corpus, copy, "direct_share") == ["של direct_share 1"]
    assert _own_rows(test_client, corpus, copy, "has_results") is None


@pytest.mark.e2e
def test_the_upgrade_serves_a_copied_result_after_another_conversation_took_the_dashboard_over(
        corpus, corpus_rows, test_client, monkeypatch):
    """An editor's conversation joins the published dashboard, and the
    owner's conversation is deleted: the editor's becomes the dashboard's
    own. The copies still came from the owner's conversation."""
    monkeypatch.setattr("app.core.fire_and_forget.spawn", [].append)  # the join's rerun is not run
    _copied_between_readers(corpus, corpus_rows)

    def hand_over(copy):
        did = copy["did"]
        grant = test_client.put(f"/api/artifacts/{did}/sharing", headers=as_(corpus, "owner"),
                                json={"visibility": "internal", "editor_user_ids": [corpus.cast.id("member")]})
        assert grant.status_code == 200, grant.text
        joined = test_client.post(f"/api/artifacts/{did}/edit", headers=as_(corpus, "member"))
        assert joined.status_code == 200, joined.text
        resp = test_client.delete(f"/api/reports/{corpus.reports['run_viewer_results']}",
                                  headers=as_(corpus, "owner"))
        assert resp.status_code == 200, resp.text
        assert _rows(corpus_rows, "SELECT report_id FROM artifacts WHERE id = :d",
                     d=did) == [{"report_id": joined.json()["report_id"]}]

    copy = _upgraded_with(test_client, corpus, corpus_rows, hand_over)
    assert _own_rows(test_client, corpus, copy, "direct_share") == ["של direct_share 1"]
    assert _own_rows(test_client, corpus, copy, "has_results") is None


@pytest.mark.e2e
def test_the_upgrade_does_not_take_the_time_of_an_older_step_that_holds_the_same_data(
        corpus, corpus_rows, test_client):
    """The conversation's query kept an older step holding the same data
    (byte for byte, as on an install without encryption) as the one the
    upgrade copied. That step changed after the copy: the copied results
    are refused, never served on the older step's time."""
    import sqlalchemy as sa

    source = corpus.ids["run_viewer_results/q_sales/step"]
    (first_run,) = _rows(corpus_rows, "SELECT MIN(last_run_at) AS at FROM step_user_results WHERE step_id = :s",
                         s=source)
    older_at = _when(first_run["at"]) - timedelta(days=1)
    # An older run of the query (no route keeps one beside the step it shows).
    steps = sa.Table("steps", sa.MetaData(), autoload_with=corpus_rows.engine)
    kept = [c for c in steps.c if c.name not in ("id", "slug", "created_at", "updated_at")]
    older = str(uuid.uuid4())
    with corpus_rows.engine.begin() as conn:
        conn.execute(steps.insert().from_select(
            ["id", "slug", "created_at", "updated_at", *(c.name for c in kept)],
            sa.select(sa.literal(older), sa.literal(f"older-{older[:8]}"), sa.literal(older_at),
                      sa.literal(older_at), *kept).where(steps.c.id == source)))

    def the_step_changed(copy):
        corpus_rows.update("steps", source, data=STALE, updated_at=datetime.utcnow())

    copy = _upgraded_with(test_client, corpus, corpus_rows, the_step_changed)
    assert _own_rows(test_client, corpus, copy, "direct_share") is None
    assert _own_rows(test_client, corpus, copy, "has_results") is None


def _twin_of_sales(corpus, corpus_rows, at, shown=True) -> str:
    """A second chart of the conversation's numbers: another query whose
    default step has the same title and the same data as q_sales's, last
    changed at `at`. `shown` on the dashboard, the upgrade copies both; not
    shown, it copies only q_sales. Its step id. (Rows, as the corpus writes
    them: no route builds the conversation as it was before the upgrade.)"""
    import json

    import sqlalchemy as sa

    key = "run_viewer_results"
    tables = {n: sa.Table(n, sa.MetaData(), autoload_with=corpus_rows.engine)
              for n in ("queries", "steps", "visualizations", "artifact_versions")}

    def copy(name, source_id, **values):
        tbl = tables[name]
        kept = [c for c in tbl.c if c.name not in values]
        with corpus_rows.engine.begin() as conn:
            assert conn.execute(tbl.insert().from_select(
                [*values, *(c.name for c in kept)],
                sa.select(*(sa.literal(v, type_=tbl.c[k].type) for k, v in values.items()), *kept)
                .where(tbl.c.id == source_id))).rowcount == 1

    qid, sid, vid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    copy("queries", corpus.ids[f"{key}/q_sales"], id=qid, title="הכנסות לפי חודש (עמודות)",
         created_at=at, updated_at=at)
    copy("steps", corpus.ids[f"{key}/q_sales/step"], id=sid, slug=f"twin-{sid[:8]}", query_id=qid,
         created_at=at, updated_at=at)
    corpus_rows.update("queries", qid, default_step_id=sid)
    copy("visualizations", corpus.ids[f"{key}/q_sales/viz"], id=vid, query_id=qid, created_at=at, updated_at=at)
    versions = tables["artifact_versions"]
    version = corpus.ids[f"{key}/dash/v1"]
    with corpus_rows.engine.begin() as conn:
        content = conn.execute(sa.select(versions.c.content).where(versions.c.id == version)).scalar_one()
        content = json.loads(content) if isinstance(content, str) else dict(content)
        # A version that lists its charts shows only those: the twin is shown if listed.
        assert corpus.ids[f"{key}/q_sales/viz"] in content["visualization_ids"]
        if shown:
            content["visualization_ids"] = [*content["visualization_ids"], vid]
            assert conn.execute(versions.update().where(versions.c.id == version)
                                .values(content=content)).rowcount == 1
    return sid


def _moved_to_a_new_step(corpus, corpus_rows, source) -> None:
    """q_sales was run again under another title: it shows a new step, and
    `source`, the step it showed, is kept beside it (query_service's run)."""
    import sqlalchemy as sa

    query = corpus.ids["run_viewer_results/q_sales"]
    steps = sa.Table("steps", sa.MetaData(), autoload_with=corpus_rows.engine)
    kept = [c for c in steps.c if c.name not in ("id", "slug", "title", "created_at", "updated_at")]
    new, now = str(uuid.uuid4()), datetime.utcnow()
    with corpus_rows.engine.begin() as conn:
        assert conn.execute(steps.insert().from_select(
            ["id", "slug", "title", "created_at", "updated_at", *(c.name for c in kept)],
            sa.select(sa.literal(new), sa.literal(f"new-{new[:8]}"), sa.literal("הכנסות לפי רבעון"),
                      sa.literal(now), sa.literal(now), *kept).where(steps.c.id == source))).rowcount == 1
    corpus_rows.update("queries", query, default_step_id=new)
    assert _rows(corpus_rows, "SELECT q.default_step_id AS shows, s.query_id AS kept_by FROM queries q "
                              "JOIN steps s ON s.id = :src WHERE q.id = :q", q=query, src=source
                 ) == [{"shows": new, "kept_by": query}]


def _first_run(corpus, corpus_rows):
    (first,) = _rows(corpus_rows, "SELECT MIN(last_run_at) AS at FROM step_user_results WHERE step_id = :s",
                     s=corpus.ids["run_viewer_results/q_sales/step"])
    return _when(first["at"])


# Where the conversation has more than one step the copy could have come
# from, nothing written at the upgrade says which one it was: the copy is
# left as it is and its readers' results stay refused, though the twins may
# both still hold what was copied. (Before, the latest of their times was
# kept; a twin that changed since then could no longer be told apart.)

@pytest.mark.e2e
def test_the_upgrade_does_not_serve_a_copied_result_of_one_of_twin_steps(corpus, corpus_rows, test_client):
    """Two queries' default steps have the same title and data, and neither
    changed after the upgrade: which one a copy came from is not known, so
    the copied results are refused, even the run that was current."""
    _copied_between_readers(corpus, corpus_rows)
    _twin_of_sales(corpus, corpus_rows, _first_run(corpus, corpus_rows) - timedelta(days=1))

    copy = _upgraded_with(test_client, corpus, corpus_rows, lambda copy: None)
    assert _own_rows(test_client, corpus, copy, "direct_share") is None
    assert _own_rows(test_client, corpus, copy, "has_results") is None


@pytest.mark.e2e
def test_the_upgrade_does_not_serve_a_copied_result_once_its_twin_source_changed(
        corpus, corpus_rows, test_client):
    """The copy's source changed after the upgrade copied it; its twin did
    not. The copy is not given the twin's time: its results stay refused."""
    source = _copied_between_readers(corpus, corpus_rows)
    _twin_of_sales(corpus, corpus_rows, _first_run(corpus, corpus_rows) - timedelta(days=1))

    def the_source_changed(copy):
        corpus_rows.update("steps", source, data=STALE, updated_at=datetime.utcnow())

    copy = _upgraded_with(test_client, corpus, corpus_rows, the_source_changed)
    assert _own_rows(test_client, corpus, copy, "direct_share") is None
    assert _own_rows(test_client, corpus, copy, "has_results") is None


@pytest.mark.e2e
def test_the_upgrade_does_not_serve_a_copied_result_once_its_source_query_moved_to_a_new_step(
        corpus, corpus_rows, test_client):
    """After the upgrade the source's query shows a new step under another
    title, so only the twin still matches the copy by title and data. The
    release holds two copies of that title: the copy is not given the
    twin's time, and its results stay refused."""
    source = _copied_between_readers(corpus, corpus_rows)
    _twin_of_sales(corpus, corpus_rows, _first_run(corpus, corpus_rows) - timedelta(days=1))

    def the_query_moved(copy):
        _moved_to_a_new_step(corpus, corpus_rows, source)

    copy = _upgraded_with(test_client, corpus, corpus_rows, the_query_moved)
    # The twin was copied too: the release holds two copies of the title.
    assert len(_rows(corpus_rows, "SELECT s.id FROM steps s JOIN queries q ON q.id = s.query_id "
                                  "JOIN artifacts a ON a.published_release_id = q.artifact_release_id "
                                  "JOIN steps src ON src.title = s.title AND src.id = :src WHERE a.id = :d",
                     d=copy["did"], src=source)) == 2
    assert _own_rows(test_client, corpus, copy, "direct_share") is None
    assert _own_rows(test_client, corpus, copy, "has_results") is None


@pytest.mark.e2e
def test_the_upgrade_does_not_serve_a_copied_result_on_the_time_of_a_twin_the_dashboard_never_showed(
        corpus, corpus_rows, test_client):
    """The twin is not on the dashboard, so the release holds one copy of
    the title. After the upgrade the copy's own query shows a new step under
    another title, and the twin is the only step still shown under the
    copy's: it is not the source, and the copy is not given its time."""
    source = _copied_between_readers(corpus, corpus_rows)
    _twin_of_sales(corpus, corpus_rows, _first_run(corpus, corpus_rows) - timedelta(days=1), shown=False)

    copy = _upgraded_with(test_client, corpus, corpus_rows,
                          lambda copy: _moved_to_a_new_step(corpus, corpus_rows, source))
    assert len(_rows(corpus_rows, "SELECT s.id FROM steps s JOIN queries q ON q.id = s.query_id "
                                  "JOIN artifacts a ON a.published_release_id = q.artifact_release_id "
                                  "JOIN steps src ON src.title = s.title AND src.id = :src WHERE a.id = :d",
                     d=copy["did"], src=source)) == 1
    assert _own_rows(test_client, corpus, copy, "direct_share") is None
    assert _own_rows(test_client, corpus, copy, "has_results") is None
