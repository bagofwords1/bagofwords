"""Per-member identity from admin-set member attributes ("member_mapped" auth).

Some sources authenticate BOW with one shared, admin-equivalent secret that can
*act as* any named account — a Qlik client certificate plus the account in the
``X-Qlik-User`` header. Under "Require user authentication" each member must
act as their OWN account, and that account must come from something the
member cannot change: otherwise a member types their CEO's user id and reads
the CEO's data.

So the admin binds identity fields of the connection form (Qlik ``user_id``,
``user_directory``) to a member attribute key instead of a fixed value::

    connection.member_bindings = {"user_id": "qlik_user_id"}

At query time the field takes the member's admin-set attribute
(``Membership.custom_attributes`` — only full admins can read or write those,
see services/member_attributes.py). Fields that are not bound keep the value
saved on the connection.

Rules, enforced here once for every query path:

* only under ``auth_policy == "user_required"`` with the ``member_mapped``
  mode — a system_only connection always runs as its saved identity;
* only admin-set attributes count: never the display name or email (both
  member-editable through ``PATCH /users/me``) and never IdP-synced values;
* a member without a value is BLOCKED (403) — never given the connection's
  own identity, and there is no owner/admin fallback;
* no signed-in user (indexing, warm-up) → the connection's saved identity,
  which builds the shared catalog. Callers record a mapped resolve as a
  *user* identity so it never rewrites that shared catalog.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.services.member_attributes import ATTRIBUTE_KEY_RE

MEMBER_MAPPED_MODE = "member_mapped"


def member_mapped(connection: Any) -> bool:
    """True when members of this connection act as an admin-mapped identity."""
    return (
        (getattr(connection, "auth_policy", None) or "system_only") == "user_required"
        and MEMBER_MAPPED_MODE in (getattr(connection, "allowed_user_auth_modes", None) or [])
    )


def bindings_of(connection: Any) -> Dict[str, str]:
    raw = getattr(connection, "member_bindings", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _auth_type_of(connection: Any) -> Optional[str]:
    import json

    config = getattr(connection, "config", None)
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            config = {}
    return (config or {}).get("auth_type") if isinstance(config, dict) else None


def validate_member_bindings(
    ds_type: str, auth_type: Optional[str], raw: Optional[Mapping[str, Any]]
) -> Dict[str, str]:
    """Normalize ``{field: attribute_key}``; 400 on a field the connector does
    not let members bind or a malformed attribute key. Empty values drop the
    binding (the field goes back to its fixed value)."""
    from app.schemas.data_source_registry import member_bindable_fields

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AppError.bad_request(
            ErrorCode.MEMBER_IDENTITY_INVALID,
            "member_bindings must be an object of field -> member attribute.",
        )
    allowed = set(member_bindable_fields(ds_type, auth_type))
    out: Dict[str, str] = {}
    for field, key in raw.items():
        field = str(field or "").strip()
        if field not in allowed:
            raise AppError.bad_request(
                ErrorCode.MEMBER_IDENTITY_INVALID,
                f"'{field}' cannot be mapped to a member attribute on this connection.",
                field=field,
            )
        key = str(key or "").strip()
        if not key:
            continue
        if not ATTRIBUTE_KEY_RE.match(key):
            raise AppError.bad_request(
                ErrorCode.MEMBER_IDENTITY_INVALID,
                f"Invalid member attribute name '{key}'.",
                field=field,
            )
        out[field] = key
    return out


def resolve_bound_values(
    bindings: Mapping[str, str], custom_attributes: Optional[Mapping[str, Any]]
) -> tuple[Dict[str, str], List[str]]:
    """``(values, missing_fields)`` for one member's admin-set attributes."""
    attrs = custom_attributes if isinstance(custom_attributes, Mapping) else {}
    values: Dict[str, str] = {}
    missing: List[str] = []
    for field, key in bindings.items():
        value = attrs.get(key)
        text = str(value).strip() if value is not None else ""
        if text:
            values[field] = text
        else:
            missing.append(field)
    return values, missing


