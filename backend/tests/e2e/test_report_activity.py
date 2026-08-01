"""
E2E tests for report activity badges (GET /reports/activity + POST /reports/{id}/viewed).

Contract under test:
- ``state`` reflects live conversation activity: ``running`` while a system
  completion is in_progress, ``awaiting_user`` when the run paused on the user
  (clarify form / pending tool confirmation), ``idle`` otherwise.
- ``unread`` is per-user: true until *that* user bumps their watermark via
  POST /reports/{id}/viewed, and true again once new activity lands after
  the watermark.
- ``error`` is true when the latest system completion ended in error.
- The endpoint never leaks reports outside the caller's organization.

The agent loop is stubbed at the AgentV2.main_execution boundary (the LLM
boundary): a hanging stub leaves the run in_progress; success/error stubs
finish it deterministically. Routes, services and DB run real.
"""
import time
import uuid

import pytest

from app.ai.agent_v2 import AgentV2
from app.settings.config import settings as bow_settings


@pytest.fixture
def _allow_multiple_orgs():
    """Allow a user to belong to more than one org — needed to construct the
    cross-org isolation scenario."""
    flags = bow_settings.bow_config.features
    saved = (flags.allow_multiple_organizations, flags.allow_uninvited_signups)
    flags.allow_multiple_organizations = True
    flags.allow_uninvited_signups = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations, flags.allow_uninvited_signups = saved


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _setup_org_with_model(monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models):
    # The provider fixture requires the env var but provider/model creation
    # never calls the LLM — a dummy key keeps these tests offline.
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(token, org_id)
    return token, org_id


