"""A workspace lives on one compute's disk. A snapshot is a copy of it on
BoW's side, so that losing the compute, or moving a sandbox off it, does not
lose the workspace."""
import os
import uuid

import pytest

from app.models.sandbox import Sandbox, SandboxEvent
from app.services.sandbox import snapshots
from app.services.sandbox.service import RunRequest
from app.services.sandbox.testing.sandboxd_stub import get_stub_host

pytestmark = pytest.mark.e2e

BASE = "/api/sandbox/sandboxes"
COMPUTES = "/api/sandbox/computes"
WRITE = RunRequest(language="bash", code="mkdir -p lib; echo package > lib/pkg.txt; echo work > notes.txt; echo out > outputs/o.txt")
READ = RunRequest(language="bash", code="cat lib/pkg.txt notes.txt outputs/o.txt")


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


def _stop(world, sandbox_id):
    async def go(db):
        await world.service.stop(db, await world.service.get(db, world.org_id, sandbox_id), world.admin_id)
    world.call(go)


def test_no_snapshot_is_taken_unless_it_is_turned_on(world, report):
    world.add_compute("one")
    world.add_environment("Default", is_default=True)
    sandbox_id = world.run_code(report(), world.admin_id, WRITE).sandbox_id
    _stop(world, sandbox_id)
    assert _row(world, sandbox_id).snapshot is None and "snapshot" not in _events(world, sandbox_id)


def test_a_sandbox_lost_with_its_compute_comes_back_from_its_last_snapshot(world, report):
    world.add_compute("one")
    world.add_environment("Default", is_default=True, setup_script="echo setup >> setup.log")
    world.configure(snapshot_on_stop=True)
    rep = report()
    first = world.run_code(rep, world.admin_id, WRITE).sandbox_id
    _stop(world, first)
    snapshot = _row(world, first).snapshot
    assert snapshot["bytes"] > 0 and os.path.exists(snapshot["path"])

    get_stub_host().remove_sandbox(f"sbx-{first}")  # the server is gone, and the workspace with it
    again = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="cat lib/pkg.txt notes.txt outputs/o.txt setup.log"))

    assert again.sandbox_id != first and again.recreated is True
    assert again.restored_snapshot_at == snapshot["at"]
    assert again.stdout == "package\nwork\nout\nsetup\n", "everything in the workspace is back, and setup did not run twice"
    assert "snapshot_restored" in _events(world, again.sandbox_id)
    assert _row(world, again.sandbox_id).snapshot["path"] == snapshot["path"], "it stays this report's snapshot"


def test_a_snapshot_over_the_size_limit_is_skipped_and_the_stop_still_happens(world, report):
    world.add_compute("one")
    world.add_environment("Default", is_default=True)
    world.configure(snapshot_on_stop=True, snapshot_max_mb=1)
    sandbox_id = world.run_code(report(), world.admin_id, RunRequest(
        language="bash", code="head -c 3000000 /dev/urandom > big.bin")).sandbox_id
    _stop(world, sandbox_id)
    row = _row(world, sandbox_id)
    assert row.state == "stopped" and row.snapshot is None
    assert "snapshot_skipped" in _events(world, sandbox_id)
    assert not os.path.exists(snapshots.path_for(sandbox_id)) and not os.path.exists(snapshots.path_for(sandbox_id) + ".partial")


def test_deleting_a_sandbox_on_purpose_deletes_its_snapshot_too(world, report, test_client, admin):
    world.add_compute("one")
    world.add_environment("Default", is_default=True)
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, WRITE).sandbox_id
    taken = test_client.post(f"{BASE}/{sandbox_id}/snapshot", headers=admin)
    assert taken.status_code == 200 and taken.json()["snapshot_bytes"] > 0 and taken.json()["snapshot_at"], taken.text
    path = _row(world, sandbox_id).snapshot["path"]
    assert os.path.exists(path)

    assert test_client.delete(f"{BASE}/{sandbox_id}", headers=admin).status_code == 200
    assert not os.path.exists(path), "a copy must not outlive a deliberate delete"
    fresh = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="ls"))
    assert fresh.restored_snapshot_at is None and "notes.txt" not in fresh.stdout


def test_evacuating_a_compute_moves_its_sandboxes_to_another_with_their_files(world, report, test_client, admin):
    old = world.add_compute("old")
    world.add_environment("Default", is_default=True)
    rep, idle = report(), report()
    moved_id = world.run_code(rep, world.admin_id, WRITE).sandbox_id
    stopped_id = world.run_code(idle, world.admin_id, WRITE).sandbox_id
    _stop(world, stopped_id)

    refused = test_client.post(f"{COMPUTES}/{old}/evacuate", headers=admin)
    assert refused.status_code == 409, "it must be disabled first, or new sandboxes land on it again"
    new = world.add_compute("new")
    test_client.put(f"{COMPUTES}/{old}", json={"enabled": False}, headers=admin)

    result = test_client.post(f"{COMPUTES}/{old}/evacuate", headers=admin)
    assert result.status_code == 200, result.text
    assert sorted(result.json()["moved"]) == sorted([moved_id, stopped_id]) and result.json()["left"] == []
    assert _row(world, moved_id).state == "deleted" and get_stub_host().get(f"sbx-{moved_id}") is None

    for original, its_report in ((moved_id, rep), (stopped_id, idle)):
        again = world.run_code(its_report, world.admin_id, READ)
        assert again.sandbox_id != original and again.stdout == "package\nwork\nout\n"
        assert _row(world, again.sandbox_id).compute_id == new


def test_a_sandbox_that_cannot_be_moved_is_listed_and_the_others_still_move(world, report, test_client, admin):
    old = world.add_compute("old")
    world.add_environment("Default", is_default=True)
    busy_report, calm_report = report(), report()
    busy_id = world.run_code(busy_report, world.admin_id, WRITE).sandbox_id
    calm_id = world.run_code(calm_report, world.admin_id, WRITE).sandbox_id

    async def mark_busy(db):
        (await db.get(Sandbox, busy_id)).state = "busy"  # it is in the middle of a run
        await db.commit()
    world.call(mark_busy)
    world.add_compute("new")
    test_client.put(f"{COMPUTES}/{old}", json={"enabled": False}, headers=admin)

    result = test_client.post(f"{COMPUTES}/{old}/evacuate", headers=admin)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["moved"] == [calm_id]
    assert [(left["sandbox_id"], left["code"]) for left in body["left"]] == [(busy_id, "sandbox.busy")]
    assert _row(world, busy_id).state == "busy", "left exactly where it is"


def test_a_sandbox_that_comes_back_from_a_snapshot_keeps_its_environment(world, report, test_client, admin):
    old = world.add_compute("old")
    world.add_environment("Default", is_default=True)
    world.add_environment("Special", network_policy="registries", start_script="echo special > which.txt")
    rep = report()
    first = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="echo work > notes.txt", environment="Special"))
    special_id = _row(world, first.sandbox_id).environment_id

    world.add_compute("new")
    test_client.put(f"{COMPUTES}/{old}", json={"enabled": False}, headers=admin)
    assert test_client.post(f"{COMPUTES}/{old}/evacuate", headers=admin).json()["moved"] == [first.sandbox_id]

    again = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="cat notes.txt which.txt"))  # no environment named
    row = _row(world, again.sandbox_id)
    assert row.environment_id == special_id and (row.limits or {})["network_policy"] == "registries", \
        "it comes back as what it was, not as the default environment"
    assert again.stdout == "work\nspecial\n"
