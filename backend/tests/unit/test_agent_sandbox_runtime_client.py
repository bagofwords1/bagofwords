"""The sandboxd runtime client, against the in-process stand-in.

The stand-in serves the two sandboxd APIs (gRPC processes, REST files) over
TLS with its own CA, and runs real local processes, so what is checked here
is the client's real behaviour: streaming, enforcing the run timeout,
cancelling, moving large files, and trusting only the endpoint's CA.
"""
import asyncio
import dataclasses
import hashlib
import os
import time
import uuid

import pytest
import pytest_asyncio

from app.services.sandbox.drivers.base import Endpoint
from app.services.sandbox.runtime_client import (
    RUN_CANCELLED, RUN_FAILED, RUN_SUCCEEDED, RUN_TIMEOUT, SandboxRuntimeClient, SandboxRuntimeError,
)
from app.services.sandbox.testing.sandboxd_stub import get_stub_host

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def endpoint():
    host = get_stub_host()
    sandbox_id, token = f"rt-{uuid.uuid4().hex[:10]}", uuid.uuid4().hex
    host.add_sandbox(sandbox_id, hashlib.sha256(token.encode()).hexdigest(), {"GREETING": "hello"})
    yield Endpoint(
        base_url=f"https://127.0.0.1:{host.rest_port}", grpc_target=f"127.0.0.1:{host.grpc_port}",
        ca_cert=host.ca_pem, headers={"X-Sandbox-Id": sandbox_id}, token=token,
    )
    host.remove_sandbox(sandbox_id)


@pytest_asyncio.fixture
async def runtime(endpoint):
    async with SandboxRuntimeClient(endpoint) as client:
        yield client


async def test_output_is_streamed_while_the_process_runs(runtime):
    seen = []

    def on_output(kind, chunk):
        seen.append((time.monotonic(), kind, chunk))

    result = await runtime.run(
        ["sh", "-c", "echo first; sleep 0.4; echo second; echo oops >&2; exit 3"], on_output=on_output,
    )
    assert (result.status, result.exit_code) == (RUN_FAILED, 3)
    assert result.stdout == b"first\nsecond\n" and result.stderr == b"oops\n"
    stdout_times = [t for t, kind, _ in seen if kind == "stdout"]
    # "first" arrived well before the process finished, not in one batch at the end.
    assert len(stdout_times) >= 2 and stdout_times[-1] - stdout_times[0] >= 0.3


async def test_environment_and_working_directory_are_applied(runtime):
    await runtime.make_dir("project")
    result = await runtime.run(
        ["sh", "-c", 'printf "%s %s %s" "$GREETING" "$EXTRA" "$(basename "$PWD")"'],
        env={"EXTRA": "there"}, cwd="project",
    )
    assert result.ok and result.stdout == b"hello there project"


async def test_a_run_past_its_timeout_is_stopped_and_reported(runtime):
    started = time.monotonic()
    result = await runtime.run(["sh", "-c", "echo started; sleep 60; echo never"], timeout=0.5)
    assert result.status == RUN_TIMEOUT
    assert result.stdout == b"started\n"
    assert result.exit_code not in (0, None), "the process was terminated, not left to finish"
    assert time.monotonic() - started < 10


async def test_a_process_that_ignores_sigterm_is_killed(runtime, monkeypatch):
    import app.services.sandbox.runtime_client as module

    monkeypatch.setattr(module, "TERMINATE_GRACE_SECONDS", 0.3)
    result = await runtime.run(["sh", "-c", "trap '' TERM; echo ready; while true; do sleep 0.1; done"], timeout=0.5)
    assert result.status == RUN_TIMEOUT
    assert result.exit_code == 137  # 128 + SIGKILL


async def test_a_run_can_be_cancelled_and_cleans_up_first(runtime):
    cancel = asyncio.Event()
    asyncio.get_running_loop().call_later(0.4, cancel.set)
    result = await runtime.run(
        ["sh", "-c", "trap 'echo cleaned up; exit 9' TERM; echo running; while true; do sleep 0.1; done"],
        cancel=cancel,
    )
    assert result.status == RUN_CANCELLED
    assert result.exit_code == 9 and b"cleaned up" in result.stdout


async def test_the_process_id_is_reported_as_soon_as_it_starts(runtime):
    started = []
    result = await runtime.run(["sh", "-c", "echo $$"], on_started=started.append)
    assert result.ok and started == [result.process_id] and int(result.stdout) == result.process_id


async def test_only_the_tail_of_a_large_output_is_kept_but_all_of_it_is_seen(runtime):
    total = []
    result = await runtime.run(
        ["sh", "-c", "i=0; while [ $i -lt 2000 ]; do echo line-$i; i=$((i+1)); done"],
        on_output=lambda kind, chunk: total.append(len(chunk)), output_tail_bytes=1000,
    )
    assert result.ok and result.stdout_truncated
    assert len(result.stdout) <= 1000 and result.stdout.endswith(b"line-1999\n")
    assert result.stdout_bytes == sum(total) > 1000


async def test_short_commands_run_with_the_daemons_own_limits(runtime):
    ok = await runtime.execute(["sh", "-c", "echo out; echo err >&2"])
    assert (ok.status, ok.stdout, ok.stderr) == (RUN_SUCCEEDED, b"out\n", b"err\n")
    limited = await runtime.execute(["sh", "-c", "sleep 30"], time_limit=0.3)
    assert limited.status == RUN_TIMEOUT
    capped = await runtime.execute(["sh", "-c", "printf 0123456789"], stdout_limit_bytes=4)
    assert capped.stdout == b"0123" and capped.stdout_truncated


