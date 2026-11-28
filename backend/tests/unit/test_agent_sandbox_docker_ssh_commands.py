"""The docker-ssh driver's command templates and configuration documents.

These are pure functions, so what the driver would run on a server for a
given sandbox spec is checked here without a server: the container hardening,
that secrets never reach a command line, the proxy rules and firewall
settings for each network policy, and that arguments survive quoting.
"""
import json
import shlex
from datetime import datetime

import pytest

from app.services.sandbox.drivers.base import SandboxDriverError, SandboxSpec
from app.services.sandbox.drivers.docker_ssh import commands, layout
from app.services.sandbox.drivers.docker_ssh.driver import DockerSSHDriver
from app.services.sandbox.drivers.docker_ssh.schemas import DockerSSHConfig
from app.services.sandbox.drivers.docker_ssh.ssh import MODE_ROOT, MODE_SUDO, Result, SSHError, SSHHost, generate_keypair

ADDR = layout.addresses()
TOKEN_HASH = "ab" * 32


def _spec(**overrides):
    values = dict(sandbox_id="1f0c9d2e-aaaa-bbbb-cccc-000000000001", image="python:3.12-slim", token_sha256=TOKEN_HASH)
    values.update(overrides)
    return SandboxSpec(**values)


def _run(spec, runtime="runsc"):
    return commands.sandbox_run(spec, ip="10.213.77.10", runtime=runtime, instance_id="inst-1", env_file_path="/x/e.env")


def _flag(argv, name):
    return [argv[i + 1] for i, part in enumerate(argv) if part == name]


# ── the sandbox container ────────────────────────────────────────────────


def test_every_sandbox_is_hardened_the_same_way():
    argv = _run(_spec(cpus=2, memory_mb=1024, pids_limit=256))
    assert _flag(argv, "--cap-drop") == ["ALL"]
    assert _flag(argv, "--security-opt") == ["no-new-privileges"]
    assert _flag(argv, "--user") == ["1000:1000"]
    assert "--read-only" in argv and "--privileged" not in argv
    assert _flag(argv, "--pids-limit") == ["256"] and _flag(argv, "--cpus") == ["2"]
    # Memory and memory+swap are equal: no swap to escape the limit into.
    assert _flag(argv, "--memory") == ["1024m"] == _flag(argv, "--memory-swap")
    assert _flag(argv, "--network") == [layout.NETWORK] and "-p" not in argv, "sandboxes publish no ports"
    mounts = _flag(argv, "-v")
    assert f"{layout.SANDBOXD_PATH}:/usr/local/bin/sandboxd:ro" in mounts
    assert not any("docker.sock" in m for m in mounts)
    host_paths = {m.split(":")[0] for m in mounts}
    assert all(p.startswith(layout.ROOT) for p in host_paths), "nothing of the host outside BoW's directory is mounted"
    assert _flag(argv, "--entrypoint") == ["/usr/local/bin/sandboxd"]


@pytest.mark.parametrize("runtime", ["runsc", "runc"])
def test_the_runtime_is_always_named(runtime):
    assert _flag(_run(_spec(), runtime=runtime), "--runtime") == [runtime]


def test_support_containers_always_use_the_standard_runtime():
    for argv in (commands.caddy_run("img", ADDR, "i", 8443), commands.egress_run("img", ADDR, "i"),
                 commands.firewall_run("img", "i")):
        assert _flag(argv, "--runtime") == ["runc"]
    assert _flag(commands.firewall_run("img", "i"), "--restart") == ["always"]
    assert _flag(commands.caddy_run("img", ADDR, "i", 9443), "-p") == ["9443:8443"], "one published port, the router's"
    assert "-p" not in commands.egress_run("img", ADDR, "i")


