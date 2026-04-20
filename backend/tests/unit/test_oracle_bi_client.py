"""Unit tests for OracleBIClient — all SOAP transport is mocked."""

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from app.data_sources.clients.oracle_bi_client import OracleBIClient


# ---------------------------------------------------------------------------
# Fixtures: canned SOAP response bodies captured from a real OAC instance.
# ---------------------------------------------------------------------------

LOGON_OK = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:sawsoap="urn://oracle.bi.webservices/v12">
  <soap:Body>
    <sawsoap:logonResult>
      <sawsoap:sessionID>test-session-id</sawsoap:sessionID>
    </sawsoap:logonResult>
  </soap:Body>
</soap:Envelope>"""

SUBJECT_AREAS_TWO = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:sawsoap="urn://oracle.bi.webservices/v12">
  <soap:Body>
    <sawsoap:getSubjectAreasResult>
      <sawsoap:subjectArea><sawsoap:name>"A - Sample Sales"</sawsoap:name></sawsoap:subjectArea>
      <sawsoap:subjectArea><sawsoap:name>"SH"</sawsoap:name></sawsoap:subjectArea>
    </sawsoap:getSubjectAreasResult>
  </soap:Body>
</soap:Envelope>"""

SUBJECT_AREAS_EMPTY = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:sawsoap="urn://oracle.bi.webservices/v12">
  <soap:Body>
    <sawsoap:getSubjectAreasResult></sawsoap:getSubjectAreasResult>
  </soap:Body>
</soap:Envelope>"""

DESCRIBE_SAMPLE_SALES = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:sawsoap="urn://oracle.bi.webservices/v12">
  <soap:Body>
    <sawsoap:describeSubjectAreaResult>
      <sawsoap:subjectArea>
        <sawsoap:name>"A - Sample Sales"</sawsoap:name>
        <sawsoap:displayName>A - Sample Sales</sawsoap:displayName>
        <sawsoap:description>Sample sales subject area</sawsoap:description>
        <sawsoap:businessModel>Sample Sales BM</sawsoap:businessModel>
        <sawsoap:tables>
          <sawsoap:name>"A - Sample Sales"."Products"</sawsoap:name>
          <sawsoap:displayName>Products</sawsoap:displayName>
          <sawsoap:description>Product dimension</sawsoap:description>
          <sawsoap:columns>
            <sawsoap:name>"A - Sample Sales"."Products"."Product"</sawsoap:name>
            <sawsoap:displayName>Product</sawsoap:displayName>
            <sawsoap:dataType>VARCHAR</sawsoap:dataType>
          </sawsoap:columns>
          <sawsoap:columns>
            <sawsoap:name>"A - Sample Sales"."Products"."Category"</sawsoap:name>
            <sawsoap:displayName>Category</sawsoap:displayName>
            <sawsoap:dataType>VARCHAR</sawsoap:dataType>
          </sawsoap:columns>
        </sawsoap:tables>
        <sawsoap:tables>
          <sawsoap:name>"A - Sample Sales"."Base Facts"</sawsoap:name>
          <sawsoap:displayName>Base Facts</sawsoap:displayName>
          <sawsoap:columns>
            <sawsoap:name>"A - Sample Sales"."Base Facts"."Revenue"</sawsoap:name>
            <sawsoap:displayName>Revenue</sawsoap:displayName>
            <sawsoap:dataType>DOUBLE</sawsoap:dataType>
          </sawsoap:columns>
        </sawsoap:tables>
      </sawsoap:subjectArea>
    </sawsoap:describeSubjectAreaResult>
  </soap:Body>
</soap:Envelope>"""

DESCRIBE_PHANTOM_ECHO = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:sawsoap="urn://oracle.bi.webservices/v12">
  <soap:Body>
    <sawsoap:describeSubjectAreaResult>
      <sawsoap:subjectArea>
        <sawsoap:name>"DoesNotExist"</sawsoap:name>
        <sawsoap:displayName>DoesNotExist</sawsoap:displayName>
        <sawsoap:description></sawsoap:description>
        <sawsoap:businessModel></sawsoap:businessModel>
      </sawsoap:subjectArea>
    </sawsoap:describeSubjectAreaResult>
  </soap:Body>
