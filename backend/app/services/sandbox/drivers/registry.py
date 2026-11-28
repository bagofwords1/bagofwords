"""Compute types and their drivers.

The registry is the only place that maps a compute's ``type`` string to code.
Everything above it works through :class:`~.base.SandboxDriver` and the
capabilities a driver declares; nothing else may branch on the type name.

Each type carries its own form: a Pydantic config schema and a credentials
schema, returned to the UI by ``GET /api/sandbox/compute-types`` so the admin
form is rendered from the declaration (the way data source connections work).

Drivers, their schemas and the libraries they need are imported lazily, by
dotted path, the first time a type is actually used. Importing this module
loads none of them.
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

from app.services.sandbox.drivers.base import ComputeContext, SandboxDriver, SandboxDriverError


@dataclass(frozen=True)
class ComputeType:
    name: str
    title: str
    description: str
    # "package.module:Attribute", resolved on first use.
    driver: str
    config_schema: str
    credentials_schema: str
    # Shown only when this returns True (the fake type is for tests and demos).
    available: Any = None


def fake_driver_enabled() -> bool:
    """The ``fake`` compute type runs code on the BoW server itself, with no
    isolation. It exists for the contract suite, end-to-end tests and UI
    development, and is offered only under ``TESTING`` or when
    ``BOW_AGENT_SANDBOXES_FAKE_DRIVER`` is set explicitly. Never in production.
    """
    from app.settings.config import settings

    if getattr(settings, "TESTING", False):
        return True
    return os.getenv("BOW_AGENT_SANDBOXES_FAKE_DRIVER", "").strip().lower() in ("1", "true", "yes", "on")


_TYPES: Dict[str, ComputeType] = {
    "docker-ssh": ComputeType(
        name="docker-ssh",
        title="Linux server (Docker over SSH)",
        description="One Linux server with Docker that BoW reaches over SSH. gVisor is used when the server has it.",
        driver="app.services.sandbox.drivers.docker_ssh.driver:DockerSSHDriver",
        config_schema="app.services.sandbox.drivers.docker_ssh.schemas:DockerSSHConfig",
        credentials_schema="app.services.sandbox.drivers.docker_ssh.schemas:DockerSSHCredentials",
    ),
    "fake": ComputeType(
        name="fake",
        title="Fake (tests and demos only)",
        description="Runs code on the BoW server itself with no isolation. For automated tests and UI development.",
        driver="app.services.sandbox.drivers.fake:FakeDriver",
        config_schema="app.services.sandbox.drivers.fake:FakeConfig",
        credentials_schema="app.services.sandbox.drivers.fake:FakeCredentials",
        available=fake_driver_enabled,
    ),
}


def _load(path: str) -> Any:
    module_name, _, attribute = path.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def _is_available(compute_type: ComputeType) -> bool:
    return compute_type.available is None or bool(compute_type.available())


def compute_types() -> List[ComputeType]:
    """The compute types this deployment offers."""
    return [t for t in _TYPES.values() if _is_available(t)]


def get_compute_type(name: str) -> ComputeType:
    compute_type = _TYPES.get(name)
    if compute_type is None or not _is_available(compute_type):
        raise SandboxDriverError(f"Unknown sandbox compute type: {name}", code="unknown_type")
    return compute_type


def driver_class(name: str) -> Type[SandboxDriver]:
    return _load(get_compute_type(name).driver)


def create_driver(type_name: str, ctx: ComputeContext) -> SandboxDriver:
    """A driver instance for one compute. Cheap: connections open on first use."""
    return driver_class(type_name)(ctx)


def config_schema(name: str) -> Type[BaseModel]:
    return _load(get_compute_type(name).config_schema)


def credentials_schema(name: str) -> Type[BaseModel]:
    return _load(get_compute_type(name).credentials_schema)


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error.get("loc", ()))
        parts.append(f"{location}: {error.get('msg')}" if location else str(error.get("msg")))
    return "; ".join(parts)


def validate_config(name: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate and normalise a compute's ``config`` against its type's schema."""
    try:
        return config_schema(name)(**(config or {})).model_dump(mode="json")
    except ValidationError as exc:
        raise SandboxDriverError(_validation_message(exc), code="invalid_config") from exc


def validate_credentials(name: str, credentials: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate a compute's ``credentials``; empty values are dropped."""
    try:
        cleaned = credentials_schema(name)(**(credentials or {})).model_dump(mode="json")
    except ValidationError as exc:
        # Never echo the input: it holds secrets.
        raise SandboxDriverError("The credentials are not valid for this compute type", code="invalid_credentials") from exc
    return {k: v for k, v in cleaned.items() if v not in (None, "")}


def transient_credential_fields(name: str) -> List[str]:
    """Credential fields that are used once and never stored (e.g. a one-time password)."""
    schema = credentials_schema(name)
    return [
        field_name for field_name, info in schema.model_fields.items()
        if (info.json_schema_extra or {}).get("transient")
    ]


def readonly_config_fields(name: str) -> List[str]:
    """Config fields the driver fills in itself (e.g. a generated public key); input for them is ignored."""
    schema = config_schema(name)
    return [
        field_name for field_name, info in schema.model_fields.items()
        if (info.json_schema_extra or {}).get("readOnly")
    ]


def prepare_credentials(name: str, config: Dict[str, Any], credentials: Dict[str, Any]) -> tuple:
    """Let the driver complete a compute's settings before they are stored
    (e.g. generate a key pair when none was given). Returns ``(config, credentials)``."""
    hook = getattr(driver_class(name), "prepare_credentials", None)
    if hook is None:
        return config, credentials
    return hook(config, credentials)


def is_another_compute(name: str, previous_config: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """Whether a settings change points the compute at a different machine or
    cluster, so that what the driver learned about the old one (pinned
    identity, certificates, "prepared") must be forgotten."""
    hook = getattr(driver_class(name), "is_another_compute", None)
    return bool(hook(previous_config, config)) if hook is not None else False


def describe_type(name: str) -> Dict[str, Any]:
    """What the admin form needs for one type: its schemas and what it can offer."""
    compute_type = get_compute_type(name)
    cls = driver_class(name)
    return {
        "name": compute_type.name,
        "title": compute_type.title,
        "description": compute_type.description,
        "config_schema": config_schema(name).model_json_schema(),
        "credentials_schema": credentials_schema(name).model_json_schema(),
        # The most this type can offer; a given compute may offer less.
        "capabilities": sorted(getattr(cls, "potential_capabilities", frozenset())),
    }


def public_info(name: str, driver_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The driver-state facts that are safe to return to an admin."""
    cls = driver_class(name)
    describe = getattr(cls, "public_info", None)
    return dict(describe(driver_state or {})) if describe else {}
