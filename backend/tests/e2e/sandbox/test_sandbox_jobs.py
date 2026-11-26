"""The scheduled sandbox jobs: lifecycle, backend health, reconcile.

A sandbox left alone is marked idle, then stopped, then deleted, at the
configured times, and resumes if it is used while stopped. Each job is one
``*_tick(now=...)`` pass, so the timeouts are tested by moving ``now``, never
by waiting.
"""
import logging
import uuid
from datetime import datetime, timedelta

import pytest

from app.ee.audit.models import AuditLog
from app.models.sandbox import Sandbox, SandboxBackend, SandboxEvent, SandboxRun
from app.schemas.sandbox_schema import SandboxBackendUpdate
from app.services.sandbox import backends as backend_service
from app.services.sandbox import errors, jobs
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import RunRequest
from app.services.sandbox.testing.sandboxd_stub import get_stub_host

from .conftest import run

pytestmark = pytest.mark.e2e

ECHO = RunRequest(language="bash", code="echo ran")
MINUTE = 60


@pytest.fixture
def world(sandbox_world):
    sandbox_world.backend_id = sandbox_world.add_backend("fake-1")
    sandbox_world.add_environment("Default", is_default=True)
    # Short, distinct timeouts so each step of the lifecycle has its own moment.
    sandbox_world.configure(mark_idle_after_seconds=5 * MINUTE, idle_timeout_seconds=30 * MINUTE,
                            stopped_retention_seconds=7 * 24 * 3600, max_lifetime_seconds=30 * 24 * 3600,
                            max_running_per_user=10)
    return sandbox_world


@pytest.fixture
def report(world, create_report):
    def _new(title=None):
        return create_report(title=title or f"Report {uuid.uuid4().hex[:6]}",
                             user_token=world.cast["admin"]["token"], org_id=world.org_id)
    return _new


def _tick(job, at=None):
    return run(job(now=at))


def _state(world, sandbox_id):
    return world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state


def _events(world, sandbox_id):
    rows = world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox_id)
    return [e.type for e in sorted(rows, key=lambda e: (e.created_at, e.id))]


def _used_at(world, sandbox_id):
    return world.rows(Sandbox, Sandbox.id == sandbox_id)[0].last_used_at


# ── lifecycle ────────────────────────────────────────────────────────────


def test_a_sandbox_left_alone_is_marked_idle_then_stopped_then_deleted_at_the_configured_times(world, report):
    rep = report("Left alone")
    sandbox_id = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="echo kept > kept.txt")).sandbox_id
    used = _used_at(world, sandbox_id)

    _tick(jobs.lifecycle_tick, used + timedelta(minutes=4))
    assert _state(world, sandbox_id) == "ready", "used four minutes ago: nothing happens"

    assert _tick(jobs.lifecycle_tick, used + timedelta(minutes=6))["idle"] == 1
    assert _state(world, sandbox_id) == "idle"
    _tick(jobs.lifecycle_tick, used + timedelta(minutes=29))
    assert _state(world, sandbox_id) == "idle", "idle is still running"

    assert _tick(jobs.lifecycle_tick, used + timedelta(minutes=31))["stopped"] == 1
    stopped = world.rows(Sandbox, Sandbox.id == sandbox_id)[0]
    assert stopped.state == "stopped" and stopped.stopped_at is not None
    assert get_stub_host().get(f"sbx-{sandbox_id}").running is False, "really stopped on the backend, not deleted"

    _tick(jobs.lifecycle_tick, stopped.stopped_at + timedelta(days=6, hours=23))
    assert _state(world, sandbox_id) == "stopped", "kept for the whole retention period"
    assert _tick(jobs.lifecycle_tick, stopped.stopped_at + timedelta(days=7, minutes=1))["deleted"] == 1
    assert _state(world, sandbox_id) == "deleted"
    assert get_stub_host().get(f"sbx-{sandbox_id}") is None

    events = _events(world, sandbox_id)
    assert [e for e in events if e in ("idle", "stopped", "deleted")] == ["idle", "stopped", "deleted"]
    audited = {a.action: a for a in world.rows(AuditLog, AuditLog.resource_id == sandbox_id)}
    assert audited["sandbox.stopped"].details["reason"] == "idle" and audited["sandbox.stopped"].user_id is None
    assert audited["sandbox.deleted"].details["reason"] == "stopped too long"
    assert audited["sandbox.deleted"].details["title"] == "Left alone", "the report name is there without anyone passing it"