async def _membership_attributes(db: AsyncSession, connection: Any, user: Any) -> Dict[str, Any]:
    from app.models.membership import Membership

    row = (
        await db.execute(
            select(Membership.custom_attributes).where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(connection.organization_id),
                Membership.deleted_at.is_(None),
            )
        )
    ).first()
    attrs = row[0] if row else None
    return attrs if isinstance(attrs, dict) else {}


async def resolve_member_credentials(
    db: AsyncSession, connection: Any, user: Any, bindings: Optional[Mapping[str, str]] = None
) -> dict:
    """The connection's saved credentials with each bound field replaced by
    this member's attribute value. Raises 403 when the connection has no
    bindings or the member lacks a value — never falls back. ``bindings``
    overrides the saved ones (admin test of an unsaved mapping)."""
    bindings = dict(bindings) if bindings is not None else bindings_of(connection)
    if not bindings:
        raise AppError.forbidden(
            ErrorCode.MEMBER_IDENTITY_NOT_CONFIGURED,
            "This connection runs queries as each member's own account, but an admin "
            "has not chosen which member attribute identifies them yet.",
        )
    values, missing = resolve_bound_values(
        bindings, await _membership_attributes(db, connection, user)
    )
    if missing:
        attributes = ", ".join(sorted({bindings[f] for f in missing}))
        raise AppError.forbidden(
            ErrorCode.MEMBER_IDENTITY_UNRESOLVED,
            f"Your account on this connection is not set up yet: an admin needs to set "
            f"the member attribute(s) {attributes} for you.",
            attributes=attributes,
        )
    try:
        base = connection.decrypt_credentials() or {}
    except Exception:
        base = {}
    return {**base, **values}


async def build_member_mapped_status(db: AsyncSession, connection: Any, user: Any, last_checked=None):
    """Per-member status: "user" access when every bound field resolves, else
    "none" with nothing for the member to connect (an admin must act)."""
    from app.schemas.data_source_schema import DataSourceUserStatus

    bindings = bindings_of(connection)
    ok = False
    if bindings:
        _, missing = resolve_bound_values(
            bindings, await _membership_attributes(db, connection, user)
        )
        ok = not missing
    return DataSourceUserStatus(
        has_user_credentials=ok,
        auth_mode=MEMBER_MAPPED_MODE,
        connection="unknown" if ok else "not_connected",
        effective_auth="user" if ok else "none",
        last_checked_at=last_checked,
    )


async def preview_member_identity(
    db: AsyncSession, connection: Any, bindings: Optional[Mapping[str, str]] = None
) -> dict:
    """Who acts as which identity: every member of the org with the values the
    bindings resolve to, or the fields they are missing. Full admins only (it
    reveals member attributes)."""
    from app.models.membership import Membership
    from app.models.user import User

    bindings = dict(bindings) if bindings is not None else bindings_of(connection)
    try:
        saved = connection.decrypt_credentials() or {}
    except Exception:
        saved = {}
    rows = (
        await db.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.organization_id == str(connection.organization_id),
                Membership.deleted_at.is_(None),
            )
            .order_by(User.email)
        )
    ).all()
    fixed = {
        field: (saved.get(field) or None)
        for field in member_identity_fields(connection)
        if field not in bindings
    }
    members = []
    attribute_keys: set[str] = set()
    for membership, user in rows:
        if isinstance(membership.custom_attributes, dict):
            attribute_keys.update(membership.custom_attributes.keys())
        values, missing = resolve_bound_values(bindings, membership.custom_attributes)
        members.append({
            "membership_id": str(membership.id),
            "user_id": str(user.id),
            "email": user.email,
            "name": getattr(user, "name", None),
            "identity": {**fixed, **{field: values.get(field) for field in bindings}},
            "missing": [bindings[f] for f in missing],
            "status": "ok" if bindings and not missing else "blocked",
        })
    ok = sum(1 for m in members if m["status"] == "ok")
    return {
        "bindings": bindings,
        "fields": member_identity_fields(connection),
        "attribute_keys": sorted(attribute_keys),
        "members": members,
        "ok_count": ok,
        "blocked_count": len(members) - ok,
    }


def member_identity_fields(connection: Any) -> List[str]:
    from app.schemas.data_source_registry import member_bindable_fields

    return member_bindable_fields(getattr(connection, "type", ""), _auth_type_of(connection))

