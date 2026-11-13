"""Binds a connection's stored credentials to the destination they were saved for.

A connection manager without org-level rights may edit a connection, but a
changed destination (host, port, URL, TLS setting, token endpoint, auth type)
needs the secret re-entered in the same request, or an org admin. Every field a
connector's config declares is treated as part of the destination unless it is
listed in ``NON_DESTINATION_CONFIG_KEYS``, so a new connector field is safe by
default. Comparison happens on a normalized view: case, a trailing slash, a
default port or a form sending back a field's default are not changes.
"""
from __future__ import annotations

import base64
import json
import re
import inspect
from functools import lru_cache
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from pydantic_core import PydanticUndefined
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.models.connection import RUNTIME_CONFIG_KEYS, Connection
from app.models.user_connection_credentials import UserConnectionCredentials
from app.models.user_data_source_credentials import UserDataSourceCredentials
from app.schemas.data_source_registry import REGISTRY, resolve_client_class
from app.services.connection_identity import (
    SERVICE_ACCOUNT_MARKER_MODE,
    can_use_system_credentials,
)

# Config fields that select something within the saved destination or tune
# behaviour. Everything else counts as destination.
NON_DESTINATION_CONFIG_KEYS = frozenset({
    # Selection within the destination.
    "additional_catalogs", "api_version", "app_filter", "attachment_tables",
    "boards", "branch", "bucket", "catalog", "classes",
    "cost_type", "cube_list_process", "cube_schema_process", "data_source",
    "database", "dataset", "default_project_id", "display_values", "drive_name", "file_paths",
    "folder_path", "forms", "group_id", "index_pattern", "indexes", "landscapes", "lists",
    "log_group_prefix", "mdx_process", "measure_dimension_pattern",
    "metric_namespaces", "metric_prefix", "namespaces", "notebook_source", "object_types",
    "objects", "olap_database", "ontology", "prefix", "repo",
    "repository", "s3_output_location", "sap_language", "schema", "scope",
    "service_name", "share", "space", "space_filter", "stream_filter",
    "table_filter", "tables", "warehouse", "workgroup", "workspaces",
    # Behaviour, limits and presentation.
    "aggregation_limits_observe_only", "allow_downloads", "allow_gds", "allow_secret_files",
    "allowed_extensions", "allowed_index_patterns", "allowed_operations", "allowed_paths",
    "api_mode", "async_mode", "auth_policy", "allowed_user_auth_modes",
    "block_all_indices_search", "connection_idle_seconds", "csrf_token_flow", "csrf_fetch_path",
    "default_limit", "default_lookback", "default_lookback_hours", "delimiter", "discover_all",
    "discover_crds", "discovery_lookback_hours", "discovery_window_days",
    "discovery_window_hours", "enable_attachments", "enable_shell", "encoding", "endpoints",
    "graph_depth", "has_header", "header_injection", "headers", "history_window_days", "icon",
    "include_advanced", "include_globs", "include_hidden", "include_tags", "include_v1",
    "index_content", "index_mode", "index_routines", "include_system_schemas", "infer_schema_from_data", "inspection_mode",
    # Catalog filters and query tags, checked in their clients: none of them
    # reaches a host, port or URL (KMS ids and limits parse as integers, the
    # Snowflake label is restricted to a plain character set, the Documentum
    # root is a quoted DQL literal).
    "include_offline", "layout_id", "root_folder_id", "max_results",
    "query_tag_enabled", "query_tag_label", "root_path",
    "linux_shell", "log_tail_default", "log_tail_max", "max_aggregation_buckets",
    "max_aggregation_depth", "max_aggregations", "max_calls_per_minute", "max_catalog_objects",
    "max_concurrency", "max_crd_tables", "max_file_mb", "max_file_size_mb", "max_items",
    "max_lines", "max_log_pods", "max_metric_tables", "max_output_bytes", "max_result_size",
    "max_rows", "max_sampled_log_groups", "max_sampled_sourcetypes", "maximum_bytes_billed",
    "api_key_header", "metadata_injection", "operating_system", "poll_interval_sec", "published_only",
    "query_options", "recursive", "request_timeout", "request_timeout_s",
    "require_partition_filter", "results_limit", "retries", "schema_sample_size", "shell_policy",
    "shell_rules", "throttlesize", "timeout", "timeout_sec", "timeout_seconds", "timezone",
    "transport", "use_query_cache", "vf_ids", "walk_concurrency", "writable",
}) | RUNTIME_CONFIG_KEYS

