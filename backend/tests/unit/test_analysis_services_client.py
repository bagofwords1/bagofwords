"""Unit tests for AnalysisServicesClient — all XMLA transport is mocked."""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.data_sources.clients.analysis_services_client import AnalysisServicesClient


# ---------------------------------------------------------------------------
# Fixtures: canned XMLA SOAP response bodies (Discover/Execute rowsets).
# ---------------------------------------------------------------------------

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


CATALOGS_TWO = _discover_envelope(
    "<row><CATALOG_NAME>AdventureWorks</CATALOG_NAME></row>"
    "<row><CATALOG_NAME>Finance</CATALOG_NAME></row>"
)

# TMSCHEMA_MODEL probe: a row => Tabular; an XMLA error => Multidimensional.
TMSCHEMA_TABULAR = _execute_envelope("<row><Name>Model</Name></row>")
TMSCHEMA_MULTIDIM_ERR = _execute_envelope(
    '<Messages><Error ErrorCode="3240034318" '
    'Description="The $SYSTEM.TMSCHEMA_MODEL request is not supported."/></Messages>'
)

CUBES_SALES = _discover_envelope(
    "<row><CUBE_NAME>Sales</CUBE_NAME><CUBE_CAPTION>Sales</CUBE_CAPTION>"
    "<CUBE_TYPE>CUBE</CUBE_TYPE></row>"
)

HIERARCHIES_SALES = _discover_envelope(
    "<row><HIERARCHY_NAME>Category</HIERARCHY_NAME>"
    "<HIERARCHY_UNIQUE_NAME>[Product].[Category]</HIERARCHY_UNIQUE_NAME>"
    "<HIERARCHY_CAPTION>Category</HIERARCHY_CAPTION>"
    "<DIMENSION_UNIQUE_NAME>[Product]</DIMENSION_UNIQUE_NAME></row>"
)

MEASURES_SALES = _discover_envelope(
    "<row><MEASURE_NAME>Sales Amount</MEASURE_NAME>"
    "<MEASURE_UNIQUE_NAME>[Measures].[Sales Amount]</MEASURE_UNIQUE_NAME>"
    "<MEASURE_CAPTION>Sales Amount</MEASURE_CAPTION></row>"
)

EXECUTE_OK = _execute_envelope(
    "<row><Category>Bikes</Category><Sales_x0020_Amount>900</Sales_x0020_Amount></row>"
    "<row><Category>Helmets</Category><Sales_x0020_Amount>120</Sales_x0020_Amount></row>"
)


def _make_response(body: bytes, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.text = body.decode("utf-8", errors="ignore")
    return resp


def _install_post(client: AnalysisServicesClient, responses):
    session = MagicMock()
    iterator = iter(responses)

    def _post(url, data=None, headers=None, timeout=None, verify=None):
        try:
            return next(iterator)
        except StopIteration:  # pragma: no cover
            raise AssertionError(f"Unexpected extra POST to {url}")

    session.post.side_effect = _post
    client._http = session
    return session


def _client(**kwargs):
    return AnalysisServicesClient(
        host="https://ssas.example.com/olap/msmdpump.dll",
        username="u",
        password="p",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Model-type detection
# ---------------------------------------------------------------------------

class TestModelDetection:
    def test_tabular_detected(self):
        client = _client()
        _install_post(client, [_make_response(TMSCHEMA_TABULAR)])
        ctx = client._catalog_context("AdventureWorks")
        assert ctx == {"modelType": "TABULAR", "supportsDax": True}

    def test_multidimensional_detected(self):
        client = _client()
        _install_post(client, [_make_response(TMSCHEMA_MULTIDIM_ERR)])
        ctx = client._catalog_context("AdventureWorks")
        assert ctx == {"modelType": "MULTIDIMENSIONAL", "supportsDax": False}

    def test_get_schemas_tags_model_type(self):
        client = _client(catalog="AdventureWorks")
        _install_post(client, [
            _make_response(TMSCHEMA_TABULAR),   # _catalog_context probe
            _make_response(CUBES_SALES),
            _make_response(HIERARCHIES_SALES),
            _make_response(MEASURES_SALES),
        ])
        tables = client.get_schemas()
        assert [t.name for t in tables] == ["AdventureWorks/Sales"]
        meta = tables[0].metadata_json["analysis_services"]
        assert meta["modelType"] == "TABULAR"
        assert meta["supportsDax"] is True
        assert [(c.name, c.dtype) for c in tables[0].columns] == [
            ("Category", "dimension"),
            ("Sales Amount", "measure"),
        ]


# ---------------------------------------------------------------------------
# Query execution + dialect guard
# ---------------------------------------------------------------------------

class TestExecuteQuery:
    def test_mdx_executes(self):
        client = _client(catalog="AdventureWorks")
        _install_post(client, [_make_response(EXECUTE_OK)])
        df = client.execute_query("SELECT {[Measures].[Sales Amount]} ON COLUMNS FROM [Sales]")
        assert isinstance(df, pd.DataFrame)
        # _x0020_ in the tabular column tag is decoded back to a space.
        assert list(df.columns) == ["Category", "Sales Amount"]
        assert df.iloc[0]["Category"] == "Bikes"

    def test_dax_executes_without_table_hint(self):
        # No table_name → model type unknown → DAX is allowed through.
        client = _client(catalog="AdventureWorks")
        _install_post(client, [_make_response(EXECUTE_OK)])
        df = client.execute_query("EVALUATE SUMMARIZECOLUMNS(Product[Category])")
        assert list(df.columns) == ["Category", "Sales Amount"]

    def test_dax_rejected_on_multidimensional(self):
        client = _client(catalog="AdventureWorks")
        _install_post(client, [
            _make_response(TMSCHEMA_MULTIDIM_ERR),  # detection → multidimensional
            _make_response(CUBES_SALES),
            _make_response(HIERARCHIES_SALES),
            _make_response(MEASURES_SALES),
        ])
        with pytest.raises(RuntimeError, match="Multidimensional"):
            client.execute_query("EVALUATE Product", "AdventureWorks/Sales")

    def test_mdx_allowed_on_multidimensional_with_table(self):
        client = _client(catalog="AdventureWorks")
        _install_post(client, [
            _make_response(TMSCHEMA_MULTIDIM_ERR),  # detection → multidimensional
            _make_response(CUBES_SALES),
            _make_response(HIERARCHIES_SALES),
            _make_response(MEASURES_SALES),
            _make_response(EXECUTE_OK),             # the MDX query itself
        ])
        df = client.execute_query("SELECT {[Measures].[Sales Amount]} ON 0 FROM [Sales]", "AdventureWorks/Sales")
        assert len(df) == 2

    def test_empty_query_rejected(self):
        client = _client()
        with pytest.raises(ValueError, match="An MDX or DAX query is required"):
            client.execute_query("  ")


# ---------------------------------------------------------------------------
# test_connection / prompt / registry wiring
# ---------------------------------------------------------------------------

class TestTopLevel:
    def test_test_connection_ok(self):
        client = _client()
        _install_post(client, [_make_response(CATALOGS_TWO)])
        result = client.test_connection()
        assert result["success"] is True
        assert result["catalogs"] == 2

    def test_description_includes_both_dialects(self):
        text = _client().description
        assert "Analysis Services" in text
        assert "MDX" in text
        assert "DAX" in text

    def test_resolve_client_class(self):
        from app.schemas.data_source_registry import resolve_client_class
        assert resolve_client_class("analysis_services") is AnalysisServicesClient
