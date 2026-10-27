"""Forking a conversation copies every dashboard in it (found in manual testing).

A conversation can hold several dashboards. The fork used to copy one version
only: the conversation's newest page or slides version, whatever its status --
so the other dashboards were lost, a failed or still-generating version could
be the one copied, and a doc never came along.

Now each live dashboard of the conversation (page, slides or doc) becomes a
dashboard of the forker's, unpublished, from its newest completed version; the
summary the agent reads names them all.
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


def _dashboards(corpus_rows, report_id):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT a.id, a.title, a.mode, a.owner_id, a.published_release_id, "
            "       (SELECT count(*) FROM artifact_versions v WHERE v.artifact_id = a.id) AS versions, "
            "       (SELECT v.status FROM artifact_versions v WHERE v.artifact_id = a.id) AS status "
            "FROM artifacts a WHERE a.report_id = :r AND a.deleted_at IS NULL ORDER BY a.title"),
            {"r": report_id}).mappings()]


@pytest.mark.e2e
def test_every_dashboard_of_the_conversation_comes_along(corpus, corpus_rows, test_client):
    fork = _fork(test_client, corpus, "conv_three_dash")
    copies = _dashboards(corpus_rows, fork)
    assert {(d["title"], d["mode"]) for d in copies} == {
        ("לוח מכירות", "page"), ("לוח מלאי", "page"), ("מצגת רבעונית", "slides")}
    forker = corpus.cast.id("owner")
    for d in copies:
        assert d["versions"] == 1 and d["published_release_id"] is None, d
        assert str(d["owner_id"]) == str(forker), d


def _content(corpus_rows, dashboard_id):
    with corpus_rows.engine.connect() as conn:
        value = conn.execute(text("SELECT content FROM artifact_versions WHERE artifact_id = :a"),
                             {"a": dashboard_id}).scalar_one()
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.e2e
def test_each_dashboard_comes_from_its_newest_completed_version(corpus, corpus_rows, test_client):
    # The newest version failed: mark it, so a copy of it is told apart.
    corpus_rows.update("artifact_versions", corpus.ids["dash_newest_failed/dash/v2"],
                       content={"code": "FAILED VERSION", "runtime_version": 1, "visualization_ids": []})
    (copy,) = _dashboards(corpus_rows, _fork(test_client, corpus, "dash_newest_failed"))
    assert "FAILED VERSION" not in json.dumps(_content(corpus_rows, copy["id"])), "the failed version was copied"
    # The newest version is still generating (no content yet).
    (copy,) = _dashboards(corpus_rows, _fork(test_client, corpus, "dash_newest_pending"))
    assert _content(corpus_rows, copy["id"]).get("code"), "the empty pending version was copied"
    assert copy["status"] == "completed"


@pytest.mark.e2e
def test_a_doc_comes_along_too(corpus, corpus_rows, test_client):
    copies = _dashboards(corpus_rows, _fork(test_client, corpus, "doc_after_page"))
    assert {(d["title"], d["mode"]) for d in copies} == {("לוח מכירות", "page"), ("סיכום כתוב", "doc")}


@pytest.mark.e2e
def test_the_fork_summary_names_every_dashboard(corpus, corpus_rows, test_client):
    fork = _fork(test_client, corpus, "conv_three_dash")
    with corpus_rows.engine.connect() as conn:
        row = conn.execute(text("SELECT completion, fork_asset_refs FROM completions WHERE report_id = :r "
                                "AND is_fork_summary = 'true'"), {"r": fork}).mappings().one()
    completion = json.loads(row["completion"]) if isinstance(row["completion"], str) else row["completion"]
    summary = completion["content"]
    refs = row["fork_asset_refs"] if not isinstance(row["fork_asset_refs"], str) else json.loads(row["fork_asset_refs"])
    for title in ("לוח מכירות", "לוח מלאי", "מצגת רבעונית"):
        assert title in summary, title
    assert len([r for r in refs if r.get("type") == "artifact"]) == 3


@pytest.mark.e2e
def test_old_versions_with_no_dashboard_tie_give_one_dashboard(corpus, corpus_rows, test_client):
    """Versions from before the tie are each a dashboard of their own (same
    id): the fork takes the newest of them, not one dashboard per version."""
    (copy,) = _dashboards(corpus_rows, _fork(test_client, corpus, "artver01_chain"))
    newest = _content(corpus_rows, corpus.ids["artver01_chain/legacy_3"])
    assert _content(corpus_rows, copy["id"])["code"] == newest["code"]
