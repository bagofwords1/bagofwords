"""The Sandbox settings tab: what an admin with ``manage_sandboxes`` can set.

Stored as a plain dict under ``agent_sandboxes`` in the organization's
settings (see ``feature.ORG_SETTINGS_KEY``); the generic settings writer
refuses that key, so only the sandbox routes change it.

Defaults are the design's starting values. Everything is editable.
"""
from __future__ import annotations

import uuid
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.services.sandbox.feature import ORG_SETTINGS_KEY

DEFAULT_REGISTRY_DOMAINS = [
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "proxy.golang.org",
    "sum.golang.org",
]


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Level 2 of the gate: the organization switch.
    enabled: bool = False
    default_environment_id: Optional[str] = None
    # A running sandbox with no run for this long is shown as idle.
    mark_idle_after_seconds: int = Field(default=5 * 60, ge=30, le=24 * 3600)
    # ... and is stopped after this long (the idle timeout).
    idle_timeout_seconds: int = Field(default=30 * 60, ge=60, le=7 * 24 * 3600)
    # A stopped sandbox is deleted after this long.
    stopped_retention_seconds: int = Field(default=7 * 24 * 3600, ge=60, le=365 * 24 * 3600)
    # Any sandbox is deleted this long after it was created. 0: no maximum lifetime.
    max_lifetime_seconds: int = Field(default=30 * 24 * 3600, ge=0, le=365 * 24 * 3600)
    # How long before a retention or lifetime deletion it is announced (in the
    # sandbox's timeline, to the admin, and to the agent on the next run).
    delete_warning_seconds: int = Field(default=24 * 3600, ge=0, le=30 * 24 * 3600)
    # What the "package registries only" policy allows.
    registry_domains: List[str] = Field(default_factory=lambda: list(DEFAULT_REGISTRY_DOMAINS))
    max_running_per_user: int = Field(default=3, ge=1, le=1000)
    # None: bounded only by the computes' capacity.
    max_running_total: Optional[int] = Field(default=None, ge=1, le=100000)
    # Reserved for hosted apps. Stored, unused in v1.
    apps_domain: Optional[str] = None

    # Not editable: set once by BoW.
    instance_id: Optional[str] = None
    builtin_environment_seeded: bool = False

    @field_validator("max_lifetime_seconds")
    @classmethod
    def _lifetime(cls, value):
        if value and value < 300:
            raise ValueError("must be 0 (no maximum lifetime) or at least 300 seconds")
        return value

    @field_validator("registry_domains")
    @classmethod
    def _domains(cls, value):
        out: List[str] = []
        for item in value or []:
            domain = str(item).strip().lower().rstrip(".")
            if not domain:
                continue
            if "/" in domain or ":" in domain or " " in domain:
                raise ValueError(f"'{item}' is not a domain name (no scheme, port or path)")
            if domain not in out:
                out.append(domain)
        return out


class SandboxSettingsUpdate(BaseModel):
    """What the Settings tab may change. ``instance_id`` and the seed marker are not here."""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    default_environment_id: Optional[str] = None
    mark_idle_after_seconds: Optional[int] = Field(default=None, ge=30, le=24 * 3600)
    idle_timeout_seconds: Optional[int] = Field(default=None, ge=60, le=7 * 24 * 3600)
    stopped_retention_seconds: Optional[int] = Field(default=None, ge=60, le=365 * 24 * 3600)
    max_lifetime_seconds: Optional[int] = Field(default=None, ge=0, le=365 * 24 * 3600)
    delete_warning_seconds: Optional[int] = Field(default=None, ge=0, le=30 * 24 * 3600)
    registry_domains: Optional[List[str]] = None
    max_running_per_user: Optional[int] = Field(default=None, ge=1, le=1000)
    max_running_total: Optional[int] = Field(default=None, ge=1, le=100000)
    apps_domain: Optional[str] = None


async def _settings_row(db: AsyncSession, organization_id: str):
    from app.models.organization_settings import OrganizationSettings

    row = (await db.execute(
        select(OrganizationSettings).where(OrganizationSettings.organization_id == str(organization_id))
    )).scalar_one_or_none()
    if row is None:
        row = OrganizationSettings(organization_id=str(organization_id), config={})
        db.add(row)
        await db.flush()
    return row


async def load_settings(db: AsyncSession, organization_id: str) -> SandboxSettings:
    row = await _settings_row(db, organization_id)
    raw = (row.config or {}).get(ORG_SETTINGS_KEY)
    return SandboxSettings(**(raw if isinstance(raw, dict) else {}))


async def save_settings(db: AsyncSession, organization_id: str, settings: SandboxSettings) -> SandboxSettings:
    row = await _settings_row(db, organization_id)
    config = dict(row.config or {})
    config[ORG_SETTINGS_KEY] = settings.model_dump(mode="json")
    row.config = config
    flag_modified(row, "config")
    await db.flush()
    return settings


async def ensure_instance_id(db: AsyncSession, organization_id: str) -> str:
    """The stable id of this BoW deployment, created once.

    Every resource a driver creates on a compute carries it, and a compute
    remembers which instance owns it, so two BoW instances never clean up each
    other's sandboxes.
    """
    settings = await load_settings(db, organization_id)
    if settings.instance_id:
        return settings.instance_id
    settings.instance_id = uuid.uuid4().hex
    await save_settings(db, organization_id, settings)
    return settings.instance_id
