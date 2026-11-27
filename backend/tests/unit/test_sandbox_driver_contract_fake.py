"""The sandbox driver contract, run against the fake driver.

Part of the default unit run, so the driver interface always has an
implementation that is checked in CI. The same tests run against a real
Docker host from ``tests/contracts/sandbox_driver/test_docker_ssh_live.py``.
"""
import uuid

import pytest
import pytest_asyncio

from app.services.sandbox.drivers import registry
from app.services.sandbox.drivers.base import BackendContext, SandboxHandle
from app.services.sandbox.drivers.fake import reset_fake_backends
from tests.contracts.sandbox_driver.contract import *  # noqa: F401,F403 - the contract tests
from tests.contracts.sandbox_driver.harness import DriverHarness

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def harness():
    backend_id, instance_id = uuid.uuid4().hex, "this-bow-instance"

    def make_driver():
        ctx = BackendContext(backend_id=backend_id, name="fake", instance_id=instance_id)
        return registry.create_driver("fake", ctx)

    driver = make_driver()
    steps = await driver.prepare()
    assert [s.status for s in steps] == ["ok"] * len(steps)

    async def exists(sandbox_id):
        return driver.has_sandbox(sandbox_id)

    async def plant_orphan(instance_id):
        sandbox_id = uuid.uuid4().hex[:12]
        driver.plant_foreign_sandbox(sandbox_id, instance_id=instance_id)
        return sandbox_id

    async def remove(sandbox_id):
        await driver.delete(SandboxHandle(sandbox_id=sandbox_id))

    harness = DriverHarness(
        driver=driver, make_driver=make_driver, image="fake-image",
        probe_command=lambda domain: ["bow-net-probe", domain],
        registry_domain="pypi.org", other_domain="example.com",
        plant_orphan=plant_orphan, exists_on_backend=exists, remove_from_backend=remove,
    )
    yield harness
    await harness.cleanup()
    reset_fake_backends()