def test_a_sandbox_used_while_stopped_resumes_with_its_files_and_its_clock_starts_again(world, report):
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="echo kept > kept.txt")).sandbox_id
    used = _used_at(world, sandbox_id)
    _tick(jobs.lifecycle_tick, used + timedelta(minutes=31))
    assert _state(world, sandbox_id) == "stopped"

    outcome = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="cat kept.txt"))
    assert outcome.resumed and outcome.sandbox_id == sandbox_id and outcome.stdout == "kept\n"
    # Just used: the same moment that stopped it before does nothing now.
    again = _used_at(world, sandbox_id)
    _tick(jobs.lifecycle_tick, again + timedelta(minutes=4))
    assert _state(world, sandbox_id) == "ready"


def test_a_run_brings_an_idle_sandbox_back(world, report):
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    _tick(jobs.lifecycle_tick, _used_at(world, sandbox_id) + timedelta(minutes=6))
    assert _state(world, sandbox_id) == "idle"
    assert world.run_code(rep, world.admin_id, ECHO).stdout == "ran\n"
    assert _state(world, sandbox_id) == "ready"


def test_a_sandbox_is_deleted_at_its_maximum_lifetime_even_if_it_was_just_used(world, report):
    world.configure(max_lifetime_seconds=3600)
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    row = world.rows(Sandbox, Sandbox.id == sandbox_id)[0]
    assert abs((row.expires_at - row.created_at) - timedelta(hours=1)) < timedelta(seconds=5)

    _tick(jobs.lifecycle_tick, row.expires_at - timedelta(minutes=58))
    assert _state(world, sandbox_id) == "ready"
    assert _tick(jobs.lifecycle_tick, row.expires_at + timedelta(seconds=1))["deleted"] == 1
    assert _state(world, sandbox_id) == "deleted" and "expired" in _events(world, sandbox_id)
    # The report simply gets a fresh one next time.
    assert world.run_code(rep, world.admin_id, ECHO).created is True


def test_deleting_a_report_deletes_its_sandbox(world, report, test_client):
    kept, gone = report("Kept"), report("Going away")
    kept_id = world.run_code(kept, world.admin_id, ECHO).sandbox_id
    gone_id = world.run_code(gone, world.admin_id, ECHO).sandbox_id
    deleted = test_client.delete(f"/api/reports/{gone['id']}", headers=world.cast["admin"]["headers"])
    assert deleted.status_code in (200, 204), deleted.text

    assert _tick(jobs.lifecycle_tick)["deleted"] == 1
    assert _state(world, gone_id) == "deleted" and get_stub_host().get(f"sbx-{gone_id}") is None
    assert _state(world, kept_id) == "ready"
    entry = [a for a in world.rows(AuditLog, AuditLog.action == "sandbox.deleted") if a.resource_id == gone_id]
    assert len(entry) == 1 and entry[0].details["reason"] == "report deleted"


def test_a_run_abandoned_by_a_dead_worker_is_cleared_so_the_sandbox_is_usable_again(world, report):
    rep = report()
    outcome = world.run_code(rep, world.admin_id, ECHO)

    async def abandon(db):
        """As if the worker died mid-run: the sandbox is left busy with a run still 'running'."""
        sandbox = await db.get(Sandbox, outcome.sandbox_id)
        sandbox.state = "busy"
        db.add(SandboxRun(sandbox_id=sandbox.id, organization_id=world.org_id, report_id=rep["id"], status="running",
                          started_at=sandbox.last_used_at, input_file_ids=[], artifact_file_ids=[]))
        await db.commit()
        return sandbox.last_used_at
    used = world.call(abandon)

    with pytest.raises(SandboxError) as busy:
        world.run_code(rep, world.admin_id, ECHO)
    assert busy.value.code == errors.BUSY
    _tick(jobs.lifecycle_tick, used + timedelta(minutes=20))
    assert _state(world, outcome.sandbox_id) == "busy", "a run may legitimately still be going"

    assert _tick(jobs.lifecycle_tick, used + timedelta(hours=2))["recovered"] == 1
    assert _state(world, outcome.sandbox_id) == "ready"
    assert not [r for r in world.rows(SandboxRun, SandboxRun.sandbox_id == outcome.sandbox_id) if r.status == "running"]
    assert world.run_code(rep, world.admin_id, ECHO).stdout == "ran\n"


