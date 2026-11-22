"""Destination comparison for saved connections: what is a change, and what is not."""
import pytest

from app.schemas.data_source_registry import REGISTRY
from app.errors import AppError
from app.services.connection_destination import (
    NON_DESTINATION_CONFIG_KEYS,
    destination_summary,
    destination_view,
    reject_non_text_credentials,
    secret_field_names,
)


@pytest.mark.parametrize("value", [0, False, [], {}, 1, True, ["x"], {"a": 1}])
def test_every_secret_field_rejects_every_non_text_value(value):
    for conn_type, entry in REGISTRY.items():
        for auth_type in entry.credentials_auth.by_auth:
            for name in secret_field_names(conn_type, auth_type):
                if name in ("credentials_json", "service_account_json") and isinstance(value, dict) and value:
                    continue
                with pytest.raises(AppError):
                    reject_non_text_credentials(conn_type, {name: value})


def test_a_non_text_access_key_is_rejected_because_clients_test_it_for_truth():
    with pytest.raises(AppError):
        reject_non_text_credentials("s3", {"access_key": 0})


@pytest.mark.parametrize("credentials", [None, {}, {"password": ""}, {"password": None}, {"password": "text"}, {"use_kerberos": True}])
def test_keep_saved_and_text_values_are_accepted(credentials):
    reject_non_text_credentials("neo4j", credentials)


def test_a_service_account_object_is_accepted():
    reject_non_text_credentials("bigquery", {"credentials_json": {"type": "service_account"}})
    with pytest.raises(AppError):
        reject_non_text_credentials("bigquery", {"credentials_json": {}})


def _same(conn_type, a, b, creds=None):
    return destination_view(conn_type, a, creds or {}) == destination_view(conn_type, b, creds or {})


@pytest.mark.parametrize("saved,sent", [
    ({"host": "DB.Example.com", "port": 5432}, {"host": "db.example.com", "port": "5432"}),
    ({"host": "db.example.com"}, {"host": "db.example.com", "port": 5432}),
    ({"host": "db.example.com", "port": 5432}, {"host": "db.example.com.", "port": 5432}),
    ({"host": "db.example.com", "sslmode": "require"}, {"host": "db.example.com", "sslmode": "require", "schema": ""}),
])
def test_postgres_normalization_differences_are_not_changes(saved, sent):
    assert _same("postgresql", saved, sent)


@pytest.mark.parametrize("saved,sent", [
    ({"base_url": "https://api.example.com/"}, {"base_url": "https://API.example.com"}),
    ({"base_url": "https://api.example.com:443/v1/"}, {"base_url": "https://api.example.com/v1"}),
    ({"base_url": "http://api.example.com:80"}, {"base_url": "http://api.example.com"}),
])
def test_url_normalization_differences_are_not_changes(saved, sent):
    assert _same("custom_api", saved, sent)


@pytest.mark.parametrize("key,saved_value,new_value", [
    ("host", "db.example.com", "other.example.com"),
    ("port", 5432, 6543),
    ("sslmode", "require", "disable"),
])
def test_postgres_destination_fields_change(key, saved_value, new_value):
    saved = {"host": "db.example.com", "port": 5432, key: saved_value}
    assert not _same("postgresql", saved, {**saved, key: new_value})


@pytest.mark.parametrize("value", ["warehouse?host=other.example.com", "db/x", "a@b", "a;b"])
def test_connection_string_metacharacters_in_a_scope_field_count_as_a_change(value):
    saved = {"host": "db.example.com", "database": "warehouse"}
    assert not _same("postgresql", saved, {**saved, "database": value})
    # Unchanged, even if ugly, is not a change.
    assert _same("postgresql", {**saved, "database": value}, {**saved, "database": value})


def test_plain_scope_change_is_not_a_change():
    saved = {"host": "db.example.com", "database": "warehouse", "schema": "public"}
    assert _same("postgresql", saved, {**saved, "database": "analytics", "schema": "reporting"})


def test_credentials_only_contribute_their_destination_fields():
    cfg = {"base_url": "https://api.example.com", "auth_type": "oauth_app"}
    assert destination_view("custom_api", cfg, {"client_secret": "x"}) == destination_view("custom_api", cfg, {"client_secret": "y"})
    assert destination_view("custom_api", cfg, {"token_url": "https://a.example.com/t"}) != destination_view("custom_api", cfg, {"token_url": "https://b.example.com/t"})


def test_auth_type_defaults_to_the_registry_default():
    assert _same("postgresql", {"host": "h"}, {"host": "h", "auth_type": "userpass"})
    assert not _same("custom_api", {"auth_type": "bearer"}, {"auth_type": "api_key"})


