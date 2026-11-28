"""What survives, and for how long.

Only ``outputs/`` is durable (it is saved to the report). The rest of a
workspace is a cache that can be lost to a failure or deleted by retention:
so a recreated sandbox gets the report's files back, a deletion is announced
before it happens, and an admin can pin a sandbox or push its deletion later.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app.ee.audit.models import AuditLog
from app.models.sandbox import Sandbox, SandboxEvent
from app.services.sandbox import jobs
from app.services.sandbox import service as service_module
from app.services.sandbox.service import InputFile, RunRequest
from app.services.sandbox.testing.sandboxd_stub import get_stub_host

from .conftest import run

pytestmark = pytest.mark.e2e

BASE = "/api/sandbox/sandboxes"
ECHO = RunRequest(language="bash", code="echo ran")


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
    def _new():
        return create_report(title=f"Report {uuid.uuid4().hex[:6]}",
                             user_token=world.cast["admin"]["token"], org_id=world.org_id)
    return _new


def _tick(at):
    return run(jobs.lifecycle_tick(now=at))


def _state(world, sandbox_id):
    return world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state


def _events(world, sandbox_id):
    rows = world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox_id)
    return [e.type for e in sorted(rows, key=lambda e: (e.created_at, e.id))]


# ── a recreated sandbox gets the report's files back ─────────────────────


def test_a_recreated_sandbox_gets_the_reports_files_back_and_nothing_else(world, report):
    rep = report()
    store = {}  # the report's files, as the file service would hold them

    async def save(name, size, chunks):
        file_id = f"file-{len(store)}"
        store[file_id] = (name, b"".join([c async for c in chunks]))
        return service_module.Artifact(name=name, size=size, file_id=file_id)

    async def load(file_id):
        if file_id not in store:
            return None
        name, data = store[file_id]
        return InputFile(file_id=file_id, name=name, data=data)

    store["in-1"] = ("sales.csv", b"a,b\n1,2\n")
    first = world.run_code(rep, world.admin_id, RunRequest(
        language="bash", code="cp inputs/sales.csv scratch.csv; echo v1 > outputs/report.txt; echo keep > outputs/chart.svg",
        inputs=[InputFile(file_id="in-1", name="sales.csv", data=b"a,b\n1,2\n")]), save_artifact=save, load_file=load)
    world.run_code(rep, world.admin_id, RunRequest(language="bash", code="echo v2 > outputs/report.txt"),
                   save_artifact=save, load_file=load)

    get_stub_host().remove_sandbox(f"sbx-{first.sandbox_id}")  # the server lost it
    again = world.run_code(rep, world.admin_id, RunRequest(
        language="bash", code="cat outputs/report.txt outputs/chart.svg inputs/sales.csv; ls scratch.csv 2>&1 | grep -c 'No such'"),
        save_artifact=save, load_file=load)

    assert again.recreated is True and again.sandbox_id != first.sandbox_id
    assert again.restored_files == ["inputs/sales.csv", "outputs/chart.svg", "outputs/report.txt"]
    assert again.stdout == "v2\nkeep\na,b\n1,2\n1\n", "the newest version of each output; what was outside outputs/ is gone"
    assert again.artifacts == [], "files put back are not saved to the report a second time"
    assert "files_restored" in _events(world, again.sandbox_id)


def test_restoring_survives_files_that_are_gone(world, report):
    rep = report()

    async def save(name, size, chunks):
        return service_module.Artifact(name=name, size=size, file_id=f"file-{name}")

    async def load(file_id):
        if file_id == "file-a.txt":
            raise RuntimeError("storage is down")
        return None  # deleted from the report since

    first = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="echo a > outputs/a.txt; echo b > outputs/b.txt"),
                           save_artifact=save, load_file=load)
    get_stub_host().remove_sandbox(f"sbx-{first.sandbox_id}")
    again = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="ls outputs"), save_artifact=save, load_file=load)
    assert again.recreated is True and again.restored_files == [] and again.stdout == ""


# ── deletion: announced first, skippable, postponable ────────────────────


def test_a_deletion_is_announced_before_it_happens(world, report, test_client, admin):
    # (The stopped retention is set long so that the lifetime is what ends this sandbox.)
    world.configure(max_lifetime_seconds=10 * 24 * 3600, delete_warning_seconds=24 * 3600,
                    stopped_retention_seconds=300 * 24 * 3600)
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    created = world.rows(Sandbox, Sandbox.id == sandbox_id)[0].created_at

    _tick(created + timedelta(days=8))
    assert "deletion_scheduled" not in _events(world, sandbox_id), "not yet: more than a day to go"

    _tick(created + timedelta(days=9, hours=1))
    _tick(created + timedelta(days=9, hours=2))
    assert _events(world, sandbox_id).count("deletion_scheduled") == 1, "once, not on every pass"
    notice = [r for r in world.rows(AuditLog, AuditLog.organization_id == world.org_id)
              if r.action == "sandbox.deletion_scheduled" and r.resource_id == sandbox_id]
    assert len(notice) == 1 and notice[0].details["reason"] == "expired"
    row = [r for r in test_client.get(BASE, headers=admin).json() if r["id"] == sandbox_id][0]
    assert row["delete_reason"] == "expired" and row["deletes_at"] and row["pinned"] is False

    assert _state(world, sandbox_id) != "deleted"
    _tick(created + timedelta(days=10, minutes=1))
    assert _state(world, sandbox_id) == "deleted"


def test_the_agent_is_told_on_a_run_that_the_sandbox_is_about_to_go(world, report):
    world.configure(max_lifetime_seconds=3600, delete_warning_seconds=24 * 3600)
    outcome = world.run_code(report(), world.admin_id, ECHO)
    assert len(outcome.notices) == 1 and "will be deleted on" in outcome.notices[0] and "outputs/" in outcome.notices[0]
    world.configure(max_lifetime_seconds=30 * 24 * 3600)
    assert world.run_code(report(), world.admin_id, ECHO).notices == []


def test_a_pinned_sandbox_is_kept_past_its_lifetime_and_its_retention(world, report, test_client, admin):
    rep, other = report(), report()
    pinned_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    other_id = world.run_code(other, world.admin_id, ECHO).sandbox_id
    created = world.rows(Sandbox, Sandbox.id == pinned_id)[0].created_at

    pinned = test_client.post(f"{BASE}/{pinned_id}/pin", json={"pinned": True}, headers=admin)
    assert pinned.status_code == 200 and pinned.json()["pinned"] is True and pinned.json()["deletes_at"] is None

    _tick(created + timedelta(days=40))  # past the 30-day lifetime and the 7-day stopped retention
    assert _state(world, pinned_id) != "deleted" and _state(world, other_id) == "deleted"
    assert "pinned" in _events(world, pinned_id)

    unpinned = test_client.post(f"{BASE}/{pinned_id}/pin", json={"pinned": False}, headers=admin).json()
    assert unpinned["pinned"] is False and unpinned["deletes_at"] is not None
    _tick(created + timedelta(days=41))
    assert _state(world, pinned_id) == "deleted"


def test_a_pinned_sandbox_still_goes_with_its_report(world, report, test_client, admin):
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    test_client.post(f"{BASE}/{sandbox_id}/pin", json={"pinned": True}, headers=admin)
    assert test_client.delete(f"/api/reports/{rep['id']}", headers=admin).status_code == 200
    _tick(datetime.utcnow() + timedelta(minutes=5))
    assert _state(world, sandbox_id) == "deleted"


def test_extending_pushes_the_deletion_later(world, report, test_client, admin):
    sandbox_id = world.run_code(report(), world.admin_id, ECHO).sandbox_id
    before = world.rows(Sandbox, Sandbox.id == sandbox_id)[0].expires_at

    extended = test_client.post(f"{BASE}/{sandbox_id}/extend", json={"days": 10}, headers=admin)
    assert extended.status_code == 200, extended.text
    after = world.rows(Sandbox, Sandbox.id == sandbox_id)[0].expires_at
    assert timedelta(days=9, hours=23) < after - before < timedelta(days=10, hours=1)
    assert "extended" in _events(world, sandbox_id)
    _tick(before + timedelta(days=1))
    assert _state(world, sandbox_id) != "deleted"
    for bad in ({"days": 0}, {"days": 1000}):
        assert test_client.post(f"{BASE}/{sandbox_id}/extend", json=bad, headers=admin).status_code == 422


def test_the_maximum_lifetime_can_be_turned_off(world, report, test_client, admin):
    changed = test_client.put("/api/sandbox/settings", json={"max_lifetime_seconds": 0}, headers=admin)
    assert changed.status_code == 200 and changed.json()["max_lifetime_seconds"] == 0
    assert test_client.put("/api/sandbox/settings", json={"max_lifetime_seconds": 60}, headers=admin).status_code == 422
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    sandbox = world.rows(Sandbox, Sandbox.id == sandbox_id)[0]
    assert sandbox.expires_at is None
    _tick(sandbox.created_at + timedelta(days=400))
    assert _state(world, sandbox_id) == "stopped", "stopped for being idle, never deleted for its age"