def test_the_container_gets_the_token_hash_never_a_secret_on_its_command_line():
    spec = _spec(env={"REGION": "eu"}, secrets={"API_KEY": "s3cr3t-value"})
    argv = _run(spec)
    assert argv[argv.index("--auth-token-sha256") + 1] == TOKEN_HASH
    assert "s3cr3t-value" not in " ".join(argv) and "API_KEY" not in " ".join(argv)
    # Secrets travel in the env file, which is written over stdin and removed after start.
    content = commands.env_file(commands.sandbox_environment(spec, ADDR))
    assert "API_KEY=s3cr3t-value\n" in content and "REGION=eu\n" in content


def test_values_with_line_breaks_are_refused_rather_than_mangled():
    with pytest.raises(SandboxDriverError) as refused:
        commands.env_file({"CERT": "line1\nline2"})
    assert "CERT" in refused.value.message and "line1" not in refused.value.message


@pytest.mark.parametrize("hostile", [
    "image; rm -rf /", "$(reboot)", "a b`id`", "img' --privileged '", "x\ny", "--privileged", "-v/:/host",
])
def test_an_image_name_that_could_be_read_as_anything_else_is_refused(hostile):
    with pytest.raises(SandboxDriverError):
        _run(_spec(image=hostile))
    with pytest.raises(Exception):
        from app.schemas.sandbox_schema import SandboxEnvironmentCreate
        SandboxEnvironmentCreate(name="e", image=hostile)


@pytest.mark.parametrize("image", [
    "python:3.12-slim", "registry.example.com:5000/team/img:1.2", "ghcr.io/org/img@sha256:" + "0" * 64,
])
def test_arguments_survive_quoting_as_single_arguments(image):
    argv = _run(_spec(image=image, env={"A": "b c; d"}, secrets={"S": "$(x) `y` 'z'"}))
    assert shlex.split(shlex.join(argv)) == argv
    assert argv.count(image) == 1


def test_a_malformed_token_hash_never_reaches_a_command_line():
    with pytest.raises(SandboxDriverError):
        _run(_spec(token_sha256="abc; id"))


def test_the_ssh_layer_adds_sudo_in_exactly_one_place(monkeypatch):
    sent = []

    async def fake_exec(self, command, *, input, timeout):
        sent.append((command, input))
        return Result(0, "", "")

    monkeypatch.setattr(SSHHost, "_exec", fake_exec)
    import asyncio

    for mode, prefix in ((MODE_SUDO, "sudo -n -- "), (MODE_ROOT, "")):
        host = SSHHost("h", 22, "u", None, pinned_host_key="k")
        host.mode = mode
        asyncio.run(host.run(["docker", "run", "img; id"]))
        asyncio.run(host.write_file("/var/lib/x", "secret-content"))
        asyncio.run(host.run(["cat", "f"], as_user=True))
    sudo_run, sudo_write, sudo_user, root_run, root_write, _ = sent
    assert sudo_run[0] == "sudo -n -- docker run 'img; id'" and root_run[0] == "docker run 'img; id'"
    assert sudo_write[0].startswith("sudo -n -- install ") and sudo_write[1] == "secret-content"
    assert "secret-content" not in sudo_write[0], "file content travels on stdin"
    assert sudo_user[0] == "cat f"
    assert root_write[0].startswith("install ")


def test_a_user_without_root_or_passwordless_sudo_is_refused(monkeypatch):
    async def fake_exec(self, command, *, input, timeout):
        return Result(0, "1000\n", "") if command == "id -u" else Result(1, "", "sudo: a password is required")

    monkeypatch.setattr(SSHHost, "_exec", fake_exec)
    import asyncio

    with pytest.raises(SSHError) as refused:
        asyncio.run(SSHHost("h", 22, "deploy", None, pinned_host_key="k").detect_mode())
    assert refused.value.code == "no_privileges" and "docker group" in refused.value.message


# ── DNS and proxy settings per policy ────────────────────────────────────


