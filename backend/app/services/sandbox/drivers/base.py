"""The sandbox driver interface: the one seam between BoW and the infrastructure
sandboxes run on.

Three layers, one seam:

* the **service** (``service.py``) owns permissions, environments, states,
  quotas, audit and choosing a compute. It knows nothing about containers;
* the **runtime client** (``runtime_client.py``) talks to ``sandboxd`` inside
  a sandbox. It only needs an :class:`Endpoint`;
* a **driver** creates, stops, resumes and deletes sandboxes on one compute,
  enforces network policy and isolation, prepares the compute, and reports
  health and live stats. Everything specific to a compute type lives in its
  driver and nowhere else.

The service hands a driver a declarative :class:`SandboxSpec` and gets back
an opaque :class:`SandboxHandle`. It never sees a container name, an address,
a route or a firewall rule. Nothing above a driver asks which type a compute
is: drivers declare :data:`capabilities <ALL_CAPABILITIES>` instead.

This module imports nothing infrastructural, and must stay that way.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

# ── Capabilities ─────────────────────────────────────────────────────────
# What a driver can do. The UI and environment validation read these.

CAP_STOP_RESUME = "stop_resume"            # stop a sandbox and resume it with its files
CAP_STRONG_ISOLATION = "strong_isolation"  # a kernel boundary stronger than a plain container
CAP_DOMAIN_ALLOWLIST = "domain_allowlist"  # the registries / custom allowlist policies
CAP_LIVE_STATS = "live_stats"              # live CPU, memory and disk per sandbox
CAP_WARM_POOL = "warm_pool"                # pre-started sandboxes per environment
CAP_PORTS = "ports"                        # serving declared ports (hosted apps)

ALL_CAPABILITIES = frozenset({
    CAP_STOP_RESUME, CAP_STRONG_ISOLATION, CAP_DOMAIN_ALLOWLIST, CAP_LIVE_STATS, CAP_WARM_POOL, CAP_PORTS,
})

# ── Network policies ─────────────────────────────────────────────────────

POLICY_NONE = "none"              # nothing outside the sandbox
POLICY_REGISTRIES = "registries"  # the package registries only
POLICY_ALLOWLIST = "allowlist"    # the domains listed on the environment
POLICY_FULL = "full"              # the public internet; private ranges stay blocked

NETWORK_POLICIES = (POLICY_NONE, POLICY_REGISTRIES, POLICY_ALLOWLIST, POLICY_FULL)

# The policies that need per-domain filtering (the "domain_allowlist" capability).
DOMAIN_FILTERED_POLICIES = frozenset({POLICY_REGISTRIES, POLICY_ALLOWLIST})


class SandboxDriverError(Exception):
    """A driver operation failed. ``message`` is safe to show to an admin.

    ``code`` is a short machine-readable reason (``unreachable``,
    ``not_prepared``, ``unsupported``, ``not_found``, ``owned_by_other``, ...).
    """

    def __init__(self, message: str, *, code: str = "driver_error", detail: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail


class SandboxNotFound(SandboxDriverError):
    """The sandbox the handle refers to no longer exists on the compute."""

    def __init__(self, message: str = "The sandbox no longer exists on its compute"):
        super().__init__(message, code="not_found")


# ── Data passed across the seam ──────────────────────────────────────────


@dataclass(frozen=True)
class MountSpec:
    """One of a compute's volumes, mounted into a sandbox.

    ``source`` is the compute's own way of naming the storage (a path on a
    server, a claim in a cluster): only the driver reads it. ``subpath`` is a
    directory inside the volume to mount instead of its root; the driver
    creates it when it is missing."""

    volume: str
    source: str
    path: str
    read_only: bool = True
    subpath: Optional[str] = None


@dataclass(frozen=True)
class SandboxSpec:
    """Everything a driver needs to create one sandbox. Maps onto upstream's ``SandboxTemplate``."""

    sandbox_id: str
    # The image, pinned by digest when the environment has one ("repo@sha256:...").
    image: str
    # SHA-256 (hex) of the sandboxd bearer token. The sandbox only ever gets the hash.
    token_sha256: str
    cpus: float = 1.0
    memory_mb: int = 2048
    disk_mb: int = 5120
    pids_limit: int = 512
    env: Mapping[str, str] = field(default_factory=dict)
    # Injected like ``env`` but never logged, echoed or put on a command line.
    secrets: Mapping[str, str] = field(default_factory=dict)
    network_policy: str = POLICY_NONE
    # The domains for ``registries`` (the package registry list) and ``allowlist``.
    allowed_domains: Sequence[str] = ()
    require_strong_isolation: bool = False
    ports: Sequence[int] = ()
    registry_username: Optional[str] = None
    registry_password: Optional[str] = None
    labels: Mapping[str, str] = field(default_factory=dict)
    # Volumes of the compute to mount. They do not count against ``disk_mb``.
    mounts: Sequence[MountSpec] = ()

    def __post_init__(self):
        if self.network_policy not in NETWORK_POLICIES:
            raise ValueError(f"unknown network policy: {self.network_policy!r}")

    def __repr__(self) -> str:  # never print secrets or credentials
        return (
            f"SandboxSpec(sandbox_id={self.sandbox_id!r}, image={self.image!r}, "
            f"network_policy={self.network_policy!r}, secrets=<{len(self.secrets)}>)"
        )


