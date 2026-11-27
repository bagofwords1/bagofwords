"""What is specific to the docker-ssh driver, checked on a real Docker host.

The contract suite (``test_docker_ssh_live.py``) covers what every driver
must do. This module covers what only this driver promises: what a sandbox
with no internet really cannot reach, the container's hardening and limits,
which runtime each container gets, that bootstrap can be repeated, that a
failed step is recorded, and that a server belongs to one BoW instance.

Same environment variables as the contract module. It ends by cleaning the
server up completely, so it also checks that nothing is left behind.
"""
import json
import os
import uuid

import pytest

from app.services.sandbox.drivers import registry
from app.services.sandbox.drivers.base import CAP_STRONG_ISOLATION, SandboxDriverError
from app.services.sandbox.drivers.docker_ssh import layout
from tests.contracts.sandbox_driver.live_support import HOST, IMAGE, INSTANCE, LIVE, context, harness  # noqa: F401

pytestmark = LIVE

_REACH = '''
import json, socket, sys
def tcp(host, port):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); return True
    except Exception:
        return False
    finally:
        s.close()
def resolves(name):
    try:
        socket.gethostbyname(name); return True
    except Exception:
        return False
targets = json.loads(sys.argv[1])
print(json.dumps({"tcp": {k: tcp(h, p) for k, (h, p) in targets.items()}, "dns": resolves("pypi.org")}))
'''


async def _docker_inspect(harness, name, template):
    result = await harness.driver.host.docker("inspect", "--format", template, name)
    return result.stdout.strip()


async def test_a_sandbox_with_no_internet_reaches_nothing_but_the_proxy_which_refuses_it(harness):
    first, first_token = await harness.create(network_policy="none")
    second, second_token = await harness.create(network_policy="none")
    addr = harness.driver.addr
    targets = {
        "another sandbox": (second.ref["ip"], 8080),
        "the router": (addr.caddy, 8443),
        "the host, by its bridge address": (addr.gateway, 22),
        "the host, by its own address": (HOST, 22),
        "the host's sandbox port": (HOST, 8443),
        "the cloud metadata address": ("169.254.169.254", 80),
        "a private address": ("10.0.0.1", 80),
        "the internet": ("1.1.1.1", 443),
        "outbound mail": ("142.250.102.26", 25),
        "the egress proxy": (addr.egress, 3128),
    }
    async with harness.runtime(first, first_token) as runtime:
        result = await runtime.run(["python", "-c", _REACH, json.dumps(targets)], timeout=120)
        refused = await runtime.run(["python", "-c", (
            "import http.client\n"
            f"c = http.client.HTTPConnection('{addr.egress}', 3128, timeout=10)\n"
            "c.request('CONNECT', 'pypi.org:443'); print(c.getresponse().status)\n"
        )], timeout=60)
    assert result.ok, result.stderr
    seen = json.loads(result.stdout)
    reachable = {name for name, ok in seen["tcp"].items() if ok}
    assert reachable == {"the egress proxy"}, f"reachable from a no-internet sandbox: {sorted(reachable)}"
    assert seen["dns"] is False, "a sandbox without internet resolves no names"
    assert refused.stdout.strip() == b"403", "the proxy has no rule for it, so it refuses"


