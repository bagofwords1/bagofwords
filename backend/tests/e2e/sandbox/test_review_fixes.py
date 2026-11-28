"""Races and leaks found in review of the snapshot, evacuation and placement
paths. Each test is one finding: the scenario the reviewer described, and what
must happen instead."""
import asyncio
import os
import uuid

import pytest

from app.models.sandbox import Sandbox, SandboxCompute, SandboxEnvironment, SandboxEvent
from app.services.sandbox import environments as environment_service
from app.services.sandbox import errors, snapshots
from app.services.sandbox.drivers import fake as fake_driver
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import RunRequest

from .conftest import run

pytestmark = pytest.mark.e2e

BASE = "/api/sandbox/sandboxes"
COMPUTES = "/api/sandbox/computes"
ENVS = "/api/sandbox/environments"


def code(source, env=None):
    return RunRequest(language="bash", code=source, environment=env)


@pytest.fixture
def world(sandbox_world, enterprise_license, tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "_root", lambda: str(tmp_path / "sandbox_snapshots"))
    sandbox_world.configure(max_running_per_user=20)
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


def _events(world, sandbox_id):
    rows = world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox_id)
    return [e.type for e in sorted(rows, key=lambda e: (e.created_at, e.id))]


def _evacuate(world, test_client, admin, old):
    world.add_compute("new")
    test_client.put(f"{COMPUTES}/{old}", json={"enabled": False}, headers=admin)
    result = test_client.post(f"{COMPUTES}/{old}/evacuate", headers=admin)
    assert result.status_code == 200, result.text
    return result.json()


# ── evacuation claims the sandbox; a failed snapshot gives it back ───────


def test_evacuation_claims_the_sandbox_so_a_run_cannot_start_under_it(world, report, monkeypatch):
    world.add_compute("old")
    world.add_environment("Default", is_default=True)
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, code("echo work > notes.txt")).sandbox_id
    seen = {}

    real_take = snapshots.take

    async def take_and_look(driver, handle, **kwargs):
        # While the snapshot is being taken, a run arrives on another worker.
        async with world.session() as other:
            seen["state"] = (await other.get(Sandbox, sandbox_id)).state
            with pytest.raises(SandboxError) as refused:
                await world.service.run(other, world.report_ref(rep), world.admin_id, code("echo late >> notes.txt"))
            seen["refused"] = refused.value.code
        return await real_take(driver, handle, **kwargs)
    monkeypatch.setattr(snapshots, "take", take_and_look)

    async def move(db):
        await world.service.move_off(db, await db.get(Sandbox, sandbox_id), world.admin_id)
    world.call(move)
    assert seen == {"state": "busy", "refused": errors.BUSY}, "claimed before the snapshot, the way a run claims"
    assert _row(world, sandbox_id).state == "deleted"