@pytest.mark.parametrize("policy, resolv, proxied", [
    ("none", layout.RESOLV_NONE, False),
    ("registries", layout.RESOLV_NONE, True),
    ("allowlist", layout.RESOLV_NONE, True),
    ("full", layout.RESOLV_FULL, True),
])
def test_dns_and_proxy_settings_follow_the_network_policy(policy, resolv, proxied):
    spec = _spec(network_policy=policy)
    assert f"{resolv}:/etc/resolv.conf:ro" in _flag(_run(spec), "-v")
    env = commands.sandbox_environment(spec, ADDR)
    assert ("HTTPS_PROXY" in env) is proxied
    if proxied:
        assert env["HTTPS_PROXY"] == env["https_proxy"] == f"http://{ADDR.egress}:3128"
    assert env["HOME"] == "/workspace/.home", "what a run installs lands in the size-limited workspace"


def test_only_the_full_policy_gets_real_name_servers():
    assert "nameserver 1.1.1.1" in commands.resolv_conf(True)
    restricted = [l for l in commands.resolv_conf(False).splitlines() if l.startswith("nameserver")]
    assert restricted == ["nameserver 127.0.0.1"], "no resolver of its own: the proxy resolves names"


def test_policy_none_has_no_proxy_route_at_all():
    assert commands.egress_route("s", "10.213.77.10", "none", ["pypi.org"]) is None


@pytest.mark.parametrize("policy", ["registries", "allowlist"])
def test_listed_policies_allow_their_domains_and_nothing_else(policy):
    route = commands.egress_route("s", "10.213.77.10", policy, ["PyPI.org", "files.pythonhosted.org", "pypi.org", ""])
    assert route["match"] == [{"remote_ip": {"ranges": ["10.213.77.10"]}}], "the rule is keyed by the sandbox's address"
    acl = route["handle"][0]["acl"]
    assert acl[0] == {"subjects": commands.PROXY_DENIED_RANGES} and "allow" not in acl[0]
    assert acl[1] == {"subjects": ["pypi.org", "files.pythonhosted.org"], "allow": True}
    assert acl[-1] == {"subjects": ["all"]}, "everything not listed is refused"


def test_a_listed_policy_with_no_domains_allows_nothing():
    acl = commands.egress_route("s", "10.213.77.10", "allowlist", [])["handle"][0]["acl"]
    assert not any(rule.get("allow") for rule in acl)


def test_the_full_policy_allows_the_internet_but_never_private_destinations():
    acl = commands.egress_route("s", "10.213.77.10", "full", [])["handle"][0]["acl"]
    assert acl == [{"subjects": commands.PROXY_DENIED_RANGES}, {"subjects": ["all"], "allow": True}]
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8"):
        assert network in commands.PROXY_DENIED_RANGES


def test_an_unknown_policy_is_an_error_not_open_access():
    with pytest.raises(SandboxDriverError):
        commands.egress_route("s", "10.213.77.10", "anything-goes", [])


# ── router and firewall ──────────────────────────────────────────────────


def test_the_router_route_sends_grpc_and_files_to_the_right_ports():
    route = commands.router_route("abc", "10.213.77.10")
    assert route["@id"] == "sbx-abc" and route["match"] == [{"header": {"X-Sandbox-Id": ["abc"]}}]
    grpc, rest = route["handle"][0]["routes"]
    assert grpc["match"] == [{"header": {"Content-Type": ["application/grpc*"]}}]
    assert grpc["handle"][0]["upstreams"] == [{"dial": "10.213.77.10:9090"}]
    assert grpc["handle"][0]["transport"]["versions"] == ["h2c", "2"]
    assert "match" not in rest and rest["handle"][0]["upstreams"] == [{"dial": "10.213.77.10:8080"}]
    assert all(branch["handle"][0]["flush_interval"] == -1 for branch in (grpc, rest)), "streams are never buffered"


def test_app_ports_are_routed_only_when_the_environment_declares_them():
    assert len(commands.router_route("abc", "10.213.77.10")["handle"][0]["routes"]) == 2
    with_ports = commands.router_route("abc", "10.213.77.10", [8501])["handle"][0]["routes"]
    assert with_ports[1]["match"] == [{"header": {"X-Sandbox-Port": ["8501"]}}]
    assert with_ports[1]["handle"][0]["upstreams"] == [{"dial": "10.213.77.10:8501"}]