def test_one_broken_sandbox_does_not_stop_the_pass(world, report):
    first = world.run_code(report(), world.admin_id, ECHO).sandbox_id
    second = world.run_code(report(), world.admin_id, ECHO).sandbox_id

    async def break_first(db):
        sandbox = await db.get(Sandbox, first)
        sandbox.backend_id = None  # nothing can be done on a backend it no longer has
        await db.commit()
    world.call(break_first)

    counts = _tick(jobs.lifecycle_tick, _used_at(world, second) + timedelta(minutes=31))
    assert counts["errors"] == 1 and counts["stopped"] == 1
    assert _state(world, second) == "stopped"


# ── backend health ───────────────────────────────────────────────────────


def _set_unreachable(world, unreachable):
    async def _go(db):
        await backend_service.update_backend(db, world.org_id, world.admin_id, world.backend_id,
                                             SandboxBackendUpdate(config={"unreachable": unreachable}))
    world.call(_go)


def test_an_unreachable_backend_gets_no_new_sandboxes_until_it_answers_again(world, report):
    assert _tick(jobs.backend_health_tick)["healthy"] == 1
    _set_unreachable(world, True)
    assert _tick(jobs.backend_health_tick)["unreachable"] == 1
    backend = world.rows(SandboxBackend, SandboxBackend.id == world.backend_id)[0]
    assert backend.status == "unreachable" and backend.last_error and backend.last_checked_at
    with pytest.raises(SandboxError) as refused:
        world.run_code(report(), world.admin_id, ECHO)
    assert refused.value.code == errors.NO_BACKEND

    _set_unreachable(world, False)
    _tick(jobs.backend_health_tick)
    assert world.rows(SandboxBackend, SandboxBackend.id == world.backend_id)[0].status == "healthy"
    assert world.run_code(report(), world.admin_id, ECHO).stdout == "ran\n"


def test_sandboxes_on_an_unreachable_backend_are_not_judged(world, report):
    sandbox_id = world.run_code(report(), world.admin_id, ECHO).sandbox_id
    _set_unreachable(world, True)
    _tick(jobs.backend_health_tick)
    assert _state(world, sandbox_id) == "ready", "the backend is down, which says nothing about the sandbox"


def test_the_health_check_notices_a_sandbox_that_is_gone_or_was_stopped(world, report):
    lost_report, stopped_report, fine_report = report(), report(), report()
    lost = world.run_code(lost_report, world.admin_id, ECHO).sandbox_id
    stopped = world.run_code(stopped_report, world.admin_id, RunRequest(language="bash", code="echo x > f")).sandbox_id
    fine = world.run_code(fine_report, world.admin_id, ECHO).sandbox_id
    get_stub_host().remove_sandbox(f"sbx-{lost}")
    get_stub_host().set_running(f"sbx-{stopped}", False)

    counts = _tick(jobs.backend_health_tick)
    assert (counts["sandboxes_failed"], counts["sandboxes_stopped"]) == (1, 1)
    assert (_state(world, lost), _state(world, stopped), _state(world, fine)) == ("failed", "stopped", "ready")
    assert world.rows(Sandbox, Sandbox.id == fine)[0].last_seen_at is not None

    assert world.run_code(lost_report, world.admin_id, ECHO).recreated is True
    resumed = world.run_code(stopped_report, world.admin_id, RunRequest(language="bash", code="cat f"))
    assert resumed.resumed and resumed.stdout == "x\n"


# ── reconcile ────────────────────────────────────────────────────────────


def _driver_call(world, fn):
    async def _go(db):
        backend = await backend_service.get_backend(db, world.org_id, world.backend_id)
        driver = await backend_service.build_driver(db, backend)
        return fn(driver)
    return world.call(_go)


