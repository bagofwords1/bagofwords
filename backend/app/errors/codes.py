"""Machine-readable error codes for client-side localization.

Each code maps to an `errors.<code>` key in the frontend locale catalogs
(see locales/en.json). Keep codes dotted and namespaced by resource so the
frontend catalog stays organized.
"""
from enum import Enum


class ErrorCode(str, Enum):
    # Generic
    VALIDATION = "validation"
    INVALID_JSON = "body.invalid_json"
    INTERNAL = "generic"

    # Authentication / authorization
    UNAUTHORIZED = "unauthorized"
    ACCESS_DENIED = "access.denied"
    API_KEY_INVALID = "api_key.invalid"
    VERIFICATION_INVALID = "verification.invalid"

    # Organization
    ORG_NOT_FOUND = "organization.not_found"
    ORG_HEADER_REQUIRED = "organization.required"
    LOCALE_INVALID = "locale.invalid"
    MCP_DISABLED = "mcp.disabled"

    # Licensing / features
    FEATURE_LOCKED = "feature.locked"
    ENTERPRISE_REQUIRED = "license.enterprise_required"

    # Resources (common CRUD)
    REPORT_NOT_FOUND = "report.not_found"
    ENTITY_NOT_FOUND = "entity.not_found"
    ARTIFACT_NOT_FOUND = "artifact.not_found"
    FILE_NOT_FOUND = "file.not_found"
    DATA_SOURCE_NOT_FOUND = "data_source.not_found"
    CONNECTION_NOT_FOUND = "connection.not_found"
    USER_NOT_FOUND = "user.not_found"
    MEMBERSHIP_NOT_FOUND = "membership.not_found"
    ROLE_NOT_FOUND = "role.not_found"
    GROUP_NOT_FOUND = "group.not_found"
    INSTRUCTION_NOT_FOUND = "instruction.not_found"
    INSTRUCTION_VERSION_NOT_FOUND = "instruction.version_not_found"
    INSTRUCTION_LABEL_NOT_FOUND = "instruction.label_not_found"
    INTEGRATION_NOT_FOUND = "integration.not_found"

    # Conflicts
    RESOURCE_CONFLICT = "resource.conflict"
    DUPLICATE_RESOURCE = "resource.duplicate"

    # Settings
    SETTING_UPDATE_FAILED = "setting.update_failed"
