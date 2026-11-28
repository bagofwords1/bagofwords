"""The sandbox driver contract.

Every test takes a ``harness`` (see ``harness.py``) and uses only the driver
interface and the runtime client. Import this module's tests into a module
that defines the ``harness`` fixture for one driver.

The tests state what the layers above a driver rely on:

- create from a spec, and the endpoint it returns answers;
- only the sandbox's own token opens it;
- stop and resume keep files; delete removes everything and is idempotent;
- each network policy lets through exactly what it says;
- environment variables and secrets reach the code;
- reconcile removes this instance's orphans and nothing else;
- what the driver says it can do matches what it does.
"""
import dataclasses

import pytest

from app.services.sandbox.drivers.base import (
    ALL_CAPABILITIES,
    CAP_DOMAIN_ALLOWLIST,
    CAP_LIVE_STATS,
    CAP_STOP_RESUME,
    POLICY_FULL,
    POLICY_NONE,
    POLICY_REGISTRIES,
    STATUS_MISSING,
    STATUS_RUNNING,
    STATUS_STOPPED,
    SandboxNotFound,
)
from app.services.sandbox.runtime_client import SandboxRuntimeClient, SandboxRuntimeError

__all__ = [
    "test_a_created_sandbox_answers_and_runs_commands",
    "test_only_the_sandbox_token_opens_it",
    "test_environment_variables_and_secrets_reach_the_code",
    "test_stop_and_resume_keep_files",
    "test_delete_removes_everything_and_can_be_repeated",
    "test_sandboxes_do_not_share_files",
    "test_network_policy_none_reaches_nothing",
    "test_network_policy_registries_reaches_only_the_registries",
    "test_network_policy_full_reaches_the_internet",
    "test_reconcile_removes_this_instances_orphans_only",
    "test_reconcile_reports_known_sandboxes_that_are_gone",
    "test_capabilities_match_behaviour",
    "test_check_reports_a_prepared_compute_and_its_load",
    "test_another_driver_instance_reaches_the_same_sandbox",
]

pytestmark = pytest.mark.asyncio


async def test_a_created_sandbox_answers_and_runs_commands(harness):
    handle, token = await harness.create()
    assert (await harness.driver.status(handle)).state == STATUS_RUNNING
    async with harness.runtime(handle, token) as runtime:
        assert await runtime.health()
        await runtime.write_file("hello.txt", b"from bow")
        result = await runtime.run(["sh", "-c", "cat hello.txt && echo done >&2"])
    assert result.ok and result.exit_code == 0
    assert result.stdout == b"from bow"
    assert result.stderr == b"done\n"


async def test_only_the_sandbox_token_opens_it(harness):
    first, first_token = await harness.create()
    second, second_token = await harness.create()
    # No token, a wrong token, and another sandbox's valid token are all refused.
    for bad in (None, "not-the-token", second_token):
        endpoint = dataclasses.replace(harness.driver.endpoint(first), token=bad)
        async with SandboxRuntimeClient(endpoint) as runtime:
            with pytest.raises(SandboxRuntimeError) as files:
                await runtime.list_dir("")
            assert files.value.code == "unauthenticated"
            with pytest.raises(SandboxRuntimeError) as process:
                await runtime.run(["true"])
            assert process.value.code == "unauthenticated"
    async with harness.runtime(first, first_token) as runtime:
        assert (await runtime.run(["true"])).ok


async def test_environment_variables_and_secrets_reach_the_code(harness):
    handle, token = await harness.create(env={"REGION": "eu-1"}, secrets={"API_KEY": "s3cr3t-value"})
    async with harness.runtime(handle, token) as runtime:
        result = await runtime.run(["sh", "-c", 'printf "%s|%s" "$REGION" "$API_KEY"'])
    assert result.stdout == b"eu-1|s3cr3t-value"
    # The handle BoW stores must not carry the secret.
    assert "s3cr3t-value" not in repr(handle) and "s3cr3t-value" not in str(dict(handle.ref))


async def test_stop_and_resume_keep_files(harness):
    if CAP_STOP_RESUME not in harness.driver.capabilities():
        pytest.skip("this compute cannot stop and resume")
    handle, token = await harness.create()
    async with harness.runtime(handle, token) as runtime:
        await runtime.write_file("outputs/kept.txt", b"still here")

    await harness.driver.stop(handle)
    assert (await harness.driver.status(handle)).state == STATUS_STOPPED
    async with harness.runtime(handle, token) as runtime:
        assert not await runtime.health(timeout=2)

    await harness.driver.start(handle)
    assert (await harness.driver.status(handle)).state == STATUS_RUNNING
    async with harness.runtime(handle, token) as runtime:
        assert await runtime.read_file("outputs/kept.txt") == b"still here"
        assert (await runtime.run(["true"])).ok  # same token after the resume


