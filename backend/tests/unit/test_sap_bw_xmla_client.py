"""Unit tests for SapBwXmlaClient (SAP BW / BW4HANA over XMLA).

The XMLA SOAP transport is inherited from XmlaClient and mocked here (a
MagicMock session returning canned Discover/Execute envelopes). These tests
assert the BW-specific behavior:

- Endpoint construction: a bare host / origin gets /sap/bw/xml/soap/xmla
  appended; an already-full endpoint is preserved; sap-client / sap-language
  become query params; http/https normalization.
- Discovery: InfoProvider/query cubes -> one Table per cube named
  "Catalog/Cube" with characteristics (dimension) + key figures (measure),
  tagged under metadata key "sap_bw".
- Query: execute_query sends the MDX via XMLA Execute and returns a DataFrame,
  resolving the catalog from the "Catalog/Cube" table name.
- test_connection reports the catalog count; description carries BW MDX guidance.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.data_sources.clients.sap_bw_xmla_client import SapBwXmlaClient


# --- canned XMLA SOAP envelopes -------------------------------------------

def _discover_envelope(rows_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        '<DiscoverResponse xmlns="urn:schemas-microsoft-com:xml-analysis"><return>'
        '<root xmlns="urn:schemas-microsoft-com:xml-analysis:rowset">'
        f"{rows_xml}"
        "</root></return></DiscoverResponse>"
        "</soap:Body></soap:Envelope>"
    ).encode()


def _execute_envelope(rows_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        '<ExecuteResponse xmlns="urn:schemas-microsoft-com:xml-analysis"><return>'
        '<root xmlns="urn:schemas-microsoft-com:xml-analysis:rowset">'
        f"{rows_xml}"
        "</root></return></ExecuteResponse>"
        "</soap:Body></soap:Envelope>"
    ).encode()


CUBES = _discover_envelope(
    "<row><CUBE_NAME>0D_NW_C01_Q001</CUBE_NAME>"
    "<CUBE_CAPTION>Net Weight Query</CUBE_CAPTION><CUBE_TYPE>CUBE</CUBE_TYPE></row>"
)

HIERARCHIES = _discover_envelope(
    "<row><HIERARCHY_NAME>Country</HIERARCHY_NAME>"
    "<HIERARCHY_UNIQUE_NAME>[0D_NW_C01__ZCOUNTRY]</HIERARCHY_UNIQUE_NAME>"
    "<HIERARCHY_CAPTION>Country</HIERARCHY_CAPTION>"
    "<DIMENSION_UNIQUE_NAME>[0D_NW_C01__ZCOUNTRY]</DIMENSION_UNIQUE_NAME></row>"
)

MEASURES = _discover_envelope(
    "<row><MEASURE_NAME>Net Weight</MEASURE_NAME>"
    "<MEASURE_UNIQUE_NAME>[Measures].[4GBQ8]</MEASURE_UNIQUE_NAME>"
    "<MEASURE_CAPTION>Net Weight</MEASURE_CAPTION></row>"
)

EXECUTE_OK = _execute_envelope(
    "<row><Country>US</Country><Net_x0020_Weight>900</Net_x0020_Weight></row>"
    "<row><Country>DE</Country><Net_x0020_Weight>120</Net_x0020_Weight></row>"
)


def _make_response(body: bytes, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.text = body.decode("utf-8", errors="ignore")
    resp.headers = {}
    return resp


def _install_post(client: SapBwXmlaClient, responses):
    session = MagicMock()
    # Accept raw SOAP-envelope bytes and wrap them into response objects.
    iterator = iter(r if not isinstance(r, (bytes, bytearray)) else _make_response(r)
                    for r in responses)

    def _post(url, data=None, headers=None, timeout=None, verify=None):
        try:
            return next(iterator)
        except StopIteration:  # pragma: no cover
            raise AssertionError(f"Unexpected extra POST to {url}")

    session.post.side_effect = _post
    client._http = session
    return session


def _client(**kwargs):
    kwargs.setdefault("host", "https://bw.example.com:44300")
    kwargs.setdefault("username", "u")
    kwargs.setdefault("password", "p")
    return SapBwXmlaClient(**kwargs)


# --------------------------------------------------------------------------
# Endpoint construction
# --------------------------------------------------------------------------

class TestEndpoint:
    def test_bare_host_gets_scheme_and_xmla_path(self):
        c = SapBwXmlaClient(host="bw.example.com:44300", username="u", password="p")
        assert c.host == "https://bw.example.com:44300/sap/bw/xml/soap/xmla"

    def test_origin_gets_xmla_path_appended(self):
        c = _client()
        assert c.host == "https://bw.example.com:44300/sap/bw/xml/soap/xmla"

    def test_full_endpoint_is_preserved(self):
        c = SapBwXmlaClient(
            host="https://bw.example.com:44300/sap/bw/xml/soap/xmla",
            username="u", password="p",
        )
        assert c.host == "https://bw.example.com:44300/sap/bw/xml/soap/xmla"

    def test_sap_client_and_language_become_query_params(self):
        c = _client(sap_client="100", sap_language="EN")
        assert c.host.endswith("/sap/bw/xml/soap/xmla?sap-client=100&sap-language=EN")

    def test_custom_xmla_path(self):
        c = _client(xmla_path="/sap/bw/xml/soap/xmla_custom")
        assert c.host.endswith("/sap/bw/xml/soap/xmla_custom")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class TestDiscovery:
    def test_get_schemas_builds_table_with_dims_and_measures(self):
        c = _client(catalog="0D_NW_C01")
        _install_post(c, [CUBES, HIERARCHIES, MEASURES])
        tables = c.get_schemas()
        assert [t.name for t in tables] == ["0D_NW_C01/0D_NW_C01_Q001"]
        cols = [(col.name, col.dtype) for col in tables[0].columns]
        assert ("Country", "dimension") in cols
        assert ("Net Weight", "measure") in cols
        assert "sap_bw" in tables[0].metadata_json

    def test_measure_unique_name_carried_in_metadata(self):
        c = _client(catalog="0D_NW_C01")
        _install_post(c, [CUBES, HIERARCHIES, MEASURES])
        t = c.get_schemas()[0]
        measure = next(col for col in t.columns if col.name == "Net Weight")
        assert measure.metadata["unique_name"] == "[Measures].[4GBQ8]"
        assert measure.metadata["role"] == "measure"


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------

class TestQuery:
    def test_execute_query_returns_dataframe(self):
        c = _client()
        _install_post(c, [EXECUTE_OK])
        df = c.execute_query(
            "SELECT {[Measures].[4GBQ8]} ON COLUMNS FROM [0D_NW_C01_Q001]",
            "0D_NW_C01/0D_NW_C01_Q001",
        )
        assert isinstance(df, pd.DataFrame)
        assert list(df["Country"]) == ["US", "DE"]
        # XMLA _x0020_ escaping decoded back to a space in the column name.
        assert "Net Weight" in df.columns

    def test_execute_query_requires_statement(self):
        c = _client()
        with pytest.raises(ValueError):
            c.execute_query("", "0D_NW_C01/0D_NW_C01_Q001")


# --------------------------------------------------------------------------
# test_connection & prompt
# --------------------------------------------------------------------------

class TestConnectionAndPrompt:
    def test_connection_success_reports_catalog_count(self):
        c = _client(catalog="0D_NW_C01")
        _install_post(c, [CUBES])  # _list_catalogs short-circuits (catalog set)
        result = c.test_connection()
        assert result["success"] is True
        assert result["catalogs"] == 1

    def test_description_includes_bw_mdx_guide(self):
        c = _client()
        assert "SAP BW" in c.description
        assert "MDX" in c.description