def test_endpoint_paths_on_the_saved_host_are_not_a_change():
    a = {"base_url": "https://api.example.com", "endpoints": [{"name": "a", "path": "/a"}]}
    b = {"base_url": "https://api.example.com", "endpoints": [{"name": "b", "path": "/b"}]}
    assert _same("custom_api", a, b)


def test_endpoint_path_that_changes_the_host_is_a_change():
    a = {"base_url": "https://api.example.com", "endpoints": [{"name": "a", "path": "/a"}]}
    b = {"base_url": "https://api.example.com", "endpoints": [{"name": "a", "path": ".other.example.com/a"}]}
    assert not _same("custom_api", a, b)


def test_summary_never_carries_userinfo_or_secrets():
    cfg = {"host": "user:hunter2@db.example.com", "port": 5432, "additional_params": {"Password": "hunter2"}, "database": "w"}
    summary = destination_summary("postgresql", cfg, {"password": "hunter2"})
    assert "hunter2" not in str(summary)


@pytest.mark.parametrize("conn_type,config,credential", [
    ("postgresql", {"host": "db.example.com"}, {"host": "other.example.com"}),
    ("custom_api", {"base_url": "https://api.example.com"}, {"base_url": "https://other.example.com"}),
    ("netsuite", {}, {"account_id": "other"}),
    ("custom_api", {"base_url": "https://api.example.com"}, {"not_a_declared_field": "x"}),
    ("iceberg", {"uri": "https://catalog.example.com", "auth_type": "oauth2"}, {"oauth2_server_uri": "https://other.example.com/t"}),
])
def test_credential_keys_the_client_uses_as_a_destination_count(conn_type, config, credential):
    assert destination_view(conn_type, config, {}) != destination_view(conn_type, config, credential)


def test_every_destination_field_of_every_connector_counts_when_sent_as_a_credential():
    failures = []
    for conn_type, entry in REGISTRY.items():
        config = {"auth_type": entry.credentials_auth.default}
        for name, field in entry.config_schema.model_fields.items():
            if name in NON_DESTINATION_CONFIG_KEYS:
                continue
            for value in ("other.example.com", 9999, True):
                if destination_view(conn_type, config, {}) != destination_view(conn_type, config, {name: value}):
                    break
            else:
                failures.append(f"{conn_type}.{name}")
    assert not failures, failures


@pytest.mark.parametrize("config", [{"port": "²"}, {"auth_type": ["x"]}, {"auth_type": {"a": 1}}])
def test_malformed_values_do_not_raise(config):
    destination_view("postgresql", config, {"auth_type": {"a": 1}})


def test_a_non_string_auth_type_is_a_change():
    assert destination_view("custom_api", {}, {}) != destination_view("custom_api", {"auth_type": ["x"]}, {})


def test_a_port_past_the_integer_conversion_limit_does_not_raise():
    assert not _same("postgresql", {"host": "h", "port": 5432}, {"host": "h", "port": "9" * 5000})


@pytest.mark.parametrize("key", [
    "role", "org_id", "sap_client", "account_name", "auth_login_domain", "login_provider", "project_id",
    "site_name", "auth_source", "cluster_identifier",
])
def test_config_fields_that_choose_the_identity_or_tenant_are_not_safe_listed(key):
    assert key not in NON_DESTINATION_CONFIG_KEYS


@pytest.mark.parametrize("key", [
    "role_arn", "kerberos_principal", "principal", "impersonated_user", "trusted_user", "user_directory",
    "user_id", "documentum_login", "tenant_id", "member_cid", "partition", "domain", "workspace_domain",
    "target_service_account",
])
def test_credential_fields_that_choose_the_identity_or_tenant_count(key):
    config = {"host": "db.example.com"}
    assert destination_view("postgresql", config, {key: "a"}) != destination_view("postgresql", config, {key: "b"})


