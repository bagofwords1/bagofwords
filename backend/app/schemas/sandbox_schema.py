"""API schemas for agent sandboxes.

One rule shapes this file: **a response never carries a secret**. Private
keys, registry passwords, environment secret values and sandbox tokens are
write-only. The response models below are built from the ORM rows by their
``from_model`` constructors, which list every field they copy; nothing is
passed through wholesale, so a new secret column on a model cannot leak by
default.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

NetworkPolicy = Literal["none", "registries", "allowlist", "full"]
SandboxState = Literal["provisioning", "ready", "busy", "idle", "stopped", "failed", "deleted"]
RunStatus = Literal["running", "succeeded", "failed", "timeout", "cancelled", "killed"]

# Defaults from the design ("Defaults" table). Per environment unless noted.
DEFAULT_CPUS = 1.0
DEFAULT_MEMORY_MB = 2048
DEFAULT_DISK_MB = 5120
DEFAULT_RUN_TIMEOUT_SECONDS = 600
MAX_RUN_TIMEOUT_SECONDS = 3600
DEFAULT_COMPUTE_CAPACITY = 20

_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
# An image reference: registry/path:tag or registry/path@sha256:digest. It must
# start with a letter or digit, so it can never be read as a command option.
IMAGE_REFERENCE = r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,499}$"


def valid_image(value: str) -> str:
    import re

    value = str(value).strip()
    if not re.match(IMAGE_REFERENCE, value):
        raise ValueError("is not a valid image reference (for example registry.example.com/team/image:tag)")
    return value


def _clean_labels(value: Optional[List[str]]) -> List[str]:
    seen: List[str] = []
    for item in value or []:
        label = str(item).strip().lower()
        if label and label not in seen:
            seen.append(label)
    return seen


# ── Computes ─────────────────────────────────────────────────────────────


class SandboxComputeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    type: str = Field(min_length=1, max_length=40)
    # Validated against the compute type's own schemas by the driver registry.
    config: Dict[str, Any] = Field(default_factory=dict)
    credentials: Dict[str, Any] = Field(default_factory=dict)
    labels: List[str] = Field(default_factory=list)
    # An upper bound on running sandboxes, on top of the resource rules.
    capacity: int = Field(default=DEFAULT_COMPUTE_CAPACITY, ge=1, le=1000)
    # How much of the compute sandboxes may be given (services/sandbox/capacity.py: Allocation).
    allocation: Dict[str, Any] = Field(default_factory=dict)

    _labels = field_validator("labels")(_clean_labels)


class SandboxComputeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    config: Optional[Dict[str, Any]] = None
    # Only the keys present are replaced; an absent key keeps the stored secret.
    credentials: Optional[Dict[str, Any]] = None
    labels: Optional[List[str]] = None
    capacity: Optional[int] = Field(default=None, ge=1, le=1000)
    # Only the keys present are changed.
    allocation: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None

    @field_validator("labels")
    @classmethod
    def _labels(cls, value):
        return None if value is None else _clean_labels(value)


class BootstrapStep(BaseModel):
    key: str
    title: str
    status: Literal["pending", "running", "ok", "warning", "failed", "skipped"] = "pending"
    detail: Optional[str] = None
    code: Optional[str] = None  # why it failed, e.g. "owned_by_other"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class SandboxComputeSchema(BaseModel):
    id: str
    name: str
    type: str
    config: Dict[str, Any]
    # Names of the credentials that are stored, never their values.
    credentials_set: List[str]
    capabilities: List[str]
    labels: List[str]
    capacity: int
    enabled: bool
    bootstrap_status: str
    bootstrap_steps: List[BootstrapStep]
    bootstrapped_at: Optional[datetime]
    status: str
    last_checked_at: Optional[datetime]
    last_error: Optional[str]
    # Driver facts safe to show (isolation level, user mode, versions). Pinned
    # keys and certificates are fingerprints only.
    info: Dict[str, Any]
    running_sandboxes: int = 0
    # What the compute has ({"cpus", "memory_mb", "disk_mb"}; a missing key is unknown),
    # the admin's allocation policy with its defaults filled in, what sandboxes
    # may be given in total, and what they hold now by their limits.
    resources: Dict[str, float] = {}
    allocation: Dict[str, Any] = {}
    allocatable: Dict[str, float] = {}
    allocated: Dict[str, float] = {}
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_model(cls, compute, *, public_config: Dict[str, Any], info: Dict[str, Any],
                   running_sandboxes: int = 0, allocation: Optional[Dict[str, Any]] = None,
                   allocatable: Optional[Dict[str, float]] = None,
                   allocated: Optional[Dict[str, float]] = None) -> "SandboxComputeSchema":
        """``public_config`` and ``info`` come from the driver registry, which knows
        which config fields and driver-state facts are safe to return."""
        return cls(
            id=compute.id,
            name=compute.name,
            type=compute.type,
            config=public_config,
            credentials_set=sorted(k for k, v in (compute.credentials or {}).items() if v),
            capabilities=list(compute.capabilities or []),
            labels=list(compute.labels or []),
            capacity=compute.capacity,
            enabled=bool(compute.enabled),
            bootstrap_status=compute.bootstrap_status,
            bootstrap_steps=[BootstrapStep(**s) for s in (compute.bootstrap_steps or [])],
            bootstrapped_at=compute.bootstrapped_at,
            status=compute.status,
            last_checked_at=compute.last_checked_at,
            last_error=compute.last_error,
            info=info,
            running_sandboxes=running_sandboxes,
            resources=dict(compute.resources or {}),
            allocation=dict(allocation or {}),
            allocatable=dict(allocatable or {}),
            allocated=dict(allocated or {}),
            created_at=compute.created_at,
            updated_at=compute.updated_at,
        )


# ── Environments ─────────────────────────────────────────────────────────


class _EnvironmentFields(BaseModel):
    description: Optional[str] = Field(default=None, max_length=2000)
    registry_username: Optional[str] = Field(default=None, max_length=200)
    # Hooks: once at creation; on every start (create and resume); before a stop or delete.
    setup_script: Optional[str] = Field(default=None, max_length=20000)
    start_script: Optional[str] = Field(default=None, max_length=20000)
    stop_script: Optional[str] = Field(default=None, max_length=20000)
    allowed_domains: Optional[List[str]] = None
    env_vars: Optional[Dict[str, str]] = None
    compute_labels: Optional[List[str]] = None
    ports: Optional[List[int]] = None

    @field_validator("env_vars")
    @classmethod
    def _env_names(cls, value):
        import re
        for name in (value or {}):
            if not re.match(_ENV_NAME, name):
                raise ValueError(f"'{name}' is not a valid environment variable name")
        return value

    @field_validator("compute_labels")
    @classmethod
    def _labels(cls, value):
        return None if value is None else _clean_labels(value)

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, value):
        if value is None:
            return None
        out: List[str] = []
        for item in value:
            domain = str(item).strip().lower().rstrip(".")
            if not domain:
                continue
            if "/" in domain or ":" in domain or " " in domain:
                raise ValueError(f"'{item}' is not a domain name (no scheme, port or path)")
            if domain not in out:
                out.append(domain)
        return out

    @field_validator("ports")
    @classmethod
    def _ports(cls, value):
        for port in value or []:
            if not 1024 <= int(port) <= 65535:
                raise ValueError("ports must be between 1024 and 65535")
        return value


class SandboxEnvironmentCreate(_EnvironmentFields):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    image: str = Field(min_length=1, max_length=500)
    registry_password: Optional[str] = None
    cpus: float = Field(default=DEFAULT_CPUS, gt=0, le=64)
    memory_mb: int = Field(default=DEFAULT_MEMORY_MB, ge=128, le=262144)
    disk_mb: int = Field(default=DEFAULT_DISK_MB, ge=256, le=1048576)
    run_timeout_seconds: int = Field(default=DEFAULT_RUN_TIMEOUT_SECONDS, ge=10, le=MAX_RUN_TIMEOUT_SECONDS)
    network_policy: NetworkPolicy = "none"
    secrets: Optional[Dict[str, str]] = None
    require_strong_isolation: bool = False
    is_default: bool = False
    enabled: bool = True

    @field_validator("secrets")
    @classmethod
    def _secret_names(cls, value):
        return _EnvironmentFields._env_names(value)

    _image = field_validator("image")(valid_image)


class SandboxEnvironmentUpdate(_EnvironmentFields):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    image: Optional[str] = Field(default=None, min_length=1, max_length=500)
    # ``""`` clears the stored password; absent keeps it.
    registry_password: Optional[str] = None
    cpus: Optional[float] = Field(default=None, gt=0, le=64)
    memory_mb: Optional[int] = Field(default=None, ge=128, le=262144)
    disk_mb: Optional[int] = Field(default=None, ge=256, le=1048576)
    run_timeout_seconds: Optional[int] = Field(default=None, ge=10, le=MAX_RUN_TIMEOUT_SECONDS)
    network_policy: Optional[NetworkPolicy] = None
    # Per name: a value sets it, ``None`` removes it; names not listed are kept.
    secrets: Optional[Dict[str, Optional[str]]] = None
    require_strong_isolation: Optional[bool] = None
    is_default: Optional[bool] = None
    enabled: Optional[bool] = None

    @field_validator("secrets")
    @classmethod
    def _secret_names(cls, value):
        return _EnvironmentFields._env_names(value)

    @field_validator("image")
    @classmethod
    def _image(cls, value):
        return None if value is None else valid_image(value)


class SandboxEnvironmentSchema(BaseModel):
    id: str
    name: str
    description: Optional[str]
    image: str
    image_digest: Optional[str]
    registry_username: Optional[str]
    has_registry_password: bool
    setup_script: Optional[str]
    start_script: Optional[str] = None
    stop_script: Optional[str] = None
    # Creations that failed in a row because a hook failed, and the last error.
    hook_failures: int = 0
    hook_last_error: Optional[str] = None
    cpus: float
    memory_mb: int
    disk_mb: int
    run_timeout_seconds: int
    network_policy: str
    allowed_domains: List[str]
    env_vars: Dict[str, str]
    # Names only. Values are never returned.
    secret_names: List[str]
    compute_labels: List[str]
    require_strong_isolation: bool
    ports: List[int]
    is_default: bool
    enabled: bool
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_model(cls, env) -> "SandboxEnvironmentSchema":
        return cls(
            id=env.id,
            name=env.name,
            description=env.description,
            image=env.image,
            image_digest=env.image_digest,
            registry_username=env.registry_username,
            has_registry_password=bool(env.registry_password),
            setup_script=env.setup_script,
            start_script=env.start_script,
            stop_script=env.stop_script,
            hook_failures=int(env.hook_failures or 0),
            hook_last_error=env.hook_last_error,
            cpus=env.cpus,
            memory_mb=env.memory_mb,
            disk_mb=env.disk_mb,
            run_timeout_seconds=env.run_timeout_seconds,
            network_policy=env.network_policy,
            allowed_domains=list(env.allowed_domains or []),
            env_vars=dict(env.env_vars or {}),
            secret_names=sorted((env.secrets or {}).keys()),
            compute_labels=list(env.compute_labels or []),
            require_strong_isolation=bool(env.require_strong_isolation),
            ports=list(env.ports or []),
            is_default=bool(env.is_default),
            enabled=bool(env.enabled),
            created_at=env.created_at,
            updated_at=env.updated_at,
        )


class SandboxEnvironmentSummary(BaseModel):
    """What the agent and non-admin users see of an environment they may use."""

    id: str
    name: str
    description: Optional[str]
    network_policy: str
    cpus: float
    memory_mb: int
    disk_mb: int
    run_timeout_seconds: int
    is_default: bool

    @classmethod
    def from_model(cls, env) -> "SandboxEnvironmentSummary":
        return cls(
            id=env.id, name=env.name, description=env.description, network_policy=env.network_policy,
            cpus=env.cpus, memory_mb=env.memory_mb, disk_mb=env.disk_mb,
            run_timeout_seconds=env.run_timeout_seconds, is_default=bool(env.is_default),
        )


class EnvironmentAccessGrant(BaseModel):
    """Who may use an environment: one row of the per-environment ``use`` grant."""

    principal_type: Literal["user", "group", "role"]
    principal_id: str


class EnvironmentAccessUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grants: List[EnvironmentAccessGrant]


# ── Sandboxes, runs, events ──────────────────────────────────────────────


class SandboxSchema(BaseModel):
    id: str
    report_id: str
    user_id: Optional[str]
    compute_id: Optional[str]
    environment_id: Optional[str]
    image_digest: Optional[str]
    state: str
    limits: Dict[str, Any]
    last_seen_at: Optional[datetime]
    last_used_at: Optional[datetime]
    stopped_at: Optional[datetime]
    expires_at: Optional[datetime]
    error: Optional[str]
    created_at: Optional[datetime]

    @classmethod
    def from_model(cls, sandbox) -> "SandboxSchema":
        # Deliberately no `token` and no `driver_ref`.
        return cls(
            id=sandbox.id,
            report_id=sandbox.report_id,
            user_id=sandbox.user_id,
            compute_id=sandbox.compute_id,
            environment_id=sandbox.environment_id,
            image_digest=sandbox.image_digest,
            state=sandbox.state,
            limits=dict(sandbox.limits or {}),
            last_seen_at=sandbox.last_seen_at,
            last_used_at=sandbox.last_used_at,
            stopped_at=sandbox.stopped_at,
            expires_at=sandbox.expires_at,
            error=sandbox.error,
            created_at=sandbox.created_at,
        )


class SandboxRunSchema(BaseModel):
    id: str
    sandbox_id: str
    report_id: str
    user_id: Optional[str]
    completion_id: Optional[str]
    language: Optional[str]
    command: Optional[List[str]]
    # ``None`` when the viewer lacks ``view_code``.
    code: Optional[str]
    code_sha256: Optional[str]
    status: str
    exit_code: Optional[int]
    stdout: Optional[str]
    stderr: Optional[str]
    input_file_ids: List[str]
    artifact_file_ids: List[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]

    @classmethod
    def from_model(cls, run, *, include_code: bool, include_output: bool = True) -> "SandboxRunSchema":
        return cls(
            id=run.id,
            sandbox_id=run.sandbox_id,
            report_id=run.report_id,
            user_id=run.user_id,
            completion_id=run.completion_id,
            language=run.language,
            command=list(run.command) if (include_code and run.command) else None,
            code=run.code if include_code else None,
            code_sha256=run.code_sha256,
            status=run.status,
            exit_code=run.exit_code,
            stdout=run.stdout if include_output else None,
            stderr=run.stderr if include_output else None,
            input_file_ids=list(run.input_file_ids or []),
            artifact_file_ids=list(run.artifact_file_ids or []),
            started_at=run.started_at,
            finished_at=run.finished_at,
        )


class SandboxEventSchema(BaseModel):
    id: str
    sandbox_id: str
    type: str
    actor_user_id: Optional[str]
    details: Dict[str, Any]
    created_at: Optional[datetime]

    @classmethod
    def from_model(cls, event) -> "SandboxEventSchema":
        return cls(
            id=event.id, sandbox_id=event.sandbox_id, type=event.type,
            actor_user_id=event.actor_user_id, details=dict(event.details or {}), created_at=event.created_at,
        )


class SandboxEgressSchema(BaseModel):
    domain: str
    decision: str
    count: int
    first_at: Optional[datetime]
    last_at: Optional[datetime]

    @classmethod
    def from_model(cls, row) -> "SandboxEgressSchema":
        return cls(domain=row.domain, decision=row.decision, count=row.count, first_at=row.first_at, last_at=row.last_at)


class SandboxUsage(BaseModel):
    """Live usage of one sandbox, where the compute can report it."""
    cpu_percent: Optional[float] = None
    memory_bytes: Optional[int] = None
    memory_limit_bytes: Optional[int] = None
    disk_bytes: Optional[int] = None
    disk_limit_bytes: Optional[int] = None


class SandboxFleetRow(SandboxSchema):
    """One sandbox as the admin's Sandboxes list shows it: the row itself plus
    the names an admin recognises it by, and what it is doing right now."""
    report_title: Optional[str] = None
    user_name: Optional[str] = None
    compute_name: Optional[str] = None
    environment_name: Optional[str] = None
    # The run in progress, if any (never its code or output).
    running_run_id: Optional[str] = None
    running_since: Optional[datetime] = None
    usage: Optional[SandboxUsage] = None
    # True when the environment's image or setup script changed after this
    # sandbox was created: it was set up by the old one, and stays that way
    # until it is deleted and created again.
    setup_outdated: bool = False


class SandboxDetail(SandboxFleetRow):
    runs: List[SandboxRunSchema] = []
    events: List[SandboxEventSchema] = []
