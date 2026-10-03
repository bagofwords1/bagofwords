"""Machine-readable error codes for client-side localization.

Each code maps to an `errors.<code>` key in the frontend locale catalogs
(see locales/en.json). Keep codes dotted and namespaced by resource so the
frontend catalog stays organized.
"""
from enum import Enum


class ErrorCode(str, Enum):
    SAML_UNAVAILABLE = "saml_unavailable"
    SAML_LOGIN_FAILED = "saml_login_failed"
    ARTIFACT_RESOURCE_VALIDATION = "ARTIFACT_RESOURCE_VALIDATION"
    ARTIFACT_RESOURCE_NOT_FOUND = "ARTIFACT_RESOURCE_NOT_FOUND"
    ARTIFACT_RESOURCE_FORBIDDEN = "ARTIFACT_RESOURCE_FORBIDDEN"
    ARTIFACT_RESOURCE_UNAUTHENTICATED = "ARTIFACT_RESOURCE_UNAUTHENTICATED"
    ARTIFACT_RESOURCE_CONFLICT = "ARTIFACT_RESOURCE_CONFLICT"
    ARTIFACT_RESOURCE_QUOTA_EXCEEDED = "ARTIFACT_RESOURCE_QUOTA_EXCEEDED"
    ARTIFACT_RESOURCE_RATE_LIMITED = "ARTIFACT_RESOURCE_RATE_LIMITED"
    ARTIFACT_RESOURCE_UNAVAILABLE = "ARTIFACT_RESOURCE_UNAVAILABLE"
    ARTIFACT_RESOURCE_INTERRUPTED = "ARTIFACT_RESOURCE_INTERRUPTED"
    ARTIFACT_RESOURCE_ABORTED = "ARTIFACT_RESOURCE_ABORTED"
    ARTIFACT_RESOURCE_UNSUPPORTED_VERSION = "ARTIFACT_RESOURCE_UNSUPPORTED_VERSION"
    # Generic
    VALIDATION = "validation"
    INVALID_JSON = "body.invalid_json"
    INTERNAL = "generic"

    # Authentication / authorization
    UNAUTHORIZED = "unauthorized"
    ACCESS_DENIED = "access.denied"
    ACCOUNT_DISABLED = "account.disabled"
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
    DATA_SOURCE_IN_USE = "data_source.in_use"
    CONNECTION_NOT_FOUND = "connection.not_found"
    USER_NOT_FOUND = "user.not_found"
    MEMBERSHIP_NOT_FOUND = "membership.not_found"
    ROLE_NOT_FOUND = "role.not_found"
    GROUP_NOT_FOUND = "group.not_found"
    INSTRUCTION_NOT_FOUND = "instruction.not_found"
    INSTRUCTION_VERSION_NOT_FOUND = "instruction.version_not_found"
    INSTRUCTION_LABEL_NOT_FOUND = "instruction.label_not_found"
    INSTRUCTION_DIRECTORY_NOT_FOUND = "instruction.directory_not_found"
    INTEGRATION_NOT_FOUND = "integration.not_found"

    # User memory
    MEMORY_DISABLED = "memory.disabled"
    MEMORY_NOT_FOUND = "memory.not_found"
    MEMORY_TEXT_REQUIRED = "memory.text_required"
    MEMORY_TEXT_TOO_LONG = "memory.text_too_long"
    MEMORY_TOO_MANY_TAGS = "memory.too_many_tags"
    MEMORY_TAGS_REQUIRED = "memory.tags_required"
    MEMORY_INVALID_DATE = "memory.invalid_date"
    MEMORY_SENSITIVE = "memory.sensitive"
    MEMORY_LOOKS_LIKE_RULE = "memory.looks_like_rule"
    MEMORY_FULL = "memory.full"
    MEMORY_NOT_ACTIVE = "memory.not_active"

    # Conflicts
    RESOURCE_CONFLICT = "resource.conflict"
    DUPLICATE_RESOURCE = "resource.duplicate"

    # Settings
    SETTING_UPDATE_FAILED = "setting.update_failed"

    # LLM configuration
    LLM_MODEL_ID_REQUIRED = "llm.model_id_required"
    LLM_MODEL_ID_NOT_EDITABLE = "llm.model_id_not_editable"
    LLM_MODEL_ID_DUPLICATE = "llm.model_id_duplicate"

    # Data execution
    QUERY_TIMEOUT = "query.timeout"
    QUERY_FAILED_SILENTLY = "query.failed_silently"