@pytest.mark.parametrize("conn_type,saved,changed", [
    ("postgresql", {"host": "h", "include_system_schemas": False}, {"include_system_schemas": True}),
    ("kms_lighthouse", {"base_url": "https://kms.example.com"}, {"include_offline": True}),
    ("kms_lighthouse", {"base_url": "https://kms.example.com"}, {"layout_id": 7}),
    ("kms_lighthouse", {"base_url": "https://kms.example.com"}, {"root_folder_id": 12}),
    ("kms_lighthouse", {"base_url": "https://kms.example.com"}, {"max_results": 50}),
    ("snowflake", {"account": "a"}, {"query_tag_enabled": False}),
    ("snowflake", {"account": "a"}, {"query_tag_label": "finance team"}),
    ("documentum", {"rest_url": "https://dctm.example.com/rest"}, {"root_path": "/Finance"}),
    ("aws_athena", {"region": "us-east-1"}, {"table_exclude_pattern": "^tmp_"}),
    ("hive", {"host": "h"}, {"databases": "sales,ops", "engine": "impala"}),
    ("dremio", {"url": "https://dremio.example.com"}, {"spaces": "Finance"}),
    ("cassandra", {"contact_points": "h"}, {"keyspaces": "app", "max_rows": 10}),
    ("cosmosdb", {"endpoint": "https://c.example.com"}, {"containers": "orders", "sample_size": 50}),
    ("db2", {"host": "h"}, {"platform": "zos"}),
    ("iceberg", {"uri": "https://catalog.example.com"}, {"max_scan_rows": 1000}),
    ("azure_blob", {"account_name": "acct"}, {"container": "reports"}),
    ("jira_dc", {"base_url": "https://jira.example.com"}, {"projects": "OPS, HR", "tables": "issues", "max_rows": 50}),
    ("jira_dc", {"base_url": "https://jira.example.com"}, {"include_issue_documents": False}),
    ("confluence_dc", {"base_url": "https://wiki.example.com"}, {"spaces": "ENG", "max_results": 50}),
    ("confluence_dc", {"base_url": "https://wiki.example.com"}, {"include_blogposts": False}),
    ("teams_messages", {}, {"allowed_post_channels": "Alerts"}),
])
def test_catalog_filters_and_tags_are_not_a_destination_change(conn_type, saved, changed):
    assert _same(conn_type, saved, {**saved, **changed})


def test_the_kms_api_prefix_stays_a_destination():
    saved = {"base_url": "https://kms.example.com"}
    assert not _same("kms_lighthouse", saved, {**saved, "api_prefix": ".other.example.com/x"})


@pytest.mark.parametrize("conn_type,host", [("jira_dc", "jira.example.com"), ("confluence_dc", "wiki.example.com")])
@pytest.mark.parametrize("changed", [
    "https://other.example.com", "https://{host}/other", {"allow_http": True},
])
def test_atlassian_data_center_url_and_transport_stay_destinations(conn_type, host, changed):
    # A path on the saved host is still a change: the API is reached under base_url.
    saved = {"base_url": f"https://{host}"}
    changed = changed if isinstance(changed, dict) else {"base_url": changed.format(host=host)}
    assert not _same(conn_type, saved, {**saved, **changed})


def test_a_url_in_the_teams_post_channel_list_is_still_a_destination():
    assert not _same("teams_messages", {}, {"allowed_post_channels": "https://other.example.com/hook"})


@pytest.mark.parametrize("conn_type", ["outlook_mail", "teams_messages"])
def test_the_server_managed_action_scopes_are_not_a_destination(conn_type):
    # Written by the server when a write action is turned on; a form save never carries it.
    assert _same(conn_type, {"action_scopes": ["Mail.Send"]}, {})


def test_port_default_comes_from_the_client():
    assert _same("trino", {"host": "h"}, {"host": "h", "port": 443})
    assert not _same("trino", {"host": "h"}, {"host": "h", "port": 8080})


def test_credential_auth_type_counts_as_the_auth_type():
    config = {"base_url": "https://api.example.com", "auth_type": "bearer"}
    assert destination_view("custom_api", config, {}) != destination_view("custom_api", config, {"auth_type": "basic"})


def test_summary_does_not_carry_undeclared_credential_values():
    summary = destination_summary("custom_api", {"base_url": "https://api.example.com"}, {"not_a_declared_field": "hunter2"})
    assert "hunter2" not in str(summary)


def test_summary_strips_url_userinfo_and_query():
    summary = destination_summary("custom_api", {"base_url": "https://u:hunter2@api.example.com/v1?token=hunter2"}, {})
    assert "hunter2" not in str(summary)
    assert "api.example.com" in str(summary)


def test_oauth_token_url_in_credentials_is_summarized():
    summary = destination_summary("custom_api", {"base_url": "https://api.example.com"}, {"token_url": "https://idp.example.com/t"})
    assert "idp.example.com" in str(summary)