async def test_a_sandbox_runs_hardened_with_its_limits_enforced(harness):
    handle, token = await harness.create(memory_mb=256, disk_mb=64, env={"VISIBLE": "yes"}, secrets={"API_KEY": "s3cr3t-live"})
    name = layout.sandbox_container(handle.sandbox_id)
    config = json.loads(await _docker_inspect(harness, name, "{{json .HostConfig}}"))
    assert config["CapDrop"] == ["ALL"] and not config["Privileged"] and config["ReadonlyRootfs"]
    assert "no-new-privileges" in " ".join(config["SecurityOpt"])
    assert config["Memory"] == 256 * 1024 * 1024 == config["MemorySwap"] and config["PidsLimit"] == 512
    assert not config["PortBindings"], "a sandbox publishes no ports"
    assert not any("docker.sock" in bind for bind in config["Binds"])
    assert await _docker_inspect(harness, name, "{{.Config.User}}") == "1000:1000"

    # The secret reached the code, but is in no command line and in no file left on the server.
    command_line = await _docker_inspect(harness, name, "{{json .Config.Cmd}} {{json .Config.Entrypoint}} {{json .Path}} {{json .Args}}")
    assert "s3cr3t-live" not in command_line
    leftover = await harness.driver.host.ssh.run(["ls", "-A", layout.ENV_DIR])
    assert leftover.stdout.strip() == "", "the env file is removed once the container has started"

    async with harness.runtime(handle, token) as runtime:
        whoami = await runtime.run(["sh", "-c", 'id -u; echo "$API_KEY $VISIBLE"; touch /usr/x 2>&1 | head -1; echo home=$HOME'])
        lines = whoami.stdout.decode().splitlines()
        assert lines[0] == "1000" and lines[1] == "s3cr3t-live yes" and lines[-1] == "home=/workspace/.home"
        assert any("denied" in line.lower() or "read-only" in line.lower() for line in lines), "the root filesystem cannot be written"
        # Disk: a 64 MB workspace refuses a 200 MB file.
        disk = await runtime.run(["sh", "-c", "dd if=/dev/zero of=/workspace/fill bs=1M count=200 2>&1; "
                                                 "stat -c %s /workspace/fill; rm -f /workspace/fill"])
        assert b"No space left" in disk.stdout
        assert int(disk.stdout.split()[-1]) < 64 * 1024 * 1024
        await runtime.write_file("kept.txt", b"survives")

    # Memory: an unbounded allocation never gets past the limit. Under the
    # standard runtime the kernel kills the process (exit 137) and the sandbox
    # carries on. Under gVisor the whole sandbox is killed: it is then stopped,
    # not lost, and resumes with its files.
    from app.services.sandbox.runtime_client import SandboxRuntimeError
    async with harness.runtime(handle, token) as runtime:
        try:
            memory = await runtime.run(["python", "-c", "x=[]\nwhile True: x.append(bytearray(16*1024*1024))"], timeout=180)
            assert memory.exit_code == 137
        except SandboxRuntimeError as died:
            assert died.code == "unreachable"
            assert (await harness.driver.status(handle)).state == "stopped"
            await harness.driver.start(handle)
    async with harness.runtime(handle, token) as runtime:
        await runtime.wait_healthy(30)
        assert await runtime.read_file("kept.txt") == b"survives"
        assert (await runtime.run(["echo", "still here"])).stdout == b"still here\n"


async def test_each_container_names_its_runtime(harness):
    handle, token = await harness.create()
    strong = CAP_STRONG_ISOLATION in harness.driver.capabilities()
    runtime_name = await _docker_inspect(harness, layout.sandbox_container(handle.sandbox_id), "{{.HostConfig.Runtime}}")
    assert runtime_name == ("runsc" if strong else "runc")
    async with harness.runtime(handle, token) as runtime:
        kernel = (await runtime.run(["uname", "-r"])).stdout.decode().strip()
    assert kernel.endswith("-gvisor") is strong
    # The support containers never run under gVisor: the firewall helper needs the host's packet filter.
    for support in (layout.CADDY_CONTAINER, layout.EGRESS_CONTAINER, layout.FIREWALL_CONTAINER):
        assert await _docker_inspect(harness, support, "{{.HostConfig.Runtime}}") == "runc", support


async def test_a_server_without_gvisor_runs_standard_containers_and_says_so(harness):
    """The same server, seen as one where gVisor was not detected."""
    state = dict(harness.driver.ctx.driver_state)
    state.update(isolation="runc", isolation_detail="gVisor is not installed.")
    plain = registry.create_driver("docker-ssh", context(state=state))
    try:
        assert CAP_STRONG_ISOLATION not in plain.capabilities()
        assert plain.public_info(state)["isolation"] == "runc"
        with pytest.raises(SandboxDriverError) as refused:
            await plain.create(harness.spec(require_strong_isolation=True)[0])
        assert refused.value.code == "unsupported"

        spec, token = harness.spec()
        handle = await plain.create(spec)
        harness.created.append(handle)
        assert await _docker_inspect(harness, layout.sandbox_container(handle.sandbox_id), "{{.HostConfig.Runtime}}") == "runc"
        async with harness.runtime(handle, token, driver=plain) as runtime:
            await runtime.wait_healthy(30)
            kernel = (await runtime.run(["uname", "-r"])).stdout.decode().strip()
            # An out-of-memory process is killed on its own; the sandbox carries on.
            memory = await runtime.run(["python", "-c", "x=[]\nwhile True: x.append(bytearray(16*1024*1024))"], timeout=180)
            assert memory.exit_code == 137
            assert (await runtime.run(["echo", "still here"])).stdout == b"still here\n"
        assert not kernel.endswith("-gvisor")
    finally:
        await plain.close()


