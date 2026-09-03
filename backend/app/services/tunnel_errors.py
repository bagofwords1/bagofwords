"""Typed errors for the secure data tunnel transport (design B3).

Shared by the control-plane transport and the client proxies. Kept minimal for
the schema-discovery / query slice; more translations can be added as more
operations are proxied.
"""
from __future__ import annotations


class TunnelError(Exception):
    """Base for tunnel transport failures."""


class TunnelNotConnectedError(TunnelError):
    """No NATS connection is available (tunnel disabled or not yet started)."""


class TunnelOwnershipError(TunnelError):
    """A reply came back stamped with an edge_agent_id other than the expected
    owner of the connection (design A3 runtime guard)."""


class TunnelPayloadTooLarge(TunnelError):
    def __init__(self, size: int, ceiling: int, operation: str):
        super().__init__(
            f"tunnel request for {operation!r} is {size} bytes, over the "
            f"broker ceiling of {ceiling}"
        )
        self.size = size
        self.ceiling = ceiling
        self.operation = operation


class RemoteError(TunnelError):
    """An error the edge agent reported for an operation it ran."""

    def __init__(self, message: str, operation: str, code: int | None = None):
        super().__init__(message)
        self.operation = operation
        self.code = code


def translate_remote_error(error: dict, operation: str) -> Exception:
    """Turn a JSON-RPC error body from the edge agent into an exception."""
    message = error.get("message", "remote error")
    code = error.get("code")
    return RemoteError(message, operation, code)
