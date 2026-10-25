"""A chart that failed in a conversation is never dropped without a word.

Joining: an editor who joins gets the dashboard's every chart -- a failed one
too. Their copies run again as them anyway, so whether it failed for someone
else says nothing about their run; and the steps that load it point at its
copy, as with any other.

Publishing: a chart the version shows whose step failed in the publisher's
conversation would be left out of the release. The publish is refused,
naming the charts, and the live dashboard stays as it is. A failed query the
dashboard does not show blocks nothing.
"""
import asyncio
import json

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import PUBLISHED, as_, own, publish

CONV = "refs_in_code"
DASH = f"{CONV}/dash"


@pytest.fixture
def drawn(monkeypatch):
    from app.services.thumbnail_service import ThumbnailService
    from tests.fixtures.dashboard_corpus.kinds import TINY_PNG

    monkeypatch.setattr(ThumbnailService, "_build_thumbnail_html", lambda self, **kw: "")

    async def draw(self, artifact_id, html_content, mode="page"):
        (self.UPLOADS_DIR / f"{artifact_id}.png").write_bytes(TINY_PNG)
        return f"thumbnails/{artifact_id}.png"

    monkeypatch.setattr(ThumbnailService, "generate_thumbnail", draw)


@pytest.fixture
def spawned(monkeypatch, drawn):
    jobs = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", jobs.append)
    return jobs


def _finish(spawned):
    while spawned:
        job = spawned.pop(0)
        if asyncio.iscoroutine(job):
            asyncio.run(job)


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _fail(corpus_rows, corpus, query_key):
    corpus_rows.update("steps", corpus.ids[f"{query_key}/step"], status="error", data={})


def _queries_of(corpus_rows, report_id):
    return _rows(corpus_rows, "SELECT q.id, q.title, s.id AS step_id, s.slug, s.code, s.status FROM queries q "
                              "JOIN steps s ON s.id = q.default_step_id WHERE q.report_id = :r", r=report_id)


def _charts_of(corpus_rows, report_id):
    return {r["id"]: r["title"] for r in _rows(
        corpus_rows, "SELECT v.id, v.title FROM visualizations v JOIN queries q ON q.id = v.query_id "
                     "WHERE q.report_id = :r AND v.deleted_at IS NULL", r=report_id)}


def _join(test_client, corpus, aid, spawned, person="member"):
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"),
                           json={"visibility": "none", "editor_user_ids": [corpus.cast.id(person)]})
    assert resp.status_code == 200, resp.text
    joined = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, person))
    assert joined.status_code == 200, joined.text
    return joined.json()


def _publish(test_client, corpus, aid, person="owner"):
    resp = test_client.post(f"/api/artifacts/{aid}/publish", headers=as_(corpus, person))
    if resp.status_code == 202:
        PUBLISHED.append(resp.json()["latest"]["id"])
    return resp


@pytest.mark.e2e
def test_a_joining_editor_gets_a_chart_that_failed_for_someone_else(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    _fail(corpus_rows, corpus, f"{CONV}/q_base")  # the chart the other two load
    joined = _join(test_client, corpus, aid, spawned)
    avi = joined["report_id"]

    copies = {q["title"]: q for q in _queries_of(corpus_rows, avi)}
    owners = {q["title"]: q for q in _queries_of(corpus_rows, corpus.reports[CONV])}
    assert set(copies) == set(owners)  # every query, the failed one too
    assert all(q["status"] == "pending" for q in copies.values())  # all run again as Avi
    base = copies["בסיס"]
    # Its loaders point at its copy -- by id and by slug -- not at the owner's step.
    assert base["step_id"] in copies["לפי מזהה"]["code"]
    assert owners["בסיס"]["step_id"] not in copies["לפי מזהה"]["code"]
    assert base["slug"] and base["slug"] in copies["לפי שם קוד"]["code"]

    # His version shows every chart, each one his conversation's.
    (version,) = _rows(corpus_rows, "SELECT content FROM artifact_versions WHERE artifact_id = :a AND report_id = :r",
                       a=aid, r=avi)
    content = version["content"] if isinstance(version["content"], dict) else json.loads(version["content"])
    listed = [str(v) for v in content["visualization_ids"]]
    assert len(listed) == 3 and set(listed) <= set(_charts_of(corpus_rows, avi))


@pytest.mark.e2e
def test_publishing_with_a_failed_chart_is_refused_and_names_it(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    live_before = _rows(corpus_rows, "SELECT published_release_id FROM artifacts WHERE id = :a", a=aid)
    _fail(corpus_rows, corpus, f"{CONV}/q_by_slug")

    resp = _publish(test_client, corpus, aid)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_code"] == "dashboard.publish_charts_failed"
    (title,) = [r["title"] for r in _rows(corpus_rows, "SELECT title FROM visualizations WHERE id = :v",
                                          v=corpus.ids[f"{CONV}/q_by_slug/viz"])]
    assert body["params"]["names"] == title
    _finish(spawned)
    assert _rows(corpus_rows, "SELECT published_release_id FROM artifacts WHERE id = :a", a=aid) == live_before
    assert _rows(corpus_rows, "SELECT id FROM artifact_releases WHERE artifact_id = :a AND status = 'publishing'",
                 a=aid) == []


@pytest.mark.e2e
def test_a_failed_query_the_dashboard_does_not_show_blocks_nothing(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    _fail(corpus_rows, corpus, "conv_three_dash/q_stock")  # another dashboard's chart
    resp = _publish(test_client, corpus, aid)
    assert resp.status_code == 202, resp.text
    _finish(spawned)
