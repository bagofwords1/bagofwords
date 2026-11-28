"""Preparing a Linux server to run sandboxes ("Bootstrap").

Nine steps, each reporting its own result. Bootstrap is safe to run again: a
step skips what is in place and repairs what is missing, so the same action
is also "Repair" and the way to roll out new images. It stops at the first
failed step.

BoW installs neither Docker nor gVisor and edits no Docker configuration; it
uses what the admin prepared and says clearly what is missing.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from app.services.sandbox.drivers.base import (
    POLICY_NONE, SandboxDriverError, SandboxHandle, SandboxSpec, StepCallback, StepResult, StepSpec,
)
from app.services.sandbox.drivers.docker_ssh import commands, layout
from app.services.sandbox.drivers.docker_ssh.host import RECORDS_DIR, Host

STEPS = [
    StepSpec("docker", "Check Docker and server ownership"),
    StepSpec("isolation", "Detect gVisor"),
    StepSpec("network", "Create the sandbox network"),
    StepSpec("sandboxd", "Place the sandboxd binary"),
    StepSpec("images", "Pull images"),
    StepSpec("router", "Start the router and pin its CA"),
    StepSpec("egress", "Start the egress proxy"),
    StepSpec("firewall", "Apply the firewall rules"),
    StepSpec("verify", "Verify with a test sandbox"),
]


class _Warning(Exception):
    """A step that finished but has something the admin should see."""


class Bootstrap:
    def __init__(self, driver, on_step: Optional[StepCallback], *, take_over: bool, images: Sequence[str]):
        self.driver = driver
        self.host: Host = driver.host
        self.state: Dict[str, Any] = driver.ctx.driver_state
        self.config = driver.config
        self.on_step = on_step
        self.take_over = take_over
        self.images = list(dict.fromkeys(images))
        self.results = [StepResult(key=s.key, title=s.title) for s in STEPS]
        self.runtime = layout.RUNTIME_STANDARD

    async def _report(self, result: StepResult, status: str, detail: Optional[str] = None) -> None:
        now = datetime.utcnow()
        if status == "running":
            result.started_at = now
        else:
            result.finished_at = now
        result.status, result.detail = status, detail
        if self.on_step is not None:
            await self.on_step(result)

    async def run(self) -> List[StepResult]:
        for result in self.results:
            await self._report(result, "running")
            try:
                detail = await getattr(self, f"_{result.key}")()
                await self._report(result, "ok", detail)
            except _Warning as warning:
                await self._report(result, "warning", str(warning))
            except SandboxDriverError as exc:
                result.code = exc.code
                await self._report(result, "failed", exc.message)
                for rest in self.results[self.results.index(result) + 1:]:
                    rest.status = "skipped"
                self.state["prepared"] = False
                return self.results
        self.state["prepared"] = True
        self.state["bootstrapped_images"] = {
            "caddy": layout.caddy_image(), "firewall": layout.firewall_image(), "tools": layout.tools_image(),
        }
        return self.results

    # ── 1. Docker, a supported setup, and ownership ──

    async def _docker(self) -> str:
        self.host.ssh.mode = None  # detect again: the user's privileges may have changed
        mode = await self.host.ssh.detect_mode()
        self.state["user_mode"] = mode
        facts = await self.host.docker_info()
        self.state["docker"] = {k: facts[k] for k in ("docker_version", "storage_driver", "firewall_compute", "cgroup_version", "cpus", "memory_bytes")}
        if facts["problems"]:
            raise SandboxDriverError(" ".join(facts["problems"]), code="unsupported")
        self._facts = facts

        await self.host.ssh.run(["mkdir", "-p", f"{layout.ROOT}/bin", layout.WORKSPACES, layout.ENV_DIR, layout.ETC_DIR,
                                 RECORDS_DIR, layout.FIREWALL_DIR])
        await self.host.ssh.run(["chmod", "700", layout.ENV_DIR, RECORDS_DIR])
        owner = await self._owner()
        note = ""
        if owner and owner.get("instance_id") != self.host.instance_id:
            if not self.take_over:
                raise SandboxDriverError(
                    f"This server is owned by another BoW instance ({owner.get('base_url') or 'unknown address'}, "
                    f"claimed {owner.get('claimed_at', 'earlier')}). Two instances would delete each other's sandboxes. "
                    "Choose Take over to remove the other instance's sandboxes and claim the server.",
                    code="owned_by_other",
                )
            removed = await self._remove_instance(owner["instance_id"])
            note = f" Taken over from another BoW instance; {removed} of its containers were removed."
            owner = None
        if not owner:
            await self.host.ssh.write_file(layout.OWNER_FILE, json.dumps({
                "instance_id": self.host.instance_id, "base_url": self.driver.ctx.base_url,
                "claimed_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            }) + "\n", mode="644")
        return f"Docker {facts['docker_version']}, connected as {'root' if mode == 'root' else 'a sudo user'}.{note}"

    async def _owner(self) -> Optional[Dict[str, Any]]:
        raw = await self.host.ssh.read_file(layout.OWNER_FILE)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    async def _remove_instance(self, instance_id: str) -> int:
        containers = await self.host.containers(instance_id=instance_id)
        for container in containers:
            await self.host.docker("rm", "-f", container["name"], check=False)
        for sandbox_id, record in (await self.host.records()).items():
            if record.get("instance") == instance_id:
                await self.host.drop_record(sandbox_id)
                await self.host.remove_workspace(sandbox_id)
        await self.host.docker("network", "rm", layout.NETWORK, check=False)
        # The other instance's router state (routes, CA) goes too: this instance pins its own.
        await self.host.ssh.run(["rm", "-rf", layout.CADDY_DIR, layout.EGRESS_DIR], check=False)
        return len(containers)

    # ── 2. gVisor ──

    async def _isolation(self) -> str:
        self.runtime, isolation, explanation = await self.host.strong_runtime(self._facts)
        self.state["isolation"] = isolation
        self.state["isolation_detail"] = explanation
        self.state["runtime"] = self.runtime
        if isolation != "gvisor":
            raise _Warning("Standard isolation. " + explanation)
        return explanation

    # ── 3. network ──

    async def _network(self) -> str:
        addr = self.host.addr
        existing = await self.host.docker(
            "network", "inspect", "--format",
            '{{(index .IPAM.Config 0).Subnet}} {{index .Options "com.docker.network.bridge.enable_icc"}}', layout.NETWORK,
            check=False,
        )
        if existing.ok:
            subnet, _, icc = existing.stdout.strip().partition(" ")
            if subnet == addr.subnet and icc.strip() == "false":
                return f"{layout.NETWORK} ({addr.subnet}) is in place."
            in_use = [c for c in await self.host.containers(all_instances=True)]
            if in_use:
                raise SandboxDriverError(
                    f"The Docker network {layout.NETWORK} exists with different settings and is in use. "
                    "Delete this server's sandboxes and bootstrap again.", code="command_failed",
                )
            await self.host.docker("network", "rm", layout.NETWORK)
        await self.host.ssh.run(commands.network_create(addr, self.host.instance_id))
        return f"Created {layout.NETWORK} ({addr.subnet}); sandboxes cannot reach each other on it."

    # ── 4. sandboxd ──

    async def _ensure_image(self, image: str) -> str:
        """Make an image available: pull it, or (air-gapped) require it to be loaded already."""
        if self.config.images_preloaded:
            if not await self.host.image_present(image):
                raise SandboxDriverError(
                    f"The image {image} is not on the server. This compute expects preloaded images: load it "
                    "with `docker load` and bootstrap again.", code="command_failed",
                )
            return "present"
        await self.host.pull(image)
        return "pulled"

    async def _sandboxd(self) -> str:
        image = layout.tools_image()
        await self._ensure_image(image)
        name = f"bow-sbx-tools-{secrets.token_hex(4)}"
        await self.host.docker("create", "--name", name, image, "/sandboxd")
        try:
            await self.host.docker("cp", f"{name}:/sandboxd", f"{layout.SANDBOXD_PATH}.new")
        finally:
            await self.host.docker("rm", "-f", name, check=False)
        await self.host.ssh.run(["chmod", "755", f"{layout.SANDBOXD_PATH}.new"])
        await self.host.ssh.run(["mv", "-f", f"{layout.SANDBOXD_PATH}.new", layout.SANDBOXD_PATH])
        version = await self.host.ssh.run([layout.SANDBOXD_PATH, "--version"], check=False)
        described = (version.stdout or version.stderr).strip().splitlines()[0] if (version.stdout or version.stderr).strip() else "sandboxd"
        self.state["sandboxd"] = described[:200]
        await self.host.ssh.write_file(layout.RESOLV_NONE, commands.resolv_conf(False), mode="644")
        await self.host.ssh.write_file(layout.RESOLV_FULL, commands.resolv_conf(True), mode="644")
        return described[:200]

    # ── 5. images ──

    async def _images(self) -> str:
        wanted = [layout.caddy_image(), layout.firewall_image(), *self.images]
        outcomes = [await self._ensure_image(image) for image in wanted]
        verb = "present" if self.config.images_preloaded else "pulled"
        extra = f" and {len(self.images)} more" if self.images else ""
        return f"{len(outcomes)} image(s) {verb}: the router and the firewall helper{extra}."

    # ── 6 and 7. the router and the egress proxy ──

    async def _support(self, name: str, host_dir: str, base: Dict[str, Any], run_argv: List[str], image: str) -> bool:
        """Start a support container, or keep it when it already runs the right
        image with the right base configuration. Returns whether it was (re)created."""
        wanted = json.dumps(base, sort_keys=True) + "\n"
        current = await self.host.ssh.read_file(f"{host_dir}/etc/base.json")
        running_image = await self.host.docker("inspect", "--format", "{{.Config.Image}} {{.State.Status}}", name, check=False)
        if current == wanted and running_image.ok and running_image.stdout.strip() == f"{image} running":
            return False
        await self.host.docker("rm", "-f", name, check=False)
        await self.host.ssh.run(["mkdir", "-p", f"{host_dir}/admin", f"{host_dir}/data", f"{host_dir}/config", f"{host_dir}/etc"])
        await self.host.ssh.write_file(f"{host_dir}/etc/base.json", wanted, mode="644")
        # A saved configuration from an earlier base would win over the new one.
        await self.host.ssh.run(["rm", "-f", f"{host_dir}/config/caddy/autosave.json"], check=False)
        await self.host.ssh.run(run_argv)
        await self.host.wait_admin(host_dir)
        return True

    async def _router(self) -> str:
        image = layout.caddy_image()
        recreated = await self._support(
            layout.CADDY_CONTAINER, layout.CADDY_DIR, commands.caddy_base_config(self.config.address),
            commands.caddy_run(image, self.host.addr, self.host.instance_id, self.config.sandbox_port), image,
        )
        await self.host.wait_admin(layout.CADDY_DIR)
        status, body = await self.host.admin(layout.CADDY_DIR, "GET", "/pki/ca/local")
        try:
            certificate = json.loads(body)["root_certificate"]
        except (ValueError, KeyError) as exc:
            raise SandboxDriverError(f"Could not read the router's CA certificate (HTTP {status})", code="command_failed") from exc
        self.state["caddy_ca_cert"] = certificate
        await self._restore_routes()
        fingerprint = hashlib.sha256(certificate.encode("ascii")).hexdigest()[:16]
        return f"Router {'started' if recreated else 'already running'} on port {self.config.sandbox_port}; CA pinned ({fingerprint})."

    async def _egress(self) -> str:
        image = layout.caddy_image()
        recreated = await self._support(
            layout.EGRESS_CONTAINER, layout.EGRESS_DIR, commands.egress_base_config(),
            commands.egress_run(image, self.host.addr, self.host.instance_id), image,
        )
        await self._restore_routes(egress=True)
        return f"Egress proxy {'started' if recreated else 'already running'}. Sandboxes reach the internet only through it."

    async def _restore_routes(self, egress: bool = False) -> None:
        """Re-create the routes of this instance's existing sandboxes from their records."""
        host_dir, server = (layout.EGRESS_DIR, "egress") if egress else (layout.CADDY_DIR, "sbx")
        present = set(await self.host.route_ids(host_dir, server))
        for sandbox_id, record in (await self.host.records()).items():
            if record.get("instance") != self.host.instance_id or not record.get("ip"):
                continue
            if egress:
                route = commands.egress_route(sandbox_id, record["ip"], record.get("policy", POLICY_NONE), record.get("domains") or [])
            else:
                route = commands.router_route(sandbox_id, record["ip"], record.get("ports") or [])
            if route is not None and route["@id"] not in present:
                await self.host.set_route(host_dir, server, route)

    # ── 8. firewall ──

    async def _firewall(self) -> str:
        image = layout.firewall_image()
        await self.host.ssh.write_file(
            f"{layout.FIREWALL_DIR}/firewall.env",
            commands.firewall_env(self.host.addr, bow_address=self.config.bow_address, full_ips=[]), mode="644",
        )
        current = await self.host.docker("inspect", "--format", "{{.Config.Image}} {{.State.Status}}",
                                         layout.FIREWALL_CONTAINER, check=False)
        if not (current.ok and current.stdout.strip() == f"{image} running"):
            await self.host.docker("rm", "-f", layout.FIREWALL_CONTAINER, check=False)
            await self.host.ssh.run(commands.firewall_run(image, self.host.instance_id))
            await asyncio.sleep(1.0)
        line = await self.host.apply_firewall(bow_address=self.config.bow_address)
        if "bridge-nf=1" not in line:
            raise SandboxDriverError(
                "Traffic between containers does not pass through the packet filter on this server "
                "(bridge-nf-call-iptables is off), so sandboxes could not be kept apart.", code="unsupported",
            )
        only = f" Only {self.config.bow_address} may reach the sandbox port." if self.config.bow_address else ""
        return "Rules applied and re-asserted every few seconds, also after a reboot." + only

    # ── 9. verify ──

    async def _verify(self) -> str:
        """Start a throwaway sandbox, run a command in it over TLS, confirm it
        is cut off, and remove it. On a server with gVisor, confirm it runs under it."""
        from app.services.sandbox.runtime_client import SandboxRuntimeClient, SandboxRuntimeError

        token = secrets.token_urlsafe(24)
        spec = SandboxSpec(
            sandbox_id=f"verify-{secrets.token_hex(5)}", image=layout.caddy_image(),
            token_sha256=hashlib.sha256(token.encode()).hexdigest(), cpus=1.0, memory_mb=256, disk_mb=64,
            network_policy=POLICY_NONE,
        )
        handle: Optional[SandboxHandle] = None
        try:
            handle = await self.driver.create(spec, _verifying=True)
            endpoint = dataclasses.replace(self.driver.endpoint(handle), token=token)
            async with SandboxRuntimeClient(endpoint) as runtime:
                try:
                    await runtime.wait_healthy(30)
                except SandboxRuntimeError as exc:
                    raise SandboxDriverError(
                        f"The test sandbox started, but BoW cannot reach it on {self.config.address}:"
                        f"{self.config.sandbox_port} over TLS. Check that this port is open from the BoW server. "
                        f"({exc.message})", code="unreachable",
                    ) from exc
                kernel = await runtime.execute(["uname", "-r"], time_limit=20)
                probe = await runtime.execute([
                    "sh", "-c",
                    f"nc -w 3 {self.host.addr.gateway} 22 </dev/null >/dev/null 2>&1 && echo host-reachable; "
                    "nc -w 3 1.1.1.1 443 </dev/null >/dev/null 2>&1 && echo internet-reachable; "
                    "nc -w 3 169.254.169.254 80 </dev/null >/dev/null 2>&1 && echo metadata-reachable; echo probed",
                ], time_limit=30)
        finally:
            if handle is not None:
                await self.driver.delete(handle)
        reached = [line for line in probe.stdout.decode(errors="replace").split() if line.endswith("-reachable")]
        if b"probed" not in probe.stdout or reached:
            raise SandboxDriverError(
                "The test sandbox could reach what it must not: " + (", ".join(reached) or "the probe did not run") + ".",
                code="command_failed",
            )
        release = kernel.stdout.decode(errors="replace").strip()
        if self.state.get("isolation") == "gvisor":
            if not release.endswith("-gvisor"):
                raise SandboxDriverError(
                    f"gVisor is registered, but the test sandbox did not run under it (kernel {release}).",
                    code="command_failed",
                )
            self.state["isolation_verified"] = True
            return f"A test sandbox ran a command over TLS and could not reach the host, the internet or the metadata address. Strong isolation: verified (kernel {release})."
        return "A test sandbox ran a command over TLS and could not reach the host, the internet or the metadata address. Standard isolation."
