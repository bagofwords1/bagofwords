"""Command templates and the configuration documents they send. Pure functions.

Every command the driver runs on a server is built here as an argument list;
the SSH layer quotes it. Nothing in this module talks to a server, so the
exact commands, routes, proxy rules and firewall settings for a given spec
can be checked in unit tests.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from app.services.sandbox.drivers.base import (
    POLICY_ALLOWLIST, POLICY_FULL, POLICY_NONE, POLICY_REGISTRIES, EgressRecord, SandboxDriverError, SandboxSpec,
)
from app.services.sandbox.drivers.docker_ssh import layout
from app.services.sandbox.drivers.docker_ssh.layout import Addresses

_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,499}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Destinations the egress proxy never connects to, whatever the policy: a
# domain that resolves into one of these is refused as well.
PROXY_DENIED_RANGES = [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
    "127.0.0.0/8", "0.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10",
]


def labels(instance_id: str, **extra: str) -> List[str]:
    out = ["--label", f"{layout.LABEL_INSTANCE}={instance_id}"]
    for key, value in extra.items():
        out += ["--label", f"bow.{key}={value}"]
    return out


# ── network ──────────────────────────────────────────────────────────────


def network_create(addr: Addresses, instance_id: str) -> List[str]:
    """The private sandbox network. Inter-container traffic is switched off in
    Docker itself: that loads br_netfilter and drops sandbox-to-sandbox
    traffic even when BoW's firewall helper is not running. IPv6 stays off."""
    return [
        "docker", "network", "create", "--driver", "bridge",
        "--subnet", addr.subnet, "--gateway", addr.gateway,
        "-o", f"com.docker.network.bridge.name={layout.BRIDGE}",
        "-o", "com.docker.network.bridge.enable_icc=false",
        *labels(instance_id), layout.NETWORK,
    ]


# ── sandbox container ────────────────────────────────────────────────────


def sandbox_environment(spec: SandboxSpec, addr: Addresses) -> Dict[str, str]:
    """Everything injected as environment variables: the environment's own
    variables and secrets, plus the proxy settings its network policy needs."""
    env: Dict[str, str] = {}
    env.update({str(k): str(v) for k, v in spec.env.items()})
    env.update({str(k): str(v) for k, v in spec.secrets.items()})
    env["HOME"] = "/workspace/.home"
    env["SANDBOX_ID"] = spec.sandbox_id
    if spec.network_policy != POLICY_NONE:
        proxy = f"http://{addr.egress}:{layout.EGRESS_PORT}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[name] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
    return env


def env_file(env: Mapping[str, str]) -> str:
    """Docker ``--env-file`` content. Values are taken literally by Docker, so
    the only thing that cannot be represented is a line break."""
    lines = []
    for name, value in env.items():
        if "\n" in value or "\r" in value or "\n" in name or "=" in name:
            raise SandboxDriverError(
                f"The variable '{name}' contains a line break, which a sandbox cannot receive.", code="invalid_config",
            )
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def sandbox_run(spec: SandboxSpec, *, ip: str, runtime: str, instance_id: str, env_file_path: str) -> List[str]:
    """``docker run`` for one sandbox. The runtime is always named, so nothing
    depends on Docker's default runtime."""
    name = layout.sandbox_container(spec.sandbox_id)
    for label, value in (("image", spec.image), ("sandbox id", spec.sandbox_id)):
        # Quoting keeps a value one argument; this keeps it from being read as an option.
        if not _REFERENCE.match(str(value)):
            raise SandboxDriverError(f"The {label} is not valid: it must start with a letter or digit.", code="invalid_config")
    if not _HEX64.match(spec.token_sha256):
        raise SandboxDriverError("The sandbox token hash is malformed", code="invalid_config")
    resolv = layout.RESOLV_FULL if spec.network_policy == POLICY_FULL else layout.RESOLV_NONE
    memory = f"{int(spec.memory_mb)}m"
    return [
        "docker", "run", "-d", "--name", name, "--hostname", "sandbox",
        *labels(instance_id, sandbox=spec.sandbox_id),
        "--network", layout.NETWORK, "--ip", ip,
        "--runtime", runtime,
        "--user", f"{layout.RUN_UID}:{layout.RUN_UID}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", str(int(spec.pids_limit)),
        "--cpus", f"{float(spec.cpus):g}",
        "--memory", memory, "--memory-swap", memory,
        "--read-only", "--tmpfs", f"/tmp:rw,exec,size={layout.TMPFS_MB}m,mode=1777",
        "--env-file", env_file_path,
        "-v", f"{layout.workspace_mount(spec.sandbox_id)}:/workspace",
        "-v", f"{layout.SANDBOXD_PATH}:/usr/local/bin/sandboxd:ro",
        "-v", f"{resolv}:/etc/resolv.conf:ro",
        "-w", "/workspace",
        "--entrypoint", "/usr/local/bin/sandboxd",
        spec.image,
        "--grpc-port", str(layout.SANDBOXD_GRPC_PORT), "--rest-port", str(layout.SANDBOXD_REST_PORT),
        "--root-dir", "/workspace", "--auth-token-sha256", spec.token_sha256,
    ]


