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
  executes the XML/OData lifecycle and maps paged results into a DataFrame.
- test_connection classifies success / zero-universe / auth failure.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs
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
            if "data" not in kwargs:
                return _resp(400, text="XML query required")
            root = ET.fromstring(kwargs["data"])
            self.selected = [e.attrib["id"] for e in root.iter() if e.tag.endswith("resultObject")]
            assert root.attrib["dataSourceId"] == "101"
            return _resp(200, text="<success><id>query-1</id></success>")
        return _resp(404, {}, text="not found")

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if "/data.svc/$metadata" in url:
            names = {"o1": "Country", "o2": "Store_name", "o3": "Salesrevenue"}
            props = ''.join('<Property Name="'+names[i]+'" Type="Edm.String" />' for i in self.selected)
            return _resp(200, text='<Schema><EntityType Name="Flow"><Key><PropertyRef Name="Id" /></Key><Property Name="Id" />'+props+'</EntityType><EntityContainer><EntitySet Name="Flows0" EntityType="Flow" /></EntityContainer></Schema>')
        if url.endswith("/data.svc"):
            return _resp(200, text='<service><collection href="Flows0" /></service>')
        if url.endswith('/data.svc/Flows0/$count'):
            return _resp(200, text=str(len(self._query_result.get('rows', []))))
        if "/data.svc/Flows0" in url:
            rows = self._query_result.get("rows", [])
            cols = self._query_result.get("columns", [])
            if rows and "cells" in rows[0]:
                rows = [dict(zip([c["name"] for c in cols], r["cells"])) for r in rows]
            rows = [{k.replace(" ", ""): v for k,v in r.items()} for r in rows]
            params = kwargs.get("params", {})
            skip, top = int(params.get("$skip",0)), int(params.get("$top",50))
            return _resp(200, {"value": rows[skip:skip+top]})
        if "/sl/v1/universes/101" in url:
            return _resp(200, UNIVERSE_101_DETAIL)
        if "/sl/v1/universes/102" in url:
            return _resp(200, UNIVERSE_102_DETAIL)
        if "/sl/v1/universes" in url:
            return _resp(200, UNIVERSES)
        return _resp(404, {}, text="not found")

    def delete(self, url, **kwargs):
        self.deleted = True
        return _resp(200, text="<success />")


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
        assert list(df["Sales revenue"]) == [100, 60]
        assert session.deleted

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
        session = FakeSession(query_result={"rows": [{"Country": str(i)} for i in range(50)]})
        c = _client(session=session)
        df = c.execute_query("Country", "eFashion", max_rows=10)
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


@pytest.mark.parametrize("dtype,expected", [("Numeric", "number"), ("Date", "datetime"), ("DateTime", "datetime")])
def test_sap_attribute_data_types(dtype, expected):
    session = FakeSession()
    original = session.get
    def get(url, **kwargs):
        if "/universes/101" in url:
            return _resp(200, {"universe": {"outline": {"item": {"id": "typed", "name": "Value", "@type": "Dimension", "@dataType": dtype}}}})
        return original(url, **kwargs)
    session.get = get
    assert _client(session).get_schema("eFashion").columns[0].dtype == expected


def test_query_pages_all_results():
    c = _client(FakeSession(query_result={"rows": [{"Country": str(i)} for i in range(7)]}), page_size=2)
    assert list(c.execute_query("Country", "eFashion")["Country"]) == [str(i) for i in range(7)]


def test_unknown_object_rejected_before_query():
    with pytest.raises(ValueError):
        _client().execute_query("Forbidden", "eFashion")


@pytest.mark.parametrize('status', [403, 500])
def test_query_failure_cleans_up_temporary_resource(status):
    session = FakeSession()
    original = session.get
    def get(url, **kwargs):
        if url.endswith('/data.svc'):
            return _resp(status, text='denied or unavailable')
        return original(url, **kwargs)
    session.get = get
    with pytest.raises(RuntimeError, match=str(status)):
        _client(session).execute_query('Country', 'eFashion')
    assert session.deleted


def test_failed_schema_discovery_can_be_retried():
    session = FakeSession()
    original = session.get
    failed = False
    def get(url, **kwargs):
        nonlocal failed
        if '/universes/101' in url and not failed:
            failed = True
            return _resp(503)
        return original(url, **kwargs)
    session.get = get
    c = _client(session)
    with pytest.raises(RuntimeError):
        c.get_tables()
    assert len(c.get_schema('eFashion').columns) == 3


def test_same_named_objects_remain_distinct():
    session = FakeSession()
    original = session.get
    def get(url, **kwargs):
        if '/universes/101' in url:
            return _resp(200, {'universe': {'outline': {'item': [
                {'id': 'left', 'name': 'Code', '@type': 'Dimension', '@dataType': 'String'},
                {'id': 'right', 'name': 'Code', '@type': 'Dimension', '@dataType': 'Numeric'},
            ]}}})
        return original(url, **kwargs)
    session.get = get
    cols = _client(session).get_schema('eFashion').columns
    assert len({c.name for c in cols}) == 2
    assert {c.metadata['object_id'] for c in cols} == {'left', 'right'}


@pytest.mark.parametrize('token,status', [('restricted-token',403), ('allowed-token',200)])
def test_query_uses_callers_identity(token, status):
    session = FakeSession(query_result={'rows': [{'Country': 'US'}]})
    original = session.get
    def get(url, **kwargs):
        if '/data.svc' in url:
            assert kwargs['headers']['X-SAP-LogonToken'] == '"'+token+'"'
            if status == 403:
                return _resp(403)
        return original(url, **kwargs)
    session.get = get
    c = _client(session, logon_token=token)
    if status == 403:
        with pytest.raises(RuntimeError, match='403'):
            c.execute_query('Country', 'eFashion')
    else:
        assert c.execute_query('Country', 'eFashion')['Country'].tolist() == ['US']
    assert session.deleted


def test_legacy_odata_envelope():
    session = FakeSession(query_result={'rows': [{'Country': 'FR'}]})
    original = session.get
    def get(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith('/data.svc/Flows0'):
            return _resp(200, {'d': {'results': response.json()['value']}})
        return response
    session.get = get
    assert _client(session).execute_query('Country', 'eFashion')['Country'].tolist() == ['FR']


@pytest.mark.parametrize('body', ['<service />', '<service><collection href="A"/><collection href="B"/></service>', '<service><collection href="https://other.invalid/data"/></service>'])
def test_unsupported_flows_fail_without_partial_results(body):
    session = FakeSession()
    original = session.get
    def get(url, **kwargs):
        if url.endswith('/data.svc'):
            return _resp(200, text=body)
        return original(url, **kwargs)
    session.get = get
    with pytest.raises((RuntimeError, ValueError)):
        _client(session).execute_query('Country', 'eFashion')
    assert session.deleted


@pytest.mark.parametrize('total,page_size', [(4,2),(5,2),(0,2)])
def test_paging_never_requests_past_sap_row_count(total,page_size):
    session = FakeSession(query_result={'rows':[{'Country':str(i)} for i in range(total)]})
    original=session.get
    def get(url, **kwargs):
        if url.endswith('/data.svc/Flows0'):
            offset=kwargs.get('params',{}).get('$skip',0)
            if offset >= total:
                return _resp(400,text='Only available rows may be requested')
        return original(url,**kwargs)
    session.get=get
    assert len(_client(session,page_size=page_size).execute_query('Country','eFashion'))==total
