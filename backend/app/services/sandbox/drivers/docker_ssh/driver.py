"""The ``docker-ssh`` driver: sandboxes as Docker containers on one Linux server.

    BoW ── SSH (control) ──► docker run / stop / start / rm / stats
                             the router's and the proxy's admin APIs (local sockets)
    BoW ── HTTPS :port ────► router ──► sandbox  (sandboxd: gRPC + REST)
                             sandbox ──► egress proxy ──► internet

One instance serves one compute and holds one SSH connection, opened on first
use. Nothing is kept in memory between calls that another worker would need:
the handle BoW stores and the records on the host are enough.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from app.services.sandbox.drivers.base import (
    CAP_DOMAIN_ALLOWLIST,
    CAP_LIVE_STATS,
    CAP_STOP_RESUME,
    CAP_STRONG_ISOLATION,
    STATUS_MISSING,
    STATUS_RUNNING,
    STATUS_STOPPED,
    ComputeContext,
    ComputeHealth,
    EgressRecord,
    Endpoint,
    ReconcileResult,
    SandboxDriver,
    SandboxDriverError,
    SandboxHandle,
    SandboxNotFound,
    SandboxSpec,
    SandboxStats,
    SandboxStatus,
    StepCallback,
    StepResult,
    StepSpec,
)
from app.services.sandbox.drivers.docker_ssh import commands, layout
from app.services.sandbox.drivers.docker_ssh.bootstrap import STEPS, Bootstrap
from app.services.sandbox.drivers.docker_ssh.host import Host
from app.services.sandbox.drivers.docker_ssh.schemas import DockerSSHConfig
from app.services.sandbox.drivers.docker_ssh.ssh import (
    SSHError, SSHHost, fingerprint_of, generate_keypair, public_key_of,
)

_SIZE = re.compile(r"^\s*([0-9.]+)\s*([KMGT]?i?B)\s*$", re.I)
_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
          "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4}


def _bytes(text: str) -> Optional[int]:
    match = _SIZE.match(text or "")
    if not match:
        return None
    return int(float(match.group(1)) * _UNITS.get(match.group(2).upper(), 1))


class DockerSSHDriver(SandboxDriver):
    type_name = "docker-ssh"
    potential_capabilities = frozenset({CAP_STOP_RESUME, CAP_STRONG_ISOLATION, CAP_DOMAIN_ALLOWLIST, CAP_LIVE_STATS})

    def __init__(self, ctx: ComputeContext):
        super().__init__(ctx)
        self.config = DockerSSHConfig(**(ctx.config or {}))
        self.addr = layout.addresses()
        self._ssh: Optional[SSHHost] = None
        self._host: Optional[Host] = None

    # ── registry hooks ──

    @staticmethod
    def is_another_compute(previous_config: Dict[str, Any], config: Dict[str, Any]) -> bool:
        """A different address or SSH port is a different server: its host key,
        its router's certificate and "bootstrapped" are not this one's."""
        return any(previous_config.get(key) != config.get(key) for key in ("address", "ssh_port"))

    @staticmethod
    def prepare_credentials(config: Dict[str, Any], credentials: Dict[str, Any]) -> tuple:
        """Before a compute is stored: generate a key pair when none was pasted,
        and keep the public key with the (non-secret) config so it can be shown."""
        config, credentials = dict(config), dict(credentials)
        private_key = credentials.get("ssh_private_key")
        if private_key:
            config["ssh_public_key"] = public_key_of(private_key)
        elif not config.get("ssh_public_key") or credentials.get("_generate"):
            private_key, public_key = generate_keypair()
            credentials["ssh_private_key"] = private_key
            config["ssh_public_key"] = public_key
        credentials.pop("_generate", None)
        return config, credentials

    @staticmethod
    def public_info(driver_state: Dict[str, Any]) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        for key in ("isolation", "isolation_detail", "isolation_verified", "user_mode", "sandboxd", "runtime",
                    "password_login", "host_key_seen_fingerprint"):
            if key in driver_state:
                info[key] = driver_state[key]
        info.update(driver_state.get("docker") or {})
        if driver_state.get("ssh_host_key"):
            info["host_key_fingerprint"] = fingerprint_of(driver_state["ssh_host_key"])
        info["host_key_pinned"] = bool(driver_state.get("ssh_host_key"))
        return info

    # ── connections ──

    @property
    def ssh(self) -> SSHHost:
        if self._ssh is None:
            self._ssh = SSHHost(
                self.config.address, self.config.ssh_port, self.config.ssh_user,
                (self.ctx.credentials or {}).get("ssh_private_key"),
                pinned_host_key=self.ctx.driver_state.get("ssh_host_key"),
            )
            self._ssh.mode = self.ctx.driver_state.get("user_mode") if self.ctx.driver_state.get("ssh_host_key") else None
        return self._ssh

    @property
    def host(self) -> Host:
        if self._host is None:
            self._host = Host(self.ssh, self.addr, self.ctx.instance_id)
        return self._host

    async def close(self) -> None:
        if self._ssh is not None:
            await self._ssh.close()

    def _require_prepared(self) -> None:
        if not self.ctx.driver_state.get("prepared"):
            raise SandboxDriverError("This server is not bootstrapped", code="not_prepared")

    def _runtime(self) -> str:
        return layout.RUNTIME_STRONG if self.ctx.driver_state.get("isolation") == "gvisor" else layout.RUNTIME_STANDARD

    # ── host key ──

    async def _pin_from_config(self) -> Optional[str]:
        """Pin the host key when the admin gave its fingerprint up front and it matches."""
        keys = await self.ssh.fetch_host_keys()
        fingerprints = {fingerprint_of(key): key for key in keys}
        shown = next(iter(fingerprints))
        self.ctx.driver_state["host_key_seen_fingerprint"] = shown
        wanted = self.config.ssh_host_key_fingerprint
        if wanted and wanted in fingerprints:
            self._pin(fingerprints[wanted])
        return shown

    def _pin(self, host_key: str) -> None:
        self.ctx.driver_state["ssh_host_key"] = host_key
        self.ctx.driver_state.pop("host_key_seen_fingerprint", None)
        self.ssh.pinned_host_key = host_key

    async def confirm_identity(self, fingerprint: str) -> None:
        """Pin the server's SSH host key once the admin has confirmed its fingerprint."""
        wanted = str(fingerprint).strip().rstrip("=")
        for key in await self.ssh.fetch_host_keys():
            if fingerprint_of(key) == wanted:
                self._pin(key)
                return
        raise SandboxDriverError(
            "The fingerprint does not match any key the server presents now.", code="host_key_mismatch",
        )

    async def enroll(self, transient) -> None:
        """First login with a one-time password: install BoW's public key, verify
        key login, and forget the password. The host key must be pinned first so
        the password is only ever sent to the confirmed server."""
        password = (transient or {}).get("one_time_password")
        if not password:
            return
        if not self.ctx.driver_state.get("ssh_host_key"):
            await self._pin_from_config()
        if not self.ctx.driver_state.get("ssh_host_key"):
            raise SandboxDriverError(
                "Confirm the server's SSH host key fingerprint before using a one-time password "
                "(run Test connection), or enter the fingerprint with the compute.", code="host_key_unconfirmed",
            )
        public_key = self.config.ssh_public_key or public_key_of((self.ctx.credentials or {}).get("ssh_private_key") or "")
        await self.ssh.install_public_key(password, public_key)
        self.ctx.driver_state["enrolled_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    # ── describing the compute ──

    def capabilities(self) -> set:
        caps = {CAP_STOP_RESUME, CAP_DOMAIN_ALLOWLIST, CAP_LIVE_STATS}
        if self.ctx.driver_state.get("isolation") == "gvisor":
            caps.add(CAP_STRONG_ISOLATION)
        return caps

    def prepare_steps(self) -> List[StepSpec]:
        return list(STEPS)

    async def prepare(self, on_step: Optional[StepCallback] = None, *, take_over: bool = False,
                      images: Sequence[str] = ()) -> List[StepResult]:
        if not self.ctx.driver_state.get("ssh_host_key"):
            try:
                await self._pin_from_config()
            except SSHError as exc:
                return _failed_first(exc.message)
            if not self.ctx.driver_state.get("ssh_host_key"):
                return _failed_first(
                    "The server's SSH host key is not confirmed yet. Run Test connection and confirm its fingerprint.",
                )
        try:
            return await Bootstrap(self, on_step, take_over=take_over, images=images).run()
        except SSHError as exc:
            return _failed_first(exc.message)

    async def check(self) -> ComputeHealth:
        checks: List[Dict[str, Any]] = []

        def line(key: str, title: str, status: str, detail: Optional[str] = None) -> None:
            checks.append({"key": key, "title": title, "status": status, "detail": detail})

        state = self.ctx.driver_state
        try:
            if not state.get("ssh_host_key"):
                fingerprint = await self._pin_from_config()
                if not state.get("ssh_host_key"):
                    line("host_key", "SSH host key", "warning",
                         f"Fingerprint {fingerprint}. Confirm that this is your server's key; it is pinned after that.")
                    return ComputeHealth(reachable=True, capabilities=sorted(self.capabilities()),
                                         info=self.public_info(state), checks=checks,
                                         error="Confirm the server's SSH host key fingerprint.")
            line("host_key", "SSH host key", "ok", f"Pinned: {fingerprint_of(state['ssh_host_key'])}")

            self.ssh.mode = None  # detect again: the user's privileges may have changed
            mode = await self.ssh.detect_mode()
            state["user_mode"] = mode
            line("ssh", "SSH access", "ok", "Logged in as root." if mode == "root"
                 else f"Logged in as {self.config.ssh_user}; commands run through sudo -n.")

            facts = await self.host.docker_info()
            state["docker"] = {k: facts[k] for k in ("docker_version", "storage_driver", "firewall_compute", "cgroup_version", "cpus", "memory_bytes")}
            if facts["problems"]:
                line("docker", "Docker", "failed", " ".join(facts["problems"]))
                return ComputeHealth(reachable=True, capabilities=sorted(self.capabilities()), info=self.public_info(state),
                                     checks=checks, error=" ".join(facts["problems"]))
            line("docker", "Docker", "ok", f"Docker {facts['docker_version']}")

            _, isolation, explanation = await self.host.strong_runtime(facts)
            line("isolation", "gVisor", "ok" if isolation == "gvisor" else "warning", explanation)
            if state.get("prepared"):
                state["isolation"], state["isolation_detail"] = isolation, explanation

            password_login = await self.ssh.run(["sh", "-c", "sshd -T 2>/dev/null | grep -i '^passwordauthentication'"], check=False)
            if password_login.ok and "yes" in password_login.stdout.lower():
                state["password_login"] = True
                line("password_login", "Password login", "warning",
                     "Password login is still enabled on this server. We recommend turning it off.")
            elif password_login.ok:
                state["password_login"] = False

            owner_raw = await self.ssh.read_file(layout.OWNER_FILE)
            owner = json.loads(owner_raw) if owner_raw else None
            containers = {c["name"]: c for c in await self.host.containers()} if owner else {}
            support = [layout.CADDY_CONTAINER, layout.EGRESS_CONTAINER, layout.FIREWALL_CONTAINER]
            down = [name for name in support if containers.get(name, {}).get("state") != "running"]
            mine = bool(owner) and owner.get("instance_id") == self.ctx.instance_id
            prepared = bool(state.get("prepared")) and mine
            if owner and not mine:
                line("bootstrap", "Bootstrapped", "failed", "This server is owned by another BoW instance.")
            elif not prepared:
                line("bootstrap", "Bootstrapped", "warning", "Not bootstrapped yet.")
            elif down:
                line("bootstrap", "Bootstrapped", "failed", "Not running: " + ", ".join(down) + ". Bootstrap again to repair.")
            else:
                line("bootstrap", "Bootstrapped", "ok", "The router, the egress proxy and the firewall helper are running.")
            running = sum(1 for c in containers.values() if c.get("sandbox") and c.get("state") == "running")
            return ComputeHealth(
                reachable=True, prepared=prepared, healthy=prepared and not down, running=running,
                capabilities=sorted(self.capabilities()), info=self.public_info(state), checks=checks,
                error=None if (prepared and not down) else next((c["detail"] for c in checks if c["status"] == "failed"), None),
            )
        except SandboxDriverError as exc:
            line(exc.code if exc.code in ("host_key_mismatch", "auth_failed", "no_privileges") else "ssh",
                 "SSH access", "failed", exc.message)
            return ComputeHealth(reachable=exc.code not in ("unreachable", "timeout"), info=self.public_info(state),
                                 checks=checks, error=exc.message)

    # ── sandbox lifecycle ──

    async def _free_address(self, records: Dict[str, Dict[str, Any]]) -> str:
        used = {r.get("ip") for r in records.values()}
        for candidate in self.addr.sandbox_candidates():
            if candidate not in used:
                return candidate
        raise SandboxDriverError("The sandbox network on this server has no free address", code="capacity")

    async def create(self, spec: SandboxSpec, _verifying: bool = False) -> SandboxHandle:
        if not _verifying:
            self._require_prepared()
        host = self.host
        runtime = self._runtime()
        if spec.require_strong_isolation and runtime != layout.RUNTIME_STRONG:
            raise SandboxDriverError("This server has no strong isolation (gVisor)", code="unsupported")
        name = layout.sandbox_container(spec.sandbox_id)
        if await host.container_state(name) is not None:
            raise SandboxDriverError("A sandbox with this id already exists on the server", code="already_exists")

        if spec.registry_username and spec.registry_password:
            await host.pull(spec.image, username=spec.registry_username, password=spec.registry_password)
        env_path = f"{layout.ENV_DIR}/{spec.sandbox_id}.env"
        env_content = commands.env_file(commands.sandbox_environment(spec, self.addr))
        try:
            for argv in commands.workspace_create(spec.sandbox_id, spec.disk_mb):
                await host.ssh.run(argv)
            await host.ssh.write_file(env_path, env_content, mode="600")
            ip = ""
            for attempt in range(4):
                records = await host.records()
                ip = await self._free_address(records)
                await host.write_record(spec.sandbox_id, {
                    "sandbox_id": spec.sandbox_id, "instance": self.ctx.instance_id, "ip": ip,
                    "policy": spec.network_policy, "domains": list(spec.allowed_domains), "ports": list(spec.ports),
                })
                started = await host.ssh.run(
                    commands.sandbox_run(spec, ip=ip, runtime=runtime, instance_id=self.ctx.instance_id, env_file_path=env_path),
                    check=False, timeout=600,
                )
                if started.ok:
                    break
                await host.docker("rm", "-f", name, check=False)
                if "address already in use" not in started.detail(600).lower() or attempt == 3:
                    raise SandboxDriverError(
                        "The sandbox container could not be started: " + _clean_docker_error(started.detail(600)),
                        code="command_failed",
                    )
        except Exception:
            await host.remove_sandbox(spec.sandbox_id)
            raise
        finally:
            # Secrets were handed to Docker through this file; it is not kept.
            await host.ssh.run(["rm", "-f", env_path], check=False)

        try:
            await asyncio.sleep(0.3)
            state = await host.container_state(name)
            if state != "running":
                logs = await host.docker("logs", "--tail", "20", name, check=False)
                raise SandboxDriverError(
                    "The sandbox container stopped right after starting: " + (logs.stdout + logs.stderr).strip()[-400:],
                    code="command_failed",
                )
            egress = commands.egress_route(spec.sandbox_id, ip, spec.network_policy, spec.allowed_domains)
            if egress is not None:
                await host.set_route(layout.EGRESS_DIR, "egress", egress)
            await host.apply_firewall(bow_address=self.config.bow_address)
            await host.set_route(layout.CADDY_DIR, "sbx", commands.router_route(spec.sandbox_id, ip, spec.ports))
        except Exception:
            await host.remove_sandbox(spec.sandbox_id)
            raise
        return SandboxHandle(sandbox_id=spec.sandbox_id, ref={"container": name, "ip": ip, "runtime": runtime})

    async def _state_of(self, handle: SandboxHandle) -> str:
        state = await self.host.container_state(layout.sandbox_container(handle.sandbox_id))
        if state is None:
            raise SandboxNotFound()
        return state

    async def stop(self, handle: SandboxHandle) -> None:
        await self._state_of(handle)
        await self.host.docker("stop", "-t", "5", layout.sandbox_container(handle.sandbox_id), timeout=60)
        await self.host.ssh.run(["umount", layout.workspace_mount(handle.sandbox_id)], check=False)

    async def start(self, handle: SandboxHandle) -> None:
        await self._state_of(handle)
        await self.host.mount_workspace(handle.sandbox_id)
        started = await self.host.docker("start", layout.sandbox_container(handle.sandbox_id), check=False, timeout=120)
        if not started.ok:
            raise SandboxDriverError("The sandbox could not be resumed: " + _clean_docker_error(started.detail(400)),
                                     code="command_failed")
        # Routes survive a stop; after a reboot or a repair they are rebuilt from the record.
        record = (await self.host.records()).get(handle.sandbox_id)
        if record and record.get("ip"):
            present = set(await self.host.route_ids(layout.CADDY_DIR, "sbx"))
            if commands.router_route_id(handle.sandbox_id) not in present:
                await self.host.set_route(layout.CADDY_DIR, "sbx", commands.router_route(
                    handle.sandbox_id, record["ip"], record.get("ports") or []))
            egress = commands.egress_route(handle.sandbox_id, record["ip"], record.get("policy", "none"), record.get("domains") or [])
            if egress is not None and egress["@id"] not in set(await self.host.route_ids(layout.EGRESS_DIR, "egress")):
                await self.host.set_route(layout.EGRESS_DIR, "egress", egress)

    async def delete(self, handle: SandboxHandle) -> None:
        await self.host.remove_sandbox(handle.sandbox_id)
        try:
            await self.host.apply_firewall(bow_address=self.config.bow_address)
        except SandboxDriverError:
            pass  # the helper re-reads its settings on its own within seconds

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        state = await self.host.container_state(layout.sandbox_container(handle.sandbox_id))
        if state is None:
            return SandboxStatus(STATUS_MISSING)
        if state == "running":
            return SandboxStatus(STATUS_RUNNING)
        return SandboxStatus(STATUS_STOPPED, detail=state)

    async def stats(self, handles: Sequence[SandboxHandle]) -> Dict[str, SandboxStats]:
        if not handles:
            return {}
        names = {layout.sandbox_container(h.sandbox_id): h.sandbox_id for h in handles}
        out: Dict[str, SandboxStats] = {}
        usage = await self.host.docker("stats", "--no-stream", "--format", "{{json .}}", *names, check=False, timeout=60)
        for row in usage.stdout.splitlines():
            try:
                entry = json.loads(row)
            except ValueError:
                continue
            sandbox_id = names.get(entry.get("Name"))
            if not sandbox_id:
                continue
            used, _, limit = (entry.get("MemUsage") or "").partition("/")
            try:
                cpu = float((entry.get("CPUPerc") or "").rstrip("%"))
            except ValueError:
                cpu = None
            out[sandbox_id] = SandboxStats(cpu_percent=cpu, memory_bytes=_bytes(used), memory_limit_bytes=_bytes(limit))
        mounts = {layout.workspace_mount(h.sandbox_id): h.sandbox_id for h in handles}
        disk = await self.host.ssh.run(["df", "-B1", "--output=target,used,size", *mounts], check=False)
        for row in disk.stdout.splitlines()[1:]:
            parts = row.split()
            if len(parts) == 3 and parts[0] in mounts:
                stats = out.setdefault(mounts[parts[0]], SandboxStats())
                stats.disk_bytes, stats.disk_limit_bytes = int(parts[1]), int(parts[2])
        return out

    def endpoint(self, handle: SandboxHandle) -> Endpoint:
        ca = self.ctx.driver_state.get("caddy_ca_cert")
        if not ca:
            raise SandboxDriverError("This server is not bootstrapped", code="not_prepared")
        address = self.config.address
        host = f"[{address}]" if ":" in address else address
        return Endpoint(
            base_url=f"https://{host}:{self.config.sandbox_port}",
            grpc_target=f"{host}:{self.config.sandbox_port}",
            ca_cert=ca, headers={"X-Sandbox-Id": handle.sandbox_id},
        )

    async def egress_log(self, handle: SandboxHandle, since: Optional[datetime] = None) -> List[EgressRecord]:
        ip = (handle.ref or {}).get("ip") or ((await self.host.records()).get(handle.sandbox_id) or {}).get("ip")
        if not ip:
            return []
        start = since or (datetime.utcnow() - timedelta(hours=24))
        logs = await self.host.docker(
            "logs", "--since", start.replace(microsecond=0).isoformat() + "Z", layout.EGRESS_CONTAINER, check=False, timeout=120,
        )
        return commands.parse_egress_log((logs.stdout + "\n" + logs.stderr).splitlines(), ip, since)

    # ── housekeeping ──

    async def reconcile(self, known: Sequence[SandboxHandle]) -> ReconcileResult:
        """Make the server match ``known``. Only resources carrying this BoW
        instance's label are ever touched."""
        self._require_prepared()
        host = self.host
        result = ReconcileResult()
        known_ids = {h.sandbox_id for h in known}
        containers = {c["sandbox"]: c for c in await host.containers() if c.get("sandbox")}
        records = {sid: r for sid, r in (await host.records()).items() if r.get("instance") == self.ctx.instance_id}

        for sandbox_id in sorted((set(containers) | set(records)) - known_ids):
            if sandbox_id.startswith("verify-"):
                continue  # a bootstrap in progress on another worker owns it
            await host.remove_sandbox(sandbox_id)
            result.removed.append(sandbox_id)
        result.missing = sorted(known_ids - set(containers))

        for host_dir, server, prefix in ((layout.CADDY_DIR, "sbx", "sbx-"), (layout.EGRESS_DIR, "egress", "eg-")):
            present = set(await host.route_ids(host_dir, server))
            for route_id in sorted(present):
                if route_id.startswith(prefix) and route_id[len(prefix):] not in known_ids and not route_id[len(prefix):].startswith("verify-"):
                    await host.drop_route(host_dir, route_id)
                    result.removed.append(f"route:{route_id}")
            for sandbox_id in sorted(known_ids & set(containers)):
                record = records.get(sandbox_id)
                if not record or not record.get("ip"):
                    continue
                route = (commands.router_route(sandbox_id, record["ip"], record.get("ports") or []) if server == "sbx"
                         else commands.egress_route(sandbox_id, record["ip"], record.get("policy", "none"), record.get("domains") or []))
                if route is not None and route["@id"] not in present:
                    await host.set_route(host_dir, server, route)
                    result.repaired.append(f"route:{route['@id']}")
        await host.apply_firewall(bow_address=self.config.bow_address)
        return result

    async def cleanup(self) -> None:
        """Remove everything this BoW instance put on the server."""
        host = self.host
        owner_raw = await host.ssh.read_file(layout.OWNER_FILE)
        owner = json.loads(owner_raw) if owner_raw else None
        if owner and owner.get("instance_id") != self.ctx.instance_id:
            raise SandboxDriverError("This server is owned by another BoW instance", code="owned_by_other")
        if await host.container_state(layout.FIREWALL_CONTAINER) == "running":
            await host.docker("exec", layout.FIREWALL_CONTAINER, "/bin/sh", "/apply.sh", "remove", check=False)
        for sandbox_id, record in (await host.records()).items():
            if record.get("instance") == self.ctx.instance_id:
                await host.remove_sandbox(sandbox_id)
        for container in await host.containers():
            await host.docker("rm", "-f", container["name"], check=False)
        await host.docker("network", "rm", layout.NETWORK, check=False)
        await host.ssh.run(["sh", "-c", f"for m in {layout.WORKSPACES}/*/; do umount \"$m\" 2>/dev/null; done; true"], check=False)
        await host.ssh.run(["rm", "-rf", layout.ROOT], check=False)
        for key in ("prepared", "caddy_ca_cert", "isolation", "isolation_detail", "isolation_verified", "runtime", "sandboxd"):
            self.ctx.driver_state.pop(key, None)

    async def pull_image(self, image: str, *, registry_username: Optional[str] = None,
                         registry_password: Optional[str] = None) -> Optional[str]:
        if self.config.images_preloaded:
            return await self.host.image_digest(image) if await self.host.image_present(image) else None
        await self.host.pull(image, username=registry_username, password=registry_password)
        return await self.host.image_digest(image)


def _failed_first(message: str) -> List[StepResult]:
    results = [StepResult(key=s.key, title=s.title, status="skipped") for s in STEPS]
    results[0].status, results[0].detail = "failed", message
    results[0].started_at = results[0].finished_at = datetime.utcnow()
    return results


def _clean_docker_error(text: str) -> str:
    """The useful line of a failed ``docker run``/``start``."""
    lines = [line.strip() for line in text.splitlines() if line.strip() and "Run 'docker" not in line]
    return (lines[-1] if lines else text.strip())[:300]