@dataclass(frozen=True)
class SandboxHandle:
    """Opaque reference to a sandbox on its compute.

    ``ref`` is JSON the driver chose and BoW stores verbatim in
    ``sandboxes.driver_ref``. Only the driver that made it reads it.
    """

    sandbox_id: str
    ref: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Endpoint:
    """How to reach ``sandboxd`` inside one sandbox. All the runtime client needs."""

    # "https://host:8443" (or "http://..." for an endpoint with no TLS).
    base_url: str
    # The gRPC target, "host:port". Usually the same address as ``base_url``.
    grpc_target: str
    # PEM of the only CA to trust. ``None`` means the endpoint is plaintext.
    ca_cert: Optional[str] = None
    # Sent on every request, e.g. the header that routes to this sandbox.
    headers: Mapping[str, str] = field(default_factory=dict)
    # The name the server certificate is checked against when it differs from the address.
    tls_server_name: Optional[str] = None
    # The sandbox's bearer token. Drivers leave it empty; the service fills it in.
    token: Optional[str] = None

    @property
    def secure(self) -> bool:
        return self.base_url.startswith("https://")

    def __repr__(self) -> str:  # never print the token
        return f"Endpoint(base_url={self.base_url!r}, grpc_target={self.grpc_target!r}, headers={sorted(self.headers)})"


@dataclass(frozen=True)
class StepSpec:
    """One step of preparing a compute, as shown on the Computes page."""

    key: str
    title: str


@dataclass
class StepResult:
    key: str
    title: str
    # pending | running | ok | warning | failed | skipped
    status: str = "pending"
    detail: Optional[str] = None
    # Why a step failed, for callers that must act on it (e.g. "owned_by_other"
    # offers Take over). ``detail`` is for people; this is for code.
    code: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "code": self.code,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# Called by ``prepare`` every time a step changes status, so progress can be shown live.
StepCallback = Callable[[StepResult], Awaitable[None]]


@dataclass
class ComputeHealth:
    """The result of ``check()``: read-only facts about a compute."""

    reachable: bool
    prepared: bool = False
    healthy: bool = False
    # Sandboxes currently running on the compute (capacity in use).
    running: int = 0
    # What the compute has in total, where it can tell: {"cpus", "memory_mb",
    # "disk_mb"}. ``disk_mb`` is the size of the storage sandbox workspaces
    # live on. A missing key means "unknown": placement then does not count it.
    resources: Dict[str, float] = field(default_factory=dict)
    capabilities: List[str] = field(default_factory=list)
    # Facts safe to show an admin: isolation level, versions, user mode, fingerprints.
    info: Dict[str, Any] = field(default_factory=dict)
    # Per-check lines for "Test connection": [{"key", "title", "status", "detail"}].
    checks: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


# Status of one sandbox on its compute.
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_MISSING = "missing"


@dataclass
class SandboxStatus:
    state: str  # STATUS_RUNNING | STATUS_STOPPED | STATUS_MISSING
    detail: Optional[str] = None


@dataclass
class SandboxStats:
    cpu_percent: Optional[float] = None
    memory_bytes: Optional[int] = None
    memory_limit_bytes: Optional[int] = None
    disk_bytes: Optional[int] = None
    disk_limit_bytes: Optional[int] = None


@dataclass
class EgressRecord:
    domain: str
    decision: str  # "allowed" | "refused"
    count: int = 1
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None


@dataclass
class ReconcileResult:
    """What ``reconcile`` found and repaired."""

    removed: List[str] = field(default_factory=list)   # orphaned resources removed
    repaired: List[str] = field(default_factory=list)  # routes / registrations re-created
    missing: List[str] = field(default_factory=list)   # known sandbox ids not on the compute