</soap:Envelope>"""

# executeXMLQuery wraps a rowset XML string (entity-escaped) inside the SOAP body.
EXECUTE_QUERY_OK = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:sawsoap="urn://oracle.bi.webservices/v12">
  <soap:Body>
    <sawsoap:executeXMLQueryResult>
      <sawsoap:return>
        <sawsoap:rowset>&lt;rowset xmlns=&quot;urn:schemas-microsoft-com:xml-analysis:rowset&quot; xmlns:xsd=&quot;http://www.w3.org/2001/XMLSchema&quot;&gt;&lt;xsd:schema&gt;&lt;xsd:element name=&quot;Row&quot;&gt;&lt;xsd:complexType&gt;&lt;xsd:sequence&gt;&lt;xsd:element name=&quot;Column0&quot; type=&quot;xsd:string&quot;/&gt;&lt;xsd:element name=&quot;Column1&quot; type=&quot;xsd:double&quot;/&gt;&lt;/xsd:sequence&gt;&lt;/xsd:complexType&gt;&lt;/xsd:element&gt;&lt;/xsd:schema&gt;&lt;Row&gt;&lt;Column0&gt;Widgets&lt;/Column0&gt;&lt;Column1&gt;100.5&lt;/Column1&gt;&lt;/Row&gt;&lt;Row&gt;&lt;Column0&gt;Gadgets&lt;/Column0&gt;&lt;Column1&gt;200.25&lt;/Column1&gt;&lt;/Row&gt;&lt;/rowset&gt;</sawsoap:rowset>
        <sawsoap:queryID>RSXS1_1</sawsoap:queryID>
        <sawsoap:finished>true</sawsoap:finished>
      </sawsoap:return>
    </sawsoap:executeXMLQueryResult>
  </soap:Body>
</soap:Envelope>"""

EXECUTE_QUERY_ERROR = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:sawsoap="urn://oracle.bi.webservices/v12">
  <soap:Body>
    <sawsoap:executeXMLQueryResult>
      <sawsoap:return>
        <sawsoap:rowset>&lt;rowset xmlns=&quot;urn:schemas-microsoft-com:xml-analysis:rowset&quot;&gt;Please have your service administrator review this error.
Error Codes: ACIOA5LN
&lt;/rowset&gt;</sawsoap:rowset>
      </sawsoap:return>
    </sawsoap:executeXMLQueryResult>
  </soap:Body>
</soap:Envelope>"""

SOAP_FAULT = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <soap:Fault>
      <faultcode>soap:Server</faultcode>
      <faultstring>Invalid login credentials</faultstring>
      <detail>
        <sawsoape:Error xmlns:sawsoape="com.siebel.analytics.web/soap/error/v1">
          <sawsoape:Code>AUTH01</sawsoape:Code>
        </sawsoape:Error>
      </detail>
    </soap:Fault>
  </soap:Body>
</soap:Envelope>"""