# Credential fields that decide where a secret goes rather than what it is.
CREDENTIAL_DESTINATION_KEYS = (
    "authorize_url", "token_url", "otds_url", "gateway_token_url", "root_ca", "account_id",
    # These choose who the server acts as, or in which tenant, so they compare like a host.
    "role_arn", "kerberos_principal", "principal", "impersonated_user", "trusted_user", "user_directory",
    "user_id", "documentum_login", "tenant_id", "member_cid", "partition", "domain", "workspace_domain",
)

# Values interpolated into a connection string: a delimiter here can move the
# connection without touching a host field.
_CONNECTION_STRING_FIELDS = (
    "database", "schema", "catalog", "service_name", "warehouse", "olap_database", "dataset",
    "share", "bucket",
)
_IDENTITY_FIELDS = ("user", "username")
_CONNECTION_STRING_DELIMITERS = re.compile(r"[/?#@:;&=\\%]")

_HOST_FIELDS = (
    "host", "hostname", "server", "server_hostname", "account", "domain", "controller", "rewrite_worker_host",
)
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

_SECRET_NAME = re.compile(
    r"(secret|password|passphrase|private_key|client_key|keytab|connection_string|credentials_json"
    r"|service_account_json|access_file|(^|_)token$|api_key$)",
    re.IGNORECASE,
)
_NOT_SECRET_NAMES = frozenset({"root_ca", "certificate", "client_cert", "api_key_header"})
# Service-account keys may arrive as the parsed JSON object rather than its text.
_JSON_SECRET_FIELDS = frozenset({"credentials_json", "service_account_json"})

# Variants that use the server's own identity, so there is no secret to re-enter.
_AMBIENT_VARIANTS = frozenset({"default", "aws_default", "aws_role", "arn", "managed_identity", "kerberos", "kerberos_delegated"})


def _as_dict(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _entry(conn_type: Optional[str]):
    return REGISTRY.get(conn_type or "")


def effective_auth_type(conn_type: Optional[str], config: Any, credentials: Any = None) -> Optional[str]:
    """The auth variant in effect. A credential ``auth_type`` wins when it names a variant."""
    entry = _entry(conn_type)
    by_auth = entry.credentials_auth.by_auth if entry else {}
    credential_auth = _as_dict(credentials).get("auth_type")
    if isinstance(credential_auth, str) and credential_auth in by_auth:
        return credential_auth
    default = entry.credentials_auth.default if entry else None
    config_auth = _as_dict(config).get("auth_type")
    return config_auth if isinstance(config_auth, str) and config_auth else default


def _client_config(conn_type: Optional[str], config: dict, credentials: dict) -> dict:
    """``config`` as the client receives it: credentials overlaid on the config fields they name."""
    entry = _entry(conn_type)
    fields = entry.config_schema.model_fields if entry else {}
    merged = dict(config)
    for key, value in credentials.items():
        if value is not None and (key == "auth_type" or key in fields):
            merged[key] = value
    return merged


def _declared_credential_keys(conn_type: Optional[str]) -> set[str]:
    entry = _entry(conn_type)
    keys: set[str] = set()
    if entry:
        for variant in entry.credentials_auth.by_auth.values():
            keys.update(variant.schema.model_fields)
    return keys


def _names_a_location(value: Any) -> bool:
    return isinstance(value, str) and ("://" in value or value.strip().startswith("//"))


@lru_cache(maxsize=None)
def _client_defaults(conn_type: str) -> dict:
    """Constructor defaults: what the client uses when the row omits a field."""
    try:
        parameters = inspect.signature(resolve_client_class(conn_type).__init__).parameters.values()
    except Exception:
        return {}
    return {p.name: p.default for p in parameters if p.default not in (inspect.Parameter.empty, None)}


@lru_cache(maxsize=None)
def _client_parameters(conn_type: str) -> Optional[frozenset]:
    """The client's constructor arguments, or None when it takes anything or cannot be read."""
    try:
        parameters = inspect.signature(resolve_client_class(conn_type).__init__).parameters.values()
    except Exception:
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters):
        return None
    return frozenset(p.name for p in parameters)


