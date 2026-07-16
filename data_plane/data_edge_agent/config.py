"""Configuration for the data edge agent.

Shape comes from a YAML file, secrets come from the environment. The agent runs
in the customer's network and its config file is expected to live in their own
deployment tooling, so nothing here should ever need to hold a password.

`edge_agent_id` is the one field that cannot be generated at first boot: under
v1's static NATS config the admin has to name it in `nats.conf` *before* the
agent can connect, so an id invented on startup would deadlock the bootstrap.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

# Env wins over file: containers set these, and the file is often baked in.
_ENV_PREFIX = "BOW_EDGE_AGENT_"

_ENV_OVERRIDES = {
    "nats_url": "NATS_URL",
    "nats_token": "NATS_TOKEN",
    "org_id": "ORG_ID",
    "edge_agent_id": "EDGE_AGENT_ID",
    "edge_agent_name": "EDGE_AGENT_NAME",
    "admin_port": "ADMIN_PORT",
    "log_level": "LOG_LEVEL",
    "default_query_timeout_seconds": "QUERY_TIMEOUT_SECONDS",
    "index_timeout_seconds": "INDEX_TIMEOUT_SECONDS",
}


class ConnectionConfig(BaseModel):
    """One data source this agent serves.

    `config` is non-secret connection detail (host, port, database); credentials
    are separate so they can be overridden per connection from the environment
    and so the advertisement builder can be certain of what it is *not* sending.
    Nothing in `credentials` ever leaves this process — the whole point of the
    agent is that Bow holds the call, not the login.
    """

    name: str
    type: str
    label: Optional[str] = None

    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] = Field(default_factory=dict)

    # Effective budget for this connection; None inherits the agent default.
    query_timeout_seconds: Optional[int] = None

    def client_params(self) -> dict[str, Any]:
        """Config + credentials, merged for the client constructor."""
        return {**self.config, **self.credentials}

    def advertised(self, default_query_timeout: int) -> dict[str, Any]:
        """What the control plane is told about this connection.

        Built from an explicit allowlist rather than by removing `credentials`
        from a dump: a subtractive version silently starts leaking the moment a
        new secret-bearing field is added to the model.
        """
        return {
            "name": self.name,
            "type": self.type,
            "label": self.label,
            "query_timeout_seconds": self.query_timeout_seconds or default_query_timeout,
        }

    @field_validator("name")
    @classmethod
    def _subject_safe(cls, v: str) -> str:
        # The name becomes a NATS subject token, and NATS splits on ".".
        # A dot here would silently create an extra token and route nowhere.
        if not v or any(c in v for c in ". *>") or v.strip() != v:
            raise ValueError(
                f"connection name {v!r} is not a valid NATS subject token: "
                "no dots, spaces, '*' or '>', and no surrounding whitespace"
            )
        return v


class AgentConfig(BaseModel):
    """Everything the agent needs to start and reach its broker."""

    org_id: str
    edge_agent_id: str
    edge_agent_name: Optional[str] = None

    nats_url: str = "ws://localhost:9443"
    nats_token: Optional[str] = None

    connections: list[ConnectionConfig] = Field(default_factory=list)

    admin_port: int = 9191
    log_level: str = "INFO"

    # Budgets the control plane sizes its own waits against (A5). The query
    # budget is per-connection with this as the default; the index budget is
    # agent-level because get_schemas / warm_all run for minutes and would be
    # aborted by any sane query budget.
    default_query_timeout_seconds: int = 300
    index_timeout_seconds: int = 900

    # How often to re-publish the advertisement. It repeats rather than firing
    # once because NATS core is at-most-once with no persistence: an
    # advertisement published while the control plane is restarting is simply
    # gone, and only the next cycle heals it.
    advertise_interval_seconds: int = 60

    @field_validator("org_id", "edge_agent_id")
    @classmethod
    def _subject_safe(cls, v: str) -> str:
        if not v or any(c in v for c in ". *>") or v.strip() != v:
            raise ValueError(
                f"{v!r} is not a valid NATS subject token: no dots, spaces, "
                "'*' or '>', and no surrounding whitespace"
            )
        return v

    # -- subjects (docs/design/secure-data-tunnel-v4-nats.md, A3) -------------

    @property
    def subject_base(self) -> str:
        return f"tunnel.{self.org_id}.{self.edge_agent_id}"

    def connection_subject(self, connection_name: str) -> str:
        return f"{self.subject_base}.conn.{connection_name}"

    @property
    def control_subject(self) -> str:
        return f"{self.subject_base}.control"

    @property
    def advertisement_subject(self) -> str:
        # Org-scoped, not global: the control plane reads tenancy off the
        # subject because core NATS gives a subscriber no publisher identity.
        return f"tunnel.{self.org_id}.advertisements"


_INT_FIELDS = {
    "admin_port",
    "default_query_timeout_seconds",
    "index_timeout_seconds",
    "advertise_interval_seconds",
}


def _coerce(field: str, raw: str) -> Any:
    return int(raw) if field in _INT_FIELDS else raw


def load_config(path: str | Path | None = None) -> AgentConfig:
    """Load YAML (if present) and apply environment overrides on top."""
    data: dict[str, Any] = {}

    path = path or os.environ.get(f"{_ENV_PREFIX}CONFIG")
    if path:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        data = yaml.safe_load(p.read_text()) or {}

    for field, suffix in _ENV_OVERRIDES.items():
        raw = os.environ.get(f"{_ENV_PREFIX}{suffix}")
        if raw is not None and raw != "":
            data[field] = _coerce(field, raw)

    return AgentConfig(**data)