@dataclass
class ComputeContext:
    """What a driver is constructed with: one compute's stored settings.

    ``driver_state`` is the driver's own persistent memory (pinned keys,
    detected isolation, ...). The driver mutates it in place and the caller
    stores it back after the operation.
    """

    compute_id: str
    name: str
    # Stable id of this BoW deployment; every resource a driver creates carries it.
    instance_id: str
    config: Dict[str, Any] = field(default_factory=dict)
    credentials: Dict[str, Any] = field(default_factory=dict)
    driver_state: Dict[str, Any] = field(default_factory=dict)
    # Where this BoW instance is reachable (recorded in a compute's owner marker).
    base_url: Optional[str] = None
    # The volumes the operator declared on this compute: [{"name", "source", ...}].
    volumes: List[Dict[str, Any]] = field(default_factory=list)


class SandboxDriver(abc.ABC):
    """One instance per compute. Every method is async and safe to call from any worker."""

    #: The compute type name this driver is registered under.
    type_name: str = ""

    def __init__(self, ctx: ComputeContext):
        self.ctx = ctx

    # ── Describing the compute ──

    @abc.abstractmethod
    def capabilities(self) -> set:
        """What this compute can do, given what is known about it (see ``ALL_CAPABILITIES``)."""

    @abc.abstractmethod
    def prepare_steps(self) -> List[StepSpec]:
        """The steps ``prepare`` runs, in order."""

    @abc.abstractmethod
    async def prepare(self, on_step: Optional[StepCallback] = None, *, take_over: bool = False,
                      images: Sequence[str] = ()) -> List[StepResult]:
        """Prepare the compute to run sandboxes ("Bootstrap"). Safe to run again:
        it skips what is in place and repairs what is missing. Stops at the
        first failed step. ``images`` are extra images to fetch as part of it;
        the service passes none, because preparing a compute does not depend
        on which environments exist."""

    @abc.abstractmethod
    async def check(self) -> ComputeHealth:
        """Read-only health check ("Test connection"). Changes nothing on the compute."""

    # ── Sandbox lifecycle ──

    @abc.abstractmethod
    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        """Create and start a sandbox; returns once ``sandboxd`` in it answers."""

    @abc.abstractmethod
    async def stop(self, handle: SandboxHandle) -> None:
        """Stop a sandbox, keeping its filesystem. Needs ``CAP_STOP_RESUME``."""

    @abc.abstractmethod
    async def start(self, handle: SandboxHandle) -> None:
        """Resume a stopped sandbox; returns once ``sandboxd`` answers again."""

    @abc.abstractmethod
    async def delete(self, handle: SandboxHandle) -> None:
        """Remove a sandbox and everything it owns. Idempotent."""

    @abc.abstractmethod
    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        """Where the sandbox stands on the compute right now."""

    @abc.abstractmethod
    async def stats(self, handles: Sequence[SandboxHandle]) -> Dict[str, SandboxStats]:
        """Live usage per sandbox id. Empty without ``CAP_LIVE_STATS``."""

    @abc.abstractmethod
    def endpoint(self, handle: SandboxHandle) -> Endpoint:
        """How the runtime client reaches ``sandboxd`` in this sandbox. The driver
        leaves ``token`` empty: it never holds a sandbox's token, only its hash."""

    @abc.abstractmethod
    async def egress_log(self, handle: SandboxHandle, since: Optional[datetime] = None) -> List[EgressRecord]:
        """Domains this sandbox reached or was refused since ``since``."""

    # ── Housekeeping ──

    @abc.abstractmethod
    async def reconcile(self, known: Sequence[SandboxHandle]) -> ReconcileResult:
        """Make the compute match ``known``: remove this instance's resources that
        belong to no known sandbox and repair what a known one is missing.
        Never touches another BoW instance's resources."""

    @abc.abstractmethod
    async def cleanup(self) -> None:
        """Remove everything this BoW instance put on the compute (before deleting it)."""

    async def pull_image(self, image: str, *, registry_username: Optional[str] = None,
                         registry_password: Optional[str] = None) -> Optional[str]:
        """Pre-pull an image; returns its digest when the compute can tell. Optional."""
        return None

    async def confirm_identity(self, fingerprint: str) -> None:
        """Trust the compute's identity (e.g. a server's host key) once an admin
        has confirmed the fingerprint ``check`` reported. Optional."""
        raise SandboxDriverError("This compute type has nothing to confirm", code="unsupported")

    async def enroll(self, transient: Mapping[str, Any]) -> None:
        """Use one-time credentials (e.g. a password that installs a key) once.

        ``transient`` is never stored, logged or put in a step result. Called
        when a compute is created or edited with such credentials. Optional.
        """
        return None

    async def close(self) -> None:
        """Release connections held by this driver instance."""
        return None
