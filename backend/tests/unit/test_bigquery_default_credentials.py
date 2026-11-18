"""Keyless BigQuery auth (Application Default Credentials).

The promise under test: a BigQuery connection can run as the deployment's own
Google identity with no stored key, optionally impersonating a target service
account — and that identity is *never* reached unless the operator enabled it
and the connection explicitly asked for it.

Only Google is faked: ``google.auth.default`` (the identity lookup) and
``bigquery.Client`` (the driver). Everything of ours runs for real.
"""

from __future__ import annotations

import json
import os

import pytest

from app.data_sources import google_default_credentials as gdc
from app.data_sources.clients import bigquery_client as bq_module
from app.data_sources.clients.bigquery_client import BigqueryClient
from app.schemas.data_source_registry import (
    auth_variant_enabled,
    credentials_for_auth_method,
    get_entry,
)
from app.schemas.data_sources.configs import BigQueryDefaultCredentials

TARGET = "reader@analytics-prod.iam.gserviceaccount.com"


def _ambient_credentials():
    """Stands in for whatever google.auth.default() finds on the server. A real
    google-auth credential class, so the library can wrap it for impersonation."""
    from google.oauth2.credentials import Credentials

    return Credentials(token="ambient-token")


class _RecordingBigQuery:
    """Stands in for google.cloud.bigquery.Client; records what it was given."""

    built: list[_RecordingBigQuery] = []

    def __init__(self, project=None, credentials=None, **_):
        self.project = project
        self.credentials = credentials
        _RecordingBigQuery.built.append(self)

    def list_datasets(self):
        return []


@pytest.fixture
def google(monkeypatch):
    """A deployment with an ambient Google identity. Returns the lookup log."""
    import google.auth

    lookups: list[dict] = []
    ambient = _ambient_credentials()

    def _default(scopes=None, **kwargs):
        lookups.append({"scopes": scopes})
        return ambient, "ambient-project"

    monkeypatch.setattr(google.auth, "default", _default)
    monkeypatch.setattr(bq_module.bigquery, "Client", _RecordingBigQuery)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    _RecordingBigQuery.built = []
    gdc.reset_cache()
    yield type("Google", (), {"lookups": lookups, "ambient": ambient})
    gdc.reset_cache()


@pytest.fixture
def allow(monkeypatch):
    from app.settings.config import settings

    def _set(value: bool):
        monkeypatch.setattr(settings.bow_config.security, "allow_google_default_credentials", value)

    _set(True)
    return _set


def _keyless(**kwargs):
    return BigqueryClient(project_id="analytics-prod", dataset="sales", use_application_default_credentials=True, **kwargs)


# --------------------------------------------------------------------------
# The deployment opt-in
# --------------------------------------------------------------------------

