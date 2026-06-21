"""Unit tests for InforOlapClient — all XMLA transport is mocked."""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.data_sources.clients.infor_olap_client import InforOlapClient

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
    "<row><CATALOG_NAME>Finance</CATALOG_NAME></row>"
    "<row><CATALOG_NAME>Sales</CATALOG_NAME></row>"
)

CATALOGS_EMPTY = _discover_envelope("")

CUBES_FINANCE = _discover_envelope(
    "<row><CUBE_NAME>GL</CUBE_NAME><CUBE_CAPTION>General Ledger</CUBE_CAPTION>"
    "<CUBE_TYPE>CUBE</CUBE_TYPE><DESCRIPTION>GL cube</DESCRIPTION></row>"
    # A dimension-only row that must be filtered out.
    "<row><CUBE_NAME>$Account</CUBE_NAME><CUBE_TYPE>DIMENSION</CUBE_TYPE></row>"
)

HIERARCHIES_GL = _discover_envelope(
    "<row><HIERARCHY_NAME>Account</HIERARCHY_NAME>"
    "<HIERARCHY_UNIQUE_NAME>[Account]</HIERARCHY_UNIQUE_NAME>"
    "<HIERARCHY_CAPTION>Account</HIERARCHY_CAPTION>"
    "<DIMENSION_UNIQUE_NAME>[Account]</DIMENSION_UNIQUE_NAME></row>"
    # Measures-dimension hierarchy (DIMENSION_TYPE 2) must be skipped.
    "<row><HIERARCHY_NAME>Measures</HIERARCHY_NAME>"
    "<HIERARCHY_UNIQUE_NAME>[Measures]</HIERARCHY_UNIQUE_NAME>"
    "<DIMENSION_TYPE>2</DIMENSION_TYPE></row>"
)

MEASURES_GL = _discover_envelope(
    "<row><MEASURE_NAME>Amount</MEASURE_NAME>"
    "<MEASURE_UNIQUE_NAME>[Measures].[Amount]</MEASURE_UNIQUE_NAME>"
    "<MEASURE_CAPTION>Amount</MEASURE_CAPTION><DATA_TYPE>5</DATA_TYPE></row>"
)

EXECUTE_OK = _execute_envelope(
    "<row><Account>Cash</Account><Amount>100</Amount></row>"
    "<row><Account>Receivables</Account><Amount>250</Amount></row>"
)

EXECUTE_EMPTY = _execute_envelope("")

SOAP_FAULT = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    b"<soap:Body><soap:Fault>"
    b"<faultcode>soap:Client</faultcode>"
    b"<faultstring>Invalid credentials</faultstring>"
    b"</soap:Fault></soap:Body></soap:Envelope>"
)

XMLA_INLINE_ERROR = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    b"<soap:Body>"
    b'<ExecuteResponse xmlns="urn:schemas-microsoft-com:xml-analysis"><return>'
    b'<root xmlns="urn:schemas-microsoft-com:xml-analysis:rowset">'
    b"<Messages><Error ErrorCode=\"3238658055\" "
    b"Description=\"Query (1, 8) The Foo dimension was not found.\"/></Messages>"
    b"</root></return></ExecuteResponse>"
    b"</soap:Body></soap:Envelope>"
)