def workspace_create(sandbox_id: str, disk_mb: int) -> List[List[str]]:
    """A size-limited workspace: a sparse ext4 image, owned by the run user,
    loop mounted on the host. This is the sandbox's disk limit."""
    image, mount = layout.workspace_image(sandbox_id), layout.workspace_mount(sandbox_id)
    return [
        ["mkdir", "-p", mount],
        ["truncate", "-s", f"{int(disk_mb)}M", image],
        ["mkfs.ext4", "-q", "-F", "-m", "0", "-E", f"root_owner={layout.RUN_UID}:{layout.RUN_UID}", image],
        workspace_mount(sandbox_id),
    ]


def workspace_mount(sandbox_id: str) -> List[str]:
    return ["mount", "-o", "loop,nosuid,nodev", layout.workspace_image(sandbox_id), layout.workspace_mount(sandbox_id)]


def resolv_conf(policy_full: bool) -> str:
    if policy_full:
        return "".join(f"nameserver {r}\n" for r in layout.PUBLIC_RESOLVERS) + "options timeout:2 attempts:2\n"
    # No name server of its own: names are resolved by the egress proxy, so DNS
    # cannot be used to send data out. The loopback entry makes lookups fail fast.
    return "# This sandbox has no DNS. Outbound HTTP(S) goes through the egress proxy.\nnameserver 127.0.0.1\noptions timeout:1 attempts:1\n"


# ── support containers ───────────────────────────────────────────────────


def caddy_base_config(address: str) -> Dict[str, Any]:
    """The router: one HTTPS server, a certificate from Caddy's own CA for the
    host's address, and a 404 for any request that names no known sandbox."""
    return {
        "admin": {"listen": "unix//run/caddy-admin/admin.sock"},
        "apps": {
            "http": {"servers": {"sbx": {
                "listen": [f":{layout.CADDY_INTERNAL_PORT}"],
                "routes": [{"@id": "fallback", "handle": [{"handler": "static_response", "status_code": 404}]}],
                "tls_connection_policies": [{"default_sni": address}],
                "automatic_https": {"disable_redirects": True},
            }}},
            "tls": {
                "certificates": {"automate": [address]},
                "automation": {"policies": [{"subjects": [address], "issuers": [{"module": "internal"}]}]},
            },
        },
    }


def egress_base_config() -> Dict[str, Any]:
    """The egress proxy: refuses everything until a sandbox gets a route."""
    return {
        "admin": {"listen": "unix//run/caddy-admin/admin.sock"},
        "logging": {"logs": {
            "default": {"exclude": ["http.log.access.egress"]},
            "egress": {"include": ["http.log.access.egress"], "encoder": {"format": "json"}},
        }},
        "apps": {"http": {"servers": {"egress": {
            "listen": [f":{layout.EGRESS_PORT}"],
            "automatic_https": {"disable": True},
            "logs": {"default_logger_name": "egress"},
            "routes": [{"@id": "deny", "handle": [{
                "handler": "static_response", "status_code": 403,
                "body": "This sandbox has no internet access.\n",
            }]}],
        }}}},
    }