@pytest.mark.parametrize("key", [
    "host", "port", "url", "base_url", "server_url", "site_url", "instance_url", "api_url", "cluster_url",
    "endpoint_url", "rest_url", "controller_url", "kibana_url", "service_root", "uri", "uris", "account",
    "server_hostname", "token_url", "authorization_url", "worker_url_base", "rewrite_worker_host", "controller",
    "sslmode", "ssl_mode", "ssl_ca", "secure", "tls", "use_ssl", "use_tcps", "encrypt", "encryptdata",
    "require_encryption", "secured", "verify_ssl", "verify_certs", "ca_certificate", "ca_bundle", "ca_bundle_path",
    "allow_http", "protocol", "host_public_key", "region", "region_name", "domain", "additional_params",
    "use_srv", "qrs_port", "engine_port", "manager_discovery", "kerberos_service", "login_scheme", "sandbox",
    "auth_type", "odbc_driver", "path", "http_path", "xmla_path",
])
def test_destination_and_transport_fields_are_never_safe_listed(key):
    assert key not in NON_DESTINATION_CONFIG_KEYS


def test_every_registry_config_field_that_names_a_location_is_a_destination():
    """A new connector field that looks like a host, URL, port or TLS setting must not be safe-listed."""
    markers = ("url", "uri", "host", "ssl", "tls", "verify", "certificate")
    for conn_type, entry in REGISTRY.items():
        for field in entry.config_schema.model_fields:
            if field == "port" or field.endswith("_port") or any(m in field for m in markers):
                assert field not in NON_DESTINATION_CONFIG_KEYS, f"{conn_type}.{field}"


@pytest.mark.parametrize("conn_type,auth,expected", [
    ("postgresql", "userpass", {"password"}),
    ("salesforce", "userpass", {"password", "security_token"}),
    ("custom_api", "oauth_app", {"client_secret"}),
    ("snowflake", "keypair", {"private_key_pem", "private_key_passphrase", "oauth_client_secret"}),
    ("custom_api", "basic", {"password"}),
])
def test_secret_field_names(conn_type, auth, expected):
    assert set(secret_field_names(conn_type, auth)) >= expected


def test_secret_field_names_excludes_identifiers():
    names = set(secret_field_names("custom_api", "oauth_app"))
    assert not ({"token_url", "authorize_url", "client_id", "token_endpoint_auth_method", "scopes"} & names)
    assert "user" not in set(secret_field_names("postgresql", "userpass"))


class _SavedBigQuery:
    type = "bigquery"
    organization_id = "org"
    data_sources = []
    credentials = "stored"

    def __init__(self, config, saved):
        import json
        self.config = json.dumps(config)
        self._saved = saved

    def decrypt_credentials(self):
        return dict(self._saved)


def test_switching_bigquery_to_the_server_identity_is_not_a_secret_reentry():
    """ADC stores no secret, so a switch to it cannot be authorized by re-entering one."""
    from app.services.connection_destination import credentials_after_auth_switch

    connection = _SavedBigQuery({"project_id": "p", "dataset": "d"}, {"credentials_json": '{"type": "service_account"}'})
    assert credentials_after_auth_switch(connection, {"project_id": "p", "dataset": "d", "auth_type": "adc"}, {}) is None


def test_the_impersonated_service_account_of_the_server_identity_is_a_destination():
    from app.services.connection_destination import destination_changed

    config = {"project_id": "p", "dataset": "d", "auth_type": "adc"}
    connection = _SavedBigQuery(config, {"use_application_default_credentials": True, "target_service_account": "a@p.iam.gserviceaccount.com"})
    assert destination_changed(connection, config, {"target_service_account": "b@p.iam.gserviceaccount.com"})


def test_removing_the_impersonated_service_account_with_a_null_is_a_destination_change():
    """BigQuery's methods are exclusive, so a save or test drops a field sent as null."""
    from app.services.connection_destination import destination_changed

    config = {"project_id": "p", "dataset": "d", "auth_type": "adc"}
    connection = _SavedBigQuery(config, {"use_application_default_credentials": True, "target_service_account": "a@p.iam.gserviceaccount.com"})
    assert destination_changed(connection, config, {"target_service_account": None})
    # A blank box still means "unchanged".
    assert not destination_changed(connection, config, {"target_service_account": ""})


def test_a_null_keeps_the_saved_value_where_methods_are_not_exclusive():
    from app.services.connection_destination import destination_changed

    class _SavedRedshift(_SavedBigQuery):
        type = "aws_redshift"

    config = {"host": "rs.example.com", "port": 5439, "database": "d", "auth_type": "userpass"}
    connection = _SavedRedshift(config, {"user": "u", "password": "p", "role_arn": "arn:aws:iam::1:role/r"})
    assert not destination_changed(connection, config, {"role_arn": None})
