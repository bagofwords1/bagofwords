"""Resolve per-user identity/membership context into MCP requests.

An admin configures an MCP connection to forward the signed-in user's identity
into two places on every tool call:

  * outbound **HTTP headers** (``header_injection`` + static ``headers``), and
  * a **metadata object** merged into the tool arguments
    (``metadata_injection`` → ``custom_metadata`` by default).

Values come from a small *whitelist* of sources — never arbitrary attribute
access — so a misconfigured mapping can't reach a secret:

  * ``user.email`` / ``user.name`` / ``user.id``
  * ``membership.role``
  * ``membership.attr:<key>``   → ``Membership.profile_attributes[<key>]``
  * ``static:<text>``           → a literal, with ``{source}`` interpolation
    (e.g. ``static:elbit_nt\\{membership.attr:employeeId}``)

Each metadata field has a ``mode``:

  * ``locked`` — admin value always wins (clobbers any LLM-provided value) and
    the field is hidden from the model's tool schema.
  * ``ai``     — surfaced to the model as a default; the server only fills it
    when the model left it absent/empty.

and an ``on_missing`` policy for when the source resolves empty: ``empty``
(default, ``""``), ``omit`` (drop the key), or ``block`` (abort the call).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.membership import Membership
from app.models.user import User

DEFAULT_ARGUMENT_KEY = "custom_metadata"

# ---------------------------------------------------------------------------
# Value resolution (pure, no DB)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")


@dataclass
class IdentityContext:
    """The whitelisted values available to source expressions for one user."""

    email: str = ""
    name: str = ""
    user_id: str = ""
    role: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)


def _resolve_atom(expr: str, ctx: IdentityContext) -> Optional[str]:
    """Resolve a single whitelisted source atom to a string, or None if unknown/empty."""
    expr = (expr or "").strip()
    if expr == "user.email":
        return ctx.email or None
    if expr == "user.name":
        return ctx.name or None
    if expr == "user.id":
        return ctx.user_id or None
    if expr == "membership.role":
        return ctx.role or None
    if expr.startswith("membership.attr:"):
        key = expr.split(":", 1)[1].strip()
        val = ctx.attributes.get(key)
        if val is None or val == "":
            return None
        return val if isinstance(val, str) else str(val)
    return None


def resolve_source(source: str, ctx: IdentityContext) -> Optional[str]:
    """Resolve a full ``source`` value. Returns None when nothing resolved.

    ``static:`` values support ``{atom}`` interpolation; a static literal with
    no tokens always resolves (even to an empty string).
    """
    source = (source or "").strip()
    if not source:
        return None
    if source.startswith("static:"):
        literal = source[len("static:"):]
        return _TOKEN_RE.sub(lambda m: _resolve_atom(m.group(1), ctx) or "", literal)
    return _resolve_atom(source, ctx)


# ---------------------------------------------------------------------------
# Resolved plan
# ---------------------------------------------------------------------------


@dataclass
class ResolvedField:
    name: str
    value: str
    mode: str  # "locked" | "ai"


@dataclass
class ResolvedContext:
    headers: Dict[str, str] = field(default_factory=dict)
    argument_key: str = DEFAULT_ARGUMENT_KEY
    fields: List[ResolvedField] = field(default_factory=list)
    blocking_missing: List[str] = field(default_factory=list)

    @property
    def has_metadata(self) -> bool:
        return bool(self.fields)

    @property
    def has_headers(self) -> bool:
        return bool(self.headers)


def _identity_from(user: Optional[User], membership: Optional[Membership]) -> IdentityContext:
    attrs = {}
    role = ""
    if membership is not None:
        attrs = dict(membership.profile_attributes or {})
        role = membership.role or ""
    return IdentityContext(
        email=(getattr(user, "email", "") or "") if user else "",
        name=(getattr(user, "name", "") or "") if user else "",
        user_id=(str(getattr(user, "id", "") or "")) if user else "",
        role=role,
        attributes=attrs,
    )


def build_plan(config: Dict[str, Any], ctx: IdentityContext) -> ResolvedContext:
    """Build the resolved header/metadata plan from a connection config + identity.

    Pure function (no DB) so it is trivially unit-testable.
    """
    resolved = ResolvedContext()

    # --- headers: static first, then dynamic injection (dynamic wins) ---
    static_headers = config.get("headers") or {}
    if isinstance(static_headers, dict):
        for k, v in static_headers.items():
            if k and v not in (None, ""):
                resolved.headers[str(k)] = str(v)

    for rule in config.get("header_injection") or []:
        if not isinstance(rule, dict):
            continue
        header = (rule.get("header") or "").strip()
        if not header:
            continue
        value = resolve_source(rule.get("source", ""), ctx)
        if value:  # omit empty headers — an empty header is noise, not context
            resolved.headers[header] = value

    # --- metadata fields ---
    meta = config.get("metadata_injection") or {}
    if isinstance(meta, dict) and meta.get("fields"):
        resolved.argument_key = (meta.get("argument_key") or DEFAULT_ARGUMENT_KEY).strip() or DEFAULT_ARGUMENT_KEY
        for f in meta.get("fields") or []:
            if not isinstance(f, dict):
                continue
            name = (f.get("name") or "").strip()
            if not name:
                continue
            mode = "ai" if f.get("mode") == "ai" else "locked"
            on_missing = f.get("on_missing") or "empty"
            value = resolve_source(f.get("source", ""), ctx)
            if value is None or value == "":
                if on_missing == "omit":
                    continue
                if on_missing == "block":
                    resolved.blocking_missing.append(name)
                    continue
                value = ""  # "empty" (default)
            resolved.fields.append(ResolvedField(name=name, value=value, mode=mode))

    return resolved


def _connection_config(connection: Connection) -> Dict[str, Any]:
    import json
    cfg = connection.config
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    return cfg or {}


async def resolve_mcp_context(
    db: AsyncSession,
    connection: Connection,
    user: Optional[User],
    organization: Any,
) -> ResolvedContext:
    """Load the user's membership and resolve the connection's forwarding plan."""
    config = _connection_config(connection)
    has_spec = bool(
        config.get("headers")
        or config.get("header_injection")
        or (config.get("metadata_injection") or {}).get("fields")
    )
    if not has_spec:
        return ResolvedContext()

    membership: Optional[Membership] = None
    if user is not None and organization is not None:
        result = await db.execute(
            select(Membership).where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(organization.id),
            )
        )
        membership = result.scalars().first()

    ctx = _identity_from(user, membership)
    return build_plan(config, ctx)


def apply_metadata_injection(arguments: Dict[str, Any], plan: ResolvedContext) -> None:
    """Merge resolved metadata fields into ``arguments[argument_key]`` in place.

    ``locked`` fields clobber any model-supplied value; ``ai`` fields only fill
    when the model left the key absent or blank.
    """
    if not plan.has_metadata:
        return
    if not isinstance(arguments, dict):
        return
    bag = arguments.get(plan.argument_key)
    if not isinstance(bag, dict):
        bag = {}
    for rf in plan.fields:
        existing = bag.get(rf.name)
        if rf.mode == "locked":
            bag[rf.name] = rf.value
        else:  # ai: fill only if absent/empty
            if existing in (None, ""):
                bag[rf.name] = rf.value
    arguments[plan.argument_key] = bag


def locked_field_names(config: Dict[str, Any]) -> tuple[str, List[str]]:
    """Return (argument_key, [locked field names]) for schema hiding."""
    meta = (config or {}).get("metadata_injection") or {}
    argument_key = (meta.get("argument_key") or DEFAULT_ARGUMENT_KEY) or DEFAULT_ARGUMENT_KEY
    names = [
        (f.get("name") or "").strip()
        for f in (meta.get("fields") or [])
        if isinstance(f, dict) and f.get("mode") != "ai" and (f.get("name") or "").strip()
    ]
    return argument_key, names


def filter_locked_from_schema(input_schema: Any, config: Dict[str, Any]) -> Any:
    """Strip admin-locked metadata fields from a tool's input schema.

    Locked fields are server-injected and must never be offered to the model,
    so they're removed from ``<argument_key>.properties`` (and its ``required``).
    Returns a new schema object; the input is not mutated.
    """
    argument_key, names = locked_field_names(config)
    if not names or not isinstance(input_schema, dict):
        return input_schema
    import copy
    schema = copy.deepcopy(input_schema)
    props = schema.get("properties")
    if not isinstance(props, dict):
        return schema
    bag = props.get(argument_key)
    if not isinstance(bag, dict) or not isinstance(bag.get("properties"), dict):
        return schema
    for name in names:
        bag["properties"].pop(name, None)
    if isinstance(bag.get("required"), list):
        bag["required"] = [r for r in bag["required"] if r not in names]
    return schema
