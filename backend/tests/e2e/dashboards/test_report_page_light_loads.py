"""The small calls the report page makes on every open answer without
reading any step's rows.

Each of them needs a few columns of a conversation (who owns it, its
organization, its type, its folders and agents) or of a dashboard version
(which dashboard it belongs to). Loading the whole conversation instead
pulled its widgets, queries and charts, and with them every step's full
result grid, several times over: seconds of work per call on a large
dashboard, for a two-field answer.

Real SQL is observed at the driver boundary (an engine event); the routes,
services and ORM all run normally. Each test also pins the call's answer.
"""
import re

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.core import fire_and_forget
from tests.e2e.dashboards.dashboard_setup import as_, own
from tests.e2e.dashboards.test_collaboration import _edit, _grant_editor

CONV = "conv_three_dash"
DASH = f"{CONV}/dash_sales"


class _StepRowReads:
    """Every SELECT whose column list holds steps.data, while open."""

    def __init__(self):
        self.statements: list[str] = []

    def _observe(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            if re.search(r"\bsteps\.data\b", statement.split("FROM", 1)[0]):
                self.statements.append(statement)

    def __enter__(self):
        event.listen(Engine, "before_cursor_execute", self._observe)
        return self

    def __exit__(self, *exc):
        event.remove(Engine, "before_cursor_execute", self._observe)


def _one(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text(sql), params).mappings()]
    assert len(rows) == 1, rows
    return rows[0]


def _has_step_rows(corpus_rows, report_id):
    """The conversation really holds step rows the calls could have read."""
    row = _one(corpus_rows, "SELECT COUNT(*) AS n FROM steps s JOIN queries q ON q.id = s.query_id "
                            "WHERE q.report_id = :r AND s.data IS NOT NULL", r=report_id)
    assert row["n"] > 0


@pytest.mark.e2e
def test_the_continued_from_card_reads_no_step_rows(corpus, corpus_rows, test_client):
    """GET /api/artifacts/conversations/{id}, for a conversation that joined
    a dashboard from someone else's version (a base version row)."""
    aid = own(corpus_rows, corpus, DASH)
    _has_step_rows(corpus_rows, corpus.reports[CONV])
    _grant_editor(test_client, corpus, aid, "member")
    joined = _edit(test_client, corpus, aid, "member")
    assert joined.status_code == 200, joined.text
    # The join started the copy's rerun as a real background task; the test
    # client ends a request's tasks before the request returns, so it is
    # settled here and none of its reads fall in the window measured below.
    assert joined.json()["rerunning"] is True
    assert all(task.done() for task in list(fire_and_forget._pending_tasks))
    report_id = joined.json()["report_id"]
    base = _one(corpus_rows, "SELECT base_version_id FROM artifact_conversations WHERE report_id = :r",
                r=report_id)["base_version_id"]
    assert base == corpus.ids[f"{DASH}/v1"]

    with _StepRowReads() as reads:
        card = test_client.get(f"/api/artifacts/conversations/{report_id}", headers=as_(corpus, "member"))
    assert card.status_code == 200, card.text
    (entry,) = card.json()
    assert entry["artifact_id"] == aid
    assert entry["base_version_number"] == 1
    # The previous conversation is the owner's, not the caller's.
    assert entry["can_open_previous"] is False and entry["continued_from_report_id"] is None
    assert entry["rerunning"] is True
    assert reads.statements == []


@pytest.mark.e2e
def test_open_state_reads_no_step_rows(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    report_id = corpus.reports[CONV]
    _has_step_rows(corpus_rows, report_id)

    with _StepRowReads() as reads:
        state = test_client.get(f"/api/artifacts/{aid}/open-state", params={"report_id": report_id},
                                headers=as_(corpus, "owner"))
    assert state.status_code == 200, state.text
    body = state.json()
    assert body["live"] is None and body["behind_live"] is False and body["unpublished_here"] is False
    assert body["newest_here"]["version_id"] == corpus.ids[f"{DASH}/v1"]
    assert reads.statements == []

    # An editor of the dashboard asking about someone else's conversation is
    # still not found: the owner check on the light report load runs.
    _grant_editor(test_client, corpus, aid, "member")
    other = test_client.get(f"/api/artifacts/{aid}/open-state", params={"report_id": report_id},
                            headers=as_(corpus, "member"))
    assert other.status_code == 404, other.text


@pytest.mark.e2e
def test_the_tool_summary_reads_no_step_rows(corpus, corpus_rows, test_client):
    report_id = corpus.reports[CONV]
    _has_step_rows(corpus_rows, report_id)

    with _StepRowReads() as reads:
        summary = test_client.post("/api/tools/session_summary", json={"report_id": report_id},
                                   headers=as_(corpus, "owner"))
    assert summary.status_code == 200, summary.text
    # The corpus's agents carry no connection tools: nothing to decide.
    assert summary.json()["has_non_read_tools"] is False
    assert reads.statements == []


@pytest.mark.e2e
def test_marking_a_conversation_viewed_reads_no_step_rows(corpus, corpus_rows, test_client):
    report_id = corpus.reports[CONV]
    _has_step_rows(corpus_rows, report_id)

    with _StepRowReads() as reads:
        viewed = test_client.post(f"/api/reports/{report_id}/viewed", headers=as_(corpus, "owner"))
    assert viewed.status_code == 200, viewed.text
    assert viewed.json()["id"] == report_id and viewed.json()["viewed_at"]
    assert reads.statements == []
    row = _one(corpus_rows, "SELECT COUNT(*) AS n FROM report_views WHERE report_id = :r AND user_id = :u",
               r=report_id, u=corpus.cast.id("owner"))
    assert row["n"] == 1


@pytest.mark.e2e
def test_the_editors_refresh_on_open_reads_no_step_rows(corpus, corpus_rows, test_client):
    """POST /api/r/{id}/rerun?artifact_id=<version open in the editor>,
    declined by that dashboard's own setting."""
    aid = own(corpus_rows, corpus, DASH)
    report_id = corpus.reports[CONV]
    _has_step_rows(corpus_rows, report_id)
    # The conversation's own column: no route writes it once the conversation
    # has a dashboard (POST /api/reports/{id}/schedule sets the dashboard's
    # instead; the column is the migration's backup), so it is set directly.
    corpus_rows.update("reports", report_id, refresh_on_view=True)
    # Through the dashboards' settings: the other dashboard refreshes when
    # opened, the one open in the editor (aid) does not.
    for dashboard_id, on in ((own(corpus_rows, corpus, f"{CONV}/dash_stock"), True), (aid, False)):
        settings = test_client.put(f"/api/artifacts/{dashboard_id}/settings", json={"refresh_on_view": on},
                                   headers=as_(corpus, "owner"))
        assert settings.status_code == 200, settings.text
        assert settings.json()["refresh_on_view"] is on

    with _StepRowReads() as reads:
        resp = test_client.post(f"/api/r/{report_id}/rerun", params={"artifact_id": corpus.ids[f"{DASH}/v1"]},
                                headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped"] is True and resp.json()["steps_total"] == 0
    assert reads.statements == []
