"""Unit tests for SapDatasphereClient (SAP Datasphere semantic-layer OData).

Mocks the HTTP boundary only (a URL-dispatching fake requests.Session) and
asserts the behavior that matters:

- Auth: client_credentials grant is performed and cached; a per-user
  access_token short-circuits the token fetch entirely.
- Discovery: the catalog `assets` collection -> one Table per model named
  "Space/Model"; $metadata is parsed into role=measure / role=dimension columns
  (both explicit SAP measure annotations AND the numeric-type fallback).
- Space filter restricts discovery.
- Query: execute_query builds the correct analytical OData URL from
  select/filter/orderby/top, from a raw query string, and with analytic-model
  parameters ((P='v')/Set); parses `value` into a DataFrame; follows
  @odata.nextLink paging; honors max_rows.
- Robustness: DAC-protected model returning an empty `value` yields an empty
  DataFrame (no crash); test_connection classifies success / zero-asset / auth
  failure.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.data_sources.clients.sap_datasphere_client import SapDatasphereClient


HOST = "mytenant.us10.hcs.cloud.sap"
TOKEN_URL = "https://sub.authentication.us10.hana.ondemand.com/oauth/token"

# --- canned payloads -------------------------------------------------------

CATALOG_ASSETS = {
    "value": [
        {
            "name": "SalesAnalyticModel",
            "spaceName": "SALES",
            "label": "Sales Analytic Model",
            "supportsAnalyticalQueries": True,
            "assetAnalyticalMetadataUrl": "/api/v1/dwc/consumption/analytical/SALES/SalesAnalyticModel/$metadata",
            "assetAnalyticalDataUrl": "/api/v1/dwc/consumption/analytical/SALES/SalesAnalyticModel/SalesAnalyticModel",
        },
        {
            "name": "CustomersView",
            "spaceName": "MARKETING",
            "label": "Customers",
            "supportsAnalyticalQueries": False,
            "assetRelationalMetadataUrl": "/api/v1/dwc/consumption/relational/MARKETING/CustomersView/$metadata",
            "assetRelationalDataUrl": "/api/v1/dwc/consumption/relational/MARKETING/CustomersView/CustomersView",
        },
    ]
}

# EntityType with: two string dimensions, one annotated measure (Revenue),
# one numeric-fallback measure (Quantity, no annotation).
METADATA_XML = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
  <edmx:DataServices>
    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="SALES">
      <EntityType Name="SalesAnalyticModelType">
        <Property Name="Country" Type="Edm.String"/>
        <Property Name="Product" Type="Edm.String"/>
        <Property Name="Revenue" Type="Edm.Decimal">
          <Annotation Term="com.sap.vocabularies.Analytics.v1.Measure"/>
        </Property>
        <Property Name="Quantity" Type="Edm.Int64"/>
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""

CUSTOMERS_METADATA_XML = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
  <edmx:DataServices>
    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="MARKETING">
      <EntityType Name="CustomersViewType">
        <Property Name="CustomerName" Type="Edm.String"/>
        <Property Name="Region" Type="Edm.String"/>
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


def _resp(status=200, payload=None, content: bytes = b"", text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.content = content
    r.text = text or (json.dumps(payload) if payload is not None else "")
    return r


class FakeSession:
    """Routes GET/POST by URL substring to canned responses and records calls."""

    def __init__(self, query_pages=None):
        self.verify = True
        self.get_calls = []
        self.post_calls = []
        # query_pages: list of `value`-payloads returned successively for the
        # analytical data endpoint (to exercise @odata.nextLink paging).
        self._query_pages = list(query_pages or [{"value": []}])

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        # OAuth token endpoint.
        return _resp(200, {"access_token": "tech-tok", "expires_in": 3600})

    def get(self, url, **kwargs):
        self.get_calls.append(url)
        if "/catalog/assets" in url:
            return _resp(200, CATALOG_ASSETS)
        if url.endswith("/$metadata") and "SalesAnalyticModel" in url:
            return _resp(200, content=METADATA_XML.encode())
        if url.endswith("/$metadata") and "CustomersView" in url:
            return _resp(200, content=CUSTOMERS_METADATA_XML.encode())
        # Analytical / relational data endpoint (may be called with a query string).
        if "/consumption/analytical/" in url or "/consumption/relational/" in url:
            page = self._query_pages.pop(0) if self._query_pages else {"value": []}
            return _resp(200, page)
        return _resp(404, {}, text="not found")


def _client(space=None, access_token=None, session=None, **kw):
    c = SapDatasphereClient(
        host=HOST,
        token_url=TOKEN_URL,
        client_id="cid",
        client_secret="csecret",
        access_token=access_token,
        space=space,
        **kw,
    )
    c._http = session or FakeSession()
    return c


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

class TestAuth:
    def test_client_credentials_grant_is_performed_and_cached(self):
        c = _client()
        assert c._token() == "tech-tok"
        # Second call is cached — no second POST.
        assert c._token() == "tech-tok"
        assert len(c._http.post_calls) == 1
        url, kwargs = c._http.post_calls[0]
        assert url == TOKEN_URL
        assert kwargs["data"]["grant_type"] == "client_credentials"
        assert kwargs["auth"] == ("cid", "csecret")

    def test_per_user_access_token_short_circuits_token_fetch(self):
        c = _client(access_token="user-tok")
        assert c._token() == "user-tok"
        assert c._http.post_calls == []  # never hits the token endpoint

    def test_host_normalized_to_https_origin(self):
        c = _client()
        assert c.host == "https://mytenant.us10.hcs.cloud.sap"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class TestDiscovery:
    def test_get_schemas_builds_one_table_per_asset(self):
        c = _client()
        tables = c.get_schemas()
        names = sorted(t.name for t in tables)
        assert names == ["MARKETING/CustomersView", "SALES/SalesAnalyticModel"]

    def test_measures_and_dimensions_classified(self):
        c = _client()
        t = c.get_schema("SALES/SalesAnalyticModel")
        roles = {col.name: col.metadata.get("role") for col in t.columns}
        assert roles == {
            "Country": "dimension",
            "Product": "dimension",
            "Revenue": "measure",    # explicit SAP measure annotation
            "Quantity": "measure",   # numeric-type fallback
        }
        # Measure columns carry dtype "measure" for the schema display.
        rev = next(col for col in t.columns if col.name == "Revenue")
        assert rev.dtype == "measure"

    def test_metadata_stores_data_url_for_queries(self):
        c = _client()
        t = c.get_schema("SALES/SalesAnalyticModel")
        meta = t.metadata_json["sap_datasphere"]
        assert meta["space"] == "SALES"
        assert meta["asset"] == "SalesAnalyticModel"
        assert meta["kind"] == "analytical"
        assert meta["data_url"].endswith("/analytical/SALES/SalesAnalyticModel/SalesAnalyticModel")

    def test_space_filter_restricts_discovery(self):
        c = _client(space="SALES")
        names = [t.name for t in c.get_schemas()]
        assert names == ["SALES/SalesAnalyticModel"]

    def test_schemas_cached_across_calls(self):
        c = _client()
        c.get_schemas()
        first_get_count = len(c._http.get_calls)
        c.get_schemas()
        assert len(c._http.get_calls) == first_get_count  # no re-crawl


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------

class TestQuery:
    def test_execute_query_builds_odata_url_from_kwargs(self):
        session = FakeSession(query_pages=[{"value": [
            {"Country": "US", "Revenue": 100},
            {"Country": "DE", "Revenue": 60},
        ]}])
        c = _client(session=session)
        df = c.execute_query(
            table_name="SALES/SalesAnalyticModel",
            select="Country,Revenue",
            orderby="Revenue desc",
            top=100,
        )
        assert isinstance(df, pd.DataFrame)
        assert list(df["Country"]) == ["US", "DE"]
        data_call = [u for u in session.get_calls if "/analytical/" in u and "$select" in u][0]
        assert "$select=Country,Revenue" in data_call
        assert "$orderby=Revenue+desc" in data_call or "$orderby=Revenue desc" in data_call
        assert "$top=100" in data_call

    def test_execute_query_accepts_raw_query_string(self):
        session = FakeSession(query_pages=[{"value": [{"X": 1}]}])
        c = _client(session=session)
        c.execute_query(table_name="SALES/SalesAnalyticModel", query="$select=Country&$top=5")
        data_call = [u for u in session.get_calls if "/analytical/" in u and "$select" in u][0]
        assert "$select=Country" in data_call and "$top=5" in data_call

    def test_execute_query_parameters_apply_set_prefix(self):
        session = FakeSession(query_pages=[{"value": [{"X": 1}]}])
        c = _client(session=session)
        c.execute_query(
            table_name="SALES/SalesAnalyticModel",
            select="Country,Revenue",
            parameters={"P_Year": "2025"},
        )
        data_call = [u for u in session.get_calls if "/analytical/" in u and "/Set" in u][0]
        assert "(P_Year='2025')/Set" in data_call

    def test_execute_query_follows_nextlink_paging(self):
        session = FakeSession(query_pages=[
            {"value": [{"n": 1}], "@odata.nextLink": f"https://{HOST}/api/v1/dwc/consumption/analytical/SALES/SalesAnalyticModel/SalesAnalyticModel?$skiptoken=2"},
            {"value": [{"n": 2}]},
        ])
        c = _client(session=session)
        df = c.execute_query(table_name="SALES/SalesAnalyticModel", select="n")
        assert list(df["n"]) == [1, 2]

    def test_execute_query_honors_max_rows(self):
        session = FakeSession(query_pages=[{"value": [{"n": i} for i in range(50)]}])
        c = _client(session=session)
        df = c.execute_query(table_name="SALES/SalesAnalyticModel", select="n", max_rows=10)
        assert len(df) == 10

    def test_dac_protected_empty_result_yields_empty_dataframe(self):
        # DAC-protected model returns an empty value array to a technical user.
        session = FakeSession(query_pages=[{"value": []}])
        c = _client(session=session)
        df = c.execute_query(table_name="SALES/SalesAnalyticModel", select="Country,Revenue")
        assert isinstance(df, pd.DataFrame) and df.empty

    def test_execute_query_requires_known_table(self):
        c = _client()
        with pytest.raises(ValueError):
            c.execute_query(table_name="SALES/DoesNotExist", select="X")

    def test_query_passed_positionally_like_sql_clients(self):
        # The framework's QueryCapturingClientWrapper calls execute_query(query,
        # ...) with query as the first POSITIONAL arg. The OData options string
        # must work positionally, with table_name second (Power BI's shape).
        session = FakeSession(query_pages=[{"value": [{"Country": "US", "Revenue": 3500}]}])
        c = _client(session=session)
        df = c.execute_query("$select=Country,Revenue&$orderby=Revenue desc",
                             "SALES/SalesAnalyticModel")
        assert list(df["Country"]) == ["US"]
        data_call = [u for u in session.get_calls if "/analytical/" in u and "$select" in u][0]
        assert "$select=Country,Revenue" in data_call

    def test_model_name_as_first_positional_is_tolerated(self):
        # If the agent passes only the model name (no OData markers) as the first
        # positional arg, treat it as table_name instead of erroring.
        session = FakeSession(query_pages=[{"value": [{"Country": "US", "Revenue": 3500}]}])
        c = _client(session=session)
        df = c.execute_query("SALES/SalesAnalyticModel")
        assert not df.empty
        assert any("/analytical/SALES/SalesAnalyticModel" in u for u in session.get_calls)

    def test_odata_control_columns_dropped(self):
        session = FakeSession(query_pages=[{"value": [
            {"Country": "US", "Revenue": 100, "@odata.etag": "W/x"},
        ]}])
        c = _client(session=session)
        df = c.execute_query(table_name="SALES/SalesAnalyticModel", select="Country,Revenue")
        assert "@odata.etag" not in df.columns
        assert set(df.columns) == {"Country", "Revenue"}


# --------------------------------------------------------------------------
# test_connection & prompt
# --------------------------------------------------------------------------

class TestConnectionAndPrompt:
    def test_connection_success_reports_asset_count(self):
        c = _client()
        result = c.test_connection()
        assert result["success"] is True
        assert result["assets"] == 2

    def test_connection_zero_assets_adds_hint(self):
        session = FakeSession()
        session.get = lambda url, **kw: _resp(200, {"value": []})  # empty catalog
        c = _client(session=session)
        result = c.test_connection()
        assert result["success"] is True
        assert result["assets"] == 0
        assert "Expose for Consumption" in result["message"]

    def test_connection_auth_failure_classified(self):
        session = FakeSession()
        session.post = lambda url, **kw: _resp(401, text="bad client")
        c = _client(session=session)
        result = c.test_connection()
        assert result["success"] is False
        assert "Authentication failed" in result["message"]

    def test_prompt_schema_renders_models(self):
        c = _client()
        text = c.prompt_schema()
        assert "SalesAnalyticModel" in text

    def test_description_includes_query_guide(self):
        c = _client()
        assert "SAP Datasphere Query Guide" in c.description
