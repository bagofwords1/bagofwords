"""Where an old /r/{conversation} link leads (phase 4, part B).

`GET /api/r/{report_id}/dashboard` names the dashboard recorded for that
conversation (`artifacts.legacy_report_id`, filled by the data migration).
The answer never changes on its own -- not when another dashboard of the
conversation is published, not when the conversation is archived -- and the
dashboard's own /d check still decides who may open it.
"""
import uuid

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish


@pytest.mark.e2e
def test_an_old_link_leads_to_its_recorded_dashboard_for_good(corpus, corpus_rows, test_client):
    report = corpus.reports["conv_three_dash"]
    sales = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    url = f"/api/r/{report}/dashboard"
    # Nothing recorded (a conversation made after the upgrade): its newest
    # dashboard its editor can open (dash_stock has no owner yet, so is no
    # dashboard of its own); a reader who can open none stays on /r.
    assert test_client.get(url, headers=as_(corpus, "owner")).json() == {"dashboard_id": sales}
    assert test_client.get(url).status_code == 404

    corpus_rows.update("artifacts", sales, legacy_report_id=report)  # what the data migration writes
    publish(sales)
    for reader in ("anonymous", "member", "owner"):
        assert test_client.get(url, headers=as_(corpus, reader)).json() == {"dashboard_id": sales}, reader

    stock = own(corpus_rows, corpus, "conv_three_dash/dash_stock")
    publish(stock)  # newer, also published
    corpus_rows.update("reports", report, status="archived")
    assert test_client.get(url).json() == {"dashboard_id": sales}

    # Leading there opens nothing: /d still asks the dashboard's sharing.
    assert test_client.get(f"/api/d/{sales}").status_code == 404


@pytest.mark.e2e
def test_an_old_link_to_a_deleted_dashboard_or_unknown_conversation_leads_nowhere(corpus, corpus_rows, test_client):
    report = corpus.reports["conv_one_dash"]
    aid = own(corpus_rows, corpus, "conv_one_dash/dash")
    corpus_rows.update("artifacts", aid, legacy_report_id=report)
    corpus_rows.update("artifacts", aid, deleted_at=corpus_rows.clock())
    for rid in (report, str(uuid.uuid4()), "not-an-id"):
        resp = test_client.get(f"/api/r/{rid}/dashboard")
        # A malformed id is answered by main's guard before the route, with a
        # plain 404 that names nothing; a well-formed one by the route.
        assert resp.status_code == 404, rid
        if rid != "not-an-id":
            assert resp.json()["error_code"] == "artifact.not_found", rid