def _client_takes(conn_type: str, key: str) -> bool:
    accepted = _client_parameters(conn_type)
    return accepted is None or key in accepted


def _schema_default(entry, key: str) -> Any:
    if entry is None:
        return None
    client_default = _client_defaults(entry.type).get(key)
    if client_default is not None:
        return client_default
    field = entry.config_schema.model_fields.get(key)
    if field is None or field.default is PydanticUndefined:
        return None
    return field.default


def _normalize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return value.strip().lower()
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    userinfo = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
    port_part = f":{port}" if port and port != _DEFAULT_PORTS.get(scheme) else ""
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{userinfo}{host}{port_part}{path}{query}"


def _normalize_hosts(value: str) -> str:
    parts = [p.strip().rstrip("/").rstrip(".").lower() for p in value.split(",") if p.strip()]
    return ",".join(sorted(parts))


def _normalize(key: str, value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if "://" in text:
            return _normalize_url(text)
        if key in _HOST_FIELDS or key.endswith("_host") or key.endswith("hostname"):
            return _normalize_hosts(text)
        if text.isdecimal():
            try:
                return int(text)
            except ValueError:  # past Python's digit limit for conversion
                return text
        if text.lower() in ("true", "false"):
            return text.lower() == "true"
        return text
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str) if value else None
    return value


def _off_host_values(config: dict) -> list[str]:
    """Endpoint paths and headers that would change which host a request reaches."""
    out: list[str] = []
    endpoints = config.get("endpoints")
    if isinstance(endpoints, list):
        for endpoint in endpoints:
            path = str((endpoint or {}).get("path", "/") if isinstance(endpoint, dict) else "/")
            if path and not path.startswith("/"):
                out.append(path.strip())
    csrf_path = config.get("csrf_fetch_path")
    if isinstance(csrf_path, str) and csrf_path.strip() and not csrf_path.strip().startswith("/"):
        out.append(csrf_path.strip())
    headers = config.get("headers")
    if isinstance(headers, dict):
        for name, value in headers.items():
            if str(name).strip().lower() in ("host", "x-forwarded-host", "forwarded"):
                out.append(f"{name}:{value}")
    return sorted(out)


def destination_view(conn_type: Optional[str], config: Any, credentials: Any = None) -> dict:
    """The normalized fields that decide where a connection's credentials go,
    compared on what the client actually receives."""
    credentials = _as_dict(credentials)
    config = _client_config(conn_type, _as_dict(config), credentials)
    entry = _entry(conn_type)
    view: dict[str, Any] = {}
    keys = set(config) | (set(entry.config_schema.model_fields) if entry else set())
    for key in keys:
        if key == "auth_type":
            continue
        value = config.get(key)
        if value is None:
            value = _schema_default(entry, key)
        if key in NON_DESTINATION_CONFIG_KEYS:
            if _names_a_location(value):
                view[f"scope:{key}"] = _normalize(key, value)
            elif key in _CONNECTION_STRING_FIELDS and isinstance(value, str) and _CONNECTION_STRING_DELIMITERS.search(value):
                view[f"scope:{key}"] = value.strip()
            continue
        normalized = _normalize(key, value)
        if normalized is not None:
            view[key] = normalized
    off_host = _off_host_values(config)
    if off_host:
        view["off_host"] = off_host
    auth_type = effective_auth_type(conn_type, config)
    if auth_type:
        view["auth_type"] = auth_type
    if config.get("auth_type") is not None and not isinstance(config["auth_type"], str):
        view["auth_type"] = _normalize("auth_type", config["auth_type"])
    for key in CREDENTIAL_DESTINATION_KEYS:
        normalized = _normalize(key, credentials.get(key))
        if normalized is not None:
            view[f"credential:{key}"] = normalized
    for key in _IDENTITY_FIELDS:
        value = credentials.get(key)
        if isinstance(value, str) and _CONNECTION_STRING_DELIMITERS.search(value):
            view[f"identity:{key}"] = value
    if entry:
        declared = _declared_credential_keys(conn_type) | set(entry.config_schema.model_fields) | {"auth_type"}
        for key, value in credentials.items():
            # A client takes any constructor argument, so an undeclared key may be one.
            if key not in declared and not _blank(value):
                view[f"undeclared:{key}"] = _normalize(key, value)
    return view


