"""The sandbox driver contract, run against a real Docker host.

Skipped unless a test server is named. Run it from the live feedback loop:

    BOW_SANDBOX_TEST_HOST=<address> \\
    BOW_SANDBOX_TEST_USER=ubuntu \\
    BOW_SANDBOX_TEST_KEY=/path/to/private-key \\
    BOW_SANDBOX_TEST_FINGERPRINT=SHA256:... \\
    uv run pytest tests/contracts/sandbox_driver/test_docker_ssh_live.py

Optional: ``BOW_SANDBOX_TEST_BOW_ADDRESS`` (restrict the sandbox port to this
address), ``BOW_SANDBOX_TEST_PRELOADED=1`` (images are already on the server),
``BOW_SANDBOX_TEST_IMAGE`` (the sandbox image; needs ``python``), and the
``BOW_SANDBOX_*_IMAGE`` overrides for unpublished support images.

It bootstraps the server as its own BoW instance, so run it on a server no
other instance is using. Sandboxes get a 1 GB memory limit and there are
never more than two at once.
"""
import json
import os
import uuid

import pytest
import pytest_asyncio

from app.services.sandbox.drivers import registry
from app.services.sandbox.drivers.base import BackendContext
from app.services.sandbox.drivers.docker_ssh import layout
from tests.contracts.sandbox_driver.contract import *  # noqa: F401,F403 - the contract tests
from tests.contracts.sandbox_driver.harness import DriverHarness

HOST = os.getenv("BOW_SANDBOX_TEST_HOST")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not HOST, reason="set BOW_SANDBOX_TEST_HOST to run the contract against a real Docker host"),
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


def _context() -> BackendContext:
    with open(os.environ["BOW_SANDBOX_TEST_KEY"]) as handle:
        private_key = handle.read()
    config = {
        "address": HOST,
        "ssh_user": os.getenv("BOW_SANDBOX_TEST_USER", "root"),
        "ssh_host_key_fingerprint": os.getenv("BOW_SANDBOX_TEST_FINGERPRINT"),
        "images_preloaded": os.getenv("BOW_SANDBOX_TEST_PRELOADED", "") in ("1", "true"),
        "bow_address": os.getenv("BOW_SANDBOX_TEST_BOW_ADDRESS") or None,
    }
    return BackendContext(
        backend_id="contract-backend", name="contract", instance_id=INSTANCE,
        config=registry.validate_config("docker-ssh", config),
        credentials={"ssh_private_key": private_key}, driver_state=_STATE, base_url="http://contract-suite",
    )


@pytest_asyncio.fixture
async def harness():
    drivers = []

    def make_driver():
        driver = registry.create_driver("docker-ssh", _context())
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
        plant_orphan=plant_orphan, exists_on_backend=exists, remove_from_backend=remove,
        memory_mb=1024, disk_mb=512,
    )
    yield harness
    await harness.cleanup()
    for instance in drivers:
        await instance.close()