async def test_a_command_that_cannot_start_is_an_error_not_a_result(runtime):
    with pytest.raises(SandboxRuntimeError) as failure:
        await runtime.run(["no-such-program-anywhere"])
    assert failure.value.code == "invalid"
    with pytest.raises(SandboxRuntimeError):
        await runtime.run([])


async def test_a_large_file_survives_the_trip_each_way(runtime):
    blob = os.urandom(1024 * 1024) * 24  # 24 MiB
    digest = hashlib.sha256(blob).hexdigest()
    await runtime.write_file("data/in.bin", blob)
    inside = await runtime.execute(["sh", "-c", "sha256sum data/in.bin | cut -d' ' -f1"])
    assert inside.stdout.decode().strip() == digest

    hasher, size = hashlib.sha256(), 0
    async for chunk in runtime.iter_file("data/in.bin"):
        hasher.update(chunk)
        size += len(chunk)
    assert size == len(blob) and hasher.hexdigest() == digest


async def test_an_upload_can_be_streamed_without_holding_it_in_memory(runtime):
    async def chunks():
        for index in range(64):
            yield bytes([index]) * 65536

    await runtime.write_file("stream.bin", chunks())
    assert len(await runtime.read_file("stream.bin")) == 64 * 65536


async def test_files_can_be_listed_read_and_deleted(runtime):
    await runtime.write_file("outputs/chart.png", b"\x89PNG")
    await runtime.write_file("outputs/nested/deep/table.csv", b"a,b\n1,2\n")
    await runtime.write_file("outputs/name with spaces & ünïcode.txt", b"x")

    entries = {e.name: e for e in await runtime.list_dir("outputs", recursive=True)}
    assert {"chart.png", "nested", "nested/deep", "nested/deep/table.csv", "name with spaces & ünïcode.txt"} <= set(entries)
    assert entries["chart.png"].is_file and entries["chart.png"].size == 4
    assert not entries["nested"].is_file
    assert {e.name for e in await runtime.list_dir("outputs")} == {"chart.png", "nested", "name with spaces & ünïcode.txt"}

    assert await runtime.read_file("outputs/name with spaces & ünïcode.txt") == b"x"
    assert await runtime.exists("outputs/chart.png") and not await runtime.exists("outputs/missing.png")
    assert await runtime.list_dir("no/such/dir") == []
    with pytest.raises(SandboxRuntimeError) as missing:
        await runtime.read_file("outputs/missing.png")
    assert missing.value.code == "not_found"
    with pytest.raises(SandboxRuntimeError) as too_large:
        await runtime.read_file("outputs/nested/deep/table.csv", max_bytes=3)
    assert too_large.value.code == "too_large"

    await runtime.delete("outputs/nested")
    await runtime.delete("outputs/nested")  # already gone
    assert not await runtime.exists("outputs/nested/deep/table.csv")


async def test_paths_cannot_leave_the_sandbox_root(runtime):
    with pytest.raises(SandboxRuntimeError):
        await runtime.write_file("../escaped.txt", b"x")
    with pytest.raises(SandboxRuntimeError):
        await runtime.run(["true"], cwd="../..")


async def test_only_the_endpoints_ca_is_trusted(endpoint):
    """A certificate from any other CA is refused, on both APIs — including a
    CA the machine itself trusts: the endpoint's CA is the only root."""
    other_ca = get_stub_host()._tls_material()[0].decode()
    for ca in (other_ca,):
        async with SandboxRuntimeClient(dataclasses.replace(endpoint, ca_cert=ca)) as client:
            assert not await client.health()
            with pytest.raises(SandboxRuntimeError) as files:
                await client.list_dir("")
            assert files.value.code == "unreachable"
            with pytest.raises(SandboxRuntimeError) as process:
                await client.run(["true"])
            assert process.value.code == "unreachable"
    # No CA at all is a configuration error, never a silent fallback to system roots.
    async with SandboxRuntimeClient(dataclasses.replace(endpoint, ca_cert=None)) as client:
        with pytest.raises(SandboxRuntimeError) as invalid:
            await client.list_dir("")
        assert invalid.value.code == "invalid"
    async with SandboxRuntimeClient(endpoint) as client:
        assert await client.health()


async def test_requests_carry_the_endpoints_routing_headers(endpoint):
    """The headers pick the sandbox: with another sandbox's id the token no longer matches."""
    host = get_stub_host()
    other = f"rt-{uuid.uuid4().hex[:10]}"
    host.add_sandbox(other, hashlib.sha256(b"other-token").hexdigest())
    try:
        misrouted = dataclasses.replace(endpoint, headers={"X-Sandbox-Id": other})
        async with SandboxRuntimeClient(misrouted) as client:
            with pytest.raises(SandboxRuntimeError) as refused:
                await client.run(["true"])
            assert refused.value.code == "unauthenticated"
    finally:
        host.remove_sandbox(other)


async def test_an_unreachable_sandbox_is_reported_as_such(endpoint):
    dead = dataclasses.replace(endpoint, base_url="https://127.0.0.1:1", grpc_target="127.0.0.1:1")
    async with SandboxRuntimeClient(dead, connect_timeout=1.0) as client:
        assert not await client.health(timeout=1)
        with pytest.raises(SandboxRuntimeError) as waiting:
            await client.wait_healthy(deadline_seconds=0.3)
        assert waiting.value.code == "unreachable"


async def test_the_token_is_never_in_a_repr(endpoint):
    assert endpoint.token not in repr(endpoint)