def test_the_router_answers_404_for_unknown_sandboxes_and_serves_the_hosts_address():
    base = commands.caddy_base_config("203.0.113.7")
    server = base["apps"]["http"]["servers"]["sbx"]
    assert server["routes"] == [{"@id": "fallback", "handle": [{"handler": "static_response", "status_code": 404}]}]
    assert base["apps"]["tls"]["automation"]["policies"][0]["issuers"] == [{"module": "internal"}]
    assert base["admin"]["listen"].startswith("unix/"), "the admin API is a local socket, never a port"
    assert commands.egress_base_config()["admin"]["listen"].startswith("unix/")


def test_firewall_settings_list_full_policy_sandboxes_and_the_bow_address():
    text = commands.firewall_env(ADDR, bow_address="198.51.100.4", full_ips=["10.213.77.12", "10.213.77.11", "10.213.77.12"])
    settings = dict(line.split("=", 1) for line in text.strip().splitlines())
    assert settings["FULL_IPS"] == '"10.213.77.11 10.213.77.12"' and settings["BOW_ADDRESS"] == '"198.51.100.4"'
    assert settings["CADDY_IP"] == ADDR.caddy and settings["EGRESS_IP"] == ADDR.egress and settings["BRIDGE"] == layout.BRIDGE
    assert settings["SANDBOX_PORTS"] == "8080,9090"
    assert 'FULL_IPS=""' in commands.firewall_env(ADDR, bow_address=None, full_ips=[])


def test_the_network_is_created_with_sandboxes_unable_to_reach_each_other():
    argv = commands.network_create(ADDR, "inst")
    assert "com.docker.network.bridge.enable_icc=false" in _flag(argv, "-o")
    assert "--ipv6" not in argv


def test_the_workspace_is_a_size_limited_filesystem_owned_by_the_run_user():
    steps = commands.workspace_create("abc", 5120)
    assert ["truncate", "-s", "5120M", f"{layout.WORKSPACES}/abc.img"] in steps
    mkfs = next(s for s in steps if s[0] == "mkfs.ext4")
    assert "root_owner=1000:1000" in mkfs
    assert steps[-1][:3] == ["mount", "-o", "loop,nosuid,nodev"]


# ── reading what the host says ───────────────────────────────────────────


@pytest.mark.parametrize("info, version, expected", [
    ({"ServerVersion": "29.1", "Runtimes": {"runc": {}}}, "podman version 5.2.0", "Podman"),
    ({"ServerVersion": "29.1", "SecurityOptions": ["name=seccomp", "name=rootless"]}, "Docker version 29.1", "rootless"),
    ({"ServerVersion": "29.1", "FirewallCompute": {"Driver": "nftables"}}, "Docker version 29.1", "nftables"),
])
def test_unsupported_setups_are_named(info, version, expected):
    facts = commands.describe_docker(info, version)
    assert len(facts["problems"]) == 1 and expected in facts["problems"][0]


def test_a_supported_setup_has_no_problems_and_reports_the_strong_runtime():
    facts = commands.describe_docker(
        {"ServerVersion": "29.10.1", "Driver": "overlayfs", "FirewallCompute": {"Driver": "iptables"},
         "Runtimes": {"runc": {}, "runsc": {}, "io.containerd.runc.v2": {}}, "SecurityOptions": ["name=apparmor"]},
        "Docker version 29.10.1, build abc",
    )
    assert facts["problems"] == [] and facts["strong_runtime_registered"] is True
    assert commands.describe_docker({"Runtimes": {"runc": {}}}, "Docker version 29")["strong_runtime_registered"] is False


