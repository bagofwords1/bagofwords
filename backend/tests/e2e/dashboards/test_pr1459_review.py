"""Review probes: assert the promised collaboration/security contracts."""
import json
import asyncio

from tests.e2e.dashboards.dashboard_setup import as_, own
from tests.e2e.dashboards.test_collaboration import (
    drawn, spawned, _edit, _finish, _grant_editor, _publish_api, _rows,
)


def test_dashboard_owner_does_not_receive_editor_identity_rows(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "identity_param/dash")
    # Generated code has no non-LLM write API; seed a deterministic identity query.
    corpus_rows.update("steps", corpus.ids["identity_param/q_sales/step"], code=(
        "def generate_df(ds_clients, excel_files, params):\n"
        "    import pandas as pd\n"
        "    return pd.DataFrame({'executed_for': [params['UserEmail']]})\n"
    ))
    _grant_editor(test_client, corpus, aid, "member")
    started = _publish_api(test_client, corpus, aid, "member")
    assert started.status_code == 202, started.text
    _finish(spawned)
    status = test_client.get(f"/api/dashboards/{aid}/publish", headers=as_(corpus, "owner")).json()
    assert status["published"] and status["published"]["status"] == "ready", status
    q = test_client.get(f"/api/d/{aid}/queries", headers=as_(corpus, "owner")).json()[0]
    result = test_client.get(f"/api/d/{aid}/queries/{q['id']}/step", headers=as_(corpus, "owner"))
    assert result.status_code == 200, result.text
    print("OWNER_READS_EDITOR_RESULT", result.json())
    assert result.json()["snapshot_withheld"] is True, "Owner received the editor's identity-scoped snapshot"


def test_revoked_editor_cannot_rename_shared_dashboard_via_owned_version(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    _grant_editor(test_client, corpus, aid, "member")
    joined = _edit(test_client, corpus, aid, "member")
    assert joined.status_code == 200, joined.text
    rid = joined.json()["report_id"]
    _finish(spawned)
    vid = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id=:a AND report_id=:r", a=aid, r=rid)[0]["id"]
    _grant_editor(test_client, corpus, aid)
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "member")).status_code == 404
    renamed = test_client.patch(f"/api/artifacts/{vid}", headers=as_(corpus, "member"), json={"title": "Changed after edit permission revoked"})
    print("REVOKED_EDITOR_PATCH", renamed.status_code)
    assert renamed.status_code in (403, 404), renamed.text


def test_consecutive_joiners_keep_all_queries_while_first_hydrates(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    _grant_editor(test_client, corpus, aid, "member", "direct_share")
    first = _edit(test_client, corpus, aid, "member")
    assert first.status_code == 200 and first.json()["rerunning"]
    second = _edit(test_client, corpus, aid, "direct_share")
    assert second.status_code == 200, second.text
    first_queries = _rows(corpus_rows, "SELECT id FROM queries WHERE report_id=:r", r=first.json()["report_id"])
    second_queries = _rows(corpus_rows, "SELECT id FROM queries WHERE report_id=:r", r=second.json()["report_id"])
    _finish(spawned)
    print("CONSECUTIVE_JOIN_QUERY_COUNTS", len(first_queries), len(second_queries), second.json())
    assert len(second_queries) == len(first_queries) > 0


def test_draft_editor_can_run_filter_without_conversation_share(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    _grant_editor(test_client, corpus, aid, "member", "direct_share")
    assert _edit(test_client, corpus, aid, "member").status_code == 200
    _finish(spawned)
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "direct_share")).status_code == 200
    q = test_client.get(f"/api/d/{aid}/draft/queries", headers=as_(corpus, "direct_share")).json()[0]
    # sharedDashboardApi('draft').paramRun now sends the draft-scoped route
    # (the fix this probe asked for); the conversation's own endpoint stays
    # closed to someone the conversation is not shared with.
    result = test_client.post(f"/api/d/{aid}/draft/queries/{q['id']}/run", headers=as_(corpus, "direct_share"),
                              json={"mode": "viewer", "params": {}})
    print("DRAFT_FILTER_RUN", result.status_code, result.text[:300])
    assert result.status_code == 200, result.text
    assert result.json().get("status") == "success", result.text
    # A query of another dashboard is not reachable through this door.
    other = test_client.post(f"/api/d/{aid}/draft/queries/{corpus.ids['conv_one_dash/q_sales']}/run",
                             headers=as_(corpus, "direct_share"), json={"mode": "viewer", "params": {}})
    assert other.status_code == 404, other.text


