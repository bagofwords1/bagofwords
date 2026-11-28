"""Agent sandboxes: data model.

Eight tables, all new (migration ``agentsbx01`` adds tables and alters none):

``sandbox_computes``        a place sandboxes run (one Linux server for ``docker-ssh``)
``sandbox_environments``    admin-defined templates a sandbox is created from
``sandbox_agent_defaults``  an agent's (data source's) default environment
``sandboxes``               one per report
``sandbox_runs``            every run: who, code, exit code, output, artifacts
``sandbox_events``          the timeline shown on the sandbox page
``sandbox_egress_log``      domains a sandbox reached or was refused
``sandbox_exposures``       reserved for hosted apps; created empty

Secrets are encrypted at rest, always: SSH private keys, registry passwords,
environment secrets, sandbox tokens, and the code and output of runs. That is
not the enterprise ``data_encryption`` feature (which is licence-gated); these
columns use Fernet with the deployment's ``encryption_key`` unconditionally,
the way LLM provider credentials do.

References to rows outside these tables (reports, users, completions, files,
data sources) are plain indexed ids, not foreign keys: a sandbox must never
block or cascade into deleting a report or a user, and the migration must not
touch existing tables.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from cryptography.fernet import Fernet
from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, text,
)
from sqlalchemy.types import TypeDecorator

from app.models.base import BaseSchema
from app.settings.config import settings


# ── Always-on encrypted column types ─────────────────────────────────────


def _fernet() -> Fernet:
    return Fernet(settings.bow_config.encryption_key)


class SecretText(TypeDecorator):
    """A text column stored as a Fernet token. ``None`` and ``""`` stay as they are."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Optional[str], dialect):
        if value is None or value == "":
            return value
        return _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")

    def process_result_value(self, value: Optional[str], dialect):
        if value is None or value == "":
            return value
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")


