"""A reader who must sign in is told so (found in manual testing, phase 8).

A connection each person signs in to (Power BI, user_required) answers a
reader without an active sign-in with "Connect required: ...". That is a
reader who HAS access and needs to connect -- the gate's "connect your
account" state -- not a broken connection, and not a refusal ("no access").
Before: the cause was matched by text ("credentials required") and missed,
so it read as connection_failed; and the chart still ran without the client,
so its stored failure was the generated code's bare key lookup
("'<source>:<connection>'"), which the page then showed.

Now the cause is typed (CredentialsRequiredError), the chart gets a stand-in
client that fails with that cause, and the reader's stored result carries a
fixed reason and the `credentials_required` code, on /d and on /r alike.
"""
import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

SOURCE = "מחסן אישי"
USES_THE_CLIENT = (
    "def generate_df(ds_clients, excel_files):\n"
    f"    return ds_clients['{SOURCE}'].execute_query('EVALUATE Sales')\n"
)


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _assert_told_to_connect(run, step):
    from app.services.access_errors import CREDENTIALS_REQUIRED_REASON

    assert run["data_source_errors"], run
    assert run["data_source_errors"][0]["code"] == "credentials_required"
    result = step["viewer_result"]
    assert result["status"] == "error" and result["error_code"] == "credentials_required"
    assert result["status_reason"] == CREDENTIALS_REQUIRED_REASON
    assert SOURCE not in result["status_reason"]  # never the code's bare client key


@pytest.mark.e2e
def test_a_reader_who_must_sign_in_is_told_so_on_d(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "user_required/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                 "WHERE q.artifact_release_id = :r", r=release)
    corpus_rows.update("steps", copy["step"], code=USES_THE_CLIENT)
    run = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member"))
    assert run.status_code == 200, run.text
    step = test_client.get(f"/api/d/{aid}/queries/{copy['id']}/step", headers=as_(corpus, "member"))
    _assert_told_to_connect(run.json(), step.json())


@pytest.mark.e2e
def test_a_reader_who_must_sign_in_is_told_so_on_r(corpus, corpus_rows, test_client):
    report = corpus.reports["user_required"]
    (query,) = _rows(corpus_rows, "SELECT q.id, s.id AS step FROM queries q JOIN steps s ON s.id = q.default_step_id "
                                  "WHERE q.report_id = :r AND q.artifact_release_id IS NULL", r=report)
    corpus_rows.update("steps", query["step"], code=USES_THE_CLIENT)
    run = test_client.post(f"/api/r/{report}/run", headers=as_(corpus, "member"))
    assert run.status_code == 200, run.text
    step = test_client.get(f"/api/r/{report}/queries/{query['id']}/step", headers=as_(corpus, "member"))
    _assert_told_to_connect(run.json(), step.json())


@pytest.mark.e2e
def test_no_access_stays_no_access():
    """A refusal is still told apart from a missing sign-in."""
    from fastapi import HTTPException

    from app.services.access_errors import CredentialsRequiredError, source_error_code

    assert source_error_code(CredentialsRequiredError("Connect required: connect your account")) == "credentials_required"
    assert source_error_code(HTTPException(403, "You do not have access to data source 'x'")) == "no_access"
    assert source_error_code(RuntimeError("timeout")) == "connection_failed"
