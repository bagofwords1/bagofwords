"""Volumes: storage an operator makes available on a compute, mounted by name
in an environment's sandboxes. Read-only unless the environment says otherwise;
a writable mount gets each report its own directory unless it is shared on purpose."""
import uuid

import pytest

from app.services.sandbox import errors
from app.services.sandbox.drivers import fake as fake_driver
from app.services.sandbox.drivers.base import MountSpec, SandboxDriverError
from app.services.sandbox.drivers.docker_ssh import commands
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import RunRequest

pytestmark = pytest.mark.e2e

COMPUTES = "/api/sandbox/computes"
ENVS = "/api/sandbox/environments"
ECHO = lambda env: RunRequest(language="bash", code="echo ran", environment=env)  # noqa: E731


@pytest.fixture
def world(sandbox_world, enterprise_license):
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


def _compute(test_client, admin, name, volumes):
    created = test_client.post(COMPUTES, json={"name": name, "type": "fake", "config": {"server": name}, "volumes": volumes}, headers=admin)
    assert created.status_code == 200, created.text
    compute = created.json()
    assert test_client.post(f"{COMPUTES}/{compute['id']}/bootstrap", json={"wait": True}, headers=admin).json()["bootstrap_status"] == "ready"
    return test_client.put(f"{COMPUTES}/{compute['id']}", json={"enabled": True}, headers=admin).json()


def _env(test_client, admin, name, mounts, **fields):
    created = test_client.post(ENVS, json={"name": name, "image": "fake-image", "mounts": mounts, **fields}, headers=admin)
    assert created.status_code == 200, created.text
    return created.json()


def test_a_sandbox_gets_the_volumes_its_environment_mounts_read_only_by_default(world, test_client, admin, report):
    compute = _compute(test_client, admin, "nas", [{"name": "datasets", "source": "/mnt/datasets", "description": "Shared data"}])
    assert compute["volumes"] == [{"name": "datasets", "source": "/mnt/datasets", "description": "Shared data"}]
    env = _env(test_client, admin, "Data", [{"volume": "datasets", "path": "/data"}])
    assert env["mounts"] == [{"volume": "datasets", "path": "/data", "read_only": True, "shared": False}]

    outcome = world.run_code(report(), world.admin_id, ECHO("Data"))
    mounted = fake_driver._COMPUTES["nas"].sandboxes[outcome.sandbox_id].mounts
    assert mounted == [MountSpec(volume="datasets", source="/mnt/datasets", path="/data", read_only=True, subpath=None)]
    row = [r for r in test_client.get("/api/sandbox/sandboxes", headers=admin).json() if r["id"] == outcome.sandbox_id][0]
    assert row["limits"]["mounts"] == [{"volume": "datasets", "path": "/data", "read_only": True}]


def test_a_writable_mount_gives_each_report_its_own_directory_unless_shared_on_purpose(world, test_client, admin, report):
    _compute(test_client, admin, "nas", [{"name": "scratch", "source": "/mnt/scratch"}])
    _env(test_client, admin, "Own", [{"volume": "scratch", "path": "/scratch", "read_only": False}])
    shared = _env(test_client, admin, "Shared", [{"volume": "scratch", "path": "/scratch", "read_only": False, "shared": True}])
    assert any("writable and shared" in w for w in shared["warnings"]), "the admin is told what sharing means"

    first, second = report(), report()
    one = world.run_code(first, world.admin_id, ECHO("Own")).sandbox_id
    two = world.run_code(second, world.admin_id, ECHO("Own")).sandbox_id
    sandboxes = fake_driver._COMPUTES["nas"].sandboxes
    assert sandboxes[one].mounts[0].subpath == f"reports/{first['id']}"
    assert sandboxes[two].mounts[0].subpath == f"reports/{second['id']}", "a report never sees another report's files"
    assert sandboxes[one].mounts[0].read_only is False

    everyone = world.run_code(report(), world.admin_id, ECHO("Shared")).sandbox_id
    assert sandboxes[everyone].mounts[0].subpath is None


