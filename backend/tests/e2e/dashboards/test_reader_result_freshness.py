"""A reader's stored run of a published dashboard is served while what it read holds.

The release's step read and its filter run serve the reader's own stored
result. When the row policy of a relation its sources hold changes, that
result was filtered by the old policy: it is not served, and the filter run
executes again. (The other changes -- an extract refresh, a connection's
auth policy, the reader's groups -- are covered on the conversation's
surfaces in tests/e2e/rbac/test_viewer_result_freshness.py; both go through
the same check.)
"""
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


@pytest.mark.e2e
def test_a_row_policy_edit_stops_serving_the_readers_stored_run(corpus, corpus_rows, test_client,
                                                                unreachable_source_clients):
    aid = own(corpus_rows, corpus, "rls/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    corpus_rows.update("steps", copy["step"], data=STALE)  # tell the snapshot from a reader's run
    (relation,) = _rows(corpus_rows, "SELECT ct.id, ct.connection_id FROM connection_tables ct "
                                     "JOIN datasource_tables dt ON dt.connection_table_id = ct.id "
                                     "WHERE dt.datasource_id = :d", d=corpus.cast.data_sources["מחסן RLS"])

    step_path = f"/api/d/{aid}/queries/{copy['id']}/step"
    filter_path = f"/api/d/{aid}/queries/{copy['id']}/run"
    first = test_client.post(filter_path, json={}, headers=as_(corpus, "member"))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "success" and first.json()["cached"] is False
    ran = _months(first.json())
    assert ran and "ישן" not in ran
    assert test_client.post(filter_path, json={}, headers=as_(corpus, "member")).json()["cached"] is True
    assert _months(test_client.get(step_path, headers=as_(corpus, "member")).json()) == ran

    admin = as_(corpus, "admin")
    assert test_client.put("/api/organization/settings", headers=admin,
                           json={"config": {"enable_custom_queries": {"value": True}}}).status_code == 200
    off = test_client.put(f"/api/connections/{relation['connection_id']}/custom-queries/{relation['id']}/rls",
                          json={"rls_enabled": False}, headers=admin)
    assert off.status_code == 200, off.text

    # Without the row policy every reader shares the snapshot.
    assert _months(test_client.get(step_path, headers=as_(corpus, "member")).json()) == {"ישן"}
    again = test_client.post(filter_path, json={}, headers=as_(corpus, "member"))
    assert again.status_code == 200, again.text
    assert again.json()["cached"] is False


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
