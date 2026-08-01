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


def _stub_agent_run(monkeypatch, calls, status="success", error=None):
    """Record the run — and write the system completion the real service would,
    since the scheduler decides success or failure by reading that row back."""
    from app.models.completion import Completion
    from app.services.completion_service import CompletionService

    async def fake_create_completion(self, db, report_id, completion_data,
                                     current_user, organization, background=False, **kw):
        calls.append({
            "report_id": str(report_id),
            "content": completion_data.prompt.content,
            "scheduled_prompt_id": kw.get("scheduled_prompt_id"),
        })
        db.add(Completion(
            prompt={"content": completion_data.prompt.content},
            completion={"content": "done", **({"error": error} if error else {})},
            model="stub", report_id=str(report_id), turn_index=0,
            role="system", status=status, user_id=str(current_user.id),
        ))
        await db.commit()
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


@pytest.mark.e2e
def test_spawned_runs_stay_in_the_host_report_project(
    monkeypatch, create_user, login_user, whoami, test_client,
    create_report, create_project, get_reports,
):
    """A schedule that lives in a project keeps its dated runs there instead of
    scattering them to the root."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    project = create_project(name="Revenue", user_token=token, org_id=org_id)
    host = create_report(title="Revenue watch", user_token=token, org_id=org_id)
    moved = test_client.put(
        f"/api/reports/{host['id']}",
        json={"project_id": project["id"]},
        headers=_headers(token, org_id),
    )
    assert moved.status_code == 200, moved.json()

    sp = test_client.post(
        f"/api/reports/{host['id']}/scheduled-prompts",
        json={"prompt": {"content": "Weekly check"}, "title": "Weekly check",
              "cron_schedule": "0 9 * * 1", "spawn_new_report": True},
        headers=_headers(token, org_id),
    ).json()

    _stub_agent_run(monkeypatch, [])
    _run(sp["id"])

    spawned = _reports_by_sp(get_reports, token, org_id, sp["id"])
    assert len(spawned) == 1
    assert spawned[0]["project_id"] == project["id"]

    # The task is listed among the project's automations, under its title.
    autos = test_client.get(f"/api/projects/{project['id']}/automations",
                            headers=_headers(token, org_id)).json()
    tasks = [a for a in autos if a["kind"] == "task"]
    assert [a["id"] for a in tasks] == [sp["id"]]
    assert tasks[0]["label"] == "Weekly check"


@pytest.mark.e2e
def test_run_history_lists_the_reports_a_schedule_produced(
    monkeypatch, create_user, login_user, whoami, test_client, create_report,
):
    """The modal's Previous runs column: report-per-run mode gives one row per
    fire, newest first."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    host = create_report(title="Host", user_token=token, org_id=org_id)
    sp = test_client.post(
        f"/api/reports/{host['id']}/scheduled-prompts",
        json={"prompt": {"content": "Check"}, "title": "Check",
              "cron_schedule": "0 9 * * 1", "spawn_new_report": True},
        headers=_headers(token, org_id),
    ).json()

    _stub_agent_run(monkeypatch, [])
    _run(sp["id"])
    _run(sp["id"])

    runs = test_client.get(
        f"/api/reports/{host['id']}/scheduled-prompts/{sp['id']}/runs",
        headers=_headers(token, org_id),
    ).json()
    assert runs["spawns_reports"] is True
    assert runs["total"] == 2
    assert len(runs["runs"]) == 2
    # Newest first, and each row points at a real spawned report.
    assert runs["runs"][0]["created_at"] >= runs["runs"][1]["created_at"]
    assert all(r["title"].startswith("Check —") for r in runs["runs"])


@pytest.mark.e2e
def test_run_history_for_a_host_report_schedule(
    monkeypatch, create_user, login_user, whoami, test_client, create_report,
):
    """Host-report mode appends every run to one report, so there is no
    per-run history — the caller gets that single report and a flag saying so."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    host = create_report(title="Host", user_token=token, org_id=org_id)
    sp = test_client.post(
        f"/api/reports/{host['id']}/scheduled-prompts",
        json={"prompt": {"content": "Check"}, "cron_schedule": "0 9 * * 1",
              "spawn_new_report": False},
        headers=_headers(token, org_id),
    ).json()

    _stub_agent_run(monkeypatch, [])
    _run(sp["id"])

    runs = test_client.get(
        f"/api/reports/{host['id']}/scheduled-prompts/{sp['id']}/runs",
        headers=_headers(token, org_id),
    ).json()
    assert runs["spawns_reports"] is False
    assert [r["report_id"] for r in runs["runs"]] == [host["id"]]


@pytest.mark.e2e
def test_run_history_carries_each_run_verdict(
    monkeypatch, create_user, login_user, whoami, test_client, create_report,
):
    """The runs column has to distinguish a run that worked from one that died.

    Without a per-run verdict the history is just a list of links, and the
    failure the owner was emailed about is invisible the moment they open the
    modal to look for it."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    host = create_report(title="Verdicts", user_token=token, org_id=org_id)
    sp = test_client.post(
        f"/api/reports/{host['id']}/scheduled-prompts",
        json={"prompt": {"content": "Check"}, "title": "Check",
              "cron_schedule": "0 9 * * 1", "spawn_new_report": True},
        headers=_headers(token, org_id),
    ).json()

    _stub_agent_run(monkeypatch, [], status="error", error={"code": "auth"})
    _run(sp["id"])
    _stub_agent_run(monkeypatch, [], status="success")
    _run(sp["id"])

    runs = test_client.get(
        f"/api/reports/{host['id']}/scheduled-prompts/{sp['id']}/runs",
        headers=_headers(token, org_id),
    ).json()["runs"]
    assert [r["status"] for r in runs] == ["success", "error"], "newest first"


@pytest.mark.e2e
def test_schedule_list_reports_the_last_run_verdict(
    monkeypatch, create_user, login_user, whoami, test_client, create_report,
):
    """A broken schedule must be visible in the list without opening it —
    and must go back to clean once a later run succeeds."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    host = create_report(title="List verdict", user_token=token, org_id=org_id)
    sp = test_client.post(
        f"/api/reports/{host['id']}/scheduled-prompts",
        json={"prompt": {"content": "Check"}, "title": "Listed",
              "cron_schedule": "0 9 * * 1", "spawn_new_report": True},
        headers=_headers(token, org_id),
    ).json()

    def _listed():
        items = test_client.get("/api/scheduled-prompts", headers=_headers(token, org_id)).json()
        return next(p for p in items["scheduled_prompts"] if p["id"] == sp["id"])

    assert _listed()["last_run_status"] is None, "never run → nothing to report"

    _stub_agent_run(monkeypatch, [], status="error", error={"code": "auth"})
    _run(sp["id"])
    assert _listed()["last_run_status"] == "error"

    _stub_agent_run(monkeypatch, [], status="success")
    _run(sp["id"])
    assert _listed()["last_run_status"] == "success", "a later good run clears the flag"
