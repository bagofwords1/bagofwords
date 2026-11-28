"""Shared setup for the live docker-ssh tests: the server named by the
environment, bootstrapped once per test session as its own BoW instance.

See ``test_docker_ssh_live.py`` for the variables.
"""
import json
import os
import uuid

import pytest
import pytest_asyncio

from app.services.sandbox.drivers import registry
from app.services.sandbox.drivers.base import ComputeContext
from app.services.sandbox.drivers.docker_ssh import layout
from tests.contracts.sandbox_driver.harness import DriverHarness

HOST = os.getenv("BOW_SANDBOX_TEST_HOST")
LIVE = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not HOST, reason="set BOW_SANDBOX_TEST_HOST to run against a real Docker host"),
]

INSTANCE = os.getenv("BOW_SANDBOX_TEST_INSTANCE", "contract-suite-instance")
IMAGE = os.getenv("BOW_SANDBOX_TEST_IMAGE", "python:3.12-slim")
_STATE: dict = {}  # the driver's state, kept across tests the way BoW's database keeps it

_PROBE = (
    "import sys, urllib.request\n"
    "try:\n"
    "    urllib.request.urlopen('https://' + sys.argv[1] + '/', timeout=10)\n"
    "except Exception as exc:\n"
    "    code = getattr(exc, 'code', None)\n"
    "    sys.exit(0 if isinstance(code, int) and code < 500 and code not in (403, 407) else 1)\n"
)


def context(instance_id: str = None, state: dict = None, **config_overrides) -> ComputeContext:
    with open(os.environ["BOW_SANDBOX_TEST_KEY"]) as handle:
        private_key = handle.read()
    config = {
        "address": HOST,
        "ssh_user": os.getenv("BOW_SANDBOX_TEST_USER", "root"),
        "ssh_host_key_fingerprint": os.getenv("BOW_SANDBOX_TEST_FINGERPRINT"),
        "images_preloaded": os.getenv("BOW_SANDBOX_TEST_PRELOADED", "") in ("1", "true"),
        "bow_address": os.getenv("BOW_SANDBOX_TEST_BOW_ADDRESS") or None,
    }
    config.update(config_overrides)
    return ComputeContext(
        compute_id="contract-compute", name="contract", instance_id=instance_id or INSTANCE,
        config=registry.validate_config("docker-ssh", config),
        credentials={"ssh_private_key": private_key}, driver_state=_STATE if state is None else state,
        base_url="http://contract-suite",
    )


@pytest_asyncio.fixture
async def harness():
    drivers = []

    def make_driver():
        driver = registry.create_driver("docker-ssh", context())
        drivers.append(driver)
        return driver

    driver = make_driver()
    if not _STATE.get("prepared"):
        steps = await driver.prepare(take_over=True, images=[IMAGE])
        failed = [f"{s.key}: {s.detail}" for s in steps if s.status == "failed"]
        assert not failed, failed

    async def exists(sandbox_id):
        return await driver.host.container_state(layout.sandbox_container(sandbox_id)) is not None

    async def plant_orphan(instance_id):
        """A container carrying BoW's labels that no database knows about."""
        sandbox_id = uuid.uuid4().hex[:12]
        owner = instance_id or INSTANCE
        await driver.host.ssh.run([
            "docker", "run", "-d", "--name", layout.sandbox_container(sandbox_id),
            "--label", f"{layout.LABEL_INSTANCE}={owner}", "--label", f"{layout.LABEL_SANDBOX}={sandbox_id}",
            "--network", "none", "--runtime", "runc", "--memory", "64m", IMAGE, "sleep", "600",
        ])
        await driver.host.ssh.write_file(
            f"{layout.ROOT}/sandboxes/{sandbox_id}.json",
            json.dumps({"sandbox_id": sandbox_id, "instance": owner, "ip": None, "policy": "none"}) + "\n",
        )
        return sandbox_id

    async def remove(sandbox_id):
        await driver.host.remove_sandbox(sandbox_id)

    harness = DriverHarness(
        driver=driver, make_driver=make_driver, image=IMAGE,
        probe_command=lambda domain: ["python", "-c", _PROBE, domain],
        registry_domain="pypi.org", other_domain="example.com",
        plant_orphan=plant_orphan, exists_on_compute=exists, remove_from_compute=remove,
        memory_mb=1024, disk_mb=512,
    )
    yield harness
    await harness.cleanup()
    for instance in drivers:
        await instance.close()