def _make_response(body: bytes, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.text = body.decode("utf-8", errors="ignore")
    return resp


def _install_post(client: OracleBIClient, responses):
    """
    Replace the client's HTTP session .post with a stub that returns the
    next canned response from `responses` (list), asserting we don't run
    off the end.
    """
    session = MagicMock()
    iterator = iter(responses)

    def _post(url, data=None, headers=None, timeout=None, verify=None):
        try:
            return next(iterator)
        except StopIteration:  # pragma: no cover - helps surface missing fixtures
            raise AssertionError(f"Unexpected extra POST to {url}")

    session.post.side_effect = _post
    client._http = session
    return session


# ---------------------------------------------------------------------------
# Connect / logon
# ---------------------------------------------------------------------------

class TestConnect:
    def test_logon_stores_session_id(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(LOGON_OK)])
        client.connect()
        assert client._session_id == "test-session-id"

    def test_logon_caches_session(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        session = _install_post(client, [_make_response(LOGON_OK)])
        client.connect()
        client.connect()  # should NOT re-post
        assert session.post.call_count == 1

    def test_logon_missing_creds_raises(self):
        client = OracleBIClient(host="https://oac.example.com")
        with pytest.raises(RuntimeError, match="username and password"):
            client.connect()

    def test_logon_missing_host_raises(self):
        client = OracleBIClient(host="", username="u", password="p")
        with pytest.raises(RuntimeError, match="host is required"):
            client.connect()

    def test_soap_fault_becomes_runtime_error(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(SOAP_FAULT)])
        with pytest.raises(RuntimeError, match=r"Invalid login credentials \[AUTH01\]"):
            client.connect()

    def test_http_error_raises(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(b"boom", status=500)])
        with pytest.raises(RuntimeError, match="HTTP 500"):
            client.connect()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_list_subject_areas(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(LOGON_OK), _make_response(SUBJECT_AREAS_TWO)])
        names = client._list_subject_area_names()
        assert names == ['"A - Sample Sales"', '"SH"']

    def test_list_subject_areas_empty(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(LOGON_OK), _make_response(SUBJECT_AREAS_EMPTY)])
        assert client._list_subject_area_names() == []

    def test_describe_returns_tables_and_columns(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(LOGON_OK), _make_response(DESCRIBE_SAMPLE_SALES)])
        desc = client._describe_subject_area('"A - Sample Sales"')
        assert desc["display_name"] == "A - Sample Sales"
        assert desc["business_model"] == "Sample Sales BM"
        assert len(desc["tables"]) == 2
        products = desc["tables"][0]
        assert products["display_name"] == "Products"
        assert [c["display_name"] for c in products["columns"]] == ["Product", "Category"]
        assert products["columns"][0]["data_type"] == "VARCHAR"

    def test_describe_phantom_echo_returns_empty(self):
        """Empty businessModel + no tables means the BI Server echoed a nonexistent name."""
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(LOGON_OK), _make_response(DESCRIBE_PHANTOM_ECHO)])
        assert client._describe_subject_area('"DoesNotExist"') == {}

    def test_get_schemas_builds_tables(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [
            _make_response(LOGON_OK),
            _make_response(SUBJECT_AREAS_TWO),
            _make_response(DESCRIBE_SAMPLE_SALES),
            _make_response(DESCRIBE_PHANTOM_ECHO),  # SH lookup returns phantom → filtered out
        ])
        tables = client.get_schemas()
        names = [t.name for t in tables]
        assert names == ["A - Sample Sales/Products", "A - Sample Sales/Base Facts"]
        products = tables[0]
        assert products.metadata_json["oracle_bi"]["subjectArea"] == '"A - Sample Sales"'
        assert products.metadata_json["oracle_bi"]["tableDisplayName"] == "Products"
        assert [c.name for c in products.columns] == ["Product", "Category"]
        assert products.columns[0].dtype == "VARCHAR"

    def test_get_schema_by_display_path(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [
            _make_response(LOGON_OK),
            _make_response(SUBJECT_AREAS_TWO),
            _make_response(DESCRIBE_SAMPLE_SALES),
            _make_response(DESCRIBE_PHANTOM_ECHO),
        ])
        tbl = client.get_schema("A - Sample Sales/Base Facts")
        assert tbl.name == "A - Sample Sales/Base Facts"
        assert [c.name for c in tbl.columns] == ["Revenue"]

    def test_get_schema_not_found(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [
            _make_response(LOGON_OK),
            _make_response(SUBJECT_AREAS_EMPTY),
        ])
        with pytest.raises(RuntimeError, match="Table not found"):
            client.get_schema("anything")


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

class TestExecuteQuery:
    def test_rowset_parsed_into_dataframe(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(LOGON_OK), _make_response(EXECUTE_QUERY_OK)])
        df = client.execute_query('SELECT "P"."P", "F"."R" FROM "A - Sample Sales"')
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["Column0", "Column1"]
        assert df.iloc[0]["Column0"] == "Widgets"
        assert df.iloc[1]["Column1"] == "200.25"
        assert len(df) == 2

    def test_inline_error_becomes_runtime_error(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(LOGON_OK), _make_response(EXECUTE_QUERY_ERROR)])
        with pytest.raises(RuntimeError, match="ACIOA5LN"):
            client.execute_query('SELECT 1 FROM "A - Sample Sales"')

    def test_empty_query_rejected(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        with pytest.raises(ValueError, match="Logical SQL query is required"):
            client.execute_query("   ")


# ---------------------------------------------------------------------------
# test_connection smoke / prompt
# ---------------------------------------------------------------------------

class TestTopLevel:
    def test_test_connection_ok(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(LOGON_OK), _make_response(SUBJECT_AREAS_TWO)])
        result = client.test_connection()
        assert result["success"] is True
        assert result["subject_areas"] == 2

    def test_test_connection_auth_failure(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(SOAP_FAULT)])
        result = client.test_connection()
        assert result["success"] is False
        assert "Authentication failed" in result["message"]

    def test_test_connection_empty_instance_flag(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        _install_post(client, [_make_response(LOGON_OK), _make_response(SUBJECT_AREAS_EMPTY)])
        result = client.test_connection()
        assert result["success"] is True
        assert result["subject_areas"] == 0
        assert "no deployed semantic model" in result["message"].lower()

    def test_description_includes_system_prompt(self):
        client = OracleBIClient(host="https://oac.example.com", username="u", password="p")
        text = client.description
        assert "Oracle BI" in text
        assert "Logical SQL" in text