def test_reconcile_removes_what_the_database_does_not_know_and_leaves_the_rest(world, report):
    known = world.run_code(report(), world.admin_id, ECHO).sandbox_id
    orphan, foreign = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
    _driver_call(world, lambda d: (d.plant_foreign_sandbox(orphan), d.plant_foreign_sandbox(foreign, instance_id="another-bow")))

    counts = _tick(jobs.reconcile_tick)
    assert counts["backends"] == 1 and counts["removed"] == 1 and counts["lost"] == 0
    present = _driver_call(world, lambda d: {name: d.has_sandbox(name) for name in (known, orphan, foreign)})
    assert present == {known: True, orphan: False, foreign: True}, "another BoW instance's sandbox is left alone"
    assert _state(world, known) == "ready"
    assert _tick(jobs.reconcile_tick)["removed"] == 0, "nothing left to do the second time"


def test_reconcile_never_takes_a_sandbox_being_created_for_an_orphan(world, report):
    rep = report()
    creating = str(uuid.uuid4())

    async def mid_create(db):
        """The row exists and the container exists, but the handle is not stored yet."""
        db.add(Sandbox(id=creating, organization_id=world.org_id, report_id=rep["id"], backend_id=world.backend_id,
                       state="provisioning", limits={}))
        await db.commit()
    world.call(mid_create)
    _driver_call(world, lambda d: d.plant_foreign_sandbox(creating))

    assert _tick(jobs.reconcile_tick)["removed"] == 0
    assert _driver_call(world, lambda d: d.has_sandbox(creating)) is True


def test_reconcile_marks_a_known_sandbox_that_is_gone_as_lost(world, report):
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    get_stub_host().remove_sandbox(f"sbx-{sandbox_id}")
    _driver_call(world, lambda d: d._backend.sandboxes.pop(sandbox_id, None))

    assert _tick(jobs.reconcile_tick)["lost"] == 1
    assert _state(world, sandbox_id) == "failed"
    assert world.run_code(rep, world.admin_id, ECHO).recreated is True


def test_reconcile_skips_backends_that_are_unreachable_or_not_bootstrapped(world, report):
    world.add_backend("never-bootstrapped", bootstrap=False)
    _set_unreachable(world, True)
    _tick(jobs.backend_health_tick)
    assert _tick(jobs.reconcile_tick) == {"backends": 0, "removed": 0, "repaired": 0, "lost": 0, "errors": 0}


# ── registration and the flag ────────────────────────────────────────────


class _Scheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, function, **options):
        self.jobs[options["id"]] = (function, options)


def test_no_sandbox_job_is_scheduled_with_the_flag_off(agent_sandboxes_off):
    scheduler = _Scheduler()
    assert jobs.register_sandbox_jobs(scheduler) == [] and scheduler.jobs == {}


def test_the_three_jobs_are_scheduled_with_the_flag_on(agent_sandboxes_on):
    scheduler = _Scheduler()
    job_ids = jobs.register_sandbox_jobs(scheduler)
    assert sorted(job_ids) == sorted(scheduler.jobs) == [
        "agent_sandbox_backend_health", "agent_sandbox_lifecycle", "agent_sandbox_reconcile"]
    for function, options in scheduler.jobs.values():
        assert options["trigger"] == "interval" and options["seconds"] >= 60
        assert options["max_instances"] == 1 and options["replace_existing"] is True, "a slow pass never overlaps itself"
        assert function.__module__ == "app.services.sandbox.jobs", "importable by path, as the job store needs"
    assert jobs.register_sandbox_jobs(scheduler) == job_ids, "registering twice replaces, it does not duplicate"


def test_turning_the_flag_off_with_sandboxes_left_behind_logs_a_warning(world, report, monkeypatch, caplog):
    from app.settings.config import settings as bow_settings

    assert run(jobs.warn_if_sandboxes_exist_while_disabled()) == 0, "with the flag on there is nothing to warn about"
    world.run_code(report(), world.admin_id, ECHO)
    world.run_code(report(), world.admin_id, ECHO)
    monkeypatch.setattr(bow_settings.bow_config.agent_sandboxes, "enabled", False)

    with caplog.at_level(logging.WARNING, logger="app.services.sandbox.jobs"):
        assert run(jobs.warn_if_sandboxes_exist_while_disabled()) == 2
    assert "2 sandbox(es) still exist" in caplog.text and "BOW_AGENT_SANDBOXES_ENABLED" in caplog.text


def test_no_warning_when_nothing_was_left_behind(sandbox_cast, agent_sandboxes_off, caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.sandbox.jobs"):
        assert run(jobs.warn_if_sandboxes_exist_while_disabled()) == 0
    assert "still exist" not in caplog.text