def _make_response(body: bytes, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.text = body.decode("utf-8", errors="ignore")
    return resp


def _install_post(client: InforOlapClient, responses):
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
    return InforOlapClient(
        host="http://epm.example.com/bi/olap",
        username="u",
        password="p",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Connect / auth
# ---------------------------------------------------------------------------

class TestConnect:
    def test_connect_sets_basic_auth(self):
        client = _client()
        client.connect()
        assert client._http is not None
        assert client._http.auth == ("u", "p")

    def test_connect_missing_creds_raises(self):
        client = InforOlapClient(host="http://epm.example.com/bi/olap")
        with pytest.raises(RuntimeError, match="username and password"):
            client.connect()

    def test_connect_missing_host_raises(self):
        client = InforOlapClient(host="", username="u", password="p")
        with pytest.raises(RuntimeError, match="host is required"):
            client.connect()

    def test_soap_fault_becomes_runtime_error(self):
        client = _client()
        _install_post(client, [_make_response(SOAP_FAULT)])
        with pytest.raises(RuntimeError, match="Invalid credentials"):
            client._list_catalogs()

    def test_http_error_raises(self):
        client = _client()
        _install_post(client, [_make_response(b"boom", status=500)])
        with pytest.raises(RuntimeError, match="HTTP 500"):
            client._list_catalogs()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_list_catalogs(self):
        client = _client()
        _install_post(client, [_make_response(CATALOGS_TWO)])
        assert client._list_catalogs() == ["Finance", "Sales"]

    def test_configured_catalog_skips_discovery(self):
        client = _client(catalog="Finance")
        session = _install_post(client, [])
        assert client._list_catalogs() == ["Finance"]
        assert session.post.call_count == 0

    def test_list_cubes_filters_dimensions(self):
        client = _client()
        _install_post(client, [_make_response(CUBES_FINANCE)])
        cubes = client._list_cubes("Finance")
        assert [c["name"] for c in cubes] == ["GL"]
        assert cubes[0]["caption"] == "General Ledger"

    def test_get_schemas_builds_tables(self):
        # Scope to one catalog to keep the fixture sequence tight: cubes,
        # then per-cube hierarchies and measures.
        client = _client(catalog="Finance")
        _install_post(client, [
            _make_response(CUBES_FINANCE),
            _make_response(HIERARCHIES_GL),
            _make_response(MEASURES_GL),
        ])
        tables = client.get_schemas()
        assert [t.name for t in tables] == ["Finance/GL"]
        gl = tables[0]
        assert gl.metadata_json["infor_olap"]["cube"] == "GL"
        # Dimension (hierarchy) first, then measure; measures dim is skipped.
        assert [(c.name, c.dtype) for c in gl.columns] == [
            ("Account", "dimension"),
            ("Amount", "measure"),
        ]
        assert gl.columns[0].metadata["unique_name"] == "[Account]"
        assert gl.columns[1].metadata["unique_name"] == "[Measures].[Amount]"

    def test_get_schema_by_name(self):
        client = _client(catalog="Finance")
        _install_post(client, [
            _make_response(CUBES_FINANCE),
            _make_response(HIERARCHIES_GL),
            _make_response(MEASURES_GL),
        ])
        tbl = client.get_schema("Finance/GL")
        assert tbl.name == "Finance/GL"

    def test_get_schema_not_found(self):
        client = _client(catalog="Finance")
        _install_post(client, [
            _make_response(CUBES_FINANCE),
            _make_response(HIERARCHIES_GL),
            _make_response(MEASURES_GL),
        ])
        with pytest.raises(RuntimeError, match="Table not found"):
            client.get_schema("nope")


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

class TestExecuteQuery:
    def test_mdx_parsed_into_dataframe(self):
        client = _client(catalog="Finance")
        _install_post(client, [_make_response(EXECUTE_OK)])
        df = client.execute_query("SELECT {[Measures].[Amount]} ON COLUMNS FROM [GL]")
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["Account", "Amount"]
        assert df.iloc[0]["Account"] == "Cash"
        assert df.iloc[1]["Amount"] == "250"
        assert len(df) == 2

    def test_empty_result_returns_empty_frame(self):
        client = _client(catalog="Finance")
        _install_post(client, [_make_response(EXECUTE_EMPTY)])
        df = client.execute_query("SELECT {} ON COLUMNS FROM [GL]")
        assert df.empty

    def test_inline_error_becomes_runtime_error(self):
        client = _client(catalog="Finance")
        _install_post(client, [_make_response(XMLA_INLINE_ERROR)])
        with pytest.raises(RuntimeError, match="dimension was not found"):
            client.execute_query("SELECT {} ON COLUMNS FROM [Foo]")

    def test_empty_query_rejected(self):
        client = _client()
        with pytest.raises(ValueError, match="MDX query is required"):
            client.execute_query("   ")


# ---------------------------------------------------------------------------
# test_connection smoke / prompt / registry wiring
# ---------------------------------------------------------------------------

class TestTopLevel:
    def test_test_connection_ok(self):
        client = _client()
        _install_post(client, [_make_response(CATALOGS_TWO)])
        result = client.test_connection()
        assert result["success"] is True
        assert result["catalogs"] == 2

    def test_test_connection_empty(self):
        client = _client()
        _install_post(client, [_make_response(CATALOGS_EMPTY)])
        result = client.test_connection()
        assert result["success"] is True
        assert result["catalogs"] == 0
        assert "no olap databases" in result["message"].lower()

    def test_description_includes_system_prompt(self):
        client = _client()
        text = client.description
        assert "Infor OLAP" in text
        assert "MDX" in text

    def test_resolve_client_class(self):
        from app.schemas.data_source_registry import resolve_client_class
        assert resolve_client_class("infor_olap") is InforOlapClient
