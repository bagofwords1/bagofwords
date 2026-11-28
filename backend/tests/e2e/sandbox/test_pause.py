"""Between runs: an idle sandbox is frozen in memory where the compute can
(no CPU, thawed in milliseconds), stopped later (memory freed, processes
lost), and an admin can start one ahead of time or keep one warm."""
import uuid
from datetime import timedelta

import pytest

from app.models.sandbox import Sandbox, SandboxEvent
from app.services.sandbox import errors, jobs
from app.services.sandbox.drivers import fake as fake_driver
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import RunRequest
from app.services.sandbox.testing.sandboxd_stub import get_stub_host

from .conftest import run

pytestmark = pytest.mark.e2e

BASE = "/api/sandbox/sandboxes"
ECHO = RunRequest(language="bash", code="echo ran")


@pytest.fixture
def world(sandbox_world, enterprise_license):
    sandbox_world.add_environment("Default", is_default=True)
    return sandbox_world


@pytest.fixture
def admin(world):
    return world.cast["admin"]["headers"]


@pytest.fixture
def report(world, create_report):
    def _new():
        return create_report(title=f"Report {uuid.uuid4().hex[:6]}",
                             user_token=world.cast["admin"]["token"], org_id=world.org_id)
    return _new


def _row(world, sandbox_id):
    return world.rows(Sandbox, Sandbox.id == sandbox_id)[0]


def _idle_events(world, sandbox_id):
    rows = sorted(world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox_id), key=lambda e: (e.created_at, e.id))
    return [e.details.get("paused") for e in rows if e.type == "idle"]


def _tick(job, at):
    return run(job(now=at))


def test_an_idle_sandbox_is_frozen_and_the_next_run_thaws_it(world, report):
    world.add_compute("srv", server="srv")
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    used = _row(world, sandbox_id).last_used_at
    paused = fake_driver._COMPUTES["srv"].paused

    assert _tick(jobs.lifecycle_tick, used + timedelta(minutes=6))["idle"] == 1
    assert _row(world, sandbox_id).state == "idle" and sandbox_id in paused
    assert _idle_events(world, sandbox_id) == [True]

    # The health check does not take a frozen sandbox for a dead one.
    counts = run(jobs.compute_health_tick())
    assert counts["sandboxes_failed"] == 0 and _row(world, sandbox_id).state == "idle"

    again = world.run_code(rep, world.admin_id, ECHO)
    assert again.stdout == "ran\n" and again.resumed is False, "thawed, not restarted"
    assert sandbox_id not in paused and _row(world, sandbox_id).state == "ready"


def test_a_compute_that_cannot_pause_just_marks_the_sandbox_idle(world, report):
    world.add_compute("plain", server="plain", pause=False)
    sandbox_id = world.run_code(report(), world.admin_id, ECHO).sandbox_id
    _tick(jobs.lifecycle_tick, _row(world, sandbox_id).last_used_at + timedelta(minutes=6))
    assert _row(world, sandbox_id).state == "idle" and _idle_events(world, sandbox_id) == [False]
    assert fake_driver._COMPUTES["plain"].paused == set()


def test_a_run_that_claims_the_sandbox_first_is_not_frozen(world, report):
    world.add_compute("srv", server="srv")
    sandbox_id = world.run_code(report(), world.admin_id, ECHO).sandbox_id

    async def claimed_then_marked(db):
        stale = await db.get(Sandbox, sandbox_id)
        async with world.session() as other:
            (await other.get(Sandbox, sandbox_id)).state = "busy"
            await other.commit()
        return await world.service.mark_idle(db, stale)
    assert world.call(claimed_then_marked) is False
    assert sandbox_id not in fake_driver._COMPUTES["srv"].paused and _row(world, sandbox_id).state == "busy"


