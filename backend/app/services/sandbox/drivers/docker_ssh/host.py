"""Operations on one sandbox host, built from the command templates.

This is what the driver and the bootstrap steps share: reading what Docker
says, talking to the two Caddy admin APIs on their local sockets, keeping the
per-sandbox records, and applying the firewall settings.

Each sandbox has a small record on the host (``sandboxes/<id>.json``: its
address, network policy and domains). The records are what lets reconcile and
a repeated bootstrap rebuild routes and proxy rules without asking BoW's
database, and they are the registry of addresses in use.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Sequence

from app.services.sandbox.drivers.base import POLICY_FULL, SandboxDriverError
from app.services.sandbox.drivers.docker_ssh import commands, layout
from app.services.sandbox.drivers.docker_ssh.layout import Addresses
from app.services.sandbox.drivers.docker_ssh.ssh import SSHError, SSHHost

RECORDS_DIR = f"{layout.ROOT}/sandboxes"
PULL_TIMEOUT = 1200


class Host:
    def __init__(self, ssh: SSHHost, addr: Addresses, instance_id: str):
        self.ssh = ssh
        self.addr = addr
        self.instance_id = instance_id

    # ── docker ──

    async def docker(self, *args: str, timeout: float = 60, check: bool = True, input: Optional[str] = None):
        return await self.ssh.run(["docker", *args], timeout=timeout, check=check, input=input)

    async def docker_info(self) -> Dict[str, Any]:
        version = await self.ssh.run(["docker", "--version"], check=False)
        if not version.ok:
            raise SandboxDriverError(
                "Docker is not installed on this server, or the SSH user cannot run it: " + version.detail(200),
                code="unsupported",
            )
        info = await self.docker("info", "--format", "{{json .}}", check=False)
        if not info.ok:
            raise SandboxDriverError("The Docker daemon does not answer: " + info.detail(200), code="unsupported")
        try:
            parsed = json.loads(info.stdout)
        except ValueError as exc:
            raise SandboxDriverError("Could not read `docker info` from this server", code="unsupported") from exc
        return commands.describe_docker(parsed, version.stdout)

    async def strong_runtime(self, facts: Dict[str, Any]) -> tuple:
        """(runtime name, isolation, explanation). Detection only: BoW never
        installs the strong runtime and never edits Docker's configuration."""
        if facts.get("strong_runtime_registered"):
            return layout.RUNTIME_STRONG, "gvisor", "gVisor is installed and registered with Docker."
        present = await self.ssh.run(["sh", "-c", f"command -v {layout.RUNTIME_STRONG}"], check=False)
        if present.ok and present.stdout.strip():
            return layout.RUNTIME_STANDARD, "runc", (
                "gVisor is installed but not registered with Docker. Run `sudo runsc install` and "
                "`sudo systemctl reload docker` on the server, then bootstrap again."
            )
        return layout.RUNTIME_STANDARD, "runc", (
            "gVisor is not installed. Sandboxes run as standard containers, which share the server's kernel. "
            "Install gVisor (runsc) and register it with Docker for strong isolation."
        )

    async def container_state(self, name: str) -> Optional[str]:
        """``running`` / ``exited`` / ... or ``None`` when there is no such container."""
        result = await self.docker("inspect", "--format", "{{.State.Status}}", name, check=False)
        return result.stdout.strip() if result.ok else None

    async def containers(self, *, instance_id: Optional[str] = None, all_instances: bool = False) -> List[Dict[str, str]]:
        """Containers BoW created on this host: name, state, and their bow.* labels."""
        label = layout.LABEL_INSTANCE if all_instances else f"{layout.LABEL_INSTANCE}={instance_id or self.instance_id}"
        result = await self.docker(
            "ps", "-a", "--no-trunc", "--filter", f"label={label}",
            "--format", '{{.Names}}\t{{.State}}\t{{.Label "bow.instance"}}\t{{.Label "bow.sandbox"}}\t{{.Label "bow.role"}}',
        )
        rows = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 5:
                rows.append(dict(zip(("name", "state", "instance", "sandbox", "role"), parts)))
        return rows

    async def image_present(self, image: str) -> bool:
        return (await self.docker("image", "inspect", "--format", "{{.Id}}", image, check=False)).ok

    async def pull(self, image: str, *, username: Optional[str] = None, password: Optional[str] = None) -> None:
        if username and password:
            config_dir = (await self.ssh.run(["mktemp", "-d", f"{layout.ROOT}/login.XXXXXX"])).stdout.strip()
            try:
                registry = image.split("/", 1)[0] if ("/" in image and any(c in image.split("/", 1)[0] for c in ".:")) else None
                login = ["docker", "--config", config_dir, "login", "--username", username, "--password-stdin"]
                await self.ssh.run(login + ([registry] if registry else []), input=password, timeout=60)
                await self.ssh.run(["docker", "--config", config_dir, "pull", "-q", image], timeout=PULL_TIMEOUT)
            finally:
                await self.ssh.run(["rm", "-rf", config_dir], check=False)
        else:
            await self.docker("pull", "-q", image, timeout=PULL_TIMEOUT)

    async def image_digest(self, image: str) -> Optional[str]:
        result = await self.docker("image", "inspect", "--format", "{{range .RepoDigests}}{{.}} {{end}}", image, check=False)
        for reference in result.stdout.split():
            if "@" in reference:
                return reference.split("@", 1)[1]
        return None

    # ── the two Caddy admin APIs ──

    async def admin(self, host_dir: str, method: str, path: str, body: Optional[Any] = None) -> tuple:
        payload = json.dumps(body) if body is not None else None
        result = await self.ssh.run(commands.admin_call(host_dir, method, path), input=payload, check=False, timeout=40)
        if not result.ok:
            raise SandboxDriverError(
                "The router's admin API could not be reached on the server: " + result.detail(200), code="unreachable",
            )
        return commands.split_admin_response(result.stdout)

    async def wait_admin(self, host_dir: str, seconds: float = 30) -> None:
        deadline = asyncio.get_event_loop().time() + seconds
        last = ""
        while True:
            result = await self.ssh.run(commands.admin_call(host_dir, "GET", "/config/"), check=False, timeout=20)
            if result.ok and commands.split_admin_response(result.stdout)[0] == 200:
                return
            last = result.detail(200)
            if asyncio.get_event_loop().time() >= deadline:
                raise SandboxDriverError("A support container did not start in time: " + last, code="unreachable")
            await asyncio.sleep(0.5)

    async def route_ids(self, host_dir: str, server: str) -> List[str]:
        status, body = await self.admin(host_dir, "GET", f"/config/apps/http/servers/{server}/routes")
        if status != 200:
            raise SandboxDriverError(f"Could not read routes from the server (HTTP {status})", code="unreachable")
        return [route.get("@id") for route in json.loads(body or "[]") if route.get("@id")]

    async def set_route(self, host_dir: str, server: str, route: Dict[str, Any]) -> None:
        """Add a route ahead of the fallback, replacing one with the same id."""
        await self.admin(host_dir, "DELETE", f"/id/{route['@id']}")
        status, body = await self.admin(host_dir, "PUT", f"/config/apps/http/servers/{server}/routes/0", route)
        if status not in (200, 201):
            raise SandboxDriverError(f"The server refused a route (HTTP {status}): {body[:200]}", code="command_failed")

    async def drop_route(self, host_dir: str, route_id: str) -> None:
        await self.admin(host_dir, "DELETE", f"/id/{route_id}")

    # ── per-sandbox records ──

    async def write_record(self, sandbox_id: str, record: Dict[str, Any]) -> None:
        await self.ssh.write_file(f"{RECORDS_DIR}/{sandbox_id}.json", json.dumps(record) + "\n")

    async def records(self) -> Dict[str, Dict[str, Any]]:
        """Every sandbox record on the host, by sandbox id (all instances)."""
        result = await self.ssh.run(
            ["find", RECORDS_DIR, "-maxdepth", "1", "-name", "*.json", "-exec", "cat", "{}", "+"], check=False,
        )
        out: Dict[str, Dict[str, Any]] = {}
        for line in result.stdout.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("sandbox_id"):
                out[record["sandbox_id"]] = record
        return out

    async def drop_record(self, sandbox_id: str) -> None:
        await self.ssh.run(["rm", "-f", f"{RECORDS_DIR}/{sandbox_id}.json"], check=False)

    # ── firewall ──

    async def apply_firewall(self, *, bow_address: Optional[str], records: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
        """Write the firewall settings from the records and have the helper assert them now."""
        records = records if records is not None else await self.records()
        full = [r["ip"] for r in records.values() if r.get("policy") == POLICY_FULL and r.get("ip")]
        ports = sorted({int(p) for r in records.values() for p in (r.get("ports") or [])})
        await self.ssh.write_file(
            f"{layout.FIREWALL_DIR}/firewall.env",
            commands.firewall_env(self.addr, bow_address=bow_address, full_ips=full, sandbox_ports=ports), mode="644",
        )
        if await self.container_state(layout.FIREWALL_CONTAINER) != "running":
            return ""
        result = await self.docker("exec", layout.FIREWALL_CONTAINER, "/bin/sh", "/apply.sh", "once", check=False)
        if not result.ok or "applied" not in result.stdout:
            raise SandboxDriverError(
                "The firewall rules could not be applied: " + (result.stdout.strip() or result.detail(300))[-300:],
                code="unsupported" if "unsupported" in result.stdout else "command_failed",
            )
        return result.stdout.strip().splitlines()[-1]

    # ── workspaces ──

    async def workspace_mounted(self, sandbox_id: str) -> bool:
        return (await self.ssh.run(["mountpoint", "-q", layout.workspace_mount(sandbox_id)], check=False)).ok

    async def mount_workspace(self, sandbox_id: str) -> None:
        if not await self.workspace_mounted(sandbox_id):
            await self.ssh.run(commands.workspace_mount(sandbox_id))

    async def remove_workspace(self, sandbox_id: str) -> None:
        await self.ssh.run(["umount", layout.workspace_mount(sandbox_id)], check=False)
        await self.ssh.run(["rm", "-rf", layout.workspace_image(sandbox_id), layout.workspace_mount(sandbox_id)], check=False)

    async def remove_sandbox(self, sandbox_id: str) -> None:
        """Remove a sandbox's container, routes, proxy rule, record and workspace. Idempotent."""
        await self.docker("rm", "-f", layout.sandbox_container(sandbox_id), check=False)
        for host_dir, route_id in ((layout.CADDY_DIR, commands.router_route_id(sandbox_id)),
                                   (layout.EGRESS_DIR, commands.egress_route_id(sandbox_id))):
            try:
                await self.drop_route(host_dir, route_id)
            except (SandboxDriverError, SSHError):
                pass  # the support container is down; a repeated bootstrap rebuilds routes from the records
        await self.drop_record(sandbox_id)
        await self.remove_workspace(sandbox_id)
