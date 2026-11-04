"""Discover SSH host keys without credentials. Discovery never establishes trust."""

import base64
import hashlib
import struct

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data_sources.clients.remote_process import run_bounded
from app.errors import AppError, ErrorCode
from app.schemas.data_sources.remote_server import RemoteLimits


class SSHHostKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(22, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def hostname(cls, value):
        return RemoteLimits.host_only(value)


def discover_host_keys(target: SSHHostKeyRequest) -> dict:
    from app.core.egress import Policy, check_host

    try:
        # Resolve/validate once, then pin the subprocess to that address. The
        # original hostname is returned only to bind the UI's pending decision.
        address = str(check_host(target.host, Policy.ADMIN)[0])
        _, output, _ = run_bounded(
            ["ssh-keyscan", "-T", "5", "-p", str(target.port), "-t", "ed25519,ecdsa,rsa", address],
            timeout=8,
            limit=32768,
        )
        keys = []
        seen = set()
        for line in output.decode("ascii", "replace").splitlines():
            if line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 3 or parts[1] not in (
                "ssh-ed25519",
                "ssh-rsa",
                "ecdsa-sha2-nistp256",
                "ecdsa-sha2-nistp384",
                "ecdsa-sha2-nistp521",
            ):
                continue
            algorithm, encoded = parts[1:]
            try:
                blob = base64.b64decode(encoded, validate=True)
                size = struct.unpack(">I", blob[:4])[0]
                if blob[4 : 4 + size].decode("ascii") != algorithm:
                    continue
                # Validate the complete public-key wire format, not just base64.
                from cryptography.hazmat.primitives.serialization import load_ssh_public_key

                load_ssh_public_key((algorithm + " " + encoded).encode())
            except (ValueError, UnicodeError, struct.error):
                continue
            key = algorithm + " " + encoded
            if key in seen:
                continue
            seen.add(key)
            fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
            keys.append({"public_key": key, "fingerprint": fingerprint, "algorithm": algorithm})
        if not keys:
            raise ValueError("No usable keys")
        return {"host": target.host, "port": target.port, "keys": keys, "trusted": False}
    except Exception as exc:
        raise AppError.bad_request(
            ErrorCode.SSH_KEY_DISCOVERY_FAILED,
            "Could not fetch the server key. Check the hostname, port and network access, or enter a trusted key under Advanced.",
        ) from exc
