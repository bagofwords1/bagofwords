"""Artifact app storage declaration and record DTOs.

An artifact that stores data declares its collections in
``ArtifactVersion.content.storage``; artifacts without it have no storage.
The declaration is strict on purpose (unknown keys rejected, sharing rules
explicit) because it decides who may read and write user data.
"""
import json
import math
import re
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

FieldType = Literal["string", "number", "boolean", "date", "json"]
Scope = Literal["shared", "per_user"]
CreateRule = Literal["members", "owner"]
ModifyRule = Literal["author", "owner"]

COLLECTION_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
FIELD_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
MAX_COLLECTIONS = 20
MAX_FIELDS_PER_COLLECTION = 50
# Size limits in UTF-8 bytes of compact JSON: the non-json part of a record,
# each json field, and the whole record.
MAX_RECORD_BYTES = 65_536
MAX_JSON_FIELD_BYTES = 262_144
MAX_RECORD_TOTAL_BYTES = 262_144
MAX_RECORDS_PER_COLLECTION = 10_000

_COLLECTION_NAME_RE = re.compile(COLLECTION_NAME_PATTERN)
_FIELD_NAME_RE = re.compile(FIELD_NAME_PATTERN)


def is_iso_date(value: Any) -> bool:
    """True for an ISO 8601 date or datetime string."""
    if not isinstance(value, str):
        return False
    for parse in (date.fromisoformat, datetime.fromisoformat):
        try:
            parse(value)
            return True
        except ValueError:
            continue
    return False


def compact_json_bytes(value: Any) -> int:
    """UTF-8 size of ``value`` as compact JSON (the unit of every size limit).

    Raises TypeError/ValueError for values JSON cannot represent.
    """
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(text.encode("utf-8"))


def value_matches_type(field_type: str, value: Any) -> bool:
    """Type check for a non-null field value."""
    if field_type == "string":
        return isinstance(value, str)
    if field_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:  # an int beyond float range
            return False
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "date":
        return is_iso_date(value)
    return True  # json: any JSON value


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: FieldType
    required: bool = False
    default: Any = None
    max_length: Optional[int] = Field(None, ge=1, le=100_000)

    @property
    def has_default(self) -> bool:
        return "default" in self.model_fields_set

    @model_validator(mode="after")
    def _check_consistency(self) -> "FieldSpec":
        if self.max_length is not None and self.type != "string":
            raise ValueError("max_length is only allowed for string fields")
        if self.required and self.has_default and self.default is None:
            raise ValueError("a required field cannot default to null")
        if self.default is not None:
            if not value_matches_type(self.type, self.default):
                raise ValueError(f"default does not match field type {self.type!r}")
            if self.max_length is not None and len(self.default) > self.max_length:
                raise ValueError("default is longer than max_length")
        if self.has_default:
            # A default is returned with every record lacking the field, so it
            # is held to the limits of a stored value.
            limit = MAX_JSON_FIELD_BYTES if self.type == "json" else MAX_RECORD_BYTES
            try:
                size = compact_json_bytes(self.default)
            except (TypeError, ValueError):
                raise ValueError("default is not a JSON value")
            if size > limit:
                raise ValueError(f"default is larger than {limit} bytes")
        return self


class CollectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Scope
    # Only for shared collections; per_user ones are implicitly "self".
    create: Optional[CreateRule] = None
    modify: Optional[ModifyRule] = None
    # Anonymous visitors and signed-in outsiders of a public artifact read the
    # owner's records of this collection. A publication decision of its own,
    # never derived from who may write.
    public_read: StrictBool = False
    fields: Dict[str, FieldSpec] = Field(..., min_length=1, max_length=MAX_FIELDS_PER_COLLECTION)

    @model_validator(mode="before")
    @classmethod
    def _drop_per_user_rules(cls, data: Any) -> Any:
        # create/modify mean nothing for per_user: drop them (whatever their
        # value) instead of rejecting the declaration. Stored declarations
        # that carry them keep parsing.
        if isinstance(data, dict) and data.get("scope") == "per_user" and ("create" in data or "modify" in data):
            return {k: v for k, v in data.items() if k not in ("create", "modify")}
        return data

    @field_validator("fields")
    @classmethod
    def _check_field_names(cls, fields: Dict[str, FieldSpec]) -> Dict[str, FieldSpec]:
        for name in fields:
            if not _FIELD_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid field name {name!r}")
        return fields

    @model_validator(mode="after")
    def _check_shared_rules(self) -> "CollectionSpec":
        if self.scope == "shared" and (self.create is None or self.modify is None):
            raise ValueError("shared collections must set both 'create' and 'modify'")
        if self.public_read and not (self.scope == "shared" and self.create == "owner"):
            raise ValueError(
                "public_read is only allowed for shared collections with create 'owner' "
                "(public link visitors read only records the owner wrote)"
            )
        return self

    @model_validator(mode="after")
    def _check_default_sizes(self) -> "CollectionSpec":
        # The projection of a record with no stored values: its defaults alone
        # must fit the record limits, or every read would exceed them.
        defaults = {name: f.default for name, f in self.fields.items() if f.has_default}
        non_json = {name: v for name, v in defaults.items() if self.fields[name].type != "json"}
        if compact_json_bytes(non_json) > MAX_RECORD_BYTES or compact_json_bytes(defaults) > MAX_RECORD_TOTAL_BYTES:
            raise ValueError("field defaults together exceed the record size limit")
        return self


class StorageDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collections: Dict[str, CollectionSpec] = Field(..., max_length=MAX_COLLECTIONS)

    @field_validator("collections")
    @classmethod
    def _check_collection_names(cls, collections: Dict[str, CollectionSpec]) -> Dict[str, CollectionSpec]:
        for name in collections:
            if not _COLLECTION_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid collection name {name!r}")
        return collections


def parse_storage_declaration(raw: Any) -> Optional[StorageDeclaration]:
    """Parse ``content.storage``. None means no storage; invalid raises ValidationError."""
    if raw is None:
        return None
    return StorageDeclaration.model_validate(raw)


class AppRecordCreate(BaseModel):
    data: Dict[str, Any]


class AppRecordUpdate(BaseModel):
    data: Dict[str, Any]
    version: int = Field(..., ge=1)


class AppRecordDelete(BaseModel):
    version: int = Field(..., ge=1)


class AppRecordUser(BaseModel):
    id: str
    name: Optional[str] = None


class AppRecordOut(BaseModel):
    id: str
    data: Dict[str, Any]
    user: Optional[AppRecordUser]
    version: int
    created_at: datetime
    updated_at: datetime
    mine: bool


class AppRecordList(BaseModel):
    items: List[AppRecordOut]


class AppRecordDeleted(BaseModel):
    id: str
    deleted: bool = True
