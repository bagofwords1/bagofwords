"""A compute fills up by what its sandboxes are given, not by how many there are.

Each environment sets its own CPU, memory and disk limits, so a compute's
room is counted in those: CPU and memory for the sandboxes that are running,
disk for every sandbox that still exists (a stopped one keeps its workspace).
"""
import uuid
from types import SimpleNamespace

import pytest

from app.models.sandbox import SandboxCompute
from app.services.sandbox import capacity, errors
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import RunRequest

pytestmark = pytest.mark.e2e

ECHO = RunRequest(language="bash", code="echo ran")
BASE = "/api/sandbox/computes"


@pytest.fixture
def world(sandbox_world, enterprise_license):
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


def _run(world, report, env):
    return world.run_code(report(), world.admin_id, RunRequest(language="bash", code="echo ran", environment=env))


# ── the rules, on their own ──────────────────────────────────────────────


def _compute(**kw):
    return SimpleNamespace(capacity=kw.pop("capacity", 20), resources=kw.pop("resources", {}), allocation=kw.pop("allocation", {}))


def _sandbox(state, **limits):
    return SimpleNamespace(compute_id="c", state=state, limits=limits)


def test_what_may_be_given_out_is_the_total_less_the_reserve_times_the_overcommit():
    compute = _compute(resources={"cpus": 4, "memory_mb": 16384, "disk_mb": 100 * 1024})
    assert capacity.allocatable(compute) == {"cpus": 16.0, "memory_mb": 15360, "disk_mb": 95 * 1024}
    tuned = _compute(resources={"cpus": 4, "memory_mb": 16384}, allocation={"cpu_overcommit": 1, "reserve_memory_mb": 4096})
    assert capacity.allocatable(tuned) == {"cpus": 4.0, "memory_mb": 12288}, "an unreported resource is not counted"
    assert capacity.allocatable(_compute()) == {}


def test_running_sandboxes_hold_cpu_and_memory_and_every_existing_one_holds_disk():
    usage = capacity.usage_by_compute([
        _sandbox("ready", cpus=2, memory_mb=4096, disk_mb=5120),
        _sandbox("busy", cpus=1, memory_mb=1024, disk_mb=1024),
        _sandbox("stopped", cpus=2, memory_mb=4096, disk_mb=5120),
        _sandbox("failed", cpus=2, memory_mb=4096, disk_mb=2048),
        _sandbox("deleted", cpus=8, memory_mb=8192, disk_mb=8192),
    ])["c"]
    assert (usage.cpus, usage.memory_mb, usage.disk_mb, usage.running, usage.existing) == (3, 5120, 13312, 2, 4)


def test_twenty_large_sandboxes_do_not_fit_a_small_server_whatever_the_count_says():
    compute = _compute(capacity=20, resources={"cpus": 4, "memory_mb": 16384, "disk_mb": 200 * 1024})
    need = {"cpus": 2, "memory_mb": 4096, "disk_mb": 5120}
    placed = []
    while capacity.fits(compute, capacity.usage_by_compute(placed).get("c"), need).ok:
        placed.append(_sandbox("ready", **need))
    assert len(placed) == 3, "15 GB may be given out: three 4 GB sandboxes, not twenty"
    refused = capacity.fits(compute, capacity.usage_by_compute(placed)["c"], need)
    assert refused.short == ["memory_mb"] and capacity.describe_short(refused.short) == "memory"


def test_resuming_needs_cpu_and_memory_again_but_not_disk():
    compute = _compute(resources={"cpus": 4, "memory_mb": 8192, "disk_mb": 12 * 1024})
    need = {"cpus": 1, "memory_mb": 2048, "disk_mb": 5120}
    usage = capacity.usage_by_compute([_sandbox("stopped", **need)])["c"]
    assert capacity.fits(compute, usage, need).short == ["disk_mb"], "a second one of the same size has no disk left"
    assert capacity.fits(compute, usage, need, resume=True).ok, "its own disk is already counted"


def test_the_count_stays_an_upper_bound_and_is_all_there_is_without_reported_resources():
    compute = _compute(capacity=1)
    need = {"cpus": 64, "memory_mb": 10 ** 6, "disk_mb": 10 ** 6}
    assert capacity.fits(compute, None, need).ok
    assert capacity.fits(compute, capacity.usage_by_compute([_sandbox("ready")])["c"], need).short == ["sandboxes"]


# ── through the service and the API ──────────────────────────────────────


