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
    SHARING_PUBLIC_DISABLED = "sharing.public_disabled"
    MCP_DYNAMIC_PARAMS_INVALID = "mcp.dynamic_params_invalid"

    # Host applications and embedded artifacts
    HOST_APP_NOT_FOUND = "host_app.not_found"
    HOST_APP_DISABLED = "host_app.disabled"
    HOST_APP_CONFIG_INVALID = "host_app.config_invalid"
    HOST_APP_REVISION_CONFLICT = "host_app.revision_conflict"
    EMBED_NOT_ALLOWED = "embed.not_allowed"
    EMBED_ASSERTION_INVALID = "embed.assertion_invalid"
    EMBED_ASSERTION_REPLAYED = "embed.assertion_replayed"
    EMBED_IDENTITY_UNMAPPED = "embed.identity_unmapped"
    EMBED_IDENTITY_AMBIGUOUS = "embed.identity_ambiguous"
    EMBED_IDENTITY_UNAVAILABLE = "embed.identity_unavailable"
    EMBED_USER_INACTIVE = "embed.user_inactive"
    EMBED_SESSION_INVALID = "embed.session_invalid"
    CONVERSATION_NOT_FOUND = "conversation.not_found"
    CONVERSATION_UNAVAILABLE = "conversation.unavailable"
    CONVERSATION_AGENT_FORBIDDEN = "conversation.agent_forbidden"
    CONVERSATION_LIMIT = "conversation.limit"
    CONVERSATION_NO_AGENTS = "conversation.no_agents"

    # Licensing / features
    FEATURE_LOCKED = "feature.locked"
    ENTERPRISE_REQUIRED = "license.enterprise_required"

    # Audit log streams / export
    AUDIT_STREAM_NOT_FOUND = "audit_stream.not_found"
    AUDIT_STREAM_INVALID_DESTINATION = "audit_stream.invalid_destination"
    AUDIT_STREAM_MISSING_FIELD = "audit_stream.missing_field"
    AUDIT_STREAM_INVALID_FIELD = "audit_stream.invalid_field"
    AUDIT_EXPORT_TOO_LARGE = "audit_log.export_too_large"

    # Resources (common CRUD)
    REPORT_NOT_FOUND = "report.not_found"
    ENTITY_NOT_FOUND = "entity.not_found"
    ARTIFACT_NOT_FOUND = "artifact.not_found"
    DASHBOARD_NOT_PUBLISHED = "dashboard.not_published"
    DASHBOARD_SHARE_REQUIRES_PUBLISH = "dashboard.share_requires_publish"
    # A migrated conversation's old API with no dashboard to act on (phase 9).
    DASHBOARD_CONVERSATION_MOVED = "dashboard.conversation_moved"
    DASHBOARD_RLS_REQUIRES_VIEWER = "dashboard.rls_requires_viewer"
    DASHBOARD_EXPORT_WITHHELD = "dashboard.export_withheld"
    DASHBOARD_EXPORT_NEEDS_RUN = "dashboard.export_needs_run"
    DASHBOARD_EXPORT_UNAVAILABLE = "dashboard.export_unavailable"
    DASHBOARD_PUBLISH_IN_PROGRESS = "dashboard.publish_in_progress"
    DASHBOARD_VERSION_NOT_PUBLISHABLE = "dashboard.version_not_publishable"
    DASHBOARD_PUBLISH_PUBLIC_MONITORING = "dashboard.publish_public_monitoring"
    DASHBOARD_PUBLISH_CONNECTION = "dashboard.publish_connection"
    DASHBOARD_PUBLISH_CHARTS_FAILED = "dashboard.publish_charts_failed"
    DASHBOARD_PUBLISH_CHARTS_RUNNING = "dashboard.publish_charts_running"
    DASHBOARD_EDIT_FIRST = "dashboard.edit_first"
    DASHBOARD_SLUG_INVALID = "dashboard.slug_invalid"
    DASHBOARD_SLUG_TAKEN = "dashboard.slug_taken"
    # Collaborative dashboards (collab01)
    DASHBOARD_CREATOR_PUBLISH_OWNER_ONLY = "dashboard.creator_publish_owner_only"
    DASHBOARD_PUBLISHED_SINCE = "dashboard.published_since"
    DASHBOARD_JOIN_NO_ACCESS = "dashboard.join_no_access"
    DASHBOARD_DRAFT_NAME_TAKEN = "dashboard.draft_name_taken"
    DASHBOARD_RELEASE_NOT_KEPT = "dashboard.release_not_kept"
    DASHBOARD_TRANSFER_NOT_EDITOR = "dashboard.transfer_not_editor"
    DASHBOARD_PUBLIC_OWNER_ONLY = "dashboard.public_owner_only"
    FILE_NOT_FOUND = "file.not_found"
    DATA_SOURCE_NOT_FOUND = "data_source.not_found"
    DATA_SOURCE_IN_USE = "data_source.in_use"
    CONNECTION_NOT_FOUND = "connection.not_found"
    USER_NOT_FOUND = "user.not_found"
    MEMBERSHIP_NOT_FOUND = "membership.not_found"
    ROLE_NOT_FOUND = "role.not_found"
    GROUP_NOT_FOUND = "group.not_found"
    GROUP_MANAGED_BY_SCIM = "group.managed_by_scim"
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
    LLM_MODEL_IS_GROUP_DEFAULT = "llm.model_is_group_default"
    LLM_GROUP_DEFAULT_INVALID = "llm.group_default_invalid"

    # Member attributes
    MEMBER_ATTRIBUTES_INVALID = "member_attributes.invalid"
    # Per-member connection identity (member_mapped auth)
    MEMBER_IDENTITY_INVALID = "member_identity.invalid"
    MEMBER_IDENTITY_NOT_CONFIGURED = "member_identity.not_configured"
    MEMBER_IDENTITY_UNRESOLVED = "member_identity.unresolved"

    # Data execution
    QUERY_TIMEOUT = "query.timeout"
    QUERY_FAILED_SILENTLY = "query.failed_silently"
