"""The admin's view of the sandboxes that exist, and the switches to stop them.

An operator's first need when untrusted code runs on their servers: see what
is running, stop one run, stop one sandbox, delete one sandbox. All of it is
behind ``manage_sandboxes``.
"""
import asyncio
import json
import uuid

import pytest

from app.ee.audit.models import AuditLog
from app.models.sandbox import Sandbox, SandboxEvent, SandboxRun
from app.services.sandbox import errors
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import RunRequest
from app.services.sandbox.testing.sandboxd_stub import get_stub_host

from .conftest import headers, run

pytestmark = pytest.mark.e2e

BASE = "/api/sandbox/sandboxes"
ECHO = RunRequest(language="bash", code="echo ran; echo data > outputs/out.txt")
SLEEP = RunRequest(language="python", code="import time\nprint('go', flush=True)\ntime.sleep(120)")


@pytest.fixture
def world(sandbox_world, enterprise_license):
    sandbox_world.add_compute("fake-1")
    sandbox_world.add_environment("Default", is_default=True)
    return sandbox_world


@pytest.fixture
def admin(world):
    return world.cast["admin"]["headers"]


@pytest.fixture
def report(world, create_report):
    def _new(title=None):
        return create_report(title=title or f"Report {uuid.uuid4().hex[:6]}",
                             user_token=world.cast["admin"]["token"], org_id=world.org_id)
    return _new


def _events(world, sandbox_id):
    rows = world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox_id)
    return [e.type for e in sorted(rows, key=lambda e: (e.created_at, e.id))]


def _audit(world, action, resource_id):
    return [r for r in world.rows(AuditLog, AuditLog.organization_id == world.org_id)
            if r.action == action and r.resource_id == resource_id]


def test_only_manage_sandboxes_sees_and_controls_the_sandboxes(world, test_client, admin, report):
    sandbox_id = world.run_code(report(), world.admin_id, ECHO).sandbox_id
    member = world.cast["member"]["headers"]
    for call in (
        lambda h: test_client.get(BASE, headers=h),
        lambda h: test_client.get(f"{BASE}/{sandbox_id}", headers=h),
        lambda h: test_client.post(f"{BASE}/{sandbox_id}/stop", headers=h),
        lambda h: test_client.post(f"{BASE}/{sandbox_id}/kill-run", headers=h),
        lambda h: test_client.delete(f"{BASE}/{sandbox_id}", headers=h),
    ):
        assert call(member).status_code == 403
    assert world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state == "ready", "nothing happened"
    assert test_client.get(BASE, headers=admin).status_code == 200


def test_the_list_names_what_an_admin_recognises_and_never_shows_a_token(world, test_client, admin, report):
    rep = report("Quarterly sales")
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    world.run_code(report("Other"), world.admin_id, ECHO)

    listed = test_client.get(BASE, headers=admin)
    assert listed.status_code == 200, listed.text
    rows = {r["id"]: r for r in listed.json()}
    assert len(rows) == 2
    row = rows[sandbox_id]
    assert (row["report_title"], row["compute_name"], row["environment_name"], row["state"]) == \
        ("Quarterly sales", "fake-1", "Default", "ready")
    assert row["user_name"] and row["running_run_id"] is None and row["usage"] is None
    token = world.rows(Sandbox, Sandbox.id == sandbox_id)[0].token
    assert token and token not in listed.text and "driver_ref" not in listed.text

    with_usage = {r["id"]: r for r in test_client.get(BASE, params={"usage": "true"}, headers=admin).json()}
    assert with_usage[sandbox_id]["usage"] is not None, "the fake compute reports usage"

    detail = test_client.get(f"{BASE}/{sandbox_id}", headers=admin).json()
    assert [r["status"] for r in detail["runs"]] == ["succeeded"]
    assert "created" in [e["type"] for e in detail["events"]]
    assert token not in json.dumps(detail)
    assert test_client.get(f"{BASE}/{uuid.uuid4()}", headers=admin).status_code == 404


def test_an_admin_stops_one_sandbox_and_it_resumes_with_its_files(world, test_client, admin, report):
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    other_id = world.run_code(report(), world.admin_id, ECHO).sandbox_id

    stopped = test_client.post(f"{BASE}/{sandbox_id}/stop", headers=admin)
    assert stopped.status_code == 200 and stopped.json()["state"] == "stopped", stopped.text
    assert get_stub_host().get(f"sbx-{sandbox_id}").running is False
    assert world.rows(Sandbox, Sandbox.id == other_id)[0].state == "ready", "only the one asked for"
    entry = _audit(world, "sandbox.stopped", sandbox_id)
    assert len(entry) == 1 and entry[0].details["reason"] == "stopped by an admin" and entry[0].user_id == world.admin_id

    again = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="cat outputs/out.txt"))
    assert (again.resumed, again.stdout) == (True, "data\n")


