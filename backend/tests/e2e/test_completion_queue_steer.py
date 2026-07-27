"""Prompt queue + steering contract tests.

Covers the PromptBoxV2 queue/steer feature:
- ``POST /api/reports/{id}/completions`` with ``queue: true`` persists a
  role='user' status='queued' row and does NOT start a second concurrent run
  while one is in progress.
- ``DELETE /api/completions/{id}/queued`` removes a still-queued prompt.
- ``POST /api/completions/{id}/steer`` records a steering user message tied to
  the running system completion, and degrades to enqueueing when the run has
  already finished.
- The dispatcher starts a queued prompt when nothing is running and drains the
  queue after a successful run.

The agent loop is stubbed at the AgentV2.main_execution boundary (the LLM
boundary for these flows): a "hanging" stub leaves the run in_progress so
in-flight states are deterministic; a "success" stub completes the run so the
dispatcher chain can be observed. Everything else (routes, services, DB,
dispatcher) runs real.
"""
import time
import uuid

import pytest

from app.ai.agent_v2 import AgentV2


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


def _stub_hang(monkeypatch):
    """Agent 'runs forever': returns without touching status, so the system
    completion stays in_progress and the dispatcher must not start anything."""
    async def fake_main_execution(self):
        return None
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _stub_success(monkeypatch):
    """Agent completes instantly and successfully, no LLM involved."""
    async def fake_main_execution(self):
        self.system_completion.status = "success"
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _get_completions(test_client, report_id, token, org_id):
    r = test_client.get(f"/api/reports/{report_id}/completions?limit=50", headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    return r.json()["completions"]


def _wait_until(predicate, timeout=15.0, interval=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


@pytest.mark.e2e
def test_queue_while_running_then_remove_and_steer(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    _stub_hang(monkeypatch)
    report = create_report(title=f"queue-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])

    # Start a run that stays in_progress
    r = test_client.post(
        f"/api/reports/{report['id']}/completions?background=true",
        json={"prompt": {"content": "first question", "mentions": [{}]}},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    system = next(c for c in r.json()["completions"] if c["role"] == "system")
    assert system["status"] == "in_progress"

    # Queue a prompt while the run is live
    r = test_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "second question", "mentions": [{}]}, "queue": True},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    queued = r.json()["completions"][0]
    assert queued["role"] == "user"
    assert queued["status"] == "queued"

    # The queued prompt must NOT have started a second run (one system row only)
    comps = _get_completions(test_client, report["id"], token, org_id)
    assert len([c for c in comps if c["role"] == "system"]) == 1
    assert any(c["id"] == queued["id"] and c["status"] == "queued" for c in comps)

    # Remove it from the queue
    r = test_client.delete(f"/api/completions/{queued['id']}/queued", headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    comps = _get_completions(test_client, report["id"], token, org_id)
    assert not any(c["id"] == queued["id"] for c in comps)
    # A second delete is not valid — the row is gone
    r = test_client.delete(f"/api/completions/{queued['id']}/queued", headers=_headers(token, org_id))
    assert r.status_code in (404, 409)

    # Steer the running completion
    r = test_client.post(
        f"/api/completions/{system['id']}/steer",
        json={"content": "actually, focus on last month only"},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["status"] == "steered"
    comps = _get_completions(test_client, report["id"], token, org_id)
    steering = next(c for c in comps if c["id"] == body["completion_id"])
    assert steering["role"] == "user"
    assert steering["message_type"] == "steering"
    assert steering["parent_id"] == system["id"]

    # Steering with nothing to say is rejected
    r = test_client.post(
        f"/api/completions/{system['id']}/steer", json={}, headers=_headers(token, org_id)
    )
    assert r.status_code == 400

    # Promote a queued row into the run ("steer now")
    r = test_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "third question", "mentions": [{}]}, "queue": True},
        headers=_headers(token, org_id),
    )
    queued2 = r.json()["completions"][0]
    r = test_client.post(
        f"/api/completions/{system['id']}/steer",
        json={"queued_completion_id": queued2["id"]},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "steered"
    comps = _get_completions(test_client, report["id"], token, org_id)
    promoted = next(c for c in comps if c["id"] == queued2["id"])
    assert promoted["status"] != "queued"
    assert promoted["message_type"] == "steering"
    assert promoted["parent_id"] == system["id"]


def _drive_dispatcher(report_id, prev_system_id):
    """Run the dispatcher on a private loop and let its fire-and-forget
    dispatched-agent task finish before the loop closes.

    The sync TestClient tears down each request's event loop when the response
    returns, killing any asyncio task the route spawned — so the dispatcher's
    behavior can't be observed through HTTP alone in tests. Driving the public
    service method directly is the deterministic equivalent of what the
    agent-run ``finally`` blocks do in production.
    """
    import asyncio
    from app.services.completion_service import CompletionService

    async def _run():
        await CompletionService().start_next_queued_if_idle(report_id, prev_system_id)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=30)

    asyncio.run(_run())


@pytest.mark.e2e
def test_dispatcher_pauses_on_stop_and_drains_after_success(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    _stub_hang(monkeypatch)
    report = create_report(title=f"drain-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])

    # A run is in progress; a prompt waits in the queue.
    r = test_client.post(
        f"/api/reports/{report['id']}/completions?background=true",
        json={"prompt": {"content": "long run", "mentions": [{}]}},
        headers=_headers(token, org_id),
    )
    system = next(c for c in r.json()["completions"] if c["role"] == "system")
    r = test_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "waiting in line", "mentions": [{}]}, "queue": True},
        headers=_headers(token, org_id),
    )
    queued_id = r.json()["completions"][0]["id"]

    # The user stops the run: the queue must PAUSE, not burn through prompts.
    r = test_client.post(f"/api/completions/{system['id']}/sigkill", headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    _drive_dispatcher(report["id"], system["id"])
    comps = _get_completions(test_client, report["id"], token, org_id)
    assert any(c["id"] == queued_id and c["status"] == "queued" for c in comps), \
        "queue must stay paused after a stopped run"
    assert len([c for c in comps if c["role"] == "system"]) == 1

    # An idle dispatch (no previous-run gate) drains the queue: the queued row
    # becomes a normal turn with its own successful system completion.
    _stub_success(monkeypatch)
    _drive_dispatcher(report["id"], None)
    comps = _get_completions(test_client, report["id"], token, org_id)
    head = next(c for c in comps if c["id"] == queued_id)
    assert head["status"] != "queued"
    dispatched = [c for c in comps if c["role"] == "system" and c["parent_id"] == queued_id]
    assert len(dispatched) == 1
    assert dispatched[0]["status"] == "success"
    assert not any(c["status"] == "queued" for c in comps)


@pytest.mark.e2e
def test_steer_falls_back_to_queue_when_run_not_in_progress(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    _stub_hang(monkeypatch)
    report = create_report(title=f"fallback-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])

    r = test_client.post(
        f"/api/reports/{report['id']}/completions?background=true",
        json={"prompt": {"content": "quick run", "mentions": [{}]}},
        headers=_headers(token, org_id),
    )
    system = next(c for c in r.json()["completions"] if c["role"] == "system")

    # Finish the run through the public stop endpoint, then steer it: too late,
    # so the message degrades to a queued prompt instead of being lost.
    r = test_client.post(f"/api/completions/{system['id']}/sigkill", headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    r = test_client.post(
        f"/api/completions/{system['id']}/steer",
        json={"content": "too late to steer this"},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "queued"


@pytest.mark.e2e
def test_queue_and_steer_reject_invalid_targets_and_anonymous_callers(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    _stub_hang(monkeypatch)
    report = create_report(title=f"scope-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])
    r = test_client.post(
        f"/api/reports/{report['id']}/completions?background=true",
        json={"prompt": {"content": "a run", "mentions": [{}]}},
        headers=_headers(token, org_id),
    )
    comps = r.json()["completions"]
    head = next(c for c in comps if c["role"] == "user")
    system = next(c for c in comps if c["role"] == "system")

    # Steer target must be the system completion, not the user head
    r = test_client.post(
        f"/api/completions/{head['id']}/steer",
        json={"content": "wrong target"},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 400
    # Unknown ids are 404
    r = test_client.post(
        f"/api/completions/{uuid.uuid4()}/steer",
        json={"content": "ghost"},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 404
    # Only still-queued rows can be dequeued (the head is a normal turn)
    r = test_client.delete(f"/api/completions/{head['id']}/queued", headers=_headers(token, org_id))
    assert r.status_code == 409

    # Anonymous callers are rejected on all new endpoints
    assert test_client.post(
        f"/api/completions/{system['id']}/steer", json={"content": "x"}
    ).status_code == 401
    assert test_client.delete(f"/api/completions/{head['id']}/queued").status_code == 401
    assert test_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "x", "mentions": [{}]}, "queue": True},
    ).status_code == 401