def _support_run(name: str, role: str, image: str, ip: str, instance_id: str, host_dir: str,
                 extra: Sequence[str] = ()) -> List[str]:
    return [
        "docker", "run", "-d", "--name", name, *labels(instance_id, role=role),
        "--network", layout.NETWORK, "--ip", ip, "--runtime", layout.RUNTIME_STANDARD,
        "--restart", "unless-stopped",
        "--cap-drop", "ALL", "--cap-add", "NET_BIND_SERVICE", "--security-opt", "no-new-privileges",
        *extra,
        "-v", f"{host_dir}/admin:/run/caddy-admin",
        "-v", f"{host_dir}/data:/data", "-v", f"{host_dir}/config:/config",
        "-v", f"{host_dir}/etc:/etc/caddy:ro",
        image, "caddy", "run", "--resume", "--config", "/etc/caddy/base.json",
    ]


def caddy_run(image: str, addr: Addresses, instance_id: str, sandbox_port: int) -> List[str]:
    return _support_run(layout.CADDY_CONTAINER, "router", image, addr.caddy, instance_id, layout.CADDY_DIR,
                        extra=["-p", f"{int(sandbox_port)}:{layout.CADDY_INTERNAL_PORT}"])


def egress_run(image: str, addr: Addresses, instance_id: str) -> List[str]:
    return _support_run(layout.EGRESS_CONTAINER, "egress", image, addr.egress, instance_id, layout.EGRESS_DIR)


def firewall_run(image: str, instance_id: str) -> List[str]:
    """The firewall helper needs the host's packet filter, so it always runs
    under the standard runtime on the host network. ``--restart always``: a
    container signalled with ``docker kill`` is treated as manually stopped
    and would not come back after a reboot, so it is reloaded with ``docker exec``."""
    return [
        "docker", "run", "-d", "--name", layout.FIREWALL_CONTAINER, *labels(instance_id, role="firewall"),
        "--network", "host", "--runtime", layout.RUNTIME_STANDARD, "--restart", "always",
        "--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW",
        "-v", f"{layout.FIREWALL_DIR}:/etc/bow-sbx:ro", image,
    ]


def firewall_env(addr: Addresses, *, bow_address: Optional[str], full_ips: Iterable[str],
                 sandbox_ports: Iterable[int] = ()) -> str:
    ports = [layout.SANDBOXD_REST_PORT, layout.SANDBOXD_GRPC_PORT, *[int(p) for p in sandbox_ports]]
    return "".join([
        f"BRIDGE={layout.BRIDGE}\n",
        f"CADDY_IP={addr.caddy}\n",
        f"EGRESS_IP={addr.egress}\n",
        f"CADDY_PORT={layout.CADDY_INTERNAL_PORT}\n",
        f"SANDBOX_PORTS={','.join(str(p) for p in sorted(set(ports)))}\n",
        f'BOW_ADDRESS="{bow_address or ""}"\n',
        f'FULL_IPS="{" ".join(sorted(set(full_ips)))}"\n',
    ])


# ── admin API calls (run on the host through its local socket) ───────────


def admin_call(host_dir: str, method: str, path: str) -> List[str]:
    """``curl`` to a Caddy admin API on its local socket. The body, if any, is on standard input."""
    argv = ["curl", "-sS", "--max-time", "20", "-o", "/dev/stdout", "-w", "\n%{http_code}",
            "--unix-socket", f"{host_dir}/admin/admin.sock", "-X", method]
    if method in ("PUT", "POST", "PATCH"):
        argv += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    return argv + [f"http://caddy{path}"]


def router_route_id(sandbox_id: str) -> str:
    return f"sbx-{sandbox_id}"


def egress_route_id(sandbox_id: str) -> str:
    return f"eg-{sandbox_id}"


def router_route(sandbox_id: str, ip: str, ports: Sequence[int] = ()) -> Dict[str, Any]:
    """One route per sandbox, matched on ``X-Sandbox-Id``: gRPC to sandboxd's
    process API, a declared app port when ``X-Sandbox-Port`` names one, and
    everything else to sandboxd's file API. Streaming is never buffered."""
    def proxy(port: int, grpc: bool = False) -> Dict[str, Any]:
        handler: Dict[str, Any] = {"handler": "reverse_proxy", "flush_interval": -1, "upstreams": [{"dial": f"{ip}:{port}"}]}
        if grpc:
            handler["transport"] = {"protocol": "http", "versions": ["h2c", "2"]}
        return handler

    branches: List[Dict[str, Any]] = [{
        "match": [{"header": {"Content-Type": ["application/grpc*"]}}],
        "handle": [proxy(layout.SANDBOXD_GRPC_PORT, grpc=True)],
    }]
    for port in ports:  # reserved for hosted apps; empty in v1
        branches.append({"match": [{"header": {"X-Sandbox-Port": [str(int(port))]}}], "handle": [proxy(int(port))]})
    branches.append({"handle": [proxy(layout.SANDBOXD_REST_PORT)]})
    return {
        "@id": router_route_id(sandbox_id),
        "match": [{"header": {"X-Sandbox-Id": [sandbox_id]}}],
        "handle": [{"handler": "subroute", "routes": branches}],
    }