def test_a_compute_reports_what_it_has_and_what_its_sandboxes_hold(world, test_client, admin, report):
    world.add_compute("small", cpus=4, memory_mb=4096, disk_mb=20 * 1024)
    world.add_environment("Std", is_default=True, cpus=1, memory_mb=1024, disk_mb=2048)
    _run(world, report, "Std")

    row = [c for c in test_client.get(BASE, headers=admin).json() if c["name"] == "small"][0]
    assert row["resources"] == {"cpus": 4, "memory_mb": 4096, "disk_mb": 20480}
    assert row["allocatable"] == {"cpus": 16.0, "memory_mb": 3072, "disk_mb": 15360}
    assert row["allocated"] == {"cpus": 1, "memory_mb": 1024, "disk_mb": 2048, "running": 1, "existing": 1}
    assert row["allocation"] == {"cpu_overcommit": 4.0, "memory_overcommit": 1.0, "reserve_memory_mb": 1024, "reserve_disk_mb": 5120}
    assert row["running_sandboxes"] == 1


def test_a_sandbox_that_does_not_fit_in_memory_is_refused_in_plain_words(world, report):
    world.add_compute("small", cpus=4, memory_mb=4096, disk_mb=50 * 1024)  # 3072 MB may be given out
    world.add_environment("Big", cpus=1, memory_mb=2048, disk_mb=1024)
    world.add_environment("Tiny", cpus=1, memory_mb=512, disk_mb=1024)
    world.configure(max_running_per_user=10)

    _run(world, report, "Big")
    with pytest.raises(SandboxError) as full:
        _run(world, report, "Big")
    assert full.value.code == errors.NO_COMPUTE and "not enough memory" in full.value.message
    assert _run(world, report, "Tiny").stdout == "ran\n", "a smaller one still fits"


def test_a_stopped_sandbox_frees_memory_but_keeps_its_disk(world, report):
    compute_id = world.add_compute("small", cpus=4, memory_mb=4096, disk_mb=5120 + 3072)  # 3072 MB of disk may be given out
    world.add_environment("Big", cpus=1, memory_mb=2048, disk_mb=2048)
    world.configure(max_running_per_user=10)
    first = report()
    outcome = world.run_code(first, world.admin_id, RunRequest(language="bash", code="echo ran", environment="Big"))

    async def stop(db):
        await world.service.stop(db, await world.service.get(db, world.org_id, outcome.sandbox_id), world.admin_id)
    world.call(stop)

    with pytest.raises(SandboxError) as full:
        _run(world, report, "Big")
    assert "not enough disk space" in full.value.message, "memory is free again; its workspace is still there"
    assert world.run_code(first, world.admin_id, ECHO).resumed is True, "its own disk is already counted"
    usage = world.call(lambda db: __import__("app.services.sandbox.computes", fromlist=["x"]).usage_by_compute(db, world.org_id))
    assert usage[compute_id].as_dict()["disk_mb"] == 2048


def test_new_sandboxes_go_to_the_compute_with_the_most_room_left(world, report):
    big = world.add_compute("big", cpus=16, memory_mb=65536, disk_mb=500 * 1024)
    small = world.add_compute("small", cpus=2, memory_mb=4096, disk_mb=500 * 1024)
    world.add_environment("Std", is_default=True, cpus=1, memory_mb=1024, disk_mb=1024)
    world.configure(max_running_per_user=10)
    placed = [world.rows(__import__("app.models.sandbox", fromlist=["Sandbox"]).Sandbox,
                         __import__("app.models.sandbox", fromlist=["Sandbox"]).Sandbox.id == _run(world, report, "Std").sandbox_id)[0].compute_id
              for _ in range(3)]
    assert placed == [big, big, big], "the same count means much less on the big one"
    assert small not in placed


def test_the_allocation_policy_is_set_per_compute_and_validated(world, test_client, admin):
    compute_id = world.add_compute("tuned", cpus=4, memory_mb=8192)
    changed = test_client.put(f"{BASE}/{compute_id}", json={"allocation": {"cpu_overcommit": 1, "reserve_memory_mb": 2048}}, headers=admin)
    assert changed.status_code == 200, changed.text
    assert changed.json()["allocatable"] == {"cpus": 4.0, "memory_mb": 6144}
    assert changed.json()["allocation"]["memory_overcommit"] == 1.0, "what was not mentioned keeps its default"
    assert world.rows(SandboxCompute, SandboxCompute.id == compute_id)[0].allocation == {"cpu_overcommit": 1.0, "reserve_memory_mb": 2048}

    for bad in ({"cpu_overcommit": 0}, {"memory_overcommit": 9}, {"no_such": 1}, {"reserve_disk_mb": -1}):
        refused = test_client.put(f"{BASE}/{compute_id}", json={"allocation": bad}, headers=admin)
        assert refused.status_code == 400 and refused.json()["error_code"] == "sandbox.invalid_request", (bad, refused.text)