def test_the_domain_log_is_aggregated_per_domain_and_decision_for_one_sandbox():
    def entry(ip, host, status, ts):
        return json.dumps({"logger": "http.log.access.egress", "ts": ts, "status": status,
                           "request": {"remote_ip": ip, "host": host, "method": "CONNECT"}})

    lines = [
        entry("10.213.77.10", "pypi.org:443", 200, 1000.0), entry("10.213.77.10", "pypi.org:443", 200, 1010.0),
        entry("10.213.77.10", "example.com:443", 403, 1005.0), entry("10.213.77.10", "blocked.internal", 502, 1006.0),
        entry("10.213.77.11", "other-sandbox.example:443", 200, 1001.0),
        json.dumps({"logger": "admin.api", "msg": "received request"}), "not json at all", "",
    ]
    records = {(r.domain, r.decision): r for r in commands.parse_egress_log(lines, "10.213.77.10")}
    assert set(records) == {("pypi.org", "allowed"), ("example.com", "refused"), ("blocked.internal", "refused")}
    assert records[("pypi.org", "allowed")].count == 2
    assert records[("pypi.org", "allowed")].first_at < records[("pypi.org", "allowed")].last_at
    recent = commands.parse_egress_log(lines, "10.213.77.10", since=datetime.utcfromtimestamp(1008.0))
    assert [(r.domain, r.count) for r in recent] == [("pypi.org", 1)]


# ── the compute form ─────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    {"address": "host; reboot"}, {"address": "a b"}, {"address": ""},
    {"address": "h", "ssh_user": "root; id"}, {"address": "h", "ssh_port": 0},
    {"address": "h", "bow_address": "not-an-ip"}, {"address": "h", "ssh_host_key_fingerprint": "abc"},
    {"address": "h", "unknown_field": 1},
])
def test_the_compute_form_refuses_malformed_values(bad):
    with pytest.raises(Exception):
        DockerSSHConfig(**bad)


def test_the_compute_form_has_the_designs_defaults():
    config = DockerSSHConfig(address="SBX-1.Example.com")
    assert (config.address, config.ssh_port, config.ssh_user, config.sandbox_port) == ("sbx-1.example.com", 22, "root", 8443)
    assert config.bow_address is None and config.images_preloaded is False
    assert DockerSSHConfig(address="10.0.0.5", bow_address="10.0.0.9").bow_address == "10.0.0.9"


def test_a_key_pair_is_generated_when_none_is_pasted_and_the_private_key_stays_in_credentials():
    config, credentials = DockerSSHDriver.prepare_credentials({"address": "h"}, {})
    assert credentials["ssh_private_key"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert config["ssh_public_key"].startswith("ssh-ed25519 ")
    assert "PRIVATE" not in json.dumps(config)
    # Editing other settings later keeps the same pair.
    again, same = DockerSSHDriver.prepare_credentials(dict(config), dict(credentials))
    assert again["ssh_public_key"] == config["ssh_public_key"] and same == credentials


def test_a_pasted_private_key_yields_its_public_key_and_a_bad_one_is_refused_without_being_echoed():
    private, public = generate_keypair()
    config, _ = DockerSSHDriver.prepare_credentials({"address": "h"}, {"ssh_private_key": private})
    assert config["ssh_public_key"] == public
    with pytest.raises(SandboxDriverError) as refused:
        DockerSSHDriver.prepare_credentials({"address": "h"}, {"ssh_private_key": "not-a-key-hunter2"})
    assert "hunter2" not in refused.value.message


def test_only_safe_facts_about_a_server_are_public():
    _, public = generate_keypair()
    info = DockerSSHDriver.public_info({
        "ssh_host_key": public, "caddy_ca_cert": "-----BEGIN CERTIFICATE-----secret-ish", "isolation": "gvisor",
        "user_mode": "sudo", "prepared": True, "docker": {"docker_version": "29.10.1"},
    })
    assert info["host_key_pinned"] is True and info["host_key_fingerprint"].startswith("SHA256:")
    assert info["isolation"] == "gvisor" and info["docker_version"] == "29.10.1"
    assert "caddy_ca_cert" not in info and "ssh_host_key" not in info