def _sanitize_for_audit(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value
    if "://" in text:
        try:
            parts = urlsplit(text)
            host = parts.netloc.rsplit("@", 1)[-1]
            text = f"{parts.scheme}://{host}{parts.path}"
        except ValueError:
            text = text.split("://", 1)[0] + "://"
    elif "@" in text:
        text = text.rsplit("@", 1)[-1]
    return text


def destination_summary(conn_type: Optional[str], config: Any, credentials: Any = None) -> dict:
    """The destination in a form safe for an audit row: no secrets, no userinfo."""
    credentials = _as_dict(credentials)
    config = _client_config(conn_type, _as_dict(config), credentials)
    summary: dict[str, Any] = {}
    for key, value in destination_view(conn_type, config, credentials).items():
        if key.startswith(("scope:", "identity:", "undeclared:")) or key == "off_host":
            continue
        name = key.split(":", 1)[1] if key.startswith("credential:") else key
        raw = credentials.get(name) if key.startswith("credential:") else config.get(name, value)
        if isinstance(raw, (dict, list)) or name == "root_ca":
            continue
        summary[name] = _sanitize_for_audit(value)
    return summary


def _variant(conn_type: Optional[str], auth_type: Optional[str]):
    entry = _entry(conn_type)
    return entry.credentials_auth.by_auth.get(auth_type or "") if entry else None


def _is_secret_field(name: str, field=None) -> bool:
    if name in _NOT_SECRET_NAMES:
        return False
    extra = field.json_schema_extra if field is not None and isinstance(field.json_schema_extra, dict) else {}
    return extra.get("ui:type") in ("password", "textarea") or bool(_SECRET_NAME.search(name))


def secret_field_names(conn_type: Optional[str], auth_type: Optional[str]) -> list[str]:
    variant = _variant(conn_type, auth_type)
    if variant is None:
        return []
    return [name for name, field in variant.schema.model_fields.items() if _is_secret_field(name, field)]


def _stored_secret_keys(conn_type: Optional[str]) -> set[str]:
    entry = _entry(conn_type)
    keys: set[str] = set()
    if entry:
        for auth_type in entry.credentials_auth.by_auth:
            keys.update(secret_field_names(conn_type, auth_type))
    return keys


def _stores_secret(conn_type: Optional[str], credentials: dict) -> bool:
    if _entry(conn_type) is None:
        return any(not _blank(v) for v in credentials.values())
    secret_keys = _stored_secret_keys(conn_type)
    return any(
        not _blank(value) and (key in secret_keys or _is_secret_field(key))
        for key, value in credentials.items()
    )


def merge_config_overrides(saved: Any, overrides: Optional[dict]) -> dict:
    """The effective config of a test: non-empty override values win."""
    merged = dict(_as_dict(saved))
    for key, value in (overrides or {}).items():
        if value is not None and value != "":
            merged[key] = value
    return merged


def _saved_credentials(connection: Connection) -> Optional[dict]:
    try:
        return connection.decrypt_credentials() or {} if connection.credentials else {}
    except Exception:
        return None


def _merged_credentials(saved: dict, request_credentials: Any) -> dict:
    """Blank values keep the saved ones, the same merge a test or a save performs."""
    return {
        **saved,
        **{k: v for k, v in _as_dict(request_credentials).items() if v is not None and v != ""},
    }


def destination_changed(connection: Connection, effective_config: Any, request_credentials: Any) -> bool:
    saved_credentials = _saved_credentials(connection) or {}
    return destination_view(connection.type, connection.config, saved_credentials) != destination_view(
        connection.type, effective_config, _merged_credentials(saved_credentials, request_credentials)
    )


async def _has_stored_member_logins(db: AsyncSession, connection: Connection, user) -> bool:
    """True if members other than ``user`` have a saved login that would follow this connection."""
    rows = (await db.execute(
        select(UserConnectionCredentials).where(
            UserConnectionCredentials.connection_id == str(connection.id),
            UserConnectionCredentials.user_id != str(user.id),
            UserConnectionCredentials.is_active.is_(True),
            UserConnectionCredentials.auth_mode != SERVICE_ACCOUNT_MARKER_MODE,
        )
    )).scalars().all()
    ds_ids = [str(ds.id) for ds in (connection.data_sources or [])]
    if ds_ids:
        rows += (await db.execute(
            select(UserDataSourceCredentials).where(
                UserDataSourceCredentials.data_source_id.in_(ds_ids),
                UserDataSourceCredentials.user_id != str(user.id),
                UserDataSourceCredentials.is_active.is_(True),
            )
        )).scalars().all()
    for row in rows:
        try:
            if any(not _blank(v) for v in (row.decrypt_credentials() or {}).values()):
                return True
        except Exception:
            return True
    return False


def _is_set(value: Any) -> bool:
    return not _blank(value) and value is not False


def _has_fields_outside_variant(conn_type: Optional[str], config: Any, merged: dict, variant) -> bool:
    """True if a set credential or config field is neither the variant's nor the connector's own."""
    entry = _entry(conn_type)
    if entry is None:
        return True
    declared = set(variant.schema.model_fields) if variant else set()
    known_config = set(entry.config_schema.model_fields) | NON_DESTINATION_CONFIG_KEYS | {"auth_type"}

    return any(k != "auth_type" and k not in declared and _is_set(v) for k, v in merged.items()) or any(
        k not in known_config and k not in declared and _is_set(v) and _client_takes(conn_type, k)
        for k, v in _as_dict(config).items()
    )


def credentials_after_auth_switch(connection: Connection, effective_config: Any, request_credentials: Any) -> Optional[dict]:
    """The credentials a same-destination change of auth method leaves behind, or None if this is not one.

    Only the auth method may differ, the new variant must take a secret that is
    not the server's own identity, and every required secret must be in the
    request. The result drops other variants' stored values, so a client that
    prefers whichever credential it finds cannot pick up a stale one.
    """
    saved = _saved_credentials(connection)
    if saved is None:
        return None
    request = _as_dict(request_credentials)
    merged = _merged_credentials(saved, request)
    before = destination_view(connection.type, connection.config, saved)
    after = destination_view(connection.type, effective_config, merged)
    if {k for k in set(before) | set(after) if before.get(k) != after.get(k)} != {"auth_type"}:
        return None
    new_auth = effective_auth_type(connection.type, effective_config, merged)
    variant = _variant(connection.type, new_auth)
    secrets = secret_field_names(connection.type, new_auth)
    if new_auth == effective_auth_type(connection.type, connection.config, saved):
        return None
    if variant is None or new_auth in _AMBIENT_VARIANTS or not secrets:
        return None
    fields = variant.schema.model_fields
    if any(k != "auth_type" and k not in fields and _is_set(v) for k, v in request.items()):
        return None
    if any(fields[name].is_required() and _blank(request.get(name)) for name in secrets):
        return None
    return {k: v for k, v in merged.items() if k in fields}


def _text_credential_fields(conn_type: Optional[str]) -> set[str]:
    """Credential fields every variant of the connector declares as plain text."""
    entry = _entry(conn_type)
    text: set[str] = set()
    other: set[str] = set()
    if entry:
        for variant in entry.credentials_auth.by_auth.values():
            for name, field in variant.schema.model_fields.items():
                annotation = str(field.annotation)
                (text if "str" in annotation and "bool" not in annotation and "int" not in annotation else other).add(name)
    return text - other


def reject_non_text_credentials(conn_type: Optional[str], credentials: Any) -> None:
    """Refuse a credential field holding anything but text. Clients test these
    fields for truth, so a falsy value reads as absent and can fall through to
    the server's own identity. None and "" still mean "keep the saved value"."""
    text_fields = _text_credential_fields(conn_type) | _stored_secret_keys(conn_type)
    invalid = sorted(
        key for key, value in _as_dict(credentials).items()
        if value is not None and not isinstance(value, str)
        and (key in text_fields or _is_secret_field(key))
        and not (key in _JSON_SECRET_FIELDS and isinstance(value, dict) and value)
    )
    if invalid:
        raise AppError.bad_request(ErrorCode.VALIDATION, f"Credential fields must be text: {', '.join(invalid)}")


def _admin_required(message: str) -> AppError:
    return AppError.forbidden(ErrorCode.CONNECTION_ADMIN_REQUIRED, message)


async def require_destination_binding(
    db: AsyncSession,
    connection: Connection,
    user,
    *,
    effective_config: Any,
    request_credentials: Any = None,
    member_logins_bind: bool = False,
) -> None:
    """Raise unless ``user`` may send ``connection``'s credentials to ``effective_config``."""
    reject_non_text_credentials(connection.type, request_credentials)
    if user is None or not destination_changed(connection, effective_config, request_credentials):
        return
    if await can_use_system_credentials(db, user, connection.organization_id):
        return
    if credentials_after_auth_switch(connection, effective_config, request_credentials) is not None:
        return

    saved = _saved_credentials(connection)
    if saved is None:
        raise _admin_required("Only an organization admin can change this connection's destination.")
    if member_logins_bind and await _has_stored_member_logins(db, connection, user):
        raise _admin_required("Members have saved logins for this connection, so only an organization admin can change its destination.")

    request_credentials = _as_dict(request_credentials)
    merged = _merged_credentials(saved, request_credentials)
    new_auth = effective_auth_type(connection.type, effective_config, merged)
    variant = _variant(connection.type, new_auth)
    # Clients choose how to authenticate from which fields are present, so a
    # field outside the variant can select the server's own identity.
    if _has_fields_outside_variant(connection.type, effective_config, merged, variant):
        raise _admin_required("Only an organization admin can change this connection's destination.")
    if new_auth in _AMBIENT_VARIANTS:
        raise _admin_required("Only an organization admin can change this connection's destination.")
    saved_auth = effective_auth_type(connection.type, connection.config, saved)
    if not _stores_secret(connection.type, saved) and saved_auth not in _AMBIENT_VARIANTS:
        return

    new_secrets = secret_field_names(connection.type, new_auth)
    # A variant with no secret to enter still hands the client whatever is stored.
    if variant is None or not new_secrets:
        raise _admin_required("Only an organization admin can change this connection's destination.")
    fields = variant.schema.model_fields
    # Clients prefer whichever credential they find, so a stored secret from
    # another variant must be replaced too, not left behind.
    secret_keys = _stored_secret_keys(connection.type)
    required = {name for name in new_secrets if fields[name].is_required()} | {
        key for key, value in saved.items()
        if not _blank(value) and (key in secret_keys or _is_secret_field(key))
    }
    if not required:
        raise _admin_required("Only an organization admin can change this connection's destination.")
    missing = sorted(name for name in required if _blank(request_credentials.get(name)))
    if missing:
        raise AppError.forbidden(
            ErrorCode.CONNECTION_CREDENTIALS_REQUIRED,
            "Enter this connection's credentials again to use a changed destination.",
            fields=missing,
        )


def audit_snapshot(connection: Connection) -> dict:
    """The connection's auth policy and destination, safe for an audit row."""
    return {
        "auth_policy": connection.auth_policy or "system_only",
        "destination": destination_summary(connection.type, connection.config, _saved_credentials(connection) or {}),
    }


def _encoded_forms(value: str) -> set[str]:
    raw = value.encode()
    padded = base64.b64encode(raw).decode()
    urlsafe = base64.urlsafe_b64encode(raw).decode()
    return {padded, padded.rstrip("="), urlsafe, urlsafe.rstrip("="), raw.hex()}


def scrub_stored_secrets(text: Any, credentials: Any, conn_type: Optional[str]) -> Any:
    """Remove credential values, in the forms a server would echo, from ``text``."""
    credentials = _as_dict(credentials)
    if not isinstance(text, str) or not text or not credentials:
        return text
    secret_keys = _stored_secret_keys(conn_type)
    secrets = [
        v for k, v in credentials.items()
        if isinstance(v, str) and len(v.strip()) >= 4 and (k in secret_keys or _is_secret_field(k))
    ]
    if not secrets:
        return text
    variants: set[str] = set()
    for value in secrets:
        variants.update({value, quote(value, safe=""), quote(value), json.dumps(value)[1:-1]})
        variants.update(_encoded_forms(value))
    username = credentials.get("username") or credentials.get("user")
    if isinstance(username, str):
        for value in secrets:
            variants.update(_encoded_forms(f"{username}:{value}"))
    for variant in sorted(variants, key=len, reverse=True):
        text = text.replace(variant, "***")
    return text