def test_join_hydrates_hidden_dependencies_before_consumers(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "refs_in_code/dash")
    _grant_editor(test_client, corpus, aid, "member")
    joined = _edit(test_client, corpus, aid, "member")
    assert joined.status_code == 200, joined.text
    _finish(spawned)
    steps = _rows(corpus_rows, "SELECT s.status, s.status_reason, s.title FROM steps s JOIN queries q ON q.default_step_id=s.id WHERE q.report_id=:r", r=joined.json()["report_id"])
    print("DEPENDENT_JOIN", steps)
    assert steps and all(s["status"] == "success" for s in steps), steps


def test_draft_schedule_keeps_report_identity_after_original_conversation_deleted(corpus, corpus_rows, test_client, spawned):
    from app.services.dashboard_refresh import scheduled_refresh
    aid = own(corpus_rows, corpus, "identity_param/dash")
    corpus_rows.update("steps", corpus.ids["identity_param/q_sales/step"], code=(
        "def generate_df(ds_clients, excel_files, params):\n"
        "    import pandas as pd\n"
        "    return pd.DataFrame({'executed_for': [params['UserEmail']]})\n"
    ))
    _grant_editor(test_client, corpus, aid, "member")
    joined = _edit(test_client, corpus, aid, "member")
    assert joined.status_code == 200, joined.text
    rid = joined.json()["report_id"]
    _finish(spawned)
    removed = test_client.delete(f"/api/reports/{corpus.reports['identity_param']}", headers=as_(corpus, "owner"))
    assert removed.status_code == 200, removed.text
    settings = test_client.put(f"/api/dashboards/{aid}/settings", headers=as_(corpus, "owner"), json={"cron_schedule": "0 6 * * *"})
    assert settings.status_code == 200, settings.text
    asyncio.run(scheduled_refresh(aid))
    _finish(spawned)
    steps = _rows(corpus_rows, "SELECT s.data FROM steps s JOIN queries q ON q.default_step_id=s.id WHERE q.report_id=:r", r=rid)
    rendered = json.dumps(steps, default=str)
    print("SCHEDULED_EDITOR_ROWS", rendered)
    assert "owner@corpus.com" not in rendered, "Schedule wrote dashboard owner's data into editor's conversation"
    assert "member@corpus.com" in rendered


def test_publish_rechecks_intervening_publish_after_connection_handshake(corpus, corpus_rows, test_client, spawned, monkeypatch):
    from app.services.data_source_service import DataSourceService
    from tests.e2e.dashboards.test_collaboration import _new_version
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    _grant_editor(test_client, corpus, aid, "member")
    original = DataSourceService.construct_clients
    interleaved = False

    async def handshake(self, *args, **kwargs):
        nonlocal interleaved
        clients = await original(self, *args, **kwargs)
        if not interleaved:
            interleaved = True
            # Another editor completes a publish while this request is waiting
            # on the external client's connection handshake.
            await _new_version(aid, corpus.reports["conv_three_dash"], corpus.cast.id("owner"))
            other = await asyncio.to_thread(_publish_api, test_client, corpus, aid, "owner")
            assert other.status_code == 202, other.text
            while spawned:
                job = spawned.pop(0)
                if asyncio.iscoroutine(job):
                    await job
        return clients

    monkeypatch.setattr(DataSourceService, "construct_clients", handshake)
    attempted = _publish_api(test_client, corpus, aid, "member", version_id=corpus.ids["conv_three_dash/dash_sales/v1"])
    _finish(spawned)
    print("INTERVENING_PUBLISH", attempted.status_code)
    assert interleaved
    assert attempted.status_code == 409, attempted.text
    assert attempted.json()["error_code"] == "dashboard.published_since"