def test_a_snapshot_that_fails_during_evacuation_leaves_the_sandbox_as_it_was(world, report, test_client, admin, monkeypatch):
    old = world.add_compute("old")
    world.add_environment("Default", is_default=True)
    sandbox_id = world.run_code(report(), world.admin_id, code("echo work > notes.txt")).sandbox_id

    async def disk_full(driver, handle, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(snapshots, "take", disk_full)

    result = _evacuate(world, test_client, admin, old)
    assert result["moved"] == [] and [left["sandbox_id"] for left in result["left"]] == [sandbox_id]
    row = _row(world, sandbox_id)
    assert row.state == "ready" and row.compute_id == old, "not busy, not removed: nothing is removed without a copy"
    assert "snapshot_skipped" in _events(world, sandbox_id)


# ── stop survives a snapshot that fails for any reason ───────────────────


def test_a_stop_is_not_left_busy_when_the_snapshot_cannot_be_written(world, report, monkeypatch):
    world.add_compute("one")
    world.add_environment("Default", is_default=True)
    world.configure(snapshot_on_stop=True)
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, code("echo work > notes.txt")).sandbox_id

    async def disk_full(driver, handle, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(snapshots, "take", disk_full)

    async def stop(db):
        await world.service.stop(db, await db.get(Sandbox, sandbox_id), world.admin_id)
    world.call(stop)
    row = _row(world, sandbox_id)
    assert row.state == "stopped" and row.snapshot is None, "stopped all the same; the snapshot is what was skipped"
    assert "snapshot_skipped" in _events(world, sandbox_id)
    assert world.run_code(rep, world.admin_id, code("cat notes.txt")).stdout == "work\n"


# ── snapshots: one per report, gone after a deliberate delete ────────────


def test_a_deliberate_delete_is_not_followed_by_an_older_workspace_coming_back(world, report, test_client, admin):
    old = world.add_compute("old")
    world.add_environment("Default", is_default=True)
    rep = report()
    a = world.run_code(rep, world.admin_id, code("echo from-a > notes.txt")).sandbox_id
    assert _evacuate(world, test_client, admin, old)["moved"] == [a]
    a_file = snapshots.path_for(a)
    assert os.path.exists(a_file)

    b = world.run_code(rep, world.admin_id, code("cat notes.txt; echo from-b > notes.txt"))
    assert b.stdout == "from-a\n" and b.restored_snapshot_at
    assert _row(world, a).snapshot is None, "the snapshot now belongs to the sandbox restored from it"
    taken = test_client.post(f"{BASE}/{b.sandbox_id}/snapshot", headers=admin)
    assert taken.status_code == 200, taken.text
    assert not os.path.exists(a_file), "its own snapshot supersedes the one it came from"

    assert test_client.delete(f"{BASE}/{b.sandbox_id}", headers=admin).status_code == 200
    assert os.listdir(os.path.dirname(a_file)) == [], "no copy of this report's workspace is left anywhere"
    fresh = world.run_code(rep, world.admin_id, code("ls"))
    assert fresh.restored_snapshot_at is None and "notes.txt" not in fresh.stdout, "deleted means deleted"


def test_a_workspace_is_only_restored_into_the_environment_it_was_made_in(world, report, test_client, admin):
    old = world.add_compute("old")
    world.add_environment("Default", is_default=True, setup_script="echo default-setup > setup.txt")
    special = world.add_environment("Special", setup_script="echo special-setup > setup.txt")
    rep = report()
    a = world.run_code(rep, world.admin_id, code("echo special-work > notes.txt", env="Special")).sandbox_id
    assert _evacuate(world, test_client, admin, old)["moved"] == [a]

    test_client.put(f"{ENVS}/{special}", json={"enabled": False}, headers=admin)  # its environment can no longer be used
    again = world.run_code(rep, world.admin_id, code("cat setup.txt; ls notes.txt 2>&1 | grep -c 'No such'"))
    row = _row(world, again.sandbox_id)
    assert row.limits["environment"] == "Default"
    assert again.stdout == "default-setup\n1\n", "a fresh workspace, set up by the environment it is now in"
    assert again.restored_snapshot_at is None and row.snapshot is None
    assert not os.path.exists(snapshots.path_for(a)), "the abandoned workspace's copy is not kept around"


def test_the_confirmation_is_decided_for_the_environment_the_run_will_really_use(world, report, test_client, admin):
    old = world.add_compute("old")
    world.add_environment("Default", is_default=True)  # never asks
    world.add_environment("Prod data", confirm_runs="always")
    rep = report()
    a = world.run_code(rep, world.admin_id, code("echo x", env="Prod data")).sandbox_id
    assert _evacuate(world, test_client, admin, old)["moved"] == [a]

    env = world.call(lambda db: world.service.environment_for(db, world.report_ref(rep), world.admin_id))
    assert env.name == "Prod data", "the run recreates the sandbox there, so that is the environment that is asked about"
    assert environment_service.needs_confirmation(env, installs_packages=False)
    recreated = world.run_code(rep, world.admin_id, code("echo y"))
    assert _row(world, recreated.sandbox_id).limits["environment"] == "Prod data"


# ── a create that is cancelled ───────────────────────────────────────────


def test_a_create_cancelled_part_way_does_not_block_the_report(world, report, monkeypatch):
    world.add_compute("srv", server="srv")
    world.add_environment("Default", is_default=True)
    rep = report()
    real_create = fake_driver.FakeDriver.create
    creating = asyncio.Event()

    async def slow_create(self, spec, *args, **kwargs):
        handle = await real_create(self, spec, *args, **kwargs)
        creating.set()
        await asyncio.sleep(60)  # a slow image pull
        return handle
    monkeypatch.setattr(fake_driver.FakeDriver, "create", slow_create)

    async def cancelled_mid_create():
        nonlocal creating
        creating = asyncio.Event()

        async def first_run():
            async with world.session() as db:
                return await world.service.run(db, world.report_ref(rep), world.admin_id, code("echo never"))
        task = asyncio.ensure_future(first_run())
        await asyncio.wait_for(creating.wait(), 30)
        task.cancel()  # the user stopped the turn
        with pytest.raises(asyncio.CancelledError):
            await task
    run(cancelled_mid_create())

    rows = world.rows(Sandbox, Sandbox.report_id == rep["id"])
    assert [r.state for r in rows] == ["failed"] and rows[0].error == "Creation was cancelled"
    assert fake_driver._COMPUTES["srv"].sandboxes == {}, "what was started on the compute is removed, not orphaned"

    monkeypatch.setattr(fake_driver.FakeDriver, "create", real_create)
    assert world.run_code(rep, world.admin_id, code("echo works")).stdout == "works\n", "the next run starts over at once"


# ── a compute's address and a bootstrap in flight ────────────────────────


def test_a_compute_cannot_be_pointed_at_another_server_while_it_is_being_bootstrapped(world, test_client, admin):
    compute = test_client.post(COMPUTES, json={"name": "srv", "type": "docker-ssh", "config": {"address": "old.example.com"}}, headers=admin).json()

    async def bootstrap_in_flight(db):
        (await db.get(SandboxCompute, compute["id"])).bootstrap_status = "running"
        await db.commit()
    world.call(bootstrap_in_flight)

    refused = test_client.put(f"{COMPUTES}/{compute['id']}", json={"config": {"address": "new.example.com"}}, headers=admin)
    assert refused.status_code == 409 and refused.json()["error_code"] == "sandbox.bootstrap_running", refused.text
    assert world.rows(SandboxCompute, SandboxCompute.id == compute["id"])[0].config["address"] == "old.example.com"
    # Something that is not the server's address can still be changed.
    assert test_client.put(f"{COMPUTES}/{compute['id']}", json={"capacity": 5}, headers=admin).status_code == 200


# ── two creates at the same moment ───────────────────────────────────────


def test_of_two_sandboxes_placed_at_the_same_moment_the_later_one_backs_out(world, report, monkeypatch):
    compute_id = world.add_compute("small", cpus=4, memory_mb=1024 + 2048, disk_mb=50 * 1024)  # 2048 MB may be given out
    world.add_environment("Big", is_default=True, cpus=1, memory_mb=2048, disk_mb=1024)
    first = world.run_code(report(), world.admin_id, code("echo first")).sandbox_id

    # A second create whose placement read "nothing in use" before the first was recorded.
    async def raced(db, organization_id, env):
        return await db.get(SandboxCompute, compute_id)
    monkeypatch.setattr(world.service, "choose_compute", raced)

    rep = report()
    with pytest.raises(SandboxError) as full:
        world.run_code(rep, world.admin_id, code("echo second"))
    assert full.value.code == errors.NO_COMPUTE and "not enough memory" in full.value.message
    assert [r.state for r in world.rows(Sandbox, Sandbox.report_id == rep["id"])] == ["deleted"], "it backed out"
    assert _row(world, first).state == "ready", "the one recorded first keeps its place"
    assert fake_driver._COMPUTES[compute_id].sandboxes.keys() == {first}, "nothing was started for the second"