def test_an_environment_only_runs_where_its_volumes_are(world, test_client, admin, report):
    _compute(test_client, admin, "plain", [])
    with_volume = _compute(test_client, admin, "nas", [{"name": "datasets", "source": "/mnt/datasets"}])
    _env(test_client, admin, "Data", [{"volume": "datasets", "path": "/data"}])
    placed = {r["id"]: r["compute_name"] for r in []}
    for _ in range(3):
        sandbox_id = world.run_code(report(), world.admin_id, ECHO("Data")).sandbox_id
        row = [r for r in test_client.get("/api/sandbox/sandboxes", headers=admin).json() if r["id"] == sandbox_id][0]
        placed[sandbox_id] = row["compute_name"]
    assert set(placed.values()) == {"nas"}

    orphan = _env(test_client, admin, "Nowhere", [{"volume": "archive", "path": "/archive"}])
    assert any("No compute has every volume" in w and "archive" in w for w in orphan["warnings"])
    with pytest.raises(SandboxError) as nowhere:
        world.run_code(report(), world.admin_id, ECHO("Nowhere"))
    assert nowhere.value.code == errors.NO_COMPUTE

    # Removing the volume from the compute takes the environment with it for new sandboxes.
    assert test_client.put(f"{COMPUTES}/{with_volume['id']}", json={"volumes": []}, headers=admin).json()["volumes"] == []
    with pytest.raises(SandboxError):
        world.run_code(report(), world.admin_id, ECHO("Data"))


@pytest.mark.parametrize("mount", [
    {"volume": "datasets", "path": "/workspace/data"},   # BoW's own workspace
    {"volume": "datasets", "path": "/tmp"},
    {"volume": "datasets", "path": "/etc/ssl"},          # a system directory
    {"volume": "datasets", "path": "/data/../etc"},
    {"volume": "Bad Name", "path": "/data"},
    {"volume": "datasets", "path": "/data", "mode": "rw"},  # unknown field
])
def test_mounts_that_would_shadow_the_system_or_the_workspace_are_refused(world, test_client, admin, mount):
    refused = test_client.post(ENVS, json={"name": "Bad", "image": "fake-image", "mounts": [mount]}, headers=admin)
    assert refused.status_code == 422, refused.text
    twice = test_client.post(ENVS, json={"name": "Twice", "image": "fake-image", "mounts": [
        {"volume": "a", "path": "/data"}, {"volume": "b", "path": "/data"}]}, headers=admin)
    assert twice.status_code == 422


# ── the server driver's side (command templates only; no server needed) ──


@pytest.mark.parametrize("source", ["/", "/etc", "/etc/ssl", "/var/lib/docker", "/var/lib/docker/volumes/x", "/proc/1",
                                    "/var/lib/bow-sandbox/ws", "relative/path", "/mnt/../etc", "/mnt/a b", "/mnt/$(id)", "/mnt/x;y", ""])
def test_a_server_volume_cannot_be_a_system_directory_or_anything_a_shell_would_read(source):
    with pytest.raises(SandboxDriverError):
        commands.volume_source(source)


def test_a_server_volume_becomes_a_bind_mount_that_is_read_only_unless_asked():
    assert commands.volume_source("/mnt/datasets/") == "/mnt/datasets"
    mounts = [
        MountSpec(volume="datasets", source="/mnt/datasets", path="/data"),
        MountSpec(volume="scratch", source="/srv/scratch", path="/scratch", read_only=False, subpath="reports/abc-123"),
    ]
    assert commands.mount_args(mounts) == [
        "--mount", "type=bind,source=/mnt/datasets,target=/data,bind-propagation=rprivate,readonly",
        "--mount", "type=bind,source=/srv/scratch/reports/abc-123,target=/scratch,bind-propagation=rprivate",
    ]
    assert commands.mount_prepare(mounts) == [
        ["install", "-d", "-m", "700", "-o", "1000", "-g", "1000", "/srv/scratch/reports/abc-123"],
    ], "only the per-report directory is created; the volume itself is the operator's"
    with pytest.raises(SandboxDriverError):
        commands.mount_args([MountSpec(volume="x", source="/mnt/x", path="/data,readonly=false")])
    with pytest.raises(SandboxDriverError):
        commands.mount_args([MountSpec(volume="x", source="/mnt/x", path="/data", subpath="../../etc")])


def test_the_server_driver_refuses_a_bad_volume_when_the_compute_is_saved(world, test_client, admin):
    bad = test_client.post(COMPUTES, json={"name": "srv", "type": "docker-ssh", "config": {"address": "srv.example.com"},
                                           "volumes": [{"name": "docker", "source": "/var/lib/docker"}]}, headers=admin)
    assert bad.status_code == 400 and "system directory" in bad.json()["detail"], bad.text
    good = test_client.post(COMPUTES, json={"name": "srv", "type": "docker-ssh", "config": {"address": "srv.example.com"},
                                            "volumes": [{"name": "datasets", "source": "/mnt/datasets/"}]}, headers=admin)
    assert good.status_code == 200 and good.json()["volumes"][0]["source"] == "/mnt/datasets"
    twice = test_client.put(f"{COMPUTES}/{good.json()['id']}", json={"volumes": [
        {"name": "a", "source": "/mnt/a"}, {"name": "a", "source": "/mnt/b"}]}, headers=admin)
    assert twice.status_code == 422
