"""Versioned, closed contracts for artifact-owned resources (not UI versions)."""

import json
import math
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RuleScope(StrictModel):
    audience: Literal["owner", "authenticated", "public", "groups", "none"] = "owner"
    group_ids: list[str] = Field(default_factory=list, max_length=32)
    own: bool = False
    equals: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=8)
    fields: list[str] | None = Field(default=None, max_length=40)

    @model_validator(mode="before")
    @classmethod
    def empty_legacy_alternatives(cls, value):
        # Earlier preview definitions emitted empty any_of on every leaf.
        if cls is RuleScope and isinstance(value, dict) and value.get("any_of") == []:
            return {k: v for k, v in value.items() if k != "any_of"}
        return value

    @model_validator(mode="after")
    def valid_scope(self):
        if self.audience == "groups" and not self.group_ids:
            raise ValueError("Group rules require group IDs")
        if self.audience != "groups" and self.group_ids:
            raise ValueError("Group IDs require group audience")
        return self


class Rule(RuleScope):
    # The public JSON schema is deliberately flat, not recursively self-referencing.
    any_of: list[RuleScope] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def valid(self):
        if self.any_of and (
            self.audience != "owner" or self.group_ids or self.own or self.equals or self.fields is not None
        ):
            raise ValueError("any_of contains flat rules and cannot combine with other rule fields")
        return self


class Permissions(StrictModel):
    read: Rule = Field(default_factory=Rule)
    create: Rule = Field(default_factory=Rule)
    update: Rule = Field(default_factory=Rule)
    delete: Rule = Field(default_factory=Rule)

    @model_validator(mode="after")
    def no_public_writes(self):
        for op in ("create", "update", "delete"):
            if any(rule.audience == "public" for rule in (getattr(self, op).any_of or [getattr(self, op)])):
                raise ValueError("Anonymous mutations are not supported")
        return self


class FieldDefinition(StrictModel):
    type: Literal["string", "number", "boolean", "file"]
    required: bool = False
    default: Any = None
    enum: list[str] | None = Field(default=None, max_length=100)
    max_length: int = Field(default=10000, ge=1, le=200000)
    indexed: bool = False
    unique: bool = False
    # A field with a privileged transition (e.g. publication) is checked on
    # create as well as update. The configured default is the initial safe value.
    write: Rule | None = None


class ResourceDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    kind: Literal["collection", "files", "ai"] = "collection"
    fields: dict[str, FieldDefinition] = Field(default_factory=dict, max_length=40)
    permissions: Permissions = Field(default_factory=Permissions)
    prompt: str | None = Field(default=None, max_length=12000)
    model_id: str | None = Field(
        default=None,
        max_length=64,
        description="Approved model ID. Omit on create to pin the current report/user default; omission on update preserves its model.",
    )
    file_resource: str | None = Field(default=None, max_length=63)
    max_records: int = Field(default=10000, ge=1, le=10000)
    max_bytes: int = Field(default=67108864, ge=1, le=268435456)

    @model_validator(mode="after")
    def valid(self):
        import re

        reserved = {"id", "revision", "createdAt", "updatedAt", "owner_id", "organization_id"}
        for name, field in self.fields.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) or name in reserved:
                raise ValueError("Invalid field name")
            if field.unique and not field.indexed:
                raise ValueError("Unique fields must be indexed")
            if field.default is not None:
                validate_value(field, field.default)
        if self.kind != "collection" and self.fields:
            raise ValueError("Only collections have fields")
        if self.kind == "ai" and not self.prompt:
            raise ValueError("AI operations require a prompt")
        rules = [getattr(self.permissions, op) for op in ("read", "create", "update", "delete")]
        rules += [field.write for field in self.fields.values() if field.write]
        for rule in [leaf for r in rules for leaf in (r.any_of or [r])]:
            for key in rule.equals:
                if key not in self.fields or not self.fields[key].indexed:
                    raise ValueError("Row conditions require indexed fields")
                validate_value(self.fields[key], rule.equals[key])
            if rule.fields is not None and not set(rule.fields) <= set(self.fields):
                raise ValueError("Unknown permitted field")
        return self


def validate_value(field: FieldDefinition, value):
    if value is None:
        if field.required:
            raise ValueError("Required field is null")
        return
    if field.type in ("string", "file"):
        if not isinstance(value, str) or len(value) > field.max_length:
            raise ValueError("Invalid text field")
        if field.enum is not None and value not in field.enum:
            raise ValueError("Invalid enum value")
    elif field.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Invalid number")
    elif type(value) is not bool:
        raise ValueError("Invalid boolean")


def validate_record(definition: ResourceDefinition, data: dict) -> dict:
    if not isinstance(data, dict) or not set(data) <= set(definition.fields):
        raise ValueError("Unknown record fields")
    result = dict(data)
    for name, field in definition.fields.items():
        if name not in result and field.default is not None:
            result[name] = field.default
        if name not in result and field.required:
            raise ValueError("Missing required field")
        if name in result:
            validate_value(field, result[name])
    if len(json.dumps(result, allow_nan=False).encode()) > 262144:
        raise ValueError("Record exceeds 256 KiB")
    return result


class ResourceChange(StrictModel):
    action: Literal["create", "update", "delete"]
    resource: str | None = None
    definition: ResourceDefinition | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=8, max_length=100)


class RecordRequest(StrictModel):
    action: Literal["list", "get", "create", "update", "delete"]
    id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    filter: dict[str, Any] = Field(default_factory=dict, max_length=8)
    order_by: Literal["id", "-id", "created_at", "-created_at"] = "-created_at"
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4000)
    expected_revision: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action != "list" and (self.filter or self.cursor):
            raise ValueError("Filters and cursors are only supported on list")
        if self.action in ("get", "update", "delete") and not self.id:
            raise ValueError("Record ID is required")
        if self.action in ("update", "delete") and self.expected_revision is None:
            raise ValueError("Expected revision is required")
        if self.action in ("create", "update", "delete") and not self.idempotency_key:
            raise ValueError("Idempotency key is required")
        return self