def test_a_frozen_sandbox_is_thawed_before_it_is_stopped(world, report):
    world.add_compute("srv", server="srv")
    world.add_environment("Hooks", stop_script="echo stopped-cleanly > stop.log")
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="echo ran", environment="Hooks")).sandbox_id
    used = _row(world, sandbox_id).last_used_at
    _tick(jobs.lifecycle_tick, used + timedelta(minutes=6))
    assert sandbox_id in fake_driver._COMPUTES["srv"].paused

    assert _tick(jobs.lifecycle_tick, used + timedelta(minutes=31))["stopped"] == 1
    assert _row(world, sandbox_id).state == "stopped" and sandbox_id not in fake_driver._COMPUTES["srv"].paused
    assert world.run_code(rep, world.admin_id, RunRequest(language="bash", code="cat stop.log")).stdout == "stopped-cleanly\n", \
        "its stop script could run"


def test_an_admin_starts_a_stopped_sandbox_ahead_of_its_next_run(world, report, test_client, admin):
    world.add_compute("srv", server="srv")
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="echo kept > f.txt")).sandbox_id
    world.call(lambda db: _stop(world, db, sandbox_id))
    assert get_stub_host().get(f"sbx-{sandbox_id}").running is False

    started = test_client.post(f"{BASE}/{sandbox_id}/start", headers=admin)
    assert started.status_code == 200 and started.json()["state"] == "ready", started.text
    assert get_stub_host().get(f"sbx-{sandbox_id}").running is True
    again = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="cat f.txt"))
    assert (again.resumed, again.stdout) == (False, "kept\n"), "it was already running"
    assert test_client.post(f"{BASE}/{sandbox_id}/start", headers=admin).json()["state"] == "ready", "starting a running one is a no-op"


async def _stop(world, db, sandbox_id):
    await world.service.stop(db, await world.service.get(db, world.org_id, sandbox_id), world.admin_id)


def test_a_sandbox_kept_warm_is_neither_frozen_nor_stopped_until_then(world, report, test_client, admin):
    world.add_compute("srv", server="srv")
    sandbox_id = world.run_code(report(), world.admin_id, ECHO).sandbox_id
    used = _row(world, sandbox_id).last_used_at

    warm = test_client.post(f"{BASE}/{sandbox_id}/keep-warm", json={"hours": 4}, headers=admin)
    assert warm.status_code == 200 and warm.json()["keep_warm_until"], warm.text
    _tick(jobs.lifecycle_tick, used + timedelta(hours=2))
    assert _row(world, sandbox_id).state == "ready" and sandbox_id not in fake_driver._COMPUTES["srv"].paused

    _tick(jobs.lifecycle_tick, used + timedelta(hours=5))
    assert _row(world, sandbox_id).state == "stopped", "after that the usual rules apply"

    cleared = test_client.post(f"{BASE}/{sandbox_id}/keep-warm", json={"hours": 0}, headers=admin).json()
    assert cleared["keep_warm_until"] is None
    assert test_client.post(f"{BASE}/{sandbox_id}/keep-warm", json={"hours": 1000}, headers=admin).status_code == 422


def test_a_run_waits_for_a_pause_in_flight_instead_of_being_refused_as_busy(world, report, monkeypatch):
    """A pause holds the same claim a run does for the seconds its command
    takes. That is not "a run in progress"."""
    import asyncio

    from app.services.sandbox import service as service_module

    monkeypatch.setattr(service_module, "BRIEF_CLAIM_WAIT_SECONDS", 20)
    world.add_compute("srv", server="srv")
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id

    async def go():
        async with world.session() as db:  # the lifecycle job has claimed it to pause it
            (await db.get(Sandbox, sandbox_id)).state = "busy"
            await db.commit()

        async def release_soon():
            await asyncio.sleep(1.5)
            async with world.session() as db:
                (await db.get(Sandbox, sandbox_id)).state = "idle"
                await db.commit()
        releasing = asyncio.ensure_future(release_soon())
        async with world.session() as db:
            outcome = await world.service.run(db, world.report_ref(rep), world.admin_id, ECHO)
        await releasing
        return outcome

    outcome = run(go())
    assert outcome.stdout == "ran\n" and outcome.sandbox_id == sandbox_id
    busy = [e for e in world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox_id) if e.type == "busy"]
    assert busy[-1].details["from"] == "idle", "the real previous state is recorded"