def _with_a_run_in_progress(world, rep, act):
    """Start a long run, call ``act()`` once it is running, and return ``(what act returned, how the run ended)``."""
    async def go():
        started = asyncio.Event()

        async def long_run():
            async with world.session() as db:
                try:
                    return await world.service.run(db, world.report_ref(rep), world.admin_id, SLEEP,
                                                   on_output=lambda kind, chunk: started.set())
                except SandboxError as exc:
                    return exc

        task = asyncio.ensure_future(long_run())
        await asyncio.wait_for(started.wait(), 30)
        result = await asyncio.get_running_loop().run_in_executor(None, act)
        return result, await asyncio.wait_for(task, 60)
    return run(go())


def test_an_admin_stops_a_runaway_run_and_the_sandbox_stays_usable(world, test_client, admin, report):
    rep = report("Runaway")
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id

    def act():
        row = [r for r in test_client.get(BASE, headers=admin).json() if r["id"] == sandbox_id][0]
        assert row["state"] == "busy" and row["running_run_id"] and row["running_since"]
        refused = test_client.post(f"{BASE}/{sandbox_id}/stop", headers=admin)
        assert refused.status_code == 409 and refused.json()["error_code"] == "sandbox.busy", "stop the run first"
        return test_client.post(f"{BASE}/{sandbox_id}/kill-run", headers=admin)

    killed, ended = _with_a_run_in_progress(world, rep, act)
    assert killed.status_code == 200, killed.text
    assert isinstance(ended, SandboxError) and "stopped by an admin" in ended.message

    runs = sorted(world.rows(SandboxRun, SandboxRun.sandbox_id == sandbox_id), key=lambda r: r.started_at)
    assert runs[-1].status == "cancelled" and runs[-1].finished_at is not None
    assert world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state == "ready"
    assert "run_killed" in _events(world, sandbox_id)
    entry = _audit(world, "sandbox.run_killed", sandbox_id)
    assert len(entry) == 1 and entry[0].details["title"] == "Runaway" and entry[0].details["run_id"] == runs[-1].id
    assert world.run_code(rep, world.admin_id, RunRequest(language="bash", code="cat outputs/out.txt")).stdout == "data\n"

    nothing = test_client.post(f"{BASE}/{sandbox_id}/kill-run", headers=admin)
    assert nothing.status_code == 409 and nothing.json()["error_code"] == "sandbox.invalid_state"


def test_an_admin_deletes_one_sandbox_even_while_it_runs(world, test_client, admin, report):
    rep = report("Doomed")
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id

    deleted, ended = _with_a_run_in_progress(
        world, rep, lambda: test_client.delete(f"{BASE}/{sandbox_id}", headers=admin))
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True}, deleted.text
    assert isinstance(ended, SandboxError)

    assert world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state == "deleted", "a run that was ending does not bring it back"
    assert get_stub_host().get(f"sbx-{sandbox_id}") is None
    assert sandbox_id not in [r["id"] for r in test_client.get(BASE, headers=admin).json()]
    entry = _audit(world, "sandbox.deleted", sandbox_id)
    assert len(entry) == 1 and entry[0].details["reason"] == "deleted by an admin"
    fresh = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="ls outputs"))
    assert fresh.sandbox_id != sandbox_id and fresh.stdout == "", "the report gets a new, empty sandbox"


def test_stopping_a_sandbox_that_is_gone_from_its_server_is_not_reported_as_stopped(world, test_client, admin, report):
    sandbox_id = world.run_code(report(), world.admin_id, ECHO).sandbox_id
    get_stub_host().remove_sandbox(f"sbx-{sandbox_id}")  # someone removed the container by hand

    answer = test_client.post(f"{BASE}/{sandbox_id}/stop", headers=admin)
    assert answer.status_code == 409 and answer.json()["error_code"] == "sandbox.unavailable", answer.text
    assert world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state == "failed"


def test_a_run_that_does_not_stop_is_not_reported_as_stopped(world, test_client, admin, report, monkeypatch):
    from app.services.sandbox import runtime_client, service as service_module

    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    monkeypatch.setattr(service_module, "KILL_GRACE_SECONDS", 1)

    real_signal = runtime_client.SandboxRuntimeClient.send_signal

    async def lost_signal(self, process_id, signal_name="SIGTERM"):
        return None  # the signal goes nowhere
    runtime_client.SandboxRuntimeClient.send_signal = lost_signal

    def act():
        try:
            return test_client.post(f"{BASE}/{sandbox_id}/kill-run", headers=admin)
        finally:
            runtime_client.SandboxRuntimeClient.send_signal = real_signal  # let the delete below really stop it
            test_client.delete(f"{BASE}/{sandbox_id}", headers=admin)

    answer, _ = _with_a_run_in_progress(world, rep, act)
    assert answer.status_code == 503 and answer.json()["error_code"] == "sandbox.unavailable", answer.text
    assert "did not stop" in answer.json()["detail"]
