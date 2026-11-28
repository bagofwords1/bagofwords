"""The fake compute: a second implementation of the driver interface.

It exists so the interface always has two implementations (this one and the
real one), so the same contract suite runs in CI without a server, and so the
service, the tools and the admin UI can be exercised end to end.

A fake "sandbox" is a directory on the BoW server and an entry in the
in-process ``sandboxd`` stand-in (``testing/sandboxd_stub.py``). **Code runs
as the BoW server's own user with no isolation**, and network policy is only
simulated (``bow-net-probe <domain>`` inside a sandbox answers what the
policy would allow). The type is offered only under ``TESTING`` or an explicit
opt-in; see ``registry.fake_driver_enabled``.

Its configuration can switch capabilities off and make steps fail, which is
how tests reach the paths a healthy compute never takes.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.services.sandbox.drivers.base import (
    CAP_DOMAIN_ALLOWLIST,
    CAP_LIVE_STATS,
    CAP_STOP_RESUME,
    CAP_STRONG_ISOLATION,
    DOMAIN_FILTERED_POLICIES,
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


class FakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strong_isolation: bool = Field(default=True, title="Strong isolation", description="Pretend the compute has a strong isolation runtime.")
    stop_resume: bool = Field(default=True, title="Stop and resume")
    domain_allowlist: bool = Field(default=True, title="Domain allowlists")
    live_stats: bool = Field(default=True, title="Live stats")
    unreachable: bool = Field(default=False, title="Unreachable", description="Make every call to the compute fail as if it were down.")
    fail_step: Optional[str] = Field(default=None, title="Failing bootstrap step", description="Key of a bootstrap step that should fail.")
    server: Optional[str] = Field(
        default=None, title="Server name",
        description="Computes with the same server name are the same pretend server (for ownership and take-over).",
    )


class FakeCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_token: Optional[str] = Field(default=None, title="API token", json_schema_extra={"secret": True})
    one_time_password: Optional[str] = Field(
        default=None, title="One-time password",
        description="Used once and never stored.", json_schema_extra={"secret": True, "transient": True},
    )


@dataclass
class _FakeSandbox:
    sandbox_id: str
    instance_id: str
    network_policy: str


@dataclass
class _FakeCompute:
    """What "the server" remembers, shared by every driver instance in this process."""

    prepared: bool = False
    owner: Optional[str] = None
    sandboxes: Dict[str, _FakeSandbox] = field(default_factory=dict)
    pulled_images: List[str] = field(default_factory=list)
    # One-time passwords used by ``enroll``; lets tests assert one was used exactly once.
    passwords_used: int = 0


_COMPUTES: Dict[str, _FakeCompute] = defaultdict(_FakeCompute)
_LOCK = threading.Lock()

_STEPS = [
    StepSpec("connect", "Connect and check ownership"),
    StepSpec("isolation", "Detect strong isolation"),
    StepSpec("network", "Create the sandbox network"),
    StepSpec("images", "Pull images"),
    StepSpec("verify", "Start a test sandbox"),
]


def reset_fake_computes() -> None:
    """Forget every fake compute and remove its sandboxes (test isolation)."""
    from app.services.sandbox.testing.sandboxd_stub import get_stub_host

    with _LOCK:
        computes = list(_COMPUTES.values())
        _COMPUTES.clear()
    host = get_stub_host()
    for compute in computes:
        for sandbox_id in list(compute.sandboxes):
            host.remove_sandbox(_stub_id(sandbox_id))


def _stub_id(sandbox_id: str) -> str:
    return f"sbx-{sandbox_id}"


class FakeDriver(SandboxDriver):
    type_name = "fake"
    potential_capabilities = frozenset({CAP_STOP_RESUME, CAP_STRONG_ISOLATION, CAP_DOMAIN_ALLOWLIST, CAP_LIVE_STATS})

    def __init__(self, ctx: ComputeContext):
        super().__init__(ctx)
        self.config = FakeConfig(**(ctx.config or {}))

    @staticmethod
    def public_info(driver_state: dict) -> dict:
        return {k: driver_state[k] for k in ("isolation", "owner") if k in driver_state}

    # ── helpers ──

    @property
    def _compute(self) -> _FakeCompute:
        with _LOCK:
            return _COMPUTES[self.config.server or self.ctx.compute_id]

    @property
    def _host(self):
        from app.services.sandbox.testing.sandboxd_stub import get_stub_host

        return get_stub_host()

    def _require_reachable(self) -> None:
        if self.config.unreachable:
            raise SandboxDriverError("The compute did not answer", code="unreachable")

    def _require(self, handle: SandboxHandle):
        """The stub sandbox behind a handle, or :class:`SandboxNotFound`."""
        self._require_reachable()
        ref = dict(handle.ref or {})
        stub = self._host.get(ref.get("stub_id", ""))
        if stub is None or ref.get("pid") != os.getpid() or handle.sandbox_id not in self._compute.sandboxes:
            raise SandboxNotFound()
        return stub

    # ── describing the compute ──

    def capabilities(self) -> set:
        caps = set()
        if self.config.stop_resume:
            caps.add(CAP_STOP_RESUME)
        if self.config.strong_isolation:
            caps.add(CAP_STRONG_ISOLATION)
        if self.config.domain_allowlist:
            caps.add(CAP_DOMAIN_ALLOWLIST)
        if self.config.live_stats:
            caps.add(CAP_LIVE_STATS)
        return caps

    def prepare_steps(self) -> List[StepSpec]:
        return list(_STEPS)

    async def prepare(self, on_step: Optional[StepCallback] = None, *, take_over: bool = False,
                      images: Sequence[str] = ()) -> List[StepResult]:
        compute = self._compute
        results = [StepResult(key=s.key, title=s.title) for s in _STEPS]

        async def report(result: StepResult, status: str, detail: Optional[str] = None) -> None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if status == "running":
                result.started_at = now
            else:
                result.finished_at = now
            result.status, result.detail = status, detail
            if on_step is not None:
                await on_step(result)

        for result in results:
            await report(result, "running")
            try:
                if self.config.unreachable:
                    raise SandboxDriverError("The compute did not answer", code="unreachable")
                if self.config.fail_step == result.key:
                    raise SandboxDriverError(f"Step '{result.key}' failed (configured to fail)", code="step_failed")
                detail, status = None, "ok"
                if result.key == "connect":
                    if compute.owner and compute.owner != self.ctx.instance_id:
                        if not take_over:
                            raise SandboxDriverError(
                                "This compute is owned by another BoW instance. Choose Take over to remove the "
                                "other instance's sandboxes and claim it.", code="owned_by_other",
                            )
                        for sandbox in [s for s in compute.sandboxes.values() if s.instance_id != self.ctx.instance_id]:
                            self._host.remove_sandbox(_stub_id(sandbox.sandbox_id))
                            compute.sandboxes.pop(sandbox.sandbox_id, None)
                        detail = "Taken over from another BoW instance"
                    compute.owner = self.ctx.instance_id
                elif result.key == "isolation":
                    if self.config.strong_isolation:
                        detail = "Strong isolation available"
                    else:
                        status, detail = "warning", "Standard isolation: no strong isolation runtime on this compute"
                elif result.key == "images":
                    for image in images:
                        if image not in compute.pulled_images:
                            compute.pulled_images.append(image)
                    detail = f"{len(images)} more image(s)" if images else None
                elif result.key == "verify":
                    self._host.start()
                await report(result, status, detail)
            except SandboxDriverError as exc:
                result.code = exc.code
                await report(result, "failed", exc.message)
                for rest in results[results.index(result) + 1:]:
                    rest.status = "skipped"
                compute.prepared = False
                self.ctx.driver_state["prepared"] = False
                return results

        compute.prepared = True
        self.ctx.driver_state.update({
            "prepared": True,
            "owner": self.ctx.instance_id,
            "isolation": "strong" if self.config.strong_isolation else "standard",
        })
        return results

    async def check(self) -> ComputeHealth:
        if self.config.unreachable:
            return ComputeHealth(
                reachable=False, error="The compute did not answer",
                checks=[{"key": "connect", "title": "Connect", "status": "failed", "detail": "The compute did not answer"}],
            )
        compute = self._compute
        running = sum(
            1 for s in compute.sandboxes.values()
            if s.instance_id == self.ctx.instance_id and (stub := self._host.get(_stub_id(s.sandbox_id))) and stub.running
        )
        isolation = "strong" if self.config.strong_isolation else "standard"
        return ComputeHealth(
            reachable=True, prepared=compute.prepared, healthy=compute.prepared, running=running,
            capabilities=sorted(self.capabilities()),
            info={"isolation": isolation, "owner": compute.owner},
            checks=[
                {"key": "connect", "title": "Connect", "status": "ok", "detail": None},
                {"key": "isolation", "title": "Strong isolation",
                 "status": "ok" if self.config.strong_isolation else "warning", "detail": f"{isolation} isolation"},
                {"key": "prepared", "title": "Bootstrapped",
                 "status": "ok" if compute.prepared else "warning",
                 "detail": None if compute.prepared else "Not bootstrapped yet"},
            ],
        )

    # ── sandbox lifecycle ──

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        self._require_reachable()
        compute = self._compute
        if not compute.prepared or compute.owner != self.ctx.instance_id:
            raise SandboxDriverError("The compute is not bootstrapped", code="not_prepared")
        if spec.require_strong_isolation and not self.config.strong_isolation:
            raise SandboxDriverError("This compute has no strong isolation", code="unsupported")
        if spec.network_policy in DOMAIN_FILTERED_POLICIES and not self.config.domain_allowlist:
            raise SandboxDriverError("This compute cannot filter by domain", code="unsupported")
        if spec.sandbox_id in compute.sandboxes:
            raise SandboxDriverError("A sandbox with this id already exists on the compute", code="already_exists")

        env = dict(spec.env)
        env.update(spec.secrets)
        env["BOW_FAKE_NETWORK_POLICY"] = spec.network_policy
        env["BOW_FAKE_ALLOWED_DOMAINS"] = json.dumps(list(spec.allowed_domains))
        self._host.add_sandbox(_stub_id(spec.sandbox_id), spec.token_sha256, env)
        compute.sandboxes[spec.sandbox_id] = _FakeSandbox(spec.sandbox_id, self.ctx.instance_id, spec.network_policy)
        return SandboxHandle(
            sandbox_id=spec.sandbox_id,
            ref={"stub_id": _stub_id(spec.sandbox_id), "pid": os.getpid(), "compute": self.ctx.compute_id},
        )

    async def stop(self, handle: SandboxHandle) -> None:
        stub = self._require(handle)
        if not self.config.stop_resume:
            raise SandboxDriverError("This compute cannot stop and resume sandboxes", code="unsupported")
        self._host.set_running(stub.sandbox_id, False)

    async def start(self, handle: SandboxHandle) -> None:
        stub = self._require(handle)
        self._host.set_running(stub.sandbox_id, True)

    async def delete(self, handle: SandboxHandle) -> None:
        self._require_reachable()
        self._host.remove_sandbox((handle.ref or {}).get("stub_id") or _stub_id(handle.sandbox_id))
        self._compute.sandboxes.pop(handle.sandbox_id, None)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        try:
            stub = self._require(handle)
        except SandboxNotFound:
            return SandboxStatus(STATUS_MISSING)
        return SandboxStatus(STATUS_RUNNING if stub.running else STATUS_STOPPED)

    async def stats(self, handles: Sequence[SandboxHandle]) -> Dict[str, SandboxStats]:
        self._require_reachable()
        if not self.config.live_stats:
            return {}
        out: Dict[str, SandboxStats] = {}
        for handle in handles:
            stub = self._host.get((handle.ref or {}).get("stub_id", ""))
            if stub is None:
                continue
            disk = sum(p.stat().st_size for p in stub.root.rglob("*") if p.is_file())
            out[handle.sandbox_id] = SandboxStats(
                cpu_percent=0.0 if stub.running else None,
                memory_bytes=0 if stub.running else None,
                disk_bytes=disk,
            )
        return out

    def endpoint(self, handle: SandboxHandle) -> Endpoint:
        host = self._host
        return Endpoint(
            base_url=f"https://127.0.0.1:{host.rest_port}",
            grpc_target=f"127.0.0.1:{host.grpc_port}",
            ca_cert=host.ca_pem,
            headers={"X-Sandbox-Id": (handle.ref or {}).get("stub_id") or _stub_id(handle.sandbox_id)},
        )

    async def egress_log(self, handle: SandboxHandle, since: Optional[datetime] = None) -> List[EgressRecord]:
        stub = self._require(handle)
        if not stub.egress_log.exists():
            return []
        cutoff = since.replace(tzinfo=timezone.utc).timestamp() if since else None
        records: Dict[tuple, EgressRecord] = {}
        for line in stub.egress_log.read_text().splitlines():
            entry = json.loads(line)
            if cutoff is not None and entry["at"] < cutoff:
                continue
            at = datetime.fromtimestamp(entry["at"], timezone.utc).replace(tzinfo=None)
            key = (entry["domain"], entry["decision"])
            if key in records:
                records[key].count += 1
                records[key].last_at = at
            else:
                records[key] = EgressRecord(entry["domain"], entry["decision"], 1, at, at)
        return list(records.values())

    # ── housekeeping ──

    async def reconcile(self, known: Sequence[SandboxHandle]) -> ReconcileResult:
        self._require_reachable()
        compute = self._compute
        known_ids = {h.sandbox_id for h in known}
        result = ReconcileResult()
        for sandbox in list(compute.sandboxes.values()):
            if sandbox.instance_id != self.ctx.instance_id:
                continue  # never another instance's
            if sandbox.sandbox_id not in known_ids:
                self._host.remove_sandbox(_stub_id(sandbox.sandbox_id))
                compute.sandboxes.pop(sandbox.sandbox_id, None)
                result.removed.append(sandbox.sandbox_id)
        result.missing = sorted(known_ids - set(compute.sandboxes))
        return result

    async def cleanup(self) -> None:
        self._require_reachable()
        compute = self._compute
        for sandbox in [s for s in compute.sandboxes.values() if s.instance_id == self.ctx.instance_id]:
            self._host.remove_sandbox(_stub_id(sandbox.sandbox_id))
            compute.sandboxes.pop(sandbox.sandbox_id, None)
        if compute.owner == self.ctx.instance_id:
            compute.owner, compute.prepared = None, False
        self.ctx.driver_state.clear()

    async def pull_image(self, image: str, *, registry_username: Optional[str] = None,
                         registry_password: Optional[str] = None) -> Optional[str]:
        self._require_reachable()
        compute = self._compute
        if image not in compute.pulled_images:
            compute.pulled_images.append(image)
        return "sha256:" + hashlib.sha256(image.encode("utf-8")).hexdigest()

    async def enroll(self, transient) -> None:
        self._require_reachable()
        if transient.get("one_time_password"):
            self._compute.passwords_used += 1
            self.ctx.driver_state["enrolled"] = True

    # ── test hooks (not part of the driver interface) ──

    def plant_foreign_sandbox(self, sandbox_id: str, *, instance_id: Optional[str] = None) -> None:
        """Put a sandbox on the compute that BoW's database does not know about."""
        self._host.add_sandbox(_stub_id(sandbox_id), hashlib.sha256(b"orphan").hexdigest(), {})
        self._compute.sandboxes[sandbox_id] = _FakeSandbox(sandbox_id, instance_id or self.ctx.instance_id, "none")

    def has_sandbox(self, sandbox_id: str) -> bool:
        return sandbox_id in self._compute.sandboxes and self._host.get(_stub_id(sandbox_id)) is not None
