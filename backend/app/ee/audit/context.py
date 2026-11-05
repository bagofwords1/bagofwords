# Audit request/job context (docs/design/audit-event-contract.md, "Correlation")
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
#
# A contextvar carries provenance from the HTTP layer (or a background job)
# down to every audit producer, so services, tools and the permission
# decorator record source context and correlation without being handed the
# request. The ids here are opaque: the request id is validated or minted
# here, the session id is the random ``sid`` claim minted at login (never
# derived from the credential), and the source IP is resolved only through
# the trusted-proxy configuration.

import contextvars
import ipaddress
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_DEFAULT_TRUSTED_PROXIES = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")


@dataclass(frozen=True)
class AuditContext:
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    auth_method: Optional[str] = None
    # Background execution: the job or agent run this work belongs to and the
    # request (if any) that started it.
    parent_id: Optional[str] = None
    execution_id: Optional[str] = None
    source: str = "http"  # http | job | agent | system
    extra: Dict[str, Any] = field(default_factory=dict)

    def with_(self, **changes) -> "AuditContext":
        return replace(self, **changes)


_current: contextvars.ContextVar[Optional[AuditContext]] = contextvars.ContextVar("bow_audit_context", default=None)


def current() -> Optional[AuditContext]:
    return _current.get()


def bind(ctx: Optional[AuditContext]):
    """Set the context for the current task; returns a token for ``unbind``."""
    return _current.set(ctx)


def unbind(token) -> None:
    try:
        _current.reset(token)
    except ValueError:
        _current.set(None)


class scope:
    """``with audit_context.scope(ctx): ...`` for jobs and agent runs."""

    def __init__(self, ctx: Optional[AuditContext]):
        self.ctx = ctx
        self._token = None

    def __enter__(self):
        self._token = bind(self.ctx)
        return self.ctx

    def __exit__(self, *exc):
        unbind(self._token)
        return False


def new_request_id() -> str:
    return uuid.uuid4().hex


def validate_request_id(value: Optional[str]) -> Optional[str]:
    if value and _REQUEST_ID_RE.match(value.strip()):
        return value.strip()
    return None


def _trusted_networks() -> List[ipaddress._BaseNetwork]:
    try:
        from app.settings.config import settings
        configured = settings.bow_config.security.trusted_proxies
    except Exception:
        configured = None
    values = configured if configured is not None else list(_DEFAULT_TRUSTED_PROXIES)
    nets = []
    for v in values:
        try:
            nets.append(ipaddress.ip_network(str(v).strip(), strict=False))
        except ValueError:
            continue
    return nets


def _parse_ip(value: Optional[str]) -> Optional[ipaddress._BaseAddress]:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip().split("%")[0])
    except ValueError:
        return None


def is_trusted_proxy(host: Optional[str]) -> bool:
    ip = _parse_ip(host)
    return ip is not None and any(ip in n for n in _trusted_networks())


def resolve_client_ip(peer: Optional[str], forwarded_for: Optional[str]) -> Optional[str]:
    """The client address according to the trusted-proxy policy.

    ``X-Forwarded-For`` is honoured only when the TCP peer is a configured
    proxy, and then the hops are walked from the right: the first address
    that is not a trusted proxy is the client. A header from an untrusted
    peer is ignored, so a client cannot spoof its own address.
    """
    if not forwarded_for or not is_trusted_proxy(peer):
        return peer or None
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):
        ip = _parse_ip(hop)
        if ip is None:
            return peer  # malformed header: fall back to the peer, never trust garbage
        if not any(ip in n for n in _trusted_networks()):
            return str(ip)
    return str(_parse_ip(hops[0])) if hops and _parse_ip(hops[0]) else peer


def _session_id_from_authorization(header: Optional[str]) -> Optional[str]:
    """The opaque ``sid`` claim of a bearer JWT, without verifying the token.

    Only correlation is read here; authentication happens in the auth
    dependency. A forged ``sid`` can only mislabel the forger's own events.
    """
    if not header or not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    try:
        import jwt as pyjwt
        claims = pyjwt.decode(token, options={"verify_signature": False, "verify_aud": False, "verify_exp": False})
    except Exception:
        return None
    sid = claims.get("sid")
    return str(sid) if isinstance(sid, str) and 8 <= len(sid) <= 64 else None


def from_request(request: Any) -> AuditContext:
    headers = request.headers
    peer = request.client.host if getattr(request, "client", None) else None
    auth = headers.get("Authorization")
    method = None
    if auth:
        method = "bearer" if auth.lower().startswith("bearer ") else "api_key" if auth.lower().startswith("apikey ") else "other"
    elif headers.get("X-API-Key") or headers.get("x-api-key"):
        method = "api_key"
    return AuditContext(
        request_id=validate_request_id(headers.get(REQUEST_ID_HEADER)) or new_request_id(),
        session_id=_session_id_from_authorization(auth),
        ip_address=resolve_client_ip(peer, headers.get("X-Forwarded-For")),
        user_agent=(headers.get("User-Agent") or "")[:500] or None,
        auth_method=method,
        source="http",
    )


def for_job(name: str, *, parent: Optional[AuditContext] = None, execution_id: Optional[str] = None) -> AuditContext:
    """Context for background work; carries the originating request id when known."""
    return AuditContext(
        request_id=new_request_id(),
        session_id=parent.session_id if parent else None,
        parent_id=parent.request_id if parent else None,
        execution_id=execution_id,
        source="job",
        extra={"job": name},
    )


async def middleware(request, call_next):
    """Starlette HTTP middleware: bind the context and echo the request id."""
    ctx = from_request(request)
    token = bind(ctx)
    try:
        response = await call_next(request)
    finally:
        unbind(token)
    try:
        response.headers.setdefault(REQUEST_ID_HEADER, ctx.request_id)
    except Exception:
        pass
    return response
