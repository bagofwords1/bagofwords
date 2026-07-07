"""Scheduled prompt routing: report-per-run (spawn_new_report) vs host report.

Deterministic — the agent run is stubbed (CompletionService.create_completion
recorded), the run is invoked directly with force=True (bypasses the
cross-worker claim). Companion to tests/e2e/test_triggers.py.
"""
import asyncio
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _setup_user(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


def _stub_agent_run(monkeypatch, calls):
    from app.services.completion_service import CompletionService

    async def fake_create_completion(self, db, report_id, completion_data,
                                     current_user, organization, background=False, **kw):
        calls.append({
            "report_id": str(report_id),
            "content": completion_data.prompt.content,
            "scheduled_prompt_id": kw.get("scheduled_prompt_id"),
        })
        return None

    monkeypatch.setattr(CompletionService, "create_completion", fake_create_completion)


def _run(sp_id):
    from app.services.scheduled_prompt_service import scheduled_prompt_service
    asyncio.run(scheduled_prompt_service.scheduled_run_prompt(sp_id, force=True))


def _reports_by_sp(get_reports, token, org_id, sp_id):
    reports = get_reports(user_token=token, org_id=org_id)
    items = reports.get("reports", reports) if isinstance(reports, dict) else reports
    return [r for r in items if r.get("scheduled_prompt_id") == sp_id]


@pytest.mark.e2e
def test_spawn_new_report_routing(
    monkeypatch, create_user, login_user, whoami, test_client,
    create_report, create_data_source, dynamic_sqlite_db, get_reports,
):
    """spawn_new_report=True → each run lands in a fresh, dated, stamped report
    with the host report's agents attached."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    ds = create_data_source(
        name="Sched Spawn DB", type="sqlite",
        config={"database": dynamic_sqlite_db}, credentials={},
        user_token=token, org_id=org_id,
    )
    host = create_report(title="Weekly revenue", user_token=token, org_id=org_id,
                         data_sources=[ds["id"]])

    resp = test_client.post(
        f"/api/reports/{host['id']}/scheduled-prompts",
        json={"prompt": {"content": "Summarize weekly revenue"},
              "cron_schedule": "0 9 * * 1", "spawn_new_report": True},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    sp = resp.json()
    assert sp["spawn_new_report"] is True

    calls = []
    _stub_agent_run(monkeypatch, calls)
    _run(sp["id"])

    spawned = _reports_by_sp(get_reports, token, org_id, sp["id"])
    assert len(spawned) == 1, "expected one spawned run report"
    run_report = spawned[0]
    assert run_report["id"] != host["id"]
    assert run_report["title"].startswith("Weekly revenue — ")
    assert [d["id"] for d in run_report["data_sources"]] == [ds["id"]]

    # The agent ran in the SPAWNED report, not the host
    assert len(calls) == 1
    assert calls[0]["report_id"] == run_report["id"]
    assert calls[0]["scheduled_prompt_id"] == sp["id"]

    # A second run spawns a second, independent report
    _run(sp["id"])
    assert len(_reports_by_sp(get_reports, token, org_id, sp["id"])) == 2


@pytest.mark.e2e
def test_default_routing_runs_in_host_report(
    monkeypatch, create_user, login_user, whoami, test_client,
    create_report, get_reports,
):
    """Default (spawn_new_report=False) keeps today's behavior: the run lands
    in the host report and nothing is spawned."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    host = create_report(title="Host report", user_token=token, org_id=org_id)

    resp = test_client.post(
        f"/api/reports/{host['id']}/scheduled-prompts",
        json={"prompt": {"content": "Run in place"}, "cron_schedule": "0 9 * * *"},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    sp = resp.json()
    assert sp["spawn_new_report"] is False

    calls = []
    _stub_agent_run(monkeypatch, calls)
    _run(sp["id"])

    assert len(calls) == 1
    assert calls[0]["report_id"] == host["id"]
    assert _reports_by_sp(get_reports, token, org_id, sp["id"]) == []


@pytest.mark.e2e
def test_routing_is_updatable(
    create_user, login_user, whoami, test_client, create_report,
):
    token, org_id = _setup_user(create_user, login_user, whoami)
    host = create_report(title="Toggle host", user_token=token, org_id=org_id)
    sp = test_client.post(
        f"/api/reports/{host['id']}/scheduled-prompts",
        json={"prompt": {"content": "t"}, "cron_schedule": "0 9 * * *"},
        headers=_headers(token, org_id),
    ).json()

    up = test_client.put(
        f"/api/reports/{host['id']}/scheduled-prompts/{sp['id']}",
        json={"spawn_new_report": True},
        headers=_headers(token, org_id),
    )
    assert up.status_code == 200, up.json()
    assert up.json()["spawn_new_report"] is True
