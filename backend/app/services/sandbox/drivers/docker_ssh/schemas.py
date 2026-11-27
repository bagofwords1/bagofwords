"""The ``docker-ssh`` backend form. The admin UI renders it from these schemas."""
from __future__ import annotations

import ipaddress
import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}=?$")


def _address(value: str) -> str:
    value = str(value).strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if not _HOSTNAME.match(value):
        raise ValueError("must be a hostname or an IP address")
    return value.lower()


class DockerSSHConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(title="Address", description="Hostname or IP address of the server.")
    ssh_port: int = Field(default=22, ge=1, le=65535, title="SSH port")
    ssh_user: str = Field(
        default="root", title="SSH user",
        description="root, or a user with passwordless sudo. Every command then runs through sudo -n.",
    )
    ssh_host_key_fingerprint: Optional[str] = Field(
        default=None, title="SSH host key fingerprint",
        description="Optional. SHA256 fingerprint of the server's host key. When empty, Test connection shows "
                    "the fingerprint for you to confirm once; it is pinned after that.",
    )
    ssh_public_key: Optional[str] = Field(
        default=None, title="SSH public key",
        description="The public key BoW connects with. Install it in the SSH user's authorized_keys.",
        json_schema_extra={"readOnly": True},
    )
    sandbox_port: int = Field(default=8443, ge=1, le=65535, title="Sandbox port",
                              description="The one port the server publishes for sandbox traffic.")
    bow_address: Optional[str] = Field(
        default=None, title="BoW address",
        description="Optional. When set, only this address may connect to the sandbox port.",
    )
    images_preloaded: bool = Field(
        default=False, title="Images are preloaded (air-gapped)",
        description="Do not pull images during bootstrap; expect them to be loaded on the server already.",
    )

    _address = field_validator("address")(_address)

    @field_validator("bow_address")
    @classmethod
    def _bow_address(cls, value):
        if value in (None, ""):
            return None
        try:
            return str(ipaddress.ip_network(str(value).strip(), strict=False)) if "/" in str(value) else str(ipaddress.ip_address(str(value).strip()))
        except ValueError as exc:
            raise ValueError("must be an IP address or a CIDR range") from exc

    @field_validator("ssh_user")
    @classmethod
    def _user(cls, value):
        value = str(value).strip()
        if not _USER.match(value):
            raise ValueError("is not a valid user name")
        return value

    @field_validator("ssh_host_key_fingerprint")
    @classmethod
    def _fingerprint(cls, value):
        if value in (None, ""):
            return None
        value = str(value).strip()
        if not _FINGERPRINT.match(value):
            raise ValueError("must look like SHA256:<base64>")
        return value.rstrip("=")


class DockerSSHCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ssh_private_key: Optional[str] = Field(
        default=None, title="SSH private key",
        description="Paste a private key, or leave empty and BoW generates a key pair and shows you the public key. "
                    "The private key is encrypted at rest and never shown again.",
        json_schema_extra={"secret": True, "multiline": True},
    )
    one_time_password: Optional[str] = Field(
        default=None, title="One-time password",
        description="Optional. BoW logs in with it once to install its public key, then discards it. It is never stored.",
        json_schema_extra={"secret": True, "transient": True},
    )