def _activity(test_client, ids, token, org_id):
    r = test_client.get(
        f"/api/reports/activity?ids={','.join(ids)}",
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    return {a["id"]: a for a in r.json()["activity"]}


def _start_run(test_client, report_id, token, org_id, content="do something"):
    r = test_client.post(
        f"/api/reports/{report_id}/completions?background=true",
        json={"prompt": {"content": content, "mentions": [{}]}},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    return r.json()


def _queue_prompt(test_client, report_id, token, org_id, content="do something"):
    r = test_client.post(
        f"/api/reports/{report_id}/completions",
        json={"prompt": {"content": content, "mentions": [{}]}, "queue": True},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    return r.json()


def _sigkill(test_client, system_id, token, org_id):
    r = test_client.post(f"/api/completions/{system_id}/sigkill", headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()


def _drive_dispatcher(report_id):
    """Run the dispatcher on a private loop so the stubbed agent actually
    executes. The sync TestClient tears down each request's event loop when
    the response returns, killing any asyncio task the route spawned — see
    test_completion_queue_steer.py for the original of this pattern."""
    import asyncio
    from app.services.completion_service import CompletionService

    async def _run():
        await CompletionService().start_next_queued_if_idle(report_id, None)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=30)

    asyncio.run(_run())


def _stub_hang(monkeypatch):
    async def fake_main_execution(self):
        return None
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _stub_finish(monkeypatch, status):
    async def fake_main_execution(self):
        self.system_completion.status = status
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _stub_clarify(monkeypatch):
    """Finish the turn the way ClarifyTool does: completion succeeds and its
    last block is a clarify tool block. Seeded through the ORM inside the
    agent stub because only a live LLM run produces these rows."""
    async def fake_main_execution(self):
        from app.models.agent_execution import AgentExecution
        from app.models.tool_execution import ToolExecution
        from app.models.completion_block import CompletionBlock

        ae = AgentExecution(completion_id=str(self.system_completion.id))
        self.db.add(ae)
        await self.db.flush()
        te = ToolExecution(
            agent_execution_id=str(ae.id),
            tool_name="clarify",
            tool_action="clarify",
            arguments_json={"questions": [{"text": "Which range?"}]},
            status="success",
            success=True,
        )
        self.db.add(te)
        await self.db.flush()
        self.db.add(CompletionBlock(
            completion_id=str(self.system_completion.id),
            agent_execution_id=str(ae.id),
            source_type="tool",
            tool_execution_id=str(te.id),
            block_index=0,
            title="Clarify",
            status="completed",
        ))
        self.system_completion.status = "success"
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


@pytest.mark.e2e
def test_new_report_is_unread_until_viewed_per_user(
    test_client, create_report, create_user, login_user, whoami,
):
    """unread is a per-user watermark: viewing clears it for that user only."""
    email1 = f"act_owner_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email1, password="test123")
    token1 = login_user(email=email1, password="test123")
    org_id = whoami(token1)["organizations"][0]["id"]

    email2 = f"act_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email2, "role": "member"},
        headers=_headers(token1, org_id),
    )
    create_user(email=email2, password="test123")
    token2 = login_user(email=email2, password="test123")

    report = create_report(title="Act", user_token=token1, org_id=org_id, data_sources=[])

    a1 = _activity(test_client, [report["id"]], token1, org_id)[report["id"]]
    assert a1["state"] == "idle"
    assert a1["unread"] is True
    assert a1["error"] is False

    r = test_client.post(f"/api/reports/{report['id']}/viewed", headers=_headers(token1, org_id))
    assert r.status_code == 200, r.json()

    assert _activity(test_client, [report["id"]], token1, org_id)[report["id"]]["unread"] is False
    # Independent per user: user2 has not viewed it.
    assert _activity(test_client, [report["id"]], token2, org_id)[report["id"]]["unread"] is True


@pytest.mark.e2e
def test_running_state_while_completion_in_progress(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    _stub_hang(monkeypatch)
    report = create_report(title=f"run-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])
    _start_run(test_client, report["id"], token, org_id)

    a = _activity(test_client, [report["id"]], token, org_id)[report["id"]]
    assert a["state"] == "running"


@pytest.mark.e2e
def test_error_flag_set_and_survives_view_but_unread_clears(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    """A failed run flags error+unread; viewing clears unread (badge hides)."""
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    _stub_hang(monkeypatch)
    report = create_report(title=f"err-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])
    # A hanging run + a queued prompt behind it (running outranks queued).
    started = _start_run(test_client, report["id"], token, org_id)
    system = next(c for c in started["completions"] if c["role"] == "system")
    _queue_prompt(test_client, report["id"], token, org_id)
    assert _activity(test_client, [report["id"]], token, org_id)[report["id"]]["state"] == "running"

    # Stop the run: the queue pauses, so only the parked prompt remains live.
    _sigkill(test_client, system["id"], token, org_id)
    assert _activity(test_client, [report["id"]], token, org_id)[report["id"]]["state"] == "queued"

    # Dispatch the queued prompt through the erroring agent.
    _stub_finish(monkeypatch, "error")
    _drive_dispatcher(report["id"])

    a = _activity(test_client, [report["id"]], token, org_id)[report["id"]]
    assert a["state"] == "idle"
    assert a["error"] is True
    assert a["unread"] is True

    test_client.post(f"/api/reports/{report['id']}/viewed", headers=_headers(token, org_id))
    a = _activity(test_client, [report["id"]], token, org_id)[report["id"]]
    assert a["error"] is True
    assert a["unread"] is False


@pytest.mark.e2e
def test_success_run_is_idle_not_error(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    _stub_hang(monkeypatch)
    report = create_report(title=f"ok-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])
    started = _start_run(test_client, report["id"], token, org_id)
    system = next(c for c in started["completions"] if c["role"] == "system")
    _queue_prompt(test_client, report["id"], token, org_id)
    _sigkill(test_client, system["id"], token, org_id)

    _stub_finish(monkeypatch, "success")
    _drive_dispatcher(report["id"])

    a = _activity(test_client, [report["id"]], token, org_id)[report["id"]]
    assert a["state"] == "idle"
    assert a["error"] is False


@pytest.mark.e2e
def test_clarify_pause_reports_awaiting_user(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    """A turn that ended on the clarify tool shows awaiting_user until the
    user replies (a newer completion clears it)."""
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    _stub_hang(monkeypatch)
    report = create_report(title=f"clar-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])
    started = _start_run(test_client, report["id"], token, org_id)
    system = next(c for c in started["completions"] if c["role"] == "system")
    _queue_prompt(test_client, report["id"], token, org_id)
    _sigkill(test_client, system["id"], token, org_id)

    _stub_clarify(monkeypatch)
    _drive_dispatcher(report["id"])

    a = _activity(test_client, [report["id"]], token, org_id)[report["id"]]
    assert a["state"] == "awaiting_user"

    # The user answers: their reply becomes the newest turn — no longer awaiting.
    _stub_hang(monkeypatch)
    _start_run(test_client, report["id"], token, org_id, content="Last 30 days")
    a = _activity(test_client, [report["id"]], token, org_id)[report["id"]]
    assert a["state"] != "awaiting_user"


@pytest.mark.e2e
def test_activity_scoped_to_organization(
    _allow_multiple_orgs, test_client, create_report, create_user, login_user, whoami, create_organization,
):
    """Reports outside the calling org are silently absent from the response."""
    user1 = create_user()
    token1 = login_user(user1["email"], user1["password"])
    org1 = whoami(token1)["organizations"][0]["id"]
    report = create_report(title="Mine", user_token=token1, org_id=org1, data_sources=[])

    org2 = create_organization(name=f"Other-{uuid.uuid4().hex[:6]}", user_token=token1)
    assert org1 != org2

    out = _activity(test_client, [report["id"]], token1, org2)
    assert report["id"] not in out


def _tick_and_drain(org_id, user_id, n_ticks=1):
    """Drive the live-activity watcher's tick directly and collect the events
    it fans out. The subscriber is installed without subscribe() so no
    background watcher task races the manual ticks — this tests the tick's
    contract (state diff -> events), which is what the SSE route consumes."""
    import asyncio
    from app.streaming.report_activity_hub import report_activity_hub, _Subscriber

    async def _run():
        sub = _Subscriber(user_id=str(user_id))
        report_activity_hub._subs.setdefault(str(org_id), []).append(sub)
        try:
            batches = []
            for _ in range(n_ticks):
                await report_activity_hub._tick(str(org_id))
                batch = []
                while not sub.queue.empty():
                    batch.append(sub.queue.get_nowait())
                batches.append(batch)
            return batches
        finally:
            report_activity_hub.unsubscribe(str(org_id), sub)

    return asyncio.run(_run())


@pytest.mark.e2e
def test_watcher_tick_emits_and_dedups_activity_events(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    """One tick emits a running event for a live report; an unchanged second
    tick emits nothing (signature dedup); after the run stops, a fresh
    subscription's tick reports the terminal idle state."""
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    user_id = whoami(token)["id"]
    _stub_hang(monkeypatch)
    report = create_report(title=f"tick-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])
    started = _start_run(test_client, report["id"], token, org_id)
    system = next(c for c in started["completions"] if c["role"] == "system")

    first, second = _tick_and_drain(org_id, user_id, n_ticks=2)
    ev = next(e for e in first if e["report_id"] == report["id"])
    assert ev["state"] == "running"
    assert ev["last_activity_at"]  # clients re-sort on this
    assert all(e["report_id"] != report["id"] for e in second)

    _sigkill(test_client, system["id"], token, org_id)
    (after_stop,) = _tick_and_drain(org_id, user_id, n_ticks=1)
    ev = next(e for e in after_stop if e["report_id"] == report["id"])
    assert ev["state"] == "idle"
    assert ev["error"] is False  # a user stop is not an error


@pytest.mark.e2e
def test_activity_stream_requires_auth(test_client):
    r = test_client.get("/api/reports/activity/stream")
    assert r.status_code == 401