async def test_delete_removes_everything_and_can_be_repeated(harness):
    handle, token = await harness.create()
    assert await harness.exists_on_compute(handle.sandbox_id)
    await harness.driver.delete(handle)
    assert not await harness.exists_on_compute(handle.sandbox_id)
    assert (await harness.driver.status(handle)).state == STATUS_MISSING
    async with harness.runtime(handle, token) as runtime:
        assert not await runtime.health(timeout=2)
    await harness.driver.delete(handle)  # already gone: not an error
    with pytest.raises(SandboxNotFound):
        await harness.driver.start(handle)


async def test_sandboxes_do_not_share_files(harness):
    first, first_token = await harness.create()
    second, second_token = await harness.create()
    async with harness.runtime(first, first_token) as runtime:
        await runtime.write_file("private.txt", b"first only")
    async with harness.runtime(second, second_token) as runtime:
        assert not await runtime.exists("private.txt")


async def _reaches(harness, handle, token, domain) -> bool:
    async with harness.runtime(handle, token) as runtime:
        result = await runtime.run(list(harness.probe_command(domain)), timeout=60)
    return result.exit_code == 0


async def test_network_policy_none_reaches_nothing(harness):
    handle, token = await harness.create(network_policy=POLICY_NONE)
    assert not await _reaches(harness, handle, token, harness.registry_domain)
    assert not await _reaches(harness, handle, token, harness.other_domain)


async def test_network_policy_registries_reaches_only_the_registries(harness):
    if CAP_DOMAIN_ALLOWLIST not in harness.driver.capabilities():
        pytest.skip("this compute cannot filter by domain")
    handle, token = await harness.create(
        network_policy=POLICY_REGISTRIES, allowed_domains=[harness.registry_domain],
    )
    assert await _reaches(harness, handle, token, harness.registry_domain)
    assert not await _reaches(harness, handle, token, harness.other_domain)
    # Both attempts are in the domain log, with what happened to each.
    log = {(r.domain, r.decision) for r in await harness.driver.egress_log(handle)}
    assert (harness.registry_domain, "allowed") in log
    assert (harness.other_domain, "refused") in log


async def test_network_policy_full_reaches_the_internet(harness):
    handle, token = await harness.create(network_policy=POLICY_FULL)
    assert await _reaches(harness, handle, token, harness.registry_domain)
    assert await _reaches(harness, handle, token, harness.other_domain)


async def test_reconcile_removes_this_instances_orphans_only(harness):
    kept, _ = await harness.create()
    orphan = await harness.plant_orphan(None)
    foreign = await harness.plant_orphan("another-bow-instance")
    try:
        result = await harness.driver.reconcile([kept])
        assert orphan in result.removed
        assert not await harness.exists_on_compute(orphan)
        # Another BoW instance's sandbox is left alone, and so is the known one.
        assert await harness.exists_on_compute(foreign)
        assert foreign not in result.removed
        assert (await harness.driver.status(kept)).state == STATUS_RUNNING
    finally:
        await harness.remove_from_compute(foreign)


async def test_reconcile_reports_known_sandboxes_that_are_gone(harness):
    handle, _ = await harness.create()
    await harness.driver.delete(handle)
    result = await harness.driver.reconcile([handle])
    assert handle.sandbox_id in result.missing


async def test_capabilities_match_behaviour(harness):
    capabilities = harness.driver.capabilities()
    assert capabilities <= ALL_CAPABILITIES
    handle, _ = await harness.create()
    stats = await harness.driver.stats([handle])
    if CAP_LIVE_STATS in capabilities:
        assert handle.sandbox_id in stats
    else:
        assert stats == {}
    if CAP_STOP_RESUME in capabilities:
        await harness.driver.stop(handle)
        assert (await harness.driver.status(handle)).state == STATUS_STOPPED


async def test_check_reports_a_prepared_compute_and_its_load(harness):
    before = await harness.driver.check()
    assert before.reachable and before.prepared and before.healthy
    assert set(before.capabilities) == harness.driver.capabilities()
    await harness.create()
    await harness.create()
    assert (await harness.driver.check()).running == before.running + 2


async def test_another_driver_instance_reaches_the_same_sandbox(harness):
    """Any worker can serve any sandbox: nothing lives in one driver instance."""
    handle, token = await harness.create()
    other = harness.make_driver()
    assert (await other.status(handle)).state == STATUS_RUNNING
    async with harness.runtime(handle, token, driver=other) as runtime:
        assert (await runtime.run(["true"])).ok
    await other.delete(handle)
    assert (await harness.driver.status(handle)).state == STATUS_MISSING
