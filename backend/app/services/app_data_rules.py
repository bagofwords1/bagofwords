"""Pure Layer-2 rules for artifact app data.

Layer 1 (``report_service._check_visibility``) decides who may open the
artifact at all and always runs first, in the service. The functions here only
narrow that: which principal may read, create, or modify records of one
collection, plus record validation and read-time projection. No I/O.

Principals: ``owner`` is the report owner; ``member`` is an org member or
artifact share recipient; ``outsider`` passed visibility but is neither (for
example a signed-in visitor of a public artifact); ``anonymous`` has no session.
"""
import copy
import json
from typing import Any, Dict, Literal, Optional

from app.schemas.app_storage import (
    MAX_JSON_FIELD_BYTES,
    MAX_RECORD_BYTES,
    MAX_RECORD_TOTAL_BYTES,
    CollectionSpec,
    value_matches_type,
)

Principal = Literal["owner", "member", "outsider", "anonymous"]


class RecordValidationError(Exception):
    def __init__(self, code: Literal["validation", "too_large"], reason: str, field: Optional[str] = None):
        super().__init__(reason if field is None else f"{field}: {reason}")
        self.code = code
        self.reason = reason
        self.field = field


# ---------------------------------------------------------------------------
# Access rules
# ---------------------------------------------------------------------------


def is_publicly_readable(spec: CollectionSpec) -> bool:
    """Published to public-link visitors: an explicit ``public_read`` on a
    shared collection only the owner writes. Who may write never decides it."""
    return spec.scope == "shared" and spec.create == "owner" and spec.public_read


def can_read(spec: CollectionSpec, principal: Principal) -> bool:
    if principal in ("owner", "member"):
        return True
    # Outsiders and anonymous visitors read only what the owner published.
    return is_publicly_readable(spec)


def visible_author_filter(
    spec: CollectionSpec, principal: Principal, *, user_id: Optional[str], owner_id: Optional[str],
) -> Optional[str]:
    """Author id the list must be restricted to, or None for all records.

    per_user: the caller's own rows. Outsiders and anonymous visitors: only
    the report owner's rows, so rows other users wrote (for example under an
    earlier declaration) are never published. Raises ValueError when the id
    to filter by is missing: such a list is never returned unfiltered.
    """
    if spec.scope == "per_user":
        if user_id is None:
            raise ValueError("per_user collections need a user id to filter by")
        return user_id
    if principal in ("outsider", "anonymous"):
        if owner_id is None:
            raise ValueError("public reads need the report owner's id to filter by")
        return owner_id
    return None


def can_create(spec: CollectionSpec, principal: Principal) -> bool:
    if principal == "owner":
        return True
    if principal == "member":
        return spec.scope == "per_user" or spec.create == "members"
    return False


def can_modify(spec: CollectionSpec, principal: Principal, *, user_id: Optional[str], record_user_id: str) -> bool:
    if principal not in ("owner", "member") or user_id is None:
        return False
    is_author = user_id == record_user_id
    if spec.scope == "per_user":
        return is_author
    if principal == "owner":
        return True
    # Members never modify an owner-written collection, even rows they wrote
    # under an earlier declaration.
    return spec.create == "members" and spec.modify == "author" and is_author


# ---------------------------------------------------------------------------
# Validation and projection
# ---------------------------------------------------------------------------


def _json_bytes(value: Any, field: Optional[str]) -> int:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise RecordValidationError("validation", "not_json", field)
    return len(text.encode("utf-8"))


def _check_value(spec: CollectionSpec, name: str, value: Any) -> None:
    field = spec.fields.get(name)
    if field is None:
        raise RecordValidationError("validation", "unknown_field", name)
    if value is None:
        if field.required:
            raise RecordValidationError("validation", "required", name)
        return
    if not value_matches_type(field.type, value):
        raise RecordValidationError("validation", f"expected_{field.type}", name)
    if field.max_length is not None and len(value) > field.max_length:
        raise RecordValidationError("validation", "max_length", name)


def _check_record(spec: CollectionSpec, record: Dict[str, Any]) -> None:
    """Required fields, and size limits on the record to be stored and on
    what reads return for it (declared fields plus defaults)."""
    for name, field in spec.fields.items():
        if field.required and not field.has_default and record.get(name) is None:
            raise RecordValidationError("validation", "required", name)
    _check_sizes(spec, record)
    _check_sizes(spec, project_record_data(spec, record))


def _check_sizes(spec: CollectionSpec, record: Dict[str, Any]) -> None:
    non_json: Dict[str, Any] = {}
    for name, value in record.items():
        field = spec.fields.get(name)
        if field is not None and field.type == "json":
            if _json_bytes(value, name) > MAX_JSON_FIELD_BYTES:
                raise RecordValidationError("too_large", "field_too_large", name)
        else:
            non_json[name] = value
    if _json_bytes(non_json, None) > MAX_RECORD_BYTES:
        raise RecordValidationError("too_large", "record_too_large")
    if _json_bytes(record, None) > MAX_RECORD_TOTAL_BYTES:
        raise RecordValidationError("too_large", "record_too_large")


def _require_object(data: Any) -> None:
    if not isinstance(data, dict):
        raise RecordValidationError("validation", "not_an_object")


def validate_new_record(spec: CollectionSpec, data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a new record; returns the data to store (defaults are not filled in)."""
    _require_object(data)
    for name, value in data.items():
        _check_value(spec, name, value)
    record = dict(data)
    _check_record(spec, record)
    return record


def validate_record_patch(spec: CollectionSpec, stored: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merge ``patch`` onto ``stored``; returns the record to store.

    Only patch keys are type-checked, so stored values of removed or retyped
    fields survive untouched.
    """
    _require_object(patch)
    for name, value in patch.items():
        _check_value(spec, name, value)
    record = {**stored, **patch}
    _check_record(spec, record)
    return record


def project_record_data(spec: CollectionSpec, stored: Dict[str, Any]) -> Dict[str, Any]:
    """Declared fields only, with missing ones filled from declared defaults."""
    out: Dict[str, Any] = {}
    for name, field in spec.fields.items():
        if name in stored:
            out[name] = stored[name]
        elif field.has_default:
            out[name] = copy.deepcopy(field.default)
    return out
