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
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

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


def _hold_first_provider_call(monkeypatch, watched_prompt=None):
    """Hold the first real planner request at the LLM boundary; later calls
    receive a scripted final answer while AgentV2 and completion services run.
    """
    import asyncio
    import threading

    from app.ai.llm.llm import LLM
    from app.ai.llm.types import MessageStopEvent, TextDeltaEvent, UsageEvent

    first_call = threading.Event()
    second_call = threading.Event()
    watched_call = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    call_count = 0

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        nonlocal call_count
        with lock:
            call_count += 1
            current = call_count
        if watched_prompt and watched_prompt in repr(messages):
            watched_call.set()
        if current == 1:
            first_call.set()
            await asyncio.to_thread(release.wait, 30)
        elif current == 2:
            second_call.set()
        yield TextDeltaEvent(text="The requested check is complete.")
        yield MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")
        yield UsageEvent(input_tokens=64, output_tokens=24)

    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    monkeypatch.setattr(LLM, "inference_stream_v2", scripted)
    return first_call, second_call, release, watched_call


def _get_completions(test_client, report_id, token, org_id):
    r = test_client.get(f"/api/reports/{report_id}/completions?limit=50", headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    return r.json()["completions"]


@pytest.fixture
def live_client():
    """Keep background tasks on one event loop for queue dispatch tests."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield client


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


def _drive_dispatcher(report_id, prev_system_id, portal=None):
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
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            # The live TestClient also owns app-lifespan tasks which should
            # remain pending. Wait only for the durable dispatch workers.
            pending = [
                task for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_coro().__qualname__.endswith("CompletionService._run_dispatched_agent")
            ]
            if not pending:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            done, still_pending = await asyncio.wait(pending, timeout=remaining)
            if still_pending and not done:
                return

    if portal is None:
        asyncio.run(_run())
    else:
        portal.call(_run)


def _wait_for_automatic_queue_drain(portal, first_head_id, timeout=30.0):
    """Wait for the original run's own dispatcher chain to settle.

    Do not manually re-dispatch with the original successful completion after
    an unauthorized head fails: that would reset the dispatcher's prior-status
    gate and mask a queue-stall regression.
    """
    import asyncio

    from app.models.completion import Completion
    from app.settings.database import create_async_session_factory

    async def _wait():
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            async with create_async_session_factory()() as db:
                first_system = (await db.execute(
                    select(Completion).where(
                        Completion.parent_id == first_head_id,
                        Completion.role == "system",
                    )
                )).scalar_one_or_none()
            workers = [
                task for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_coro().__qualname__.endswith("CompletionService._run_dispatched_agent")
            ]
            if first_system is not None and first_system.status != "in_progress" and not workers:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("the automatic queue dispatch did not settle")

    portal.call(_wait)


@pytest.mark.parametrize("streaming", [False, True], ids=["direct", "stream"])
@pytest.mark.e2e
def test_busy_run_is_queued_only_when_completion_opts_in(
    streaming, monkeypatch, live_client, create_report, create_user, login_user,
    whoami, create_llm_provider_and_models,
):
    """The busy-only opt-in starts normally while idle and queues only when
    contention occurs; callers without it retain their normal direct/SSE path.
    """
    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    first_call, second_call, release, _ = _hold_first_provider_call(monkeypatch)
    report = create_report(title=f"opt-in-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])

    try:
        first = live_client.post(
            f"/api/reports/{report['id']}/completions?background=true",
            json={
                "queue_if_busy": True,
                "prompt": {"content": "keep a run active", "mentions": [{}]},
            },
            headers=_headers(token, org_id),
        )
        assert first.status_code == 200, first.text
        running = next(c for c in first.json()["completions"] if c["role"] == "system")
        assert running["status"] == "in_progress"
        assert first_call.wait(timeout=20), "the first parent never reached the provider boundary"

        second_payload = {"prompt": {"content": "start this without queueing", "mentions": [{}]}}
        if streaming:
            second_payload["stream"] = True
        second = live_client.post(
            f"/api/reports/{report['id']}/completions",
            json=second_payload,
            headers=_headers(token, org_id),
        )
        assert second.status_code == 200, second.text
        if streaming:
            assert second.headers["content-type"].startswith("text/event-stream")
        assert second_call.wait(timeout=20), "the non-queued turn did not reach the provider"

        completions = _get_completions(live_client, report["id"], token, org_id)
        assert sum(c["role"] == "system" for c in completions) == 2
        assert not any(c["role"] == "user" and c["status"] == "queued" for c in completions)
    finally:
        release.set()


@pytest.mark.e2e
def test_expired_execution_heartbeat_does_not_strand_busy_opt_in_prompt(
    monkeypatch, live_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    """A dead worker's old in-progress row cannot absorb every later prompt."""
    from app.models.completion import Completion
    from app.services import completion_service as completion_service_module
    from app.settings.database import create_async_session_factory

    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    monkeypatch.setattr(completion_service_module, "_COMPLETION_STALE_AFTER_SECONDS", 30.0)
    first_call, _, release, _ = _hold_first_provider_call(monkeypatch)
    report = create_report(title=f"stale-run-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])
    user_id = whoami(token)["id"]
    stale_id = str(uuid.uuid4())
    stale_at = datetime.utcnow() - timedelta(seconds=60)

    async def _seed_stale_execution():
        session_factory = create_async_session_factory()
        async with session_factory() as db:
            db.add(Completion(
                id=stale_id, report_id=report["id"], user_id=user_id,
                role="system", status="in_progress", model="stale-test-model",
                prompt={}, completion={"content": ""}, turn_index=0,
                created_at=stale_at, updated_at=stale_at,
            ))
            await db.commit()

    try:
        live_client.portal.call(_seed_stale_execution)
        response = live_client.post(
            f"/api/reports/{report['id']}/completions?background=true",
            json={
                "queue_if_busy": True,
                "prompt": {"content": "recover from a crashed run", "mentions": [{}]},
            },
            headers=_headers(token, org_id),
        )
        assert response.status_code == 200, response.text
        created = response.json()["completions"]
        assert any(c["role"] == "system" and c["status"] == "in_progress" and c["id"] != stale_id
                   for c in created), "a stale row must be retired so this request starts normally"
        assert first_call.wait(timeout=20), "the recovered turn never reached the provider boundary"

        async def _read_stale_row():
            session_factory = create_async_session_factory()
            async with session_factory() as db:
                return await db.get(Completion, stale_id)

        stale = live_client.portal.call(_read_stale_row)
        assert stale.status == "error"
        assert (stale.completion or {}).get("error_type") == "stale_execution"
    finally:
        release.set()


@pytest.mark.e2e
def test_old_timestamp_does_not_expire_a_locally_active_long_run(
    monkeypatch, live_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    """A live provider/tool await remains busy even if its row timestamp ages."""
    from app.models.completion import Completion
    from app.services import completion_service as completion_service_module
    from app.settings.database import create_async_session_factory

    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    monkeypatch.setattr(completion_service_module, "_COMPLETION_STALE_AFTER_SECONDS", 0.01)
    monkeypatch.setattr(completion_service_module, "_COMPLETION_HEARTBEAT_SECONDS", 60.0)
    first_call, _, release, _ = _hold_first_provider_call(monkeypatch)
    report = create_report(title=f"live-run-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])
    ghost_id = str(uuid.uuid4())

    try:
        first = live_client.post(
            f"/api/reports/{report['id']}/completions?background=true",
            json={"queue_if_busy": True, "prompt": {"content": "a long valid run", "mentions": [{}]}},
            headers=_headers(token, org_id),
        )
        assert first.status_code == 200, first.text
        running = next(c for c in first.json()["completions"] if c["role"] == "system")
        assert first_call.wait(timeout=20), "the active run never reached the provider boundary"

        async def _age_live_row():
            session_factory = create_async_session_factory()
            async with session_factory() as db:
                system = await db.get(Completion, running["id"])
                system.updated_at = datetime.utcnow() - timedelta(seconds=1)
                # Legacy duplicate rows may be newer than the actual live run.
                # A stale newest row must not mask an older live execution.
                db.add(Completion(
                    id=ghost_id, report_id=report["id"], user_id=system.user_id,
                    role="system", status="in_progress", model="stale-test-model",
                    prompt={}, completion={"content": ""}, turn_index=system.turn_index + 1,
                    created_at=datetime.utcnow(), updated_at=datetime.utcnow() - timedelta(seconds=1),
                ))
                await db.commit()

        live_client.portal.call(_age_live_row)
        queued = live_client.post(
            f"/api/reports/{report['id']}/completions",
            json={"queue_if_busy": True, "prompt": {"content": "wait behind the active run", "mentions": [{}]}},
            headers=_headers(token, org_id),
        )
        assert queued.status_code == 200, queued.text
        user_row = queued.json()["completions"][0]
        assert user_row["role"] == "user" and user_row["status"] == "queued"
        assert queued.json().get("busy_completion_id") == running["id"]

        async def _read_live_row():
            session_factory = create_async_session_factory()
            async with session_factory() as db:
                return await db.get(Completion, running["id"])

        still_live = live_client.portal.call(_read_live_row)
        assert still_live.status == "in_progress"
        async def _read_ghost_row():
            session_factory = create_async_session_factory()
            async with session_factory() as db:
                return await db.get(Completion, ghost_id)

        ghost = live_client.portal.call(_read_ghost_row)
        assert ghost.status == "error"
        assert (ghost.completion or {}).get("error_type") == "stale_execution"
        release.set()
        _wait_for_automatic_queue_drain(live_client.portal, user_row["id"])
    finally:
        release.set()


@pytest.mark.parametrize("principal_state", ["missing", "revoked"], ids=["null-principal", "removed-member"])
@pytest.mark.e2e
def test_unauthorized_queue_head_does_not_strand_later_user_prompt(
    principal_state, monkeypatch, live_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    """An authorization failure fails only that queued row and drains the next
    prompt under its own principal; the dispatcher never borrows the report owner.
    """

    from app.models.completion import Completion
    from app.settings.database import create_async_session_factory

    token, org_id = _setup_org_with_model(
        monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models
    )
    valid_prompt_text = "later prompt from the report owner"
    first_call, second_call, release, valid_turn_call = _hold_first_provider_call(
        monkeypatch, watched_prompt=valid_prompt_text
    )
    report = create_report(title=f"auth-drain-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])
    running = live_client.post(
        f"/api/reports/{report['id']}/completions?background=true",
        json={"prompt": {"content": "active first turn", "mentions": [{}]}},
        headers=_headers(token, org_id),
    )
    assert running.status_code == 200, running.text
    running_system = next(c for c in running.json()["completions"] if c["role"] == "system")

    def queue_prompt(content, caller_token=token):
        response = live_client.post(
            f"/api/reports/{report['id']}/completions",
            json={"prompt": {"content": content, "mentions": [{}]}, "queue": True},
            headers=_headers(caller_token, org_id),
        )
        assert response.status_code == 200, response.text
        return response.json()["completions"][0]

    former_email = None
    former_token = None
    invite = None
    revoked_user_id = None
    if principal_state == "revoked":
        former_email = f"former-member-{uuid.uuid4().hex}@gmail.com"
        invite = live_client.post(
            f"/api/organizations/{org_id}/members",
            json={"organization_id": org_id, "email": former_email, "role": "admin"},
            headers=_headers(token, org_id),
        )
        assert invite.status_code == 200, invite.text
        former_member = create_user(
            name=f"former-member-{uuid.uuid4().hex[:8]}",
            email=former_email,
        )
        former_token = login_user(email=former_member["email"], password=former_member["password"])
        former_user_id = whoami(former_token)["id"]
        invalid_head = queue_prompt("queued prompt with a formerly-authorized member principal")
        removed = live_client.delete(
            f"/api/organizations/{org_id}/members/{invite.json()['id']}",
            headers=_headers(token, org_id),
        )
        assert removed.status_code == 204, removed.text
        revoked_user_id = former_user_id
    else:
        invalid_head = queue_prompt("legacy machine wake without a principal")
    valid_head = queue_prompt(valid_prompt_text)

    async def _seed_invalid_queue_principal():
        # Queue creation is report-owner-only, so the public route cannot
        # enqueue as a collaborator. Model a previously accepted queue row
        # whose actor was later removed, using the real removed user's ID. The
        # null-principal branch also models a legacy machine wake that the
        # public API cannot create.
        session_factory = create_async_session_factory()
        async with session_factory() as db:
            head = await db.get(Completion, invalid_head["id"])
            assert head is not None
            if principal_state == "missing":
                head.user_id = None
                head.trigger_source = "machine_test"
            else:
                head.user_id = revoked_user_id
            await db.commit()

    try:
        live_client.portal.call(_seed_invalid_queue_principal)
        assert first_call.wait(timeout=20), "the active parent never reached the provider boundary"
        release.set()
        assert _wait_until(
            lambda: next((c for c in _get_completions(live_client, report["id"], token, org_id)
                          if c["id"] == running_system["id"]), {}).get("status") == "success",
            timeout=30,
            interval=0.3,
        ), "the active parent did not finish before queue dispatch"
        assert valid_turn_call.wait(timeout=15), (
            "the queued valid prompt never reached the provider after the authorization failure; "
            "do not manually kick the dispatcher here because that masks a stranded queue"
        )
        _wait_for_automatic_queue_drain(live_client.portal, invalid_head["id"])
    finally:
        release.set()

    async def _read_queue_state():
        session_factory = create_async_session_factory()
        async with session_factory() as db:
            machine = await db.get(Completion, invalid_head["id"])
            user = await db.get(Completion, valid_head["id"])
            machine_system = (await db.execute(
                select(Completion).where(Completion.parent_id == invalid_head["id"], Completion.role == "system")
            )).scalar_one_or_none()
            user_system = (await db.execute(
                select(Completion).where(Completion.parent_id == valid_head["id"], Completion.role == "system")
            )).scalar_one_or_none()
            return machine, machine_system, user, user_system

    machine, machine_system, user, user_system = live_client.portal.call(_read_queue_state)
    assert machine.status != "queued"
    assert machine.user_id == (None if principal_state == "missing" else revoked_user_id)
    assert machine_system is not None and machine_system.status == "error"
    assert machine_system.user_id == machine.user_id, "dispatcher must not impersonate the report owner"
    assert user.status != "queued", (
        f"later user prompt remained queued; machine={machine.status}, "
        f"machine_system={machine_system.status if machine_system else None}, "
        f"machine_error={machine_system.completion if machine_system else None}"
    )
    assert user_system is not None and user_system.status == "success"
    assert user_system.user_id == user.user_id


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
