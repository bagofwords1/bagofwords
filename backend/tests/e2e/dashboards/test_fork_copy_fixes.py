"""Fork copies through the dashboard copy engine (phase 3).

Fork keeps its own scope (the whole conversation's live queries) and its own
data policy, but copies rows and rewrites references with the shared engine.
That fixes four things a fork used to get wrong:

  1. a deleted chart or query came back to life in the fork;
  2. a filter whose options come from a query named by title lost its options;
  3. a query that loads another one by id or slug stopped resolving;
  4. a query id, or an upper-case chart id, in page code was not rewritten.
"""
import json

import pytest
from sqlalchemy import text


def _fork(test_client, corpus, report_key):
    cast = corpus.cast
    resp = test_client.post(f"/api/reports/{corpus.reports[report_key]}/fork", json={},
                            headers=cast.people["owner"].headers(cast.org_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _rows(conn, sql, **params):
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.e2e
def test_deleted_charts_and_queries_stay_deleted_in_a_fork(corpus, corpus_rows, test_client):
    with_deleted_chart = _fork(test_client, corpus, "refs_in_code")
    with_deleted_query = _fork(test_client, corpus, "viz_refs")
    with corpus_rows.engine.connect() as conn:
        charts = _rows(conn, "SELECT title, deleted_at FROM visualizations WHERE report_id = :r", r=with_deleted_chart)
        live_queries = _rows(conn, "SELECT title FROM queries WHERE report_id = :r AND deleted_at IS NULL",
                             r=with_deleted_query)
    assert [c["deleted_at"] is not None for c in charts if c["title"] == "גרף שנמחק"] == [True]
    assert "גרף שנמחק" not in {q["title"] for q in live_queries}


@pytest.mark.e2e
def test_a_filter_naming_its_option_query_by_title_keeps_its_options(corpus, corpus_rows, test_client):
    fork = _fork(test_client, corpus, "options_sources")
    with corpus_rows.engine.connect() as conn:
        queries = {q["title"]: q for q in _rows(conn, "SELECT id, title, parameters FROM queries WHERE report_id = :r",
                                                r=fork)}
    genres = queries["ז'אנרים"]["id"]
    for title in ("שירים לפי ז'אנר", "אלבומים לפי ז'אנר"):  # by id, by title
        source = _json(queries[title]["parameters"])[0]["options_source"]
        assert source and source["query_id"] == genres, title


@pytest.mark.e2e
def test_load_step_by_id_or_slug_and_ids_in_page_code_point_into_the_fork(corpus, corpus_rows, test_client):
    fork = _fork(test_client, corpus, "refs_in_code")
    with corpus_rows.engine.connect() as conn:
        steps = {s["title"]: s for s in _rows(conn, (
            "SELECT q.title, s.id, s.slug, s.code FROM queries q JOIN steps s ON s.id = q.default_step_id "
            "WHERE q.report_id = :r"), r=fork)}
        queries = {q["title"]: q["id"] for q in _rows(conn, "SELECT id, title FROM queries WHERE report_id = :r",
                                                      r=fork)}
        charts = {c["title"]: c["id"] for c in _rows(conn, "SELECT id, title FROM visualizations WHERE report_id = :r",
                                                     r=fork)}
        content = _json(conn.execute(text(
            "SELECT av.content FROM artifact_versions av WHERE av.report_id = :r"), {"r": fork}).scalar())
    base = steps["בסיס"]
    assert f"load_step('{base['id']}')" in steps["לפי מזהה"]["code"]
    assert f"load_step('{base['slug']}')" in steps["לפי שם קוד"]["code"]
    assert queries["לפי מזהה"] in content["code"]
    assert charts["לפי שם קוד"] in content["code"].lower()
    source_ids = {corpus.ids["refs_in_code/q_by_id"], corpus.ids["refs_in_code/q_by_slug/viz"].lower()}
    assert not any(i in content["code"].lower() for i in source_ids)


@pytest.mark.e2e
def test_a_deleted_query_a_step_loads_comes_along_still_deleted(corpus, corpus_rows, test_client):
    """load_step reaches a deleted query's step; dropping it would break the
    fork's loading query, so it is copied, still deleted."""
    from tests.e2e.dashboards.corpus_edits import add_query

    add_query(corpus_rows, corpus, "options_sources", "ישנה שנמחקה", chart=False, deleted=True)
    add_query(corpus_rows, corpus, "options_sources", "טוענת", code=(
        "def generate_df(ds_clients, excel_files, load_step):\n    return load_step('ישנה שנמחקה')\n"))
    fork = _fork(test_client, corpus, "options_sources")
    with corpus_rows.engine.connect() as conn:
        rows = {q["title"]: q for q in _rows(conn, "SELECT title, deleted_at FROM queries WHERE report_id = :r",
                                             r=fork)}
    assert rows["ישנה שנמחקה"]["deleted_at"] is not None
    assert rows["טוענת"]["deleted_at"] is None


@pytest.mark.e2e
def test_withheld_code_written_back_by_hydration_points_into_the_fork(corpus, monkeypatch):
    """A delegated-source fork holds its code out and hydration writes it back;
    that code's load_step calls must already point at the fork's steps."""
    import asyncio

    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.query import Query
    from app.models.step import Step
    from app.models.user import User
    from app.services import viewer_data_policy
    from app.services.fork_service import ForkService

    async def yes(*_a, **_k):
        return True

    monkeypatch.setattr(viewer_data_policy, "has_user_scoped_connections", yes)

    async def fork():
        async with async_session_maker() as db:
            owner = await db.get(User, corpus.cast.id("owner"))
            new_report = await ForkService().fork_report(db, corpus.reports["refs_in_code"], owner)
            pending = dict(new_report.pending_code)
            steps = {title: (str(step_id), slug) for title, step_id, slug in (await db.execute(
                select(Query.title, Step.id, Step.slug).join(Step, Step.id == Query.default_step_id)
                .where(Query.report_id == str(new_report.id)))).all()}
            return pending, steps

    pending, steps = asyncio.run(fork())
    base_id, _ = steps["בסיס"]
    by_id = pending[steps["לפי מזהה"][0]]
    assert f"load_step('{base_id}')" in by_id
    assert corpus.ids["refs_in_code/q_base/step"] not in by_id


@pytest.mark.e2e
def test_the_fork_summary_offers_nothing_that_was_deleted(corpus, corpus_rows, test_client):
    """The summary the agent reads in the fork names live queries and charts only."""
    fork = _fork(test_client, corpus, "viz_refs")
    with corpus_rows.engine.connect() as conn:
        row = conn.execute(text("SELECT completion, fork_asset_refs FROM completions WHERE report_id = :r "
                                "AND is_fork_summary = 'true'"), {"r": fork}).mappings().one()
    summary = json.dumps(_json(row["completion"]), ensure_ascii=False)
    refs = _json(row["fork_asset_refs"])
    assert "גרף שנמחק" not in summary and "1 queries were inherited" in summary
    assert "גרף שנמחק" not in {r.get("title") for r in refs}


@pytest.mark.e2e
def test_a_conversations_fork_does_not_copy_rows_an_identity_parameter_scoped(corpus, corpus_rows, test_client):
    """A query bound to the viewer's email holds one person's slice even over
    a shared connection: the fork gets the query and its code, never the rows
    (the same rule as row-level security), and runs it again for its owner."""
    fork = _fork(test_client, corpus, "identity_param")
    with corpus_rows.engine.connect() as conn:
        steps = _rows(conn, "SELECT s.data, s.code FROM queries q JOIN steps s ON s.id = q.default_step_id "
                            "WHERE q.report_id = :r", r=fork)
    assert steps and all(_json(s["data"]) in (None, {}) for s in steps)
