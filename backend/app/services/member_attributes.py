"""Admin-set member attributes.

An org admin can attach key/value attributes to a member — at invite time, by
editing the member, or in bulk through the members CSV import. They identify
the member in *other* systems (``qlik_user_id = dana``, ``employee_id =
E1042``), so anything that resolves ``membership.attr:<key>`` — MCP header and
metadata forwarding today — can act as that member there.

Two rules make them safe to rely on:

* only full admins read or write them (``full_admin_access`` — not merely
  ``manage_members``); a member can never see, set or change their own, so
  they cannot claim someone else's identity;
* they live in ``Membership.custom_attributes``, apart from the IdP-synced
  ``profile_attributes`` that the login sync replaces wholesale.

``effective_member_attributes`` is the one place both sets are combined: the
admin's value wins over a synced one with the same key.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

from app.errors import AppError, ErrorCode

# Keys are referenced from mapping rules (membership.attr:<key>) and CSV
# headers (attr.<key>), so keep them identifier-like.
ATTRIBUTE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
MAX_ATTRIBUTES = 50
MAX_VALUE_LENGTH = 256

# CSV/XLSX import column prefix: a column named "attr.qlik_user_id" sets the
# "qlik_user_id" attribute.
IMPORT_COLUMN_PREFIX = "attr."


def normalize_custom_attributes(raw: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """Validate and normalize an attributes dict.

    Keys and values are trimmed; an entry with an empty value is dropped (that
    is how an admin removes one). Raises AppError(400) on a bad key, an
    over-long value, or too many entries.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AppError.bad_request(
            ErrorCode.MEMBER_ATTRIBUTES_INVALID,
            "Member attributes must be an object of key/value pairs.",
        )
    out: Dict[str, str] = {}
    for key, value in raw.items():
        k = str(key or "").strip()
        if not ATTRIBUTE_KEY_RE.match(k):
            raise AppError.bad_request(
                ErrorCode.MEMBER_ATTRIBUTES_INVALID,
                f"Invalid attribute name '{k}'. Use letters, digits, '_', '.', '-' "
                "(starting with a letter, max 64 characters).",
                key=k,
            )
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            raise AppError.bad_request(
                ErrorCode.MEMBER_ATTRIBUTES_INVALID,
                f"Attribute '{k}' must be a single text value.",
                key=k,
            )
        v = str(value).strip()
        if not v:
            continue
        if len(v) > MAX_VALUE_LENGTH:
            raise AppError.bad_request(
                ErrorCode.MEMBER_ATTRIBUTES_INVALID,
                f"Attribute '{k}' is longer than {MAX_VALUE_LENGTH} characters.",
                key=k,
            )
        out[k] = v
    if len(out) > MAX_ATTRIBUTES:
        raise AppError.bad_request(
            ErrorCode.MEMBER_ATTRIBUTES_INVALID,
            f"A member can have at most {MAX_ATTRIBUTES} attributes.",
        )
    return out


def effective_member_attributes(membership: Any) -> Dict[str, Any]:
    """IdP-synced profile attributes overlaid with admin-set ones (admin wins)."""
    if membership is None:
        return {}
    synced = getattr(membership, "profile_attributes", None) or {}
    custom = getattr(membership, "custom_attributes", None) or {}
    merged: Dict[str, Any] = {}
    if isinstance(synced, dict):
        merged.update(synced)
    if isinstance(custom, dict):
        merged.update(custom)
    return merged


async def can_manage_attributes(db: Any, user_id: Any, organization_id: Any) -> bool:
    """Only full admins may read or write member attributes."""
    if not user_id or not organization_id:
        return False
    from app.core.permission_resolver import FULL_ADMIN, resolve_permissions

    resolved = await resolve_permissions(db, str(user_id), str(organization_id))
    return FULL_ADMIN in resolved.org_permissions


async def require_attribute_admin(db: Any, user_id: Any, organization_id: Any) -> None:
    if not await can_manage_attributes(db, user_id, organization_id):
        raise AppError.forbidden(
            ErrorCode.ACCESS_DENIED,
            "Only organization admins can set member attributes.",
        )
