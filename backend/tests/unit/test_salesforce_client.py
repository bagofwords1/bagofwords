"""Unit tests for SalesforceClient.

Covers:
- auth-mode selection (jwt / access-token / username-password) and the kwargs
  handed to simple_salesforce.Salesforce
- sandbox / My-Domain routing (the previously-ignored domain/sandbox fields)
- JWT Bearer token exchange (real PyJWT RS256 signing, mocked token endpoint)
- Client Credentials token exchange (no username — Run As user on the app)
- dynamic object discovery: custom objects included, system/noise/deprecated
  objects excluded, and no longer the hardcoded five
- get_schema field-type mapping + reference-field foreign keys + Id primary key
- execute_query -> DataFrame (attributes dropped, MAX_ROWS pagination cap)
- empty results keeping the SELECT list's columns, and COUNT() reporting
- relationship payloads flattened to dotted columns / primitive values
- error reporting: errorCode first, query failures not labelled connection failures
- registry wiring for the jwt + client_credentials + userpass variants

simple_salesforce.Salesforce and the token HTTP endpoint are mocked, so these
run without a live org or network.
"""
from __future__ import annotations

import json

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
    # Per-test override of the canned query pages (see _set_pages).
    pages: list | None = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self._global = _global_describe
        self._object_fields = _object_fields
        override = type(self).pages
        self._pages = list(override if override is not None else _query_pages)

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
    monkeypatch.setattr(_FakeSalesforce, "pages", None)
    monkeypatch.setattr(sfmod, "Salesforce", _FakeSalesforce)


def _set_pages(monkeypatch, pages):
    """Point the fake org at a specific set of query-result pages."""
    monkeypatch.setattr(_FakeSalesforce, "pages", pages)


class _FakeSalesforceError(Exception):
    """Stands in for simple_salesforce's SalesforceError shape.

    Its `__str__` leads with the request URL — which for a query carries the
    whole URL-encoded SOQL — and leaves errorCode/message at the end, which is
    exactly what format_salesforce_error has to undo.
    """

    def __init__(self, url, content, status=400, resource_name="query"):
        super().__init__("Malformed request")
        self.url = url
        self.status = status
        self.resource_name = resource_name
        self.content = content

    def __str__(self):
        return f"Malformed request {self.url}. Response content: {self.content}"


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

    def test_jwt_without_username_names_the_missing_field(self, private_key_pem):
        # Regression: a Connected App with a blank Username used to fall
        # through to the generic "no usable credentials" error, which read as
        # bad credentials rather than an empty required box.
        c = SalesforceClient(consumer_key="CK", private_key=private_key_pem)
        assert c._auth_mode == "none"
        with pytest.raises(RuntimeError, match="requires a Username"):
            _ = c.sf

    def test_connected_app_without_any_secret_names_both_options(self):
        c = SalesforceClient(consumer_key="CK")
        with pytest.raises(RuntimeError, match="missing its secret"):
            _ = c.sf

    def test_full_host_domain_used_verbatim(self):
        # Sandbox My Domains (acme--dev.sandbox.my.salesforce.com) cannot be
        # expressed as a bare subdomain, and the sandbox toggle must not
        # override an explicit host.
        c = SalesforceClient(domain="acme--dev.sandbox.my.salesforce.com", sandbox=True)
        assert c._login_url() == "https://acme--dev.sandbox.my.salesforce.com"
        c = SalesforceClient(domain="https://acme.my.salesforce.com/")
        assert c._login_url() == "https://acme.my.salesforce.com"


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


# ---------- Client Credentials flow ---------- #