class SecretJSON(TypeDecorator):
    """A JSON-serialisable value stored as one Fernet token (not a JSON column:
    neither the keys nor the values are readable in the database)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect):
        if value is None:
            return None
        return _fernet().encrypt(json.dumps(value).encode("utf-8")).decode("ascii")

    def process_result_value(self, value: Optional[str], dialect):
        if value is None or value == "":
            return None
        return json.loads(_fernet().decrypt(value.encode("ascii")).decode("utf-8"))


# ── Enumerations (plain strings in the database) ─────────────────────────

BOOTSTRAP_STATUSES = ("none", "running", "ready", "failed")
COMPUTE_STATUSES = ("unknown", "healthy", "unreachable")
NETWORK_POLICIES = ("none", "registries", "allowlist", "full")
SANDBOX_STATES = ("provisioning", "ready", "busy", "idle", "stopped", "failed", "deleted")
RUN_STATUSES = ("running", "succeeded", "failed", "timeout", "cancelled", "killed")
EGRESS_DECISIONS = ("allowed", "refused")

_LIVE = text("deleted_at IS NULL")


class SandboxCompute(BaseSchema):
    """A place sandboxes run. Deployment-wide; one row is one Linux server for ``docker-ssh``."""

    __tablename__ = "sandbox_computes"
    __table_args__ = (
        Index("uq_sandbox_computes_live_name", "organization_id", "name", unique=True,
              sqlite_where=_LIVE, postgresql_where=_LIVE),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    # Compute type name; resolved to a driver by the driver registry and nowhere else.
    type = Column(String(40), nullable=False)
    # Validated by the type's config schema. docker-ssh: address, ssh_port,
    # ssh_user, ssh_public_key, sandbox_port, bow_address.
    config = Column(JSON, nullable=False, default=dict)
    # Validated by the type's credentials schema. docker-ssh: ssh_private_key.
    credentials = Column(SecretJSON, nullable=True)
    # Owned by the driver, opaque above it. docker-ssh: pinned SSH host key,
    # pinned Caddy CA, docker version, isolation, user mode.
    driver_state = Column(JSON, nullable=False, default=dict)
    # What the compute has in total, as its driver last reported it:
    # {"cpus", "memory_mb", "disk_mb"}. Empty until the first check.
    resources = Column(JSON, nullable=False, default=dict)
    # How much of it sandboxes may be given (see services/sandbox/capacity.py).
    allocation = Column(JSON, nullable=False, default=dict)
    # Cached from the driver: stop_resume, strong_isolation, domain_allowlist, ...
    capabilities = Column(JSON, nullable=False, default=list)
    labels = Column(JSON, nullable=False, default=list)
    # Storage the operator makes available to sandboxes on this compute:
    # [{"name", "source", "description"}]. ``source`` is the driver's (a path
    # on a server). An environment mounts a volume by name.
    volumes = Column(JSON, nullable=False, default=list)
    capacity = Column(Integer, nullable=False, default=20)
    enabled = Column(Boolean, nullable=False, default=False)
    bootstrap_status = Column(String(20), nullable=False, default="none")
    bootstrap_steps = Column(JSON, nullable=False, default=list)
    bootstrapped_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="unknown")
    last_checked_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_by = Column(String(36), nullable=True)


class SandboxEnvironment(BaseSchema):
    """An admin-defined template every sandbox is created from."""

    __tablename__ = "sandbox_environments"
    __table_args__ = (
        Index("uq_sandbox_environments_live_name", "organization_id", "name", unique=True,
              sqlite_where=_LIVE, postgresql_where=_LIVE),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    image = Column(String(500), nullable=False)
    # The digest the image resolved to when the environment was saved.
    image_digest = Column(String(200), nullable=True)
    registry_username = Column(String(200), nullable=True)
    registry_password = Column(SecretText, nullable=True)
    # Hooks. ``setup_script`` runs once, when a sandbox is created;
    # ``start_script`` on every start (create and resume), to rebuild what a
    # stop loses (processes, memory, /tmp); ``stop_script`` before a sandbox
    # is stopped or deleted, best effort.
    setup_script = Column(Text, nullable=True)
    start_script = Column(Text, nullable=True)
    stop_script = Column(Text, nullable=True)
    # Creations that failed in a row because a hook failed. At the cap new
    # sandboxes are refused until the environment is changed.
    hook_failures = Column(Integer, nullable=False, default=0)
    hook_last_error = Column(Text, nullable=True)
    cpus = Column(Float, nullable=False, default=1.0)
    memory_mb = Column(Integer, nullable=False, default=2048)
    disk_mb = Column(Integer, nullable=False, default=5120)
    run_timeout_seconds = Column(Integer, nullable=False, default=600)
    network_policy = Column(String(20), nullable=False, default="none")
    allowed_domains = Column(JSON, nullable=False, default=list)
    env_vars = Column(JSON, nullable=False, default=dict)
    # {NAME: value}; injected as environment variables, never returned by the API.
    secrets = Column(SecretJSON, nullable=True)
    # When a run asks the person in the chat before it starts: "never",
    # "always", "full_network" (only if this environment has full internet
    # access), "packages" (only when the run installs packages).
    confirm_runs = Column(String(20), nullable=False, default="never")
    compute_labels = Column(JSON, nullable=False, default=list)
    # Volumes to mount: [{"volume", "path", "read_only", "shared"}]. A compute
    # must have every volume named here to run this environment.
    mounts = Column(JSON, nullable=False, default=list)
    require_strong_isolation = Column(Boolean, nullable=False, default=False)
    # Ports an app in the sandbox may serve. Empty for now; reserved for hosted apps.
    ports = Column(JSON, nullable=False, default=list)
    # Reserved for the warm pool (phase 3).
    warm_pool_size = Column(Integer, nullable=False, default=0)
    is_default = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
    created_by = Column(String(36), nullable=True)


class SandboxAgentDefault(BaseSchema):
    """An agent's default environment. A table of its own so the data source model stays untouched."""

    __tablename__ = "sandbox_agent_defaults"
    __table_args__ = (
        Index("uq_sandbox_agent_defaults_live_data_source", "data_source_id", unique=True,
              sqlite_where=_LIVE, postgresql_where=_LIVE),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    data_source_id = Column(String(36), nullable=False)
    environment_id = Column(String(36), ForeignKey("sandbox_environments.id"), nullable=False, index=True)


class Sandbox(BaseSchema):
    """One persistent sandbox per report, shared by everyone who can work in it.

    ``deleted_at`` is set when ``state`` becomes ``deleted``; the partial
    unique index is what enforces "one non-deleted sandbox per report".
    """

    __tablename__ = "sandboxes"
    __table_args__ = (
        Index("uq_sandboxes_one_live_per_report", "report_id", unique=True,
              sqlite_where=_LIVE, postgresql_where=_LIVE),
        Index("ix_sandboxes_compute_state", "compute_id", "state"),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    report_id = Column(String(36), nullable=False, index=True)
    # The user whose action created it. Each later action is attributed separately.
    user_id = Column(String(36), nullable=True, index=True)
    compute_id = Column(String(36), ForeignKey("sandbox_computes.id"), nullable=True, index=True)
    environment_id = Column(String(36), ForeignKey("sandbox_environments.id"), nullable=True, index=True)
    image_digest = Column(String(200), nullable=True)
    # Opaque outside the driver. docker-ssh: container name and IP.
    driver_ref = Column(JSON, nullable=True)
    state = Column(String(20), nullable=False, default="provisioning", index=True)
    # The sandboxd bearer token. The container only ever receives its SHA-256.
    token = Column(SecretText, nullable=True)
    # The limits it was created with (cpus, memory_mb, disk_mb, run_timeout_seconds, network_policy).
    limits = Column(JSON, nullable=False, default=dict)
    last_seen_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    # Kept by an admin: the retention and lifetime deletions skip it.
    pinned = Column(Boolean, nullable=False, default=False)
    # Not stopped for being idle before this moment.
    keep_warm_until = Column(DateTime, nullable=True)
    # When "this sandbox is about to be deleted" was last recorded, and for which moment.
    delete_warned_for = Column(DateTime, nullable=True)
    # The last snapshot of its workspace: {"path", "bytes", "at"}.
    snapshot = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)


class SandboxRun(BaseSchema):
    """One ``run_in_sandbox`` call. The open-source record of what ran."""

    __tablename__ = "sandbox_runs"
    __table_args__ = (
        Index("ix_sandbox_runs_sandbox_started", "sandbox_id", "started_at"),
    )

    sandbox_id = Column(String(36), ForeignKey("sandboxes.id"), nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    report_id = Column(String(36), nullable=False, index=True)
    # The user who triggered this run (the report owner for scheduled runs).
    user_id = Column(String(36), nullable=True, index=True)
    completion_id = Column(String(36), nullable=True)
    tool_execution_id = Column(String(36), nullable=True)
    language = Column(String(20), nullable=True)
    command = Column(JSON, nullable=True)
    code = Column(SecretText, nullable=True)
    code_sha256 = Column(String(64), nullable=True)
    status = Column(String(20), nullable=False, default="running")
    exit_code = Column(Integer, nullable=True)
    # Capped, appended in batches while the run is in progress.
    stdout = Column(SecretText, nullable=True)
    stderr = Column(SecretText, nullable=True)
    input_file_ids = Column(JSON, nullable=False, default=list)
    artifact_file_ids = Column(JSON, nullable=False, default=list)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class SandboxEvent(BaseSchema):
    """The sandbox timeline: created, setup script, packages, runs, idle, stopped, resumed, ..."""

    __tablename__ = "sandbox_events"
    __table_args__ = (
        Index("ix_sandbox_events_sandbox_created", "sandbox_id", "created_at"),
    )

    sandbox_id = Column(String(36), ForeignKey("sandboxes.id"), nullable=False)
    type = Column(String(60), nullable=False)
    actor_user_id = Column(String(36), nullable=True)
    details = Column(JSON, nullable=False, default=dict)


class SandboxEgressLog(BaseSchema):
    """Domains a sandbox reached or was refused, aggregated per domain and decision."""

    __tablename__ = "sandbox_egress_log"
    __table_args__ = (
        Index("uq_sandbox_egress_log_domain", "sandbox_id", "domain", "decision", unique=True),
    )

    sandbox_id = Column(String(36), ForeignKey("sandboxes.id"), nullable=False)
    domain = Column(String(255), nullable=False)
    decision = Column(String(10), nullable=False)
    count = Column(Integer, nullable=False, default=0)
    first_at = Column(DateTime, nullable=True)
    last_at = Column(DateTime, nullable=True)


class SandboxExposure(BaseSchema):
    """Reserved for hosted apps (a sandbox port exposed at a sharing level). Created empty; unused in v1."""

    __tablename__ = "sandbox_exposures"

    sandbox_id = Column(String(36), ForeignKey("sandboxes.id"), nullable=False, index=True)
    port = Column(Integer, nullable=False)
    level = Column(String(20), nullable=False)
    slug = Column(String(120), nullable=True, unique=True)
    created_by = Column(String(36), nullable=True)