async def test_bootstrap_can_be_run_again_without_disturbing_running_sandboxes(harness):
    handle, token = await harness.create()
    async with harness.runtime(handle, token) as runtime:
        await runtime.write_file("kept.txt", b"before")
    ca_before = harness.driver.ctx.driver_state["caddy_ca_cert"]
    started_before = await _docker_inspect(harness, layout.CADDY_CONTAINER, "{{.State.StartedAt}}")

    steps = await harness.driver.prepare(images=[IMAGE])
    assert [s.status for s in steps] == ["ok"] * len(steps), [(s.key, s.detail) for s in steps if s.status != "ok"]
    assert "already running" in next(s.detail for s in steps if s.key == "router")
    assert harness.driver.ctx.driver_state["caddy_ca_cert"] == ca_before, "the pinned CA does not change"
    assert await _docker_inspect(harness, layout.CADDY_CONTAINER, "{{.State.StartedAt}}") == started_before
    async with harness.runtime(handle, token) as runtime:
        assert await runtime.read_file("kept.txt") == b"before"


async def test_a_lost_route_is_repaired_by_reconcile(harness):
    handle, token = await harness.create()
    await harness.driver.host.drop_route(layout.CADDY_DIR, f"sbx-{handle.sandbox_id}")
    async with harness.runtime(handle, token) as runtime:
        assert not await runtime.health(timeout=3), "without its route the sandbox is unreachable"
    result = await harness.driver.reconcile([handle])
    assert f"route:sbx-{handle.sandbox_id}" in result.repaired
    async with harness.runtime(handle, token) as runtime:
        assert await runtime.health()


async def test_a_failed_step_is_recorded_and_leaves_the_server_unprepared(harness, monkeypatch):
    """Air-gapped mode with an image that is not on the server."""
    monkeypatch.setenv("BOW_SANDBOX_TOOLS_IMAGE", f"bagofwords/not-loaded-{uuid.uuid4().hex[:6]}:1")
    state = dict(harness.driver.ctx.driver_state)
    driver = registry.create_driver("docker-ssh", context(state=state, images_preloaded=True))
    try:
        steps = await driver.prepare()
    finally:
        await driver.close()
    by_key = {s.key: s for s in steps}
    assert by_key["sandboxd"].status == "failed" and "not on the server" in by_key["sandboxd"].detail
    assert [s.status for s in steps[:3]] == ["ok", "ok", "ok"]
    assert all(s.status == "skipped" for s in steps[4:])
    assert state["prepared"] is False


async def test_a_server_belongs_to_one_bow_instance(harness):
    kept, _ = await harness.create()
    other_state = {"ssh_host_key": harness.driver.ctx.driver_state["ssh_host_key"]}
    other = registry.create_driver("docker-ssh", context(instance_id=f"other-{uuid.uuid4().hex[:6]}", state=other_state))
    try:
        refused = await other.prepare()
        assert refused[0].status == "failed" and "another BoW instance" in refused[0].detail
        assert "Take over" in refused[0].detail and not other_state.get("prepared")
        with pytest.raises(SandboxDriverError):
            await other.create(harness.spec()[0])
        # The first instance's sandbox is untouched by the refused attempt.
        assert (await harness.driver.status(kept)).state == "running"

        taken = await other.prepare(take_over=True)
        assert [s.status for s in taken] == ["ok"] * len(taken), [(s.key, s.detail) for s in taken if s.status != "ok"]
        assert "Taken over" in taken[0].detail
        assert (await harness.driver.status(kept)).state == "missing", "take over removes the other instance's sandboxes"
        owner = json.loads(await other.host.ssh.read_file(layout.OWNER_FILE))
        assert owner["instance_id"] == other.ctx.instance_id
        # The instance that lost the server finds out from Test connection.
        health = await harness.driver.check()
        assert not health.healthy and any("another BoW instance" in (c["detail"] or "") for c in health.checks)

        await other.cleanup()
        leftovers = await other.host.ssh.run(
            ["sh", "-c", f"docker ps -aq --filter label={layout.LABEL_INSTANCE} | wc -l; docker network ls -q --filter name={layout.NETWORK} | wc -l; "
                         f"ls {layout.ROOT} 2>/dev/null | wc -l; iptables -S 2>/dev/null | grep -c BOW-SBX"], check=False)
        assert leftovers.stdout.split() == ["0", "0", "0", "0"], "cleanup leaves nothing of BoW on the server"
    finally:
        await other.close()
        harness.created.clear()
        harness.driver.ctx.driver_state.clear()  # the next live run bootstraps from scratch
