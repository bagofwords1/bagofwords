"""Per-user identity mapping for connections.

Some sources authenticate BOW with one shared, powerful credential and let it
*act as* a named account per request: a Qlik client certificate plus an
``X-Qlik-User`` header, a Postgres login user plus ``SET ROLE``. Which account
a member acts as must come from something the member cannot change — otherwise
a member types their CEO's user name and reads their data.

The connection stores ``identity_mapping`` = ``{slot_param: source}``. Slots are
declared per connector type in the registry (``IdentitySlot``); sources use the
same whitelist as MCP header forwarding (services/mcp_context_injection.py):

* ``user.email`` / ``user.name`` / ``user.id`` — from the signed-in session
* ``membership.role``
* ``membership.attr:<key>`` — an admin-set member attribute (or IdP-synced one)
* ``static:<text>`` — a literal, with ``{atom}`` interpolation
  (``static:CORP``, ``static:{membership.attr:pg_role}_ro``)

Rules enforced here, once, for every connector:

* resolution happens in the credential resolvers, so every query path (agent,
  tools, previews, per-user catalog discovery) gets the mapped identity;
* a mapped slot that resolves empty BLOCKS the call — it never falls back to
  the connection's own (usually all-seeing) identity;
* no signed-in user (indexing, warm-up) → the connection's own identity, which
  is what builds the shared catalog;
* a resolve that applied a mapping is a *user* identity, so it never rewrites
  the shared catalog (``ConnectionService.last_credential_identity``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import AppError, ErrorCode
from app.services.mcp_context_injection import IdentityContext, identity_context_for
from app.services.member_attributes import ATTRIBUTE_KEY_RE

_SIMPLE_ATOMS = {"user.email", "user.name", "user.id", "membership.role"}
_ATTR_PREFIX = "membership.attr:"
_STATIC_PREFIX = "static:"
_TOKEN_RE = re.compile(r"\{([^{}]+)\}")
MAX_SOURCE_LENGTH = 256


# ---------------------------------------------------------------------------
# Source expressions
# ---------------------------------------------------------------------------


def _atom_error(atom: str) -> Optional[str]:
    """None when ``atom`` is a valid whitelisted source atom, else a reason."""
    atom = (atom or "").strip()
    if atom in _SIMPLE_ATOMS:
        return None
    if atom.startswith(_ATTR_PREFIX):
        key = atom[len(_ATTR_PREFIX):].strip()
        if ATTRIBUTE_KEY_RE.match(key):
            return None
        return f"invalid attribute name '{key}'"
    return f"unknown source '{atom}'"


def source_error(source: Any) -> Optional[str]:
    """None when ``source`` is a valid mapping source, else a human reason."""
    if not isinstance(source, str) or not source.strip():
        return "source is empty"
    source = source.strip()
    if len(source) > MAX_SOURCE_LENGTH:
        return f"source is longer than {MAX_SOURCE_LENGTH} characters"
    if source.startswith(_STATIC_PREFIX):
        literal = source[len(_STATIC_PREFIX):]
        if not literal.strip():
            return "static value is empty"
        for token in _TOKEN_RE.findall(literal):
            err = _atom_error(token)
            if err:
                return err
        return None
    return _atom_error(source)


def _resolve_atom(atom: str, ctx: IdentityContext) -> Optional[str]:
    atom = atom.strip()
    if atom == "user.email":
        value: Any = ctx.email
    elif atom == "user.name":
        value = ctx.name
    elif atom == "user.id":
        value = ctx.user_id
    elif atom == "membership.role":
        value = ctx.role
    elif atom.startswith(_ATTR_PREFIX):
        value = (ctx.attributes or {}).get(atom[len(_ATTR_PREFIX):].strip())
    else:
        return None
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text or None


def resolve_source_strict(source: str, ctx: IdentityContext) -> Optional[str]:
    """Resolve a source; None when it — or ANY atom interpolated into a static
    value — is empty. Unlike MCP header forwarding (where an empty header is
    merely dropped), an identity built from a partly-empty template would name
    a different, possibly real, account."""
    source = (source or "").strip()
    if source.startswith(_STATIC_PREFIX):
        literal = source[len(_STATIC_PREFIX):]
        missing = False

        def _sub(match: "re.Match[str]") -> str:
            nonlocal missing
            value = _resolve_atom(match.group(1), ctx)
            if value is None:
                missing = True
                return ""
            return value

        out = _TOKEN_RE.sub(_sub, literal).strip()
        if missing or not out:
            return None
        return out
    return _resolve_atom(source, ctx)


# ---------------------------------------------------------------------------
# Mapping validation
# ---------------------------------------------------------------------------


def _slots_by_param(conn_type: str) -> Dict[str, Any]:
    from app.schemas.data_source_registry import identity_slots_for
    return {slot.param: slot for slot in identity_slots_for(conn_type)}


def validate_identity_mapping(conn_type: str, mapping: Optional[Mapping[str, Any]]) -> Optional[Dict[str, str]]:
    """Normalize an identity mapping for a connector type.

    Returns None for "off" (None / {} / every source blank). Raises
    AppError(400) when the type takes no identity, a slot is unknown, a source
    is not whitelisted, or a required slot is left unmapped while others are
    mapped (half an identity is not an identity).
    """
    if mapping is None:
        return None
    if not isinstance(mapping, Mapping):
        raise AppError.bad_request(
            ErrorCode.IDENTITY_MAPPING_INVALID,
            "Identity mapping must be an object of slot → source.",
        )
    cleaned = {
        str(k).strip(): (v.strip() if isinstance(v, str) else v)
        for k, v in mapping.items()
        if not (v is None or (isinstance(v, str) and not v.strip()))
    }
    if not cleaned:
        return None
    slots = _slots_by_param(conn_type)
    if not slots:
        raise AppError.bad_request(
            ErrorCode.IDENTITY_MAPPING_INVALID,
            f"'{conn_type}' connections do not support a per-user identity.",
            params={"type": conn_type},
        )
    for param, source in cleaned.items():
        if param not in slots:
            raise AppError.bad_request(
                ErrorCode.IDENTITY_MAPPING_INVALID,
                f"Unknown identity slot '{param}' for '{conn_type}'. "
                f"Expected one of: {', '.join(sorted(slots))}.",
                params={"slot": param},
            )
        err = source_error(source)
        if err:
            raise AppError.bad_request(
                ErrorCode.IDENTITY_MAPPING_INVALID,
                f"Invalid source for '{slots[param].title}': {err}.",
                params={"slot": param, "reason": err},
            )
    missing_required = [p for p, slot in slots.items() if slot.required and p not in cleaned]
    if missing_required:
        titles = ", ".join(slots[p].title for p in missing_required)
        raise AppError.bad_request(
            ErrorCode.IDENTITY_MAPPING_INVALID,
            f"Map every identity field, or none: missing {titles}.",
            params={"missing": missing_required},
        )
    return cleaned


def identity_mapping_of(connection: Any) -> Dict[str, str]:
    raw = getattr(connection, "identity_mapping", None)
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, str) and v.strip()}


def identity_mapping_active(connection: Any) -> bool:
    """True when this connection pins a per-user identity."""
    return bool(identity_mapping_of(connection)) and bool(
        _slots_by_param(getattr(connection, "type", "") or "")
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def _membership_for(db: AsyncSession, user: Any, organization_id: str):
    from app.models.membership import Membership

    result = await db.execute(
        select(Membership).where(
            Membership.user_id == str(user.id),
            Membership.organization_id == str(organization_id),
            Membership.deleted_at.is_(None),
        )
    )
    return result.scalars().first()


def resolve_with_context(mapping: Mapping[str, str], ctx: IdentityContext) -> tuple[Dict[str, str], List[str]]:
    """(resolved values, params that resolved empty)."""
    values: Dict[str, str] = {}
    missing: List[str] = []
    for param, source in mapping.items():
        value = resolve_source_strict(source, ctx)
        if value is None:
            missing.append(param)
        else:
            values[param] = value
    return values, missing


def needs_identity_mapping(connection: Any) -> bool:
    """A per-user (``user_required``) connection whose type offers members no
    way to bring their own credentials, only identity slots — e.g. a Qlik
    on-prem connection from before members' self-typed identities were
    retired. Its members must get their identity from a mapping; without one
    there is nothing safe to run as."""
    from app.schemas.data_source_registry import supports_user_auth

    conn_type = getattr(connection, "type", "") or ""
    return (
        getattr(connection, "auth_policy", None) == "user_required"
        and bool(_slots_by_param(conn_type))
        and not supports_user_auth(conn_type)
    )


async def resolve_identity_params(db: AsyncSession, connection: Any, user: Any) -> Optional[Dict[str, str]]:
    """Client params pinning ``user``'s identity on ``connection``.

    None when the connection has no mapping or there is no signed-in user.
    Raises AppError(403) when any mapped slot resolves empty for this user,
    or when the connection needs a mapping and has none.
    """
    if user is None:
        return None
    if not identity_mapping_active(connection):
        if needs_identity_mapping(connection):
            raise AppError.forbidden(
                ErrorCode.IDENTITY_MAPPING_UNRESOLVED,
                (
                    f"Connection '{connection.name}' needs a per-member identity, and members can "
                    "no longer enter their own. Ask an admin to turn on Per-member identity in "
                    "Manage connection and set each member's value in Settings → Members."
                ),
                params={"connection": connection.name, "fields": "", "sources": ""},
            )
        return None
    mapping = identity_mapping_of(connection)
    membership = await _membership_for(db, user, str(connection.organization_id))
    ctx = identity_context_for(user, membership)
    values, missing = resolve_with_context(mapping, ctx)
    if missing:
        slots = _slots_by_param(connection.type)
        titles = ", ".join(slots[p].title if p in slots else p for p in missing)
        sources = ", ".join(mapping[p] for p in missing)
        raise AppError.forbidden(
            ErrorCode.IDENTITY_MAPPING_UNRESOLVED,
            (
                f"Connection '{connection.name}' runs queries as your own account, but your "
                f"member profile has no value for {titles} ({sources}). Ask an admin to set "
                "it in Settings → Members."
            ),
            params={"connection": connection.name, "fields": titles, "sources": sources},
        )
    return values


# ---------------------------------------------------------------------------
# Admin preview
# ---------------------------------------------------------------------------


@dataclass
class IdentityPreviewRow:
    membership_id: str
    email: Optional[str]
    name: Optional[str]
    pending: bool
    values: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)


async def preview_identity_mapping(
    db: AsyncSession, connection: Any, mapping: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    """Resolve ``mapping`` (default: the saved one) for every member of the
    connection's org, so an admin sees who would act as whom — and who would
    be blocked — before saving."""
    from app.models.membership import Membership

    mapping = dict(mapping if mapping is not None else identity_mapping_of(connection))
    result = await db.execute(
        select(Membership)
        .options(selectinload(Membership.user))
        .where(
            Membership.organization_id == str(connection.organization_id),
            Membership.deleted_at.is_(None),
        )
    )
    rows: List[IdentityPreviewRow] = []
    attribute_keys: set = set()
    for membership in result.scalars().all():
        user = membership.user
        ctx = identity_context_for(user, membership)
        if user is None:
            ctx.email = (membership.email or "").strip()
        attribute_keys.update((ctx.attributes or {}).keys())
        values, missing = resolve_with_context(mapping, ctx) if mapping else ({}, [])
        rows.append(IdentityPreviewRow(
            membership_id=str(membership.id),
            email=(getattr(user, "email", None) or membership.email),
            name=getattr(user, "name", None),
            pending=user is None,
            values=values,
            missing=missing,
        ))
    rows.sort(key=lambda r: (not r.missing, (r.email or "").lower()))
    return {
        "mapping": mapping,
        "attribute_keys": sorted(attribute_keys),
        "members": [r.__dict__ for r in rows],
        "resolved_count": sum(1 for r in rows if mapping and not r.missing),
        "blocked_count": sum(1 for r in rows if r.missing),
    }


async def test_identity_for_member(
    db: AsyncSession,
    connection: Any,
    membership_id: str,
    mapping: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Connect as one member's mapped identity and report the outcome.

    Lets an admin prove a mapping works for a member (the role exists, the
    Qlik account is known) before that member hits it in a chat.
    """
    from app.models.membership import Membership
    from app.services.connection_service import ConnectionService

    mapping = dict(mapping if mapping is not None else identity_mapping_of(connection))
    if not mapping:
        raise AppError.bad_request(
            ErrorCode.IDENTITY_MAPPING_INVALID,
            "This connection has no identity mapping to test.",
        )
    result = await db.execute(
        select(Membership)
        .options(selectinload(Membership.user))
        .where(
            Membership.id == str(membership_id),
            Membership.organization_id == str(connection.organization_id),
            Membership.deleted_at.is_(None),
        )
    )
    membership = result.scalars().first()
    if membership is None:
        raise AppError.not_found(ErrorCode.MEMBERSHIP_NOT_FOUND, "Member not found.")
    ctx = identity_context_for(membership.user, membership)
    if membership.user is None:
        ctx.email = (membership.email or "").strip()
    values, missing = resolve_with_context(mapping, ctx)
    if missing:
        return {"success": False, "values": values, "missing": missing,
                "message": "This member has no value for: " + ", ".join(missing)}
    try:
        client = await ConnectionService().construct_client(
            db, connection, None, credential_overrides=values,
        )
        status = await client.atest_connection()
    except Exception as e:  # surfaced to the admin as the test outcome
        return {"success": False, "values": values, "missing": [], "message": str(e)}
    if isinstance(status, dict):
        return {"success": bool(status.get("success")), "values": values, "missing": [],
                "message": status.get("message") or ""}
    return {"success": bool(status), "values": values, "missing": [], "message": ""}