def test_keyless_is_refused_until_the_operator_enables_it(google, allow):
    allow(False)
    with pytest.raises(ValueError):
        _keyless()
    assert google.lookups == []
    assert _RecordingBigQuery.built == []


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("YES", True), (" on ", True),
    ("false", False), ("0", False), ("", False), ("enabled", False), (None, False),
])
def test_opt_in_env_var_enables_only_on_an_explicit_yes(monkeypatch, raw, expected):
    from app.settings.bow_config import SecurityConfig

    if raw is None:
        monkeypatch.delenv(gdc.ENABLE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(gdc.ENABLE_ENV_VAR, raw)
    assert SecurityConfig().allow_google_default_credentials is expected


def test_the_keyless_method_is_offered_only_when_enabled_and_never_to_members(allow):
    variant = get_entry("bigquery").credentials_auth.by_auth["adc"]
    assert variant.scopes == ["system"]
    assert auth_variant_enabled(variant) is True
    allow(False)
    assert auth_variant_enabled(variant) is False
    # Methods with no opt-in requirement are unaffected by the flag.
    assert auth_variant_enabled(get_entry("bigquery").credentials_auth.by_auth["service_account"]) is True


# --------------------------------------------------------------------------
# Which identity a client ends up with
# --------------------------------------------------------------------------

def test_keyless_client_runs_as_the_ambient_identity(google, allow):
    client = _keyless()
    # Building the client must not look anything up: the lookup does network
    # I/O and construction happens on the event loop.
    assert google.lookups == []

    assert client.test_connection()["success"] is True
    assert len(google.lookups) == 1
    assert _RecordingBigQuery.built[-1].credentials is google.ambient
    assert _RecordingBigQuery.built[-1].project == "analytics-prod"


def test_target_service_account_is_impersonated_from_the_ambient_identity(google, allow):
    from google.auth import impersonated_credentials

    client = _keyless(target_service_account=f"  {TARGET} ")
    client.test_connection()

    creds = _RecordingBigQuery.built[-1].credentials
    assert isinstance(creds, impersonated_credentials.Credentials)
    assert creds.service_account_email == TARGET
    # google-auth wraps a copy of the source; its token identifies it.
    assert creds._source_credentials.token == google.ambient.token
    # The ambient identity must be able to call the IAM Credentials API.
    assert gdc.CLOUD_PLATFORM_SCOPE in google.lookups[-1]["scopes"]


@pytest.mark.parametrize("bad_target", [
    "reader@example.com",
    "not-an-email",
    "x@p.iam.gserviceaccount.com/../../other",
    "x@p.iam.gserviceaccount.com:generateAccessToken?a=b",
    "x y@p.iam.gserviceaccount.com",
    "https://iamcredentials.googleapis.com/x@p.iam.gserviceaccount.com",
])
def test_a_target_that_is_not_a_service_account_email_is_refused(google, allow, bad_target):
    with pytest.raises(ValueError):
        _keyless(target_service_account=bad_target)
    assert google.lookups == []


@pytest.mark.parametrize("good_target", [
    TARGET,
    "123456789012-compute@developer.gserviceaccount.com",
    "my-project@appspot.gserviceaccount.com",
])
def test_real_service_account_address_shapes_are_accepted(good_target):
    assert gdc.normalize_target_service_account(good_target) == good_target


@pytest.mark.parametrize("not_true", [None, False, "", 0, "true", 1, "yes"])
def test_only_an_explicit_true_selects_the_ambient_identity(google, allow, not_true):
    """A connection with no usable key must fail, not quietly become the server."""
    with pytest.raises((ValueError, TypeError)):
        BigqueryClient(project_id="p", dataset="d", use_application_default_credentials=not_true)
    assert google.lookups == []


def test_a_users_own_sign_in_is_never_widened_into_the_ambient_identity(google, allow):
    client = BigqueryClient(
        project_id="p", dataset="d", access_token="user-token",
        use_application_default_credentials=True, target_service_account=TARGET,
    )
    client.test_connection()
    assert google.lookups == []
    assert getattr(_RecordingBigQuery.built[-1].credentials, "token", None) == "user-token"


def test_a_server_with_no_google_identity_fails_the_test_instead_of_raising(monkeypatch, google, allow):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def _none(scopes=None, **kwargs):
        raise DefaultCredentialsError("nothing configured")

    monkeypatch.setattr(google.auth, "default", _none)
    result = _keyless().test_connection()
    assert result["success"] is False
    assert result["message"]
    assert _RecordingBigQuery.built == []


def test_a_refused_impersonation_names_the_target_in_the_test_result(monkeypatch, google, allow):
    from google.auth.exceptions import RefreshError

    class _Refusing(_RecordingBigQuery):
        def list_datasets(self):
            raise RefreshError("Unable to acquire impersonated credentials", "403 permission denied")

    monkeypatch.setattr(bq_module.bigquery, "Client", _Refusing)
    result = _keyless(target_service_account=TARGET).test_connection()
    assert result["success"] is False
    assert TARGET in result["message"]


# --------------------------------------------------------------------------
# Lookup reuse
# --------------------------------------------------------------------------

def test_clients_share_one_lookup_per_identity(google, allow):
    for _ in range(3):
        _keyless().test_connection()
    assert len(google.lookups) == 1

    _keyless(target_service_account=TARGET).test_connection()
    assert len(google.lookups) == 2  # a different identity is its own entry


def test_a_replaced_credential_file_is_picked_up_without_a_restart(tmp_path, monkeypatch, google, allow):
    config = tmp_path / "federation.json"
    config.write_text(json.dumps({"type": "external_account", "n": 1}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(config))

    _keyless().test_connection()
    _keyless().test_connection()
    assert len(google.lookups) == 1

    config.write_text(json.dumps({"type": "external_account", "n": 2, "rotated": True}))
    os.utime(config, ns=(config.stat().st_atime_ns, config.stat().st_mtime_ns + 5_000_000_000))
    _keyless().test_connection()
    assert len(google.lookups) == 2


def test_turning_the_opt_in_off_stops_already_resolved_identities(google, allow):
    _keyless().test_connection()
    allow(False)
    with pytest.raises(ValueError):
        gdc.resolve_default_credentials()


# --------------------------------------------------------------------------
# Stored credentials follow the selected method
# --------------------------------------------------------------------------

_KEY = json.dumps({"type": "service_account", "token_uri": "https://oauth2.googleapis.com/token"})


def test_switching_to_keyless_drops_the_saved_key():
    saved = {"credentials_json": _KEY, "oauth_client_id": "app", "oauth_client_secret": "s"}
    merged = {**saved, "use_application_default_credentials": True, "target_service_account": TARGET}
    assert credentials_for_auth_method("bigquery", "adc", merged) == {
        "use_application_default_credentials": True,
        "target_service_account": TARGET,
        # Per-user sign-in is independent of how the shared identity authenticates.
        "oauth_client_id": "app",
        "oauth_client_secret": "s",
    }


@pytest.mark.parametrize("auth_type", ["service_account", None])
def test_switching_back_to_a_key_drops_the_keyless_marker(auth_type):
    merged = {"use_application_default_credentials": True, "target_service_account": TARGET, "credentials_json": _KEY}
    assert credentials_for_auth_method("bigquery", auth_type, merged) == {"credentials_json": _KEY}


def test_connectors_that_did_not_opt_in_keep_every_stored_field():
    stored = {"user": "u", "password": "p", "private_key_pem": "k", "use_kerberos": True, "anything": 1}
    for ds_type, auth in (("snowflake", "keypair"), ("MSSQL", "userpass"), ("postgresql", None), ("no_such_type", "x")):
        assert credentials_for_auth_method(ds_type, auth, stored) == stored


# --------------------------------------------------------------------------
# The form schema
# --------------------------------------------------------------------------

def test_keyless_credentials_schema_needs_no_input_and_cleans_the_target():
    assert BigQueryDefaultCredentials().model_dump(exclude_none=True) == {"use_application_default_credentials": True}
    assert BigQueryDefaultCredentials(target_service_account=f" {TARGET}\n").target_service_account == TARGET
    assert BigQueryDefaultCredentials(target_service_account="").target_service_account is None


@pytest.mark.parametrize("payload", [
    {"use_application_default_credentials": False},
    {"target_service_account": "someone@example.com"},
])
def test_keyless_credentials_schema_rejects_what_the_client_would_refuse(payload):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BigQueryDefaultCredentials(**payload)


def test_the_marker_is_not_an_editable_form_field():
    schema = BigQueryDefaultCredentials.model_json_schema()["properties"]
    assert schema["use_application_default_credentials"]["ui:hidden"] is True
    assert schema["use_application_default_credentials"]["default"] is True
    assert "ui:hidden" not in schema["target_service_account"]
