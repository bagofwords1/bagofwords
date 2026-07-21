"""Unit tests for SalesforceClient.

Covers:
- auth-mode selection (jwt / access-token / username-password) and the kwargs
  handed to simple_salesforce.Salesforce
- sandbox / My-Domain routing (the previously-ignored domain/sandbox fields)
- JWT Bearer token exchange (real PyJWT RS256 signing, mocked token endpoint)
- dynamic object discovery: custom objects included, system/noise/deprecated
  objects excluded, and no longer the hardcoded five
- get_schema field-type mapping + reference-field foreign keys + Id primary key
- execute_query -> DataFrame (attributes dropped, MAX_ROWS pagination cap)
- registry wiring for the jwt + userpass variants

simple_salesforce.Salesforce and the token HTTP endpoint are mocked, so these
run without a live org or network.
"""
from __future__ import annotations

import pandas as pd
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import app.data_sources.clients.salesforce_client as sfmod
from app.data_sources.clients.salesforce_client import SalesforceClient


# ---------- test RSA key (for the real JWT signing path) ---------- #


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


# ---------- fake simple_salesforce plumbing ---------- #


class _FakeSFType:
    def __init__(self, fields):
        self._fields = fields

    def describe(self):
        return {"fields": self._fields}


class _FakeSalesforce:
    """Records constructor kwargs; serves canned describe/query responses."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self._global = _global_describe
        self._object_fields = _object_fields
        self._pages = list(_query_pages)

    def limits(self):
        return {}

    def describe(self):
        return self._global

    def query(self, soql):
        return self._pages[0]

    def query_more(self, url, identifier_is_url=False):
        # url is the nextRecordsUrl of the previous page; return the next page.
        idx = next(i for i, p in enumerate(self._pages) if p.get("nextRecordsUrl") == url)
        return self._pages[idx + 1]

    def __getattr__(self, name):
        # sf.<ObjectName> -> SFType with that object's fields
        fields = self.__dict__.get("_object_fields", _object_fields)
        return _FakeSFType(fields.get(name, []))


# Canned data (module-level so _FakeSalesforce can reach it) ------------------

_global_describe = {
    "sobjects": [
        {"name": "Account", "queryable": True, "custom": False},
        {"name": "Widget__c", "queryable": True, "custom": True},
        {"name": "AccountShare", "queryable": True, "custom": False},          # noise suffix
        {"name": "AccountHistory", "queryable": True, "custom": False},        # noise suffix
        {"name": "OldThing", "queryable": True, "deprecatedAndHidden": True},  # deprecated
        {"name": "MySetting__c", "queryable": True, "customSetting": True},    # custom setting
        {"name": "HiddenView", "queryable": False},                           # not queryable
    ]
}

_object_fields = {
    "Account": [
        {"name": "Id", "type": "id", "label": "Account ID"},
        {"name": "Name", "type": "string", "label": "Name"},
        {"name": "OwnerId", "type": "reference", "label": "Owner", "referenceTo": ["User"]},
        {"name": "AnnualRevenue", "type": "currency", "label": "Annual Revenue"},
        {"name": "CreatedDate", "type": "datetime", "label": "Created Date"},
        {"name": "IsDeleted", "type": "boolean", "label": "Deleted"},
        {"name": "NumberOfEmployees", "type": "int", "label": "Employees"},
    ],
    "Widget__c": [
        {"name": "Id", "type": "id", "label": "Record ID"},
        {"name": "Account__c", "type": "reference", "label": "Account", "referenceTo": ["Account"]},
    ],
}

_query_pages = [
    {
        "done": False,
        "nextRecordsUrl": "/services/data/v60.0/query/next-1",
        "records": [
            {"attributes": {"type": "Account"}, "Id": "001", "Name": "Acme"},
            {"attributes": {"type": "Account"}, "Id": "002", "Name": "Globex"},
        ],
    },
    {
        "done": True,
        "records": [
            {"attributes": {"type": "Account"}, "Id": "003", "Name": "Initech"},
        ],
    },
]


@pytest.fixture(autouse=True)
def _patch_salesforce(monkeypatch):
    _FakeSalesforce.last_kwargs = {}
    monkeypatch.setattr(sfmod, "Salesforce", _FakeSalesforce)


class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
        self.text = str(json_data)

    def json(self):
        return self._json


# ---------- auth-mode selection ---------- #


class TestAuthMode:
    def test_access_token_mode(self):
        c = SalesforceClient(access_token="tok", instance_url="https://na1.my.salesforce.com")
        assert c._auth_mode == "token"
        _ = c.sf
        assert _FakeSalesforce.last_kwargs == {
            "instance_url": "https://na1.my.salesforce.com",
            "session_id": "tok",
        }

    def test_userpass_mode_production(self):
        c = SalesforceClient(username="u@x.com", password="p", security_token="t")
        assert c._auth_mode == "userpass"
        _ = c.sf
        kw = _FakeSalesforce.last_kwargs
        assert kw["username"] == "u@x.com" and kw["password"] == "p"
        assert kw["security_token"] == "t"
        assert kw["domain"] == "login"

    def test_userpass_sandbox_routes_to_test(self):
        # The bug this fixes: sandbox used to be captured but never forwarded.
        c = SalesforceClient(username="u", password="p", security_token="t", sandbox=True)
        _ = c.sf
        assert _FakeSalesforce.last_kwargs["domain"] == "test"

    def test_userpass_my_domain(self):
        c = SalesforceClient(username="u", password="p", security_token="t", domain="acme")
        _ = c.sf
        assert _FakeSalesforce.last_kwargs["domain"] == "acme"

    def test_no_credentials_raises(self):
        c = SalesforceClient()
        assert c._auth_mode == "none"
        with pytest.raises(RuntimeError, match="no usable credentials"):
            _ = c.sf


# ---------- JWT Bearer flow ---------- #


class TestJwtBearer:
    def test_jwt_exchange_and_construct(self, monkeypatch, private_key_pem):
        captured = {}

        def fake_post(url, data=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return _FakeResponse(200, {
                "access_token": "JWT_TOKEN",
                "instance_url": "https://na5.my.salesforce.com",
            })

        monkeypatch.setattr(sfmod.requests, "post", fake_post)

        c = SalesforceClient(consumer_key="CK", private_key=private_key_pem, username="svc@x.com")
        assert c._auth_mode == "jwt"
        _ = c.sf

        assert captured["url"] == "https://login.salesforce.com/services/oauth2/token"
        assert captured["data"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        assert "assertion" in captured["data"]  # a signed JWT was produced
        # The returned session is what gets handed to Salesforce.
        assert _FakeSalesforce.last_kwargs == {
            "instance_url": "https://na5.my.salesforce.com",
            "session_id": "JWT_TOKEN",
        }

    def test_jwt_sandbox_uses_test_login(self, monkeypatch, private_key_pem):
        captured = {}

        def fake_post(url, data=None, timeout=None):
            captured["url"] = url
            return _FakeResponse(200, {"access_token": "t", "instance_url": "https://cs1.my.salesforce.com"})

        monkeypatch.setattr(sfmod.requests, "post", fake_post)
        c = SalesforceClient(consumer_key="CK", private_key=private_key_pem, username="s", sandbox=True)
        _ = c.sf
        assert captured["url"].startswith("https://test.salesforce.com")

    def test_jwt_error_surfaces(self, monkeypatch, private_key_pem):
        def fake_post(url, data=None, timeout=None):
            return _FakeResponse(400, {"error": "invalid_grant", "error_description": "user hasn't approved this consumer"})

        monkeypatch.setattr(sfmod.requests, "post", fake_post)
        c = SalesforceClient(consumer_key="CK", private_key=private_key_pem, username="s")
        with pytest.raises(RuntimeError, match="invalid_grant"):
            _ = c.sf


# ---------- discovery ---------- #


class TestDiscovery:
    def test_dynamic_discovery_includes_custom_excludes_noise(self):
        c = SalesforceClient(access_token="t", instance_url="https://x")
        names = c._discover_object_names()
        assert "Account" in names          # standard
        assert "Widget__c" in names        # custom object — the whole point
        assert "AccountShare" not in names        # noise suffix
        assert "AccountHistory" not in names      # noise suffix
        assert "OldThing" not in names            # deprecated
        assert "MySetting__c" not in names        # custom setting
        assert "HiddenView" not in names          # not queryable
        # Priority object first; not the old hardcoded five.
        assert names[0] == "Account"
        assert set(names) == {"Account", "Widget__c"}

    def test_explicit_objects_override(self):
        c = SalesforceClient(access_token="t", instance_url="https://x", objects="Foo__c, Bar")
        assert c._discover_object_names() == ["Foo__c", "Bar"]

    def test_get_schemas_returns_tables(self):
        c = SalesforceClient(access_token="t", instance_url="https://x")
        tables = c.get_schemas()
        assert {t.name for t in tables} == {"Account", "Widget__c"}

    def test_get_schemas_progress_callback(self):
        seen = []
        c = SalesforceClient(access_token="t", instance_url="https://x")
        c.get_schemas(progress_callback=lambda phase, item, done, total: seen.append((item, done, total)))
        assert seen and seen[-1][1] == seen[-1][2]  # done == total at the end


# ---------- get_schema mapping ---------- #


class TestGetSchema:
    def test_types_pk_and_fks(self):
        c = SalesforceClient(access_token="t", instance_url="https://x")
        table = c.get_schema("Account")
        dtypes = {col.name: col.dtype for col in table.columns}
        assert dtypes == {
            "Id": "str",
            "Name": "str",
            "OwnerId": "reference",
            "AnnualRevenue": "float",
            "CreatedDate": "datetime",
            "IsDeleted": "bool",
            "NumberOfEmployees": "int",
        }
        assert [pk.name for pk in table.pks] == ["Id"]
        assert len(table.fks) == 1
        fk = table.fks[0]
        assert fk.column.name == "OwnerId"
        assert fk.references_name == "User"
        assert fk.references_column.name == "Id"

    def test_column_labels_kept_as_description(self):
        c = SalesforceClient(access_token="t", instance_url="https://x")
        table = c.get_schema("Account")
        name_col = next(col for col in table.columns if col.name == "Name")
        assert name_col.description == "Name"


# ---------- querying ---------- #


class TestExecuteQuery:
    def test_paginates_and_drops_attributes(self):
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query("SELECT Id, Name FROM Account")
        assert isinstance(df, pd.DataFrame)
        assert "attributes" not in df.columns
        assert list(df["Id"]) == ["001", "002", "003"]  # both pages merged

    def test_row_cap_stops_pagination(self, monkeypatch):
        # With the cap at 2, pagination must stop after the first (2-row) page.
        monkeypatch.setattr(sfmod, "MAX_ROWS", 2)
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query("SELECT Id FROM Account")
        assert len(df) == 2
        assert list(df["Id"]) == ["001", "002"]

    def test_query_error_wrapped(self, monkeypatch):
        def boom(self, soql):
            raise ValueError("MALFORMED_QUERY")
        monkeypatch.setattr(_FakeSalesforce, "query", boom)
        c = SalesforceClient(access_token="t", instance_url="https://x")
        with pytest.raises(RuntimeError, match="Error executing Salesforce query"):
            c.execute_query("SELECT bad FROM Account")


# ---------- connection / prompts ---------- #


class TestConnectionAndPrompts:
    def test_test_connection_success(self):
        c = SalesforceClient(access_token="t", instance_url="https://x")
        assert c.test_connection() == {"success": True, "message": "Connected to Salesforce"}

    def test_test_connection_failure(self, monkeypatch):
        def boom(self):
            raise RuntimeError("INVALID_SESSION_ID")
        monkeypatch.setattr(_FakeSalesforce, "limits", boom)
        c = SalesforceClient(access_token="t", instance_url="https://x")
        res = c.test_connection()
        assert res["success"] is False and "INVALID_SESSION_ID" in res["message"]

    def test_prompt_schema_renders(self):
        c = SalesforceClient(access_token="t", instance_url="https://x")
        out = c.prompt_schema()
        assert "Account" in out and "Widget__c" in out


# ---------- registry wiring ---------- #


class TestRegistryWiring:
    def test_resolves_client(self):
        from app.schemas.data_source_registry import resolve_client_class
        assert resolve_client_class("salesforce") is SalesforceClient

    def test_jwt_variant_registered(self):
        from app.schemas.data_source_registry import credentials_schema_for, get_entry
        from app.schemas.data_sources.configs import SalesforceJWTCredentials
        entry = get_entry("salesforce")
        assert "jwt" in entry.credentials_auth.by_auth
        assert entry.credentials_auth.default == "jwt"
        assert credentials_schema_for("salesforce", "jwt") is SalesforceJWTCredentials
        # System-scoped: a single shared service-account connection.
        assert entry.credentials_auth.by_auth["jwt"].scopes == ["system"]

    def test_jwt_credentials_validate(self):
        from app.schemas.data_sources.configs import SalesforceJWTCredentials
        creds = SalesforceJWTCredentials(consumer_key="ck", private_key="pem", username="u")
        assert creds.consumer_key == "ck"
        with pytest.raises(Exception):
            SalesforceJWTCredentials(consumer_key="ck")  # missing required fields

    def test_config_defaults(self):
        from app.schemas.data_sources.configs import SalesforceConfig
        cfg = SalesforceConfig()
        assert cfg.sandbox is False and cfg.domain == "login" and cfg.objects is None
