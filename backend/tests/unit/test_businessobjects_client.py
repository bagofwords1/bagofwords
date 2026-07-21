"""Unit tests for BusinessObjectsClient (SAP BusinessObjects /biprws REST).

Mocks the HTTP boundary only (a URL-dispatching fake requests.Session) and
asserts the behavior that matters:

- Auth / logon-token lifecycle: username/password logon posts the auth plugin
  and captures X-SAP-LogonToken; trusted auth sends X-SAP-TRUSTED-USER with the
  shared secret and NO password; a pre-supplied token short-circuits logon; the
  token rides on every call double-quoted.
- Discovery: /sl/v1/universes -> one Table per universe; the universe outline is
  flattened into role=dimension / role=measure columns (SAP's list-or-dict
  collapsing tolerated).
- Query: execute_query resolves the universe, posts the result objects, and
  normalizes both tabular and columnar result payloads into a DataFrame.
- test_connection classifies success / zero-universe / auth failure.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.data_sources.clients.businessobjects_client import BusinessObjectsClient


HOST = "https://boserver:6405"
TOKEN = "logon-token-abc"

# --- canned payloads -------------------------------------------------------

UNIVERSES = {
    "universes": {
        "universe": [
            {"id": "101", "name": "eFashion", "type": "unx", "folderName": "Webi Universes"},
            {"id": "102", "name": "Sales", "type": "unx"},
        ]
    }
}

# Nested outline with a folder containing a dimension, a detail (attribute) and a
# measure; a filter that must be skipped.
UNIVERSE_101_DETAIL = {
    "universe": {
        "id": "101",
        "name": "eFashion",
        "outline": {
            "folder": [
                {
                    "name": "Geography",
                    "item": [
                        {"id": "o1", "name": "Country", "type": "dimension", "dataType": "String"},
                        {"id": "o2", "name": "Store name", "type": "detail", "dataType": "String"},
                    ],
                },
                {
                    "name": "Measures",
                    # SAP collapses a single child to a dict rather than a list.
                    "item": {"id": "o3", "name": "Sales revenue", "type": "measure", "dataType": "Numeric"},
                },
                {"id": "f1", "name": "Last Year", "type": "filter"},
            ]
        },
    }
}

UNIVERSE_102_DETAIL = {"universe": {"id": "102", "name": "Sales", "outline": {"folder": []}}}


def _resp(status=200, payload=None, headers=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.headers = headers or {}
    r.text = text or (json.dumps(payload) if payload is not None else "")
    return r


class FakeSession:
    """Routes GET/POST by URL substring to canned responses; records calls."""

    def __init__(self, query_result=None, logon_status=200, logon_token=TOKEN):
        self.verify = True
        self.get_calls = []
        self.post_calls = []
        self._query_result = query_result if query_result is not None else {"rows": []}
        self._logon_status = logon_status
        self._logon_token = logon_token

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if url.endswith("/logon/long"):
            if self._logon_status >= 300:
                return _resp(self._logon_status, text="bad credentials")
            return _resp(200, {}, headers={"X-SAP-LogonToken": self._logon_token})
        if url.endswith("/logoff"):
            return _resp(200, {})
        if "/sl/v1/queries" in url:
            return _resp(200, self._query_result)
        return _resp(404, {}, text="not found")

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if "/sl/v1/universes/101" in url:
            return _resp(200, UNIVERSE_101_DETAIL)
        if "/sl/v1/universes/102" in url:
            return _resp(200, UNIVERSE_102_DETAIL)
        if "/sl/v1/universes" in url:
            return _resp(200, UNIVERSES)
        return _resp(404, {}, text="not found")


def _client(session=None, **kw):
    c = BusinessObjectsClient(host=HOST, username="admin", password="secret", **kw)
    c._http = session or FakeSession()
    return c


# --------------------------------------------------------------------------
# Base URL
# --------------------------------------------------------------------------

class TestBaseUrl:
    def test_biprws_appended_to_origin(self):
        c = _client()
        assert c.base_url == "https://boserver:6405/biprws"

    def test_existing_biprws_not_duplicated(self):
        c = BusinessObjectsClient(host="https://boserver:6405/biprws", username="a", password="b")
        assert c.base_url == "https://boserver:6405/biprws"

    def test_bare_host_gets_https(self):
        c = BusinessObjectsClient(host="boserver:6405", username="a", password="b")
        assert c.base_url == "https://boserver:6405/biprws"


# --------------------------------------------------------------------------
# Auth / logon-token lifecycle
# --------------------------------------------------------------------------

class TestAuth:
    def test_userpass_logon_posts_auth_plugin_and_captures_token(self):
        c = _client(auth_type="secLDAP")
        assert c._logon() == TOKEN
        url, kwargs = c._http.post_calls[0]
        assert url.endswith("/logon/long")
        assert kwargs["json"] == {"userName": "admin", "password": "secret", "auth": "secLDAP"}
        # Cached — second call does not re-post.
        assert c._logon() == TOKEN
        assert len([u for u, _ in c._http.post_calls if u.endswith("/logon/long")]) == 1

    def test_token_sent_double_quoted_on_calls(self):
        c = _client()
        c.get_schemas()
        # Any discovery GET carries the quoted token header.
        _, kwargs = c._http.get_calls[0]
        assert kwargs["headers"]["X-SAP-LogonToken"] == f'"{TOKEN}"'

    def test_trusted_auth_sends_impersonation_header_no_password(self):
        session = FakeSession()
        c = BusinessObjectsClient(
            host=HOST, trusted_user="jdoe", shared_secret="s3cr3t"
        )
        c._http = session
        assert c._logon() == TOKEN
        _, kwargs = session.post_calls[0]
        headers = kwargs["headers"]
        assert headers["X-SAP-TRUSTED-USER"] == "jdoe"
        assert headers["X-SAP-TRUSTED-AUTH"] == "s3cr3t"
        # No password anywhere in the trusted logon body.
        assert "password" not in (kwargs.get("json") or {})

    def test_pre_supplied_token_short_circuits_logon(self):
        session = FakeSession()
        c = BusinessObjectsClient(host=HOST, logon_token="preminted")
        c._http = session
        assert c._logon() == "preminted"
        assert session.post_calls == []  # never hits /logon/long

    def test_userpass_missing_raises(self):
        c = BusinessObjectsClient(host=HOST)
        c._http = FakeSession()
        with pytest.raises(RuntimeError):
            c._logon()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class TestDiscovery:
    def test_get_schemas_one_table_per_universe(self):
        c = _client()
        tables = c.get_schemas()
        assert sorted(t.name for t in tables) == ["Sales", "eFashion"]

    def test_universe_outline_flattened_into_roles(self):
        c = _client()
        t = c.get_schema("eFashion")
        roles = {col.name: col.metadata.get("role") for col in t.columns}
        assert roles == {
            "Country": "dimension",
            "Store name": "dimension",   # 'detail' maps to dimension
            "Sales revenue": "measure",  # single dict child normalized to list
        }
        # The filter object is not a result column.
        assert "Last Year" not in roles

    def test_measure_dtype_and_metadata(self):
        c = _client()
        t = c.get_schema("eFashion")
        rev = next(col for col in t.columns if col.name == "Sales revenue")
        assert rev.dtype == "measure"
        assert t.metadata_json["businessobjects"]["universe_id"] == "101"

    def test_universe_without_detail_still_yields_table(self):
        c = _client()
        t = c.get_schema("Sales")
        assert t.name == "Sales"
        assert t.columns == []

    def test_schemas_cached(self):
        c = _client()
        c.get_schemas()
        n = len(c._http.get_calls)
        c.get_schemas()
        assert len(c._http.get_calls) == n


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------

class TestQuery:
    def test_execute_query_posts_result_objects(self):
        session = FakeSession(query_result={"rows": [
            {"Country": "US", "Sales revenue": 100},
            {"Country": "FR", "Sales revenue": 60},
        ]})
        c = _client(session=session)
        df = c.execute_query("Country,Sales revenue", "eFashion")
        assert list(df["Country"]) == ["US", "FR"]
        # The query POST references the universe id and the result objects.
        q_url, q_kwargs = next((u, k) for u, k in session.post_calls if "/sl/v1/queries" in u)
        spec = q_kwargs["json"]["queryData"]
        assert spec["universe"]["id"] == "101"
        assert [o["name"] for o in spec["resultObjects"]] == ["Country", "Sales revenue"]

    def test_execute_query_normalizes_columnar_payload(self):
        session = FakeSession(query_result={
            "columns": [{"name": "Country"}, {"name": "Sales revenue"}],
            "rows": [{"cells": ["US", 100]}, {"cells": ["FR", 60]}],
        })
        c = _client(session=session)
        df = c.execute_query("Country,Sales revenue", "eFashion")
        assert list(df.columns) == ["Country", "Sales revenue"]
        assert list(df["Sales revenue"]) == [100, 60]

    def test_execute_query_select_kwarg_with_positional_universe(self):
        session = FakeSession(query_result={"rows": [{"Country": "US"}]})
        c = _client(session=session)
        # First positional is the universe name; objects come from select=.
        df = c.execute_query("eFashion", select="Country")
        assert list(df["Country"]) == ["US"]

    def test_execute_query_unknown_universe_raises(self):
        c = _client()
        with pytest.raises(ValueError):
            c.execute_query("Country", "DoesNotExist")

    def test_execute_query_requires_result_objects(self):
        c = _client()
        with pytest.raises(ValueError):
            c.execute_query("eFashion")  # resolves to universe, but no objects

    def test_empty_result_yields_empty_dataframe(self):
        session = FakeSession(query_result={"rows": []})
        c = _client(session=session)
        df = c.execute_query("Country,Sales revenue", "eFashion")
        assert isinstance(df, pd.DataFrame) and df.empty
        assert list(df.columns) == ["Country", "Sales revenue"]

    def test_max_rows_caps_result(self):
        session = FakeSession(query_result={"rows": [{"n": i} for i in range(50)]})
        c = _client(session=session)
        df = c.execute_query("n", "eFashion", max_rows=10)
        assert len(df) == 10


# --------------------------------------------------------------------------
# test_connection & prompt
# --------------------------------------------------------------------------

class TestConnectionAndPrompt:
    def test_connection_success_reports_universe_count(self):
        c = _client()
        result = c.test_connection()
        assert result["success"] is True
        assert result["universes"] == 2

    def test_connection_zero_universes_adds_hint(self):
        session = FakeSession()
        session.get = lambda url, **kw: _resp(200, {"universes": {"universe": []}})
        c = _client(session=session)
        result = c.test_connection()
        assert result["success"] is True
        assert result["universes"] == 0
        assert "published" in result["message"]

    def test_connection_auth_failure_classified(self):
        session = FakeSession(logon_status=401)
        c = _client(session=session)
        result = c.test_connection()
        assert result["success"] is False
        assert "Authentication failed" in result["message"]

    def test_prompt_schema_renders_universes(self):
        c = _client()
        text = c.prompt_schema()
        assert "eFashion" in text

    def test_description_includes_query_guide(self):
        c = _client()
        assert "BusinessObjects Query Guide" in c.description