class TestClientCredentials:
    def test_exchange_posts_key_and_secret_without_a_username(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return _FakeResponse(200, {
                "access_token": "CC_TOKEN",
                "instance_url": "https://rooms.my.salesforce.com",
            })

        monkeypatch.setattr(sfmod.requests, "post", fake_post)

        c = SalesforceClient(consumer_key="CK", consumer_secret="CS", domain="rooms")
        assert c._auth_mode == "client_credentials"
        _ = c.sf

        assert captured["url"] == "https://rooms.my.salesforce.com/services/oauth2/token"
        assert captured["data"] == {
            "grant_type": "client_credentials",
            "client_id": "CK",
            "client_secret": "CS",
        }
        # The identity comes from the app's Run As user — nothing user-shaped
        # may be smuggled into the request.
        assert "username" not in captured["data"] and "assertion" not in captured["data"]
        assert _FakeSalesforce.last_kwargs == {
            "instance_url": "https://rooms.my.salesforce.com",
            "session_id": "CC_TOKEN",
        }

    def test_private_key_still_selects_jwt(self, monkeypatch, private_key_pem):
        # Both variants share consumer_key; the secret-vs-private-key split is
        # what discriminates them, since the chosen auth name never reaches
        # the client (construct_client strips it).
        monkeypatch.setattr(sfmod.requests, "post", lambda *a, **k: _FakeResponse(
            200, {"access_token": "t", "instance_url": "https://x.my.salesforce.com"}))
        c = SalesforceClient(consumer_key="CK", private_key=private_key_pem, username="u@x.com")
        assert c._auth_mode == "jwt"

    def test_generic_login_host_gets_a_my_domain_hint(self, monkeypatch):
        monkeypatch.setattr(sfmod.requests, "post", lambda *a, **k: _FakeResponse(
            400, {"error": "invalid_client", "error_description": "invalid client credentials"}))
        c = SalesforceClient(consumer_key="CK", consumer_secret="CS")  # domain left at "login"
        with pytest.raises(RuntimeError, match="My Domain"):
            _ = c.sf

    def test_my_domain_failure_omits_the_hint(self, monkeypatch):
        monkeypatch.setattr(sfmod.requests, "post", lambda *a, **k: _FakeResponse(
            400, {"error": "invalid_client", "error_description": "invalid client credentials"}))
        c = SalesforceClient(consumer_key="CK", consumer_secret="CS", domain="rooms")
        with pytest.raises(RuntimeError) as exc:
            _ = c.sf
        assert "invalid_client" in str(exc.value)
        assert "Run As user" in str(exc.value)
        assert "My Domain" not in str(exc.value)


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
        with pytest.raises(RuntimeError, match="Salesforce query failed"):
            c.execute_query("SELECT bad FROM Account")

    def test_query_error_is_not_labelled_a_connection_error(self, monkeypatch):
        # connect() used to wrap the yielded body too, so every malformed query
        # was reported as "Error while connecting to Salesforce".
        def boom(self, soql):
            raise ValueError("MALFORMED_QUERY")
        monkeypatch.setattr(_FakeSalesforce, "query", boom)
        c = SalesforceClient(access_token="t", instance_url="https://x")
        with pytest.raises(RuntimeError) as excinfo:
            c.execute_query("SELECT bad FROM Account")
        assert "connect" not in str(excinfo.value).lower()
        assert not isinstance(excinfo.value, sfmod.SalesforceConnectionError)

    def test_api_error_leads_with_error_code_not_url(self, monkeypatch):
        # The actionable part must survive a 200-character truncation.
        def boom(self, soql):
            raise _FakeSalesforceError(
                url="https://acme.my.salesforce.com/services/data/v60.0/query/?q=" + "%20" * 200,
                content=[{
                    "message": "\nORDER BY totalSales DESC\n       ^\nERROR at Row:5:Column:14\n"
                               "No such column 'totalSales' on entity 'Opportunity'.",
                    "errorCode": "INVALID_FIELD",
                }],
            )
        monkeypatch.setattr(_FakeSalesforce, "query", boom)
        c = SalesforceClient(access_token="t", instance_url="https://x")
        with pytest.raises(RuntimeError) as excinfo:
            c.execute_query("SELECT SUM(Amount) totalSales FROM Opportunity ORDER BY totalSales")
        message = str(excinfo.value)
        assert "INVALID_FIELD" in message[:200]
        assert "No such column 'totalSales'" in message[:200]
        assert "salesforce.com/services/data" not in message

    def test_api_error_includes_offending_fields(self, monkeypatch):
        def boom(self, soql):
            raise _FakeSalesforceError(
                url="https://acme.my.salesforce.com/x",
                content=[{"message": "No such column", "errorCode": "INVALID_FIELD",
                          "fields": ["Revenue__c"]}],
            )
        monkeypatch.setattr(_FakeSalesforce, "query", boom)
        c = SalesforceClient(access_token="t", instance_url="https://x")
        with pytest.raises(RuntimeError, match=r"fields: Revenue__c"):
            c.execute_query("SELECT Revenue__c FROM Account")

    def test_connection_failure_is_not_relabelled_as_a_query_failure(self, monkeypatch):
        c = SalesforceClient()  # no credentials -> connection error
        with pytest.raises(sfmod.SalesforceConnectionError) as excinfo:
            c.execute_query("SELECT Id FROM Account")
        assert "query failed" not in str(excinfo.value).lower()


# ---------- empty results keep their columns ---------- #


class TestEmptyResultColumns:
    """A query that matches no rows must not come back shapeless.

    `pd.DataFrame([])` has zero columns, so callers indexing a column got a
    bare KeyError and empty-result guards read it as "execute_query was never
    called" — both of which look like a broken query rather than an empty table.
    """

    def test_empty_result_keeps_select_list_columns(self, monkeypatch):
        _set_pages(monkeypatch, [{"done": True, "totalSize": 0, "records": []}])
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query("SELECT AccountId, TotalAmount FROM Order LIMIT 10000")
        assert len(df) == 0
        assert list(df.columns) == ["AccountId", "TotalAmount"]
        # The failure this prevents: KeyError('AccountId') on an empty object.
        assert list(df["AccountId"]) == []

    def test_empty_result_keeps_aggregate_alias_columns(self, monkeypatch):
        _set_pages(monkeypatch, [{"done": True, "totalSize": 0, "records": []}])
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query(
            "SELECT AccountId, SUM(Amount) totalSales, COUNT(Id) dealCount "
            "FROM Opportunity GROUP BY AccountId"
        )
        assert list(df.columns) == ["AccountId", "totalSales", "dealCount"]

    def test_empty_result_names_unaliased_aggregates_expr_n(self, monkeypatch):
        _set_pages(monkeypatch, [{"done": True, "totalSize": 0, "records": []}])
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query("SELECT SUM(Amount), COUNT(Id) FROM Opportunity")
        assert list(df.columns) == ["expr0", "expr1"]

    def test_unparseable_query_falls_back_to_empty_frame(self, monkeypatch):
        _set_pages(monkeypatch, [{"done": True, "totalSize": 0, "records": []}])
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query("FIND {Acme} RETURNING Account(Name)")
        assert list(df.columns) == []

    def test_count_query_returns_the_count(self, monkeypatch):
        # SELECT COUNT() answers in totalSize and returns no records at all.
        _set_pages(monkeypatch, [{"done": True, "totalSize": 1474, "records": []}])
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query("SELECT COUNT() FROM Account")
        assert df.to_dict("records") == [{"expr0": 1474}]


# ---------- nested relationship payloads ---------- #


class TestRelationshipFlattening:
    def test_parent_traversal_flattens_to_dotted_columns(self, monkeypatch):
        _set_pages(monkeypatch, [{
            "done": True,
            "records": [{
                "attributes": {"type": "Opportunity"},
                "Amount": 100,
                "Account": {"attributes": {"type": "Account"}, "Name": "Acme",
                            "Owner": {"attributes": {"type": "User"}, "Name": "Dana"}},
            }],
        }])
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query("SELECT Amount, Account.Name, Account.Owner.Name FROM Opportunity")
        assert list(df.columns) == ["Amount", "Account.Name", "Account.Owner.Name"]
        assert df["Account.Name"][0] == "Acme"
        assert df["Account.Owner.Name"][0] == "Dana"
        # Nested `attributes` keys must not survive the flattening.
        assert not any("attributes" in c for c in df.columns)

    def test_null_parent_still_yields_the_dotted_column(self, monkeypatch):
        _set_pages(monkeypatch, [{
            "done": True,
            "records": [{"attributes": {}, "Amount": 100, "Account": None}],
        }])
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query("SELECT Amount, Account.Name FROM Opportunity")
        assert "Account.Name" in df.columns
        assert df["Account.Name"][0] is None

    def test_child_subquery_serialized_to_primitives(self, monkeypatch):
        _set_pages(monkeypatch, [{
            "done": True,
            "records": [{
                "attributes": {},
                "Name": "Acme",
                "Opportunities": {
                    "done": True,
                    "totalSize": 2,
                    "records": [
                        {"attributes": {}, "Amount": 10},
                        {"attributes": {}, "Amount": 20},
                    ],
                },
            }],
        }])
        c = SalesforceClient(access_token="t", instance_url="https://x")
        df = c.execute_query("SELECT Name, (SELECT Amount FROM Opportunities) FROM Account")
        assert list(df.columns) == ["Name", "Opportunities"]
        value = df["Opportunities"][0]
        # Primitive, per the output contract — not a dict the chart layer chokes on.
        assert isinstance(value, str)
        assert json.loads(value) == [{"Amount": 10}, {"Amount": 20}]


# ---------- SOQL select-list parsing ---------- #


class TestSoqlSelectColumns:
    @pytest.mark.parametrize("query,expected", [
        ("SELECT Id, Name FROM Account", ["Id", "Name"]),
        ("select id, name from account limit 5", ["id", "name"]),
        ("SELECT Account.Name, Owner.Profile.Name FROM Contact", ["Account.Name", "Owner.Profile.Name"]),
        ("SELECT SUM(Amount) totalSales FROM Opportunity", ["totalSales"]),
        ("SELECT SUM( Amount ) FROM Opportunity", ["expr0"]),
        ("SELECT COUNT() FROM Account", ["expr0"]),
        ("SELECT Id, SUM(Amount) AS total FROM Opportunity", ["Id", "total"]),
        ("SELECT Name, (SELECT Amount FROM Opportunities) FROM Account", ["Name", "Opportunities"]),
        ("SELECT Id FROM Account WHERE Name = 'from, here'", ["Id"]),
        ("SELECT\n  Id,\n  Name\nFROM Account", ["Id", "Name"]),
        ("SELECT FromDate__c FROM Event__c", ["FromDate__c"]),
        ("not a soql query", []),
    ])
    def test_parsing(self, query, expected):
        assert sfmod.soql_select_columns(query) == expected


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

    def test_client_credentials_variant_registered(self):
        from app.schemas.data_source_registry import credentials_schema_for, get_entry
        from app.schemas.data_sources.configs import SalesforceClientCredentials
        entry = get_entry("salesforce")
        assert "client_credentials" in entry.credentials_auth.by_auth
        assert credentials_schema_for("salesforce", "client_credentials") is SalesforceClientCredentials
        assert entry.credentials_auth.by_auth["client_credentials"].scopes == ["system"]
        # JWT stays the default — client credentials needs a Run As user set
        # up in Salesforce first.
        assert entry.credentials_auth.default == "jwt"

    def test_client_credentials_schema_has_no_username(self):
        from app.schemas.data_sources.configs import SalesforceClientCredentials
        fields = SalesforceClientCredentials.model_fields
        assert set(fields) == {"consumer_key", "consumer_secret"}
        creds = SalesforceClientCredentials(consumer_key="ck", consumer_secret="cs")
        assert creds.consumer_secret == "cs"
        with pytest.raises(Exception):
            SalesforceClientCredentials(consumer_key="ck")  # secret is required

    def test_client_credentials_fields_reach_the_client_constructor(self):
        # construct_client narrows params to the ctor signature, so a schema
        # field the ctor does not accept is silently dropped.
        import inspect
        from app.schemas.data_sources.configs import SalesforceClientCredentials
        sig = inspect.signature(SalesforceClient.__init__).parameters
        for key in SalesforceClientCredentials.model_fields:
            assert key in sig, f"SalesforceClient.__init__ drops {key}"

    def test_config_defaults(self):
        from app.schemas.data_sources.configs import SalesforceConfig
        cfg = SalesforceConfig()
        assert cfg.sandbox is False and cfg.domain == "login" and cfg.objects is None
