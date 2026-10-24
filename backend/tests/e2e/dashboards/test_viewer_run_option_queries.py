"""A reader's own run covers the queries that only feed a filter.

A filter's choices come from another query's rows (its `options_source`).
That query often has no chart: the page fetches it only for the choices.
When the dashboard's snapshot is withheld from readers (a source with
row-level security, or one each person signs in to), a reader sees only the
rows their own run produced -- so a run that re-executes the charts alone
leaves the filter with nothing to offer ("Couldn't load: <options query>").

Both reader runs must cover it: the conversation link's (`POST /api/r/{id}/run`)
and the published dashboard's (`POST /api/d/{id}/run`).
"""
import json

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

WITHHELD_SOURCES = ["מחסן RLS", "מחסן אישי"]  # row-level security / user_required
OPTIONS_TITLE = "ז'אנרים"


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _values(body) -> set:
    return {r.get("חודש") for r in (body.get("data") or {}).get("rows", [])}


def _filter_only_options(corpus, corpus_rows, source):
    """options_sources/dash showing one filtered chart; its genre list is a
    filter only (no chart), over a source whose snapshot readers never see."""
    ids = corpus.ids
    vid = ids["options_sources/dash/v1"]
    raw = _rows(corpus_rows, "SELECT content FROM artifact_versions WHERE id = :v", v=vid)[0]["content"]
    content = json.loads(raw) if isinstance(raw, str) else dict(raw)
    content["visualization_ids"] = [ids["options_sources/q_by_id/viz"]]
    corpus_rows.update("artifact_versions", vid, content=content)
    # No route attaches a connection-backed source without a live database
    # (see connection_source in the corpus), so the link is written directly.
    corpus_rows.insert("report_data_source_association", report_id=corpus.reports["options_sources"],
                       data_source_id=corpus.cast.data_sources[source])
    return vid


def _assert_reader_gets_options(test_client, corpus, run_path, step_path, reader="member"):
    before = test_client.get(step_path, headers=as_(corpus, reader))
    assert before.status_code == 200, before.text
    assert not _values(before.json()), "the snapshot must be withheld before the reader runs"

    run = test_client.post(run_path, headers=as_(corpus, reader))
    assert run.status_code == 200, run.text

    after = test_client.get(step_path, headers=as_(corpus, reader))
    assert after.status_code == 200, after.text
    body = after.json()
    assert body.get("viewer_result"), body
    assert _values(body) == {"ינואר", "פברואר"}


@pytest.mark.e2e
@pytest.mark.parametrize("source", WITHHELD_SOURCES, ids=["rls", "user_required"])
def test_a_readers_run_on_r_covers_a_filters_option_query(corpus, corpus_rows, test_client, source):
    vid = _filter_only_options(corpus, corpus_rows, source)
    report = corpus.reports["options_sources"]
    options_query = corpus.ids["options_sources/q_genres"]
    _assert_reader_gets_options(
        test_client, corpus,
        run_path=f"/api/r/{report}/run?artifact_id={vid}",
        step_path=f"/api/r/{report}/queries/{options_query}/step",
    )


@pytest.mark.e2e
@pytest.mark.parametrize("source", WITHHELD_SOURCES, ids=["rls", "user_required"])
def test_a_readers_run_on_d_covers_a_filters_option_query(corpus, corpus_rows, test_client, source):
    _filter_only_options(corpus, corpus_rows, source)
    aid = own(corpus_rows, corpus, "options_sources/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _rows(corpus_rows, "SELECT id FROM queries WHERE artifact_release_id = :r AND title = :t",
                    r=release, t=OPTIONS_TITLE)
    _assert_reader_gets_options(
        test_client, corpus,
        run_path=f"/api/d/{aid}/run",
        step_path=f"/api/d/{aid}/queries/{copy['id']}/step",
    )
