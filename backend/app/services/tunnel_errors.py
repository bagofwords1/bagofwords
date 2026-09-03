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


class RemoteQueryTimeout(TunnelError):
    """The edge agent reported that a query exceeded its budget."""

    def __init__(self, timeout_s: float, sql: str | None = None):
        super().__init__(f"remote query exceeded {timeout_s}s")
        self.timeout_s = timeout_s
        self.sql = sql


# Edge-agent JSON-RPC code for a query timeout (mirror of tunnel.py _QUERY_TIMEOUT).
_QUERY_TIMEOUT_CODE = -32001


def translate_remote_error(error: dict, operation: str) -> Exception:
    """Turn a JSON-RPC error body from the edge agent into an exception.

    A remote query timeout becomes QueryTimeoutError so the codegen retry loop
    behaves exactly as in direct mode (design B3). The abandoned query is
    genuinely stopped on the edge agent, not left running.
    """
    message = error.get("message", "remote error")
    code = error.get("code")
    data = error.get("data") or {}

    if code == _QUERY_TIMEOUT_CODE or data.get("kind") == "query_timeout":
        try:
            from app.ai.code_execution.code_execution import QueryTimeoutError
            return QueryTimeoutError(int(data.get("timeout_s") or 0), data.get("sql"))
        except Exception:
            # If the concrete class can't be imported here, fall back to the
            # transport-typed timeout rather than a generic error.
            return RemoteQueryTimeout(data.get("timeout_s") or 0, data.get("sql"))

    return RemoteError(message, operation, code)
