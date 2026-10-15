"""The settings a dashboard's viewers get, kept on the dashboard (phase 4).

`/api/dashboards/{id}/settings` reads and writes "run on my behalf"
(shared_run_identity), the Data tab and the viewer chat, for the owner only,
with today's rules: no "run on my behalf" over row-level security, chat
sources from the organization, a chat model the owner may use. Switching
"run on my behalf" drops the viewers' own results on the published copies,
as switching it on a conversation drops them there.
"""
import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish

DEFAULTS = {"shared_run_identity": "viewer", "include_data_tab": True, "chat_enabled": False,
            "chat_data_source_ids": None, "chat_model_id": None,
            # The schedule (phase 6, test_dashboard_refresh.py).
            "cron_schedule": None, "refresh_on_view": True, "notification_subscribers": None}


def _get(test_client, corpus, aid, by="owner"):
    return test_client.get(f"/api/dashboards/{aid}/settings", headers=as_(corpus, by))


def _put(test_client, corpus, aid, body, by="owner"):
    return test_client.put(f"/api/dashboards/{aid}/settings", json=body, headers=as_(corpus, by))


@pytest.mark.e2e
def test_only_the_owner_reads_and_changes_the_settings(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    assert _get(test_client, corpus, aid).json() == DEFAULTS
    for reader, codes in (("member", {403}), ("admin", {403}), ("other_org", {403, 404}), ("anonymous", {401})):
        assert _get(test_client, corpus, aid, by=reader).status_code in codes, reader
        assert _put(test_client, corpus, aid, {"include_data_tab": False}, by=reader).status_code in codes, reader
    assert _get(test_client, corpus, aid).json() == DEFAULTS


@pytest.mark.e2e
def test_each_setting_changes_alone_and_the_conversation_keeps_its_own(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    report = corpus.reports["conv_three_dash"]
    chinook = corpus.cast.data_sources["chinook"]

    def report_settings():
        with corpus_rows.engine.connect() as conn:
            return tuple(conn.execute(text(
                "SELECT shared_run_identity, include_data_tab, artifact_chat_enabled, "
                "artifact_chat_data_source_ids, artifact_chat_model_id FROM reports WHERE id = :r"),
                {"r": report}).one())

    before = report_settings()
    steps = [
        ({"include_data_tab": False}, {"include_data_tab": False}),
        ({"chat_enabled": True}, {"chat_enabled": True}),
        ({"chat_data_source_ids": [chinook]}, {"chat_data_source_ids": [chinook]}),
        ({"chat_data_source_ids": ["*"]}, {"chat_data_source_ids": None}),
        ({"chat_model_id": "org_default"}, {"chat_model_id": "org_default"}),
        ({"chat_model_id": ""}, {"chat_model_id": None}),
        ({"shared_run_identity": "creator"}, {"shared_run_identity": "creator"}),
    ]
    want = dict(DEFAULTS)
    for body, change in steps:
        resp = _put(test_client, corpus, aid, body)
        assert resp.status_code == 200, (body, resp.text)
        want.update(change)
        assert resp.json() == want, body
        assert _get(test_client, corpus, aid).json() == want, body
    assert report_settings() == before


@pytest.mark.e2e
def test_invalid_settings_are_refused_and_change_nothing(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    unknown_source = _put(test_client, corpus, aid, {"chat_data_source_ids": ["00000000-0000-0000-0000-000000000000"]})
    assert unknown_source.status_code == 400 and unknown_source.json()["error_code"] == "data_source.not_found"
    assert _put(test_client, corpus, aid, {"shared_run_identity": "everyone"}).status_code == 422
    assert _put(test_client, corpus, aid, {"chat_model_id": "no-such-model"}).status_code >= 400
    assert _get(test_client, corpus, aid).json() == DEFAULTS


@pytest.mark.e2e
@pytest.mark.parametrize("published", [False, True], ids=["draft", "published"])
def test_run_on_my_behalf_is_refused_over_row_level_security(corpus, corpus_rows, test_client, published):
    aid = own(corpus_rows, corpus, "rls/dash")
    if published:
        publish(aid)
    resp = _put(test_client, corpus, aid, {"shared_run_identity": "creator"})
    assert resp.status_code == 400 and resp.json()["error_code"] == "dashboard.rls_requires_viewer"
    assert _get(test_client, corpus, aid).json()["shared_run_identity"] == "viewer"


@pytest.mark.e2e
def test_switching_run_on_my_behalf_drops_viewer_results_on_the_published_copies_only(
        corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "run_viewer_results/dash")
    publish(aid)
    with corpus_rows.engine.connect() as conn:
        copy_step = conn.execute(text(
            "SELECT s.id FROM steps s JOIN queries q ON q.id = s.query_id "
            "JOIN artifacts a ON a.published_release_id = q.artifact_release_id WHERE a.id = :a"),
            {"a": aid}).scalar_one()
    # A viewer's run of the published copy (viewer runs on /d are part C).
    corpus_rows.insert("step_user_results", step_id=copy_step, user_id=corpus.cast.id("member"),
                       organization_id=corpus.cast.org_id, report_id=None, status="success", executed_as="viewer", params_fingerprint="",
                       data={"rows": [{"x": 1}], "columns": [{"field": "x"}]})

    def counts():
        with corpus_rows.engine.connect() as conn:
            on_copy = conn.execute(text("SELECT count(*) FROM step_user_results WHERE step_id = :s"),
                                   {"s": copy_step}).scalar()
            on_draft = conn.execute(text("SELECT count(*) FROM step_user_results WHERE report_id = :r"),
                                    {"r": corpus.reports["run_viewer_results"]}).scalar()
        return on_copy, on_draft

    on_copy, on_draft = counts()
    assert on_copy == 1 and on_draft > 0
    # A refused save changes nothing, not even the part that came first.
    refused = _put(test_client, corpus, aid, {"shared_run_identity": "creator",
                                              "chat_data_source_ids": ["00000000-0000-0000-0000-000000000000"]})
    assert refused.status_code == 400
    assert counts() == (1, on_draft)
    assert _get(test_client, corpus, aid).json()["shared_run_identity"] == "viewer"
    # Saving the same value keeps them.
    assert _put(test_client, corpus, aid, {"shared_run_identity": "viewer"}).status_code == 200
    assert counts() == (1, on_draft)
    assert _put(test_client, corpus, aid, {"shared_run_identity": "creator"}).status_code == 200
    assert counts() == (0, on_draft)