def egress_route(sandbox_id: str, ip: str, policy: str, domains: Sequence[str]) -> Optional[Dict[str, Any]]:
    """What a sandbox may reach, keyed by its address on the sandbox network.

    ``none``: no route, so the proxy's default (403) applies. ``registries``
    and ``allowlist``: the listed domains only. ``full``: everything public.
    Private, link-local and loopback destinations are refused under every policy.
    """
    if policy == POLICY_NONE:
        return None
    acl: List[Dict[str, Any]] = [{"subjects": list(PROXY_DENIED_RANGES)}]
    if policy == POLICY_FULL:
        acl.append({"subjects": ["all"], "allow": True})
    elif policy in (POLICY_REGISTRIES, POLICY_ALLOWLIST):
        subjects = [d for d in dict.fromkeys(str(x).strip().lower() for x in domains) if d]
        if subjects:
            acl.append({"subjects": subjects, "allow": True})
        acl.append({"subjects": ["all"]})
    else:
        raise SandboxDriverError(f"Unknown network policy: {policy}", code="invalid_config")
    return {
        "@id": egress_route_id(sandbox_id),
        "match": [{"remote_ip": {"ranges": [ip]}}],
        "handle": [{"handler": "forward_proxy", "hide_ip": True, "hide_via": True, "acl": acl}],
    }


# ── reading what the host reports ────────────────────────────────────────


def split_admin_response(output: str) -> tuple:
    """(status code, body) from an ``admin_call``."""
    body, _, status = output.rpartition("\n")
    try:
        return int(status.strip()), body
    except ValueError:
        return 0, output


def parse_egress_log(lines: Iterable[str], ip: str, since: Optional[datetime] = None) -> List[EgressRecord]:
    """Aggregate the proxy's access log for one sandbox address into domains reached and refused."""
    records: Dict[tuple, EgressRecord] = {}
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("logger") != "http.log.access.egress":
            continue
        request = entry.get("request") or {}
        if request.get("remote_ip") != ip:
            continue
        at = datetime.utcfromtimestamp(float(entry.get("ts") or 0))
        if since is not None and at < since:
            continue
        host = str(request.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if not host:
            continue
        decision = "allowed" if int(entry.get("status") or 0) < 400 else "refused"
        key = (host, decision)
        if key in records:
            records[key].count += 1
            records[key].last_at = max(records[key].last_at, at)
            records[key].first_at = min(records[key].first_at, at)
        else:
            records[key] = EgressRecord(host, decision, 1, at, at)
    return list(records.values())


def describe_docker(info: Mapping[str, Any], version_text: str) -> Dict[str, Any]:
    """What ``docker info`` says about whether this setup is supported, and why not."""
    problems: List[str] = []
    if "podman" in (version_text or "").lower():
        problems.append("This server runs Podman, not Docker. Only Docker is supported.")
    security = [str(option) for option in (info.get("SecurityOptions") or [])]
    if any("rootless" in option for option in security):
        problems.append("Docker runs in rootless mode on this server, which is not supported.")
    firewall = ((info.get("FirewallCompute") or {}).get("Driver") or "").lower()
    if firewall and firewall != "iptables":
        problems.append(
            f"Docker uses the '{firewall}' firewall compute on this server. Only Docker's default iptables "
            "compute is supported."
        )
    runtimes = sorted((info.get("Runtimes") or {}).keys())
    return {
        "docker_version": info.get("ServerVersion"),
        "storage_driver": info.get("Driver"),
        "firewall_compute": firewall or None,
        "cgroup_version": info.get("CgroupVersion"),
        "runtimes": runtimes,
        "strong_runtime_registered": layout.RUNTIME_STRONG in runtimes,
        "cpus": info.get("NCPU"),
        "memory_bytes": info.get("MemTotal"),
        "problems": problems,
    }
