"""Unit tests for InforOlapClient — all XMLA transport is mocked."""

from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

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

INFOR_SOAP_FAULT = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    b"<soap:Body><soap:Fault>"
    b"<faultcode>soap:Server</faultcode>"
    b"<faultstring>An exception was thrown to inform the client about error condition.</faultstring>"
    b'<detail><Error ErrorCode="1042" '
    b'Description="The username or password is incorrect."/></detail>'
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


def _make_response(body: bytes, status: int = 200, headers: dict = None):
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.text = body.decode("utf-8", errors="ignore")
    resp.headers = headers or {}
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


def _sent_envelopes(session):
    envelopes = []
    for call in session.post.call_args_list:
        payload = call.kwargs.get("data") or call.args[1]
        envelopes.append(ET.fromstring(payload))
    return envelopes


def _element_text(root, name: str):
    for element in root.iter():
        if element.tag.split("}", 1)[-1] == name:
            return element.text
    return None


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

    def test_infor_soap_fault_includes_error_code_and_description(self):
        client = _client()
        _install_post(client, [_make_response(INFOR_SOAP_FAULT)])
        with pytest.raises(RuntimeError, match=r"1042.*username or password"):
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

    def test_worker_discovery_carries_infor_context_and_credentials(self):
        client = InforOlapClient(
            host="http://epm.example.com/bi/olap",
            username=r"DOMAIN\reader&ops",
            password="p<>&word",
            tenant="tenant&one",
            catalog="Finance",
        )
        session = _install_post(client, [
            _make_response(CUBES_FINANCE),
            _make_response(HIERARCHIES_GL),
            _make_response(MEASURES_GL),
        ])

        client.get_schemas()

        envelopes = _sent_envelopes(session)
        assert envelopes
        for envelope in envelopes:
            assert _element_text(envelope, "Tenant") == "tenant&one"
            assert _element_text(envelope, "UserName") == r"DOMAIN\reader&ops"
            assert _element_text(envelope, "Password") == "p<>&word"

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

    def test_execute_carries_infor_context_and_credentials(self):
        client = InforOlapClient(
            host="http://epm.example.com/bi/olap",
            username="reader@example.com",
            password="p<>&word",
            tenant="tenant&one",
            catalog="Finance",
        )
        session = _install_post(client, [_make_response(EXECUTE_OK)])

        client.execute_query("SELECT {[Measures].[Amount]} ON COLUMNS FROM [GL]")

        envelope = _sent_envelopes(session)[0]
        assert _element_text(envelope, "Tenant") == "tenant&one"
        assert _element_text(envelope, "UserName") == "reader@example.com"
        assert _element_text(envelope, "Password") == "p<>&word"

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

    def test_configured_catalog_test_connection_verifies_worker(self):
        client = _manager_client(catalog="Finance", tenant="tenant-one")
        session = _install_post(client, [
            _make_response(DATASOURCES_GUID),
            _make_response(INFOR_SOAP_FAULT),
        ])

        result = client.test_connection()

        assert result["success"] is False
        assert "1042" in result["message"]
        assert session.post.call_count == 2

    def test_description_includes_system_prompt(self):
        client = _client()
        text = client.description
        assert "Infor OLAP" in text
        assert "MDX" in text

    def test_resolve_client_class(self):
        from app.schemas.data_source_registry import resolve_client_class
        assert resolve_client_class("infor_olap") is InforOlapClient

    def test_registry_exposes_ion_gateway_credentials(self):
        from app.schemas.data_source_registry import credentials_schema_for, get_entry

        entry = get_entry("infor_olap")
        schema = credentials_schema_for("infor_olap", "ion_oauth")

        assert "ion_oauth" in entry.credentials_auth.by_auth
        assert {"secured", "worker_url_base"}.issubset(entry.config_schema.model_fields)
        assert {
            "username",
            "password",
            "gateway_token_url",
            "gateway_client_id",
            "gateway_client_secret",
        }.issubset(schema.model_fields)


# ---------------------------------------------------------------------------
# Manager discovery (documented DISCOVER_DATASOURCES bootstrap)
# ---------------------------------------------------------------------------

DATASOURCES_ONE = _discover_envelope(
    "<row><DataSourceName>Planning</DataSourceName>"
    "<URL>http://internal-worker-01:8210/BI/APP/SOAP/OLAPDB/Planning</URL></row>"
)

DATASOURCES_TWO = _discover_envelope(
    "<row><DataSourceName>Planning</DataSourceName>"
    "<URL>http://internal-worker-01:8210/BI/APP/SOAP/OLAPDB/Planning</URL></row>"
    "<row><DataSourceName>Finance</DataSourceName>"
    "<URL>http://internal-worker-02:8215/BI/APP/SOAP/OLAPDB/Finance</URL></row>"
)

DATASOURCES_GUID = _discover_envelope(
    "<row><DataSourceName>Finance</DataSourceName>"
    "<URL>http://internal-manager:8200/BI/app/soap/OLAPDB/"
    "11111111-2222-3333-4444-555555555555</URL></row>"
)


def _manager_client(**kwargs):
    return InforOlapClient(
        host="http://203.0.113.5:8200/BI/APP/SOAP/OLAPDB",
        username="u",
        password="p",
        manager_discovery=True,
        **kwargs,
    )


class TestManagerDiscovery:
    def test_resolves_and_rewrites_host(self):
        client = _manager_client()
        _install_post(client, [_make_response(DATASOURCES_ONE)])
        client.connect()
        # Path and port come from the response; hostname from the configured URL.
        assert client.host == "http://203.0.113.5:8210/BI/APP/SOAP/OLAPDB/Planning"
        assert client.resolved_worker_url == client.host
        assert client.manager_url == "http://203.0.113.5:8200/BI/APP/SOAP/OLAPDB"

    def test_rewrite_disabled_keeps_returned_host(self):
        client = _manager_client(rewrite_worker_host=False)
        _install_post(client, [_make_response(DATASOURCES_ONE)])
        client.connect()
        assert client.host == "http://internal-worker-01:8210/BI/APP/SOAP/OLAPDB/Planning"

    def test_gateway_base_maps_discovered_worker_path(self):
        client = InforOlapClient(
            host="https://gateway.example.com/acme/EPM/BI/APP/SOAP/OLAPDB",
            username="u",
            password="p",
            catalog="Planning",
            manager_discovery=True,
            worker_url_base="https://gateway.example.com/acme/EPM",
        )
        session = _install_post(client, [_make_response(DATASOURCES_ONE)])

        client.connect()

        assert client.host == (
            "https://gateway.example.com/acme/EPM/BI/APP/SOAP/OLAPDB/Planning"
        )
        assert _element_text(_sent_envelopes(session)[0], "Secured") == "false"

    def test_database_guid_route_stays_on_manager_listener(self):
        client = _manager_client(catalog="Finance", tenant="tenant-one")
        _install_post(client, [_make_response(DATASOURCES_GUID)])

        client.connect()

        assert client.host == (
            "http://203.0.113.5:8200/BI/app/soap/OLAPDB/"
            "11111111-2222-3333-4444-555555555555"
        )

    def test_manager_bootstrap_drives_schema_discovery_and_query_on_guid_route(self):
        client = _manager_client(catalog="Finance", tenant="tenant-one")
        session = _install_post(client, [
            _make_response(DATASOURCES_GUID),
            _make_response(CUBES_FINANCE),
            _make_response(HIERARCHIES_GL),
            _make_response(MEASURES_GL),
            _make_response(EXECUTE_OK),
        ])

        tables = client.get_schemas()
        frame = client.execute_query(
            "SELECT {[Measures].[Amount]} ON COLUMNS FROM [GL]",
            "Finance/GL",
        )

        assert [table.name for table in tables] == ["Finance/GL"]
        assert list(frame["Account"]) == ["Cash", "Receivables"]
        urls = [call.args[0] for call in session.post.call_args_list]
        assert urls[0] == "http://203.0.113.5:8200/BI/APP/SOAP/OLAPDB"
        assert set(urls[1:]) == {
            "http://203.0.113.5:8200/BI/app/soap/OLAPDB/"
            "11111111-2222-3333-4444-555555555555"
        }
        for envelope in _sent_envelopes(session)[1:]:
            assert _element_text(envelope, "Tenant") == "tenant-one"
            assert _element_text(envelope, "UserName") == "u"
            assert _element_text(envelope, "Password") == "p"

    def test_catalog_picks_matching_database(self):
        client = _manager_client(catalog="Finance")
        _install_post(client, [_make_response(DATASOURCES_TWO)])
        client.connect()
        assert client.host == "http://203.0.113.5:8215/BI/APP/SOAP/OLAPDB/Finance"

    def test_multiple_databases_without_catalog_raises(self):
        client = _manager_client()
        _install_post(client, [_make_response(DATASOURCES_TWO)])
        with pytest.raises(RuntimeError, match="Planning.*Finance|set Catalog"):
            client.connect()

    def test_unknown_catalog_lists_available(self):
        client = _manager_client(catalog="Nope")
        _install_post(client, [_make_response(DATASOURCES_TWO)])
        with pytest.raises(RuntimeError, match="not found.*Planning"):
            client.connect()

    def test_discovery_request_carries_tenant_and_version_header(self):
        client = _manager_client(catalog="Planning")
        session = _install_post(client, [_make_response(DATASOURCES_ONE)])
        client.connect()
        sent = session.post.call_args.kwargs.get("data") or session.post.call_args.args[1]
        body = sent.decode()
        assert "DISCOVER_DATASOURCES" in body
        assert "<Tenant>single</Tenant>" in body
        assert 'Version Sequence="200"' in body
        assert "<Databasename>Planning</Databasename>" in body

    @pytest.mark.parametrize(
        ("secured", "expected"),
        [
            (False, "false"),
            (True, "true"),
        ],
    )
    def test_discovery_declares_datasource_security(self, secured, expected):
        client = InforOlapClient(
            host="https://epm.example.com/BI/APP/SOAP/OLAPDB",
            username="u",
            password="p",
            catalog="Planning",
            manager_discovery=True,
            secured=secured,
        )
        session = _install_post(client, [_make_response(DATASOURCES_ONE)])

        client.connect()

        envelope = _sent_envelopes(session)[0]
        assert _element_text(envelope, "Secured") == expected

    def test_bootstrap_runs_once(self):
        client = _manager_client()
        session = _install_post(client, [
            _make_response(DATASOURCES_ONE),
            _make_response(CATALOGS_TWO),
        ])
        client.connect()
        client.connect()  # second call must not re-discover
        client._list_catalogs()
        assert session.post.call_count == 2  # one bootstrap + one DBSCHEMA_CATALOGS


# ---------------------------------------------------------------------------
# ION API Gateway authentication
# ---------------------------------------------------------------------------

class TestIonGateway:
    def test_client_credentials_mint_gateway_bearer_token(self):
        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {
            "access_token": "gateway-token",
            "expires_in": 3600,
        }
        client = InforOlapClient(
            host="https://gateway.example.com/acme/EPM/BI/APP/SOAP/OLAPDB",
            username="epm-reader",
            password="epm-password",
            gateway_token_url="https://identity.example.com/oauth2/token",
            gateway_client_id="client-id",
            gateway_client_secret="client-secret",
            gateway_scope="olap.read",
        )

        with patch(
            "app.data_sources.clients.infor_olap_client.requests.post",
            return_value=token_response,
        ) as token_post:
            client.connect()

        assert client._http.auth is None
        assert client._http.headers["Authorization"] == "Bearer gateway-token"
        request = token_post.call_args
        assert request.args[0] == "https://identity.example.com/oauth2/token"
        assert request.kwargs["data"] == {
            "grant_type": "client_credentials",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scope": "olap.read",
        }

    def test_gateway_manager_worker_and_query_flow(self):
        token_response = MagicMock(status_code=200)
        token_response.json.return_value = {
            "access_token": "gateway-token",
            "expires_in": 3600,
        }
        gateway_base = "https://gateway.example.com/acme/EPM"
        client = InforOlapClient(
            host=f"{gateway_base}/BI/APP/SOAP/OLAPDB",
            username="epm-reader",
            password="epm-password",
            catalog="Finance",
            tenant="tenant-one",
            manager_discovery=True,
            worker_url_base=gateway_base,
            gateway_token_url="https://identity.example.com/oauth2/token",
            gateway_client_id="client-id",
            gateway_client_secret="client-secret",
        )
        session = _install_post(client, [
            _make_response(DATASOURCES_GUID),
            _make_response(CUBES_FINANCE),
            _make_response(HIERARCHIES_GL),
            _make_response(MEASURES_GL),
            _make_response(EXECUTE_OK),
        ])
        session.headers = {}

        with patch(
            "app.data_sources.clients.infor_olap_client.requests.post",
            return_value=token_response,
        ):
            tables = client.get_schemas()
            frame = client.execute_query(
                "SELECT {[Measures].[Amount]} ON COLUMNS FROM [GL]",
                "Finance/GL",
            )

        worker_url = (
            f"{gateway_base}/BI/app/soap/OLAPDB/"
            "11111111-2222-3333-4444-555555555555"
        )
        assert [table.name for table in tables] == ["Finance/GL"]
        assert list(frame["Account"]) == ["Cash", "Receivables"]
        assert session.headers["Authorization"] == "Bearer gateway-token"
        assert [call.args[0] for call in session.post.call_args_list] == [
            f"{gateway_base}/BI/APP/SOAP/OLAPDB",
            worker_url,
            worker_url,
            worker_url,
            worker_url,
        ]

    def test_gateway_401_refreshes_token_once(self):
        first_token = MagicMock(status_code=200)
        first_token.json.return_value = {"access_token": "first", "expires_in": 3600}
        second_token = MagicMock(status_code=200)
        second_token.json.return_value = {"access_token": "second", "expires_in": 3600}
        client = InforOlapClient(
            host="https://gateway.example.com/acme/EPM/BI/APP/SOAP/OLAPDB",
            username="epm-reader",
            password="epm-password",
            gateway_token_url="https://identity.example.com/oauth2/token",
            gateway_client_id="client-id",
            gateway_client_secret="client-secret",
        )

        with patch(
            "app.data_sources.clients.infor_olap_client.requests.post",
            side_effect=[first_token, second_token],
        ) as token_post:
            client.connect()
            session = _install_post(client, [
                _make_response(b"expired", status=401),
                _make_response(CATALOGS_TWO),
            ])
            catalogs = client._list_catalogs()

        assert catalogs == ["Finance", "Sales"]
        assert token_post.call_count == 2
        assert session.post.call_count == 2


# ---------------------------------------------------------------------------
# test_connection failure classification
# ---------------------------------------------------------------------------

class TestFailureClassification:
    def test_dns_failure_reports_no_connectivity(self):
        import requests as _requests
        client = _client()
        session = MagicMock()
        session.post.side_effect = _requests.exceptions.ConnectionError(
            "NameResolutionError: Temporary failure in name resolution"
        )
        client._http = session
        result = client.test_connection()
        assert result["success"] is False
        assert result["connectivity"] is False
        assert "DNS" in result["message"]

    def test_404_with_httpapi_signature_includes_path_hint(self):
        client = _client()
        _install_post(client, [
            _make_response(b"Not Found", status=404, headers={"Server": "Microsoft-HTTPAPI/2.0"}),
        ])
        result = client.test_connection()
        assert result["success"] is False
        assert result["connectivity"] is True
        assert "BI/APP/SOAP/OLAPDB" in result["message"]

    def test_404_without_signature_has_no_infor_hint(self):
        client = _client()
        _install_post(client, [_make_response(b"nope", status=404, headers={"Server": "nginx"})])
        result = client.test_connection()
        assert "HTTP 404" in result["message"] or "404" in result["message"]
        assert "BI/APP/SOAP/OLAPDB" not in result["message"]

    def test_401_reports_credentials(self):
        client = _client()
        _install_post(client, [_make_response(b"denied", status=401)])
        result = client.test_connection()
        assert result["success"] is False
        assert "credentials" in result["message"].lower()

    def test_missing_host_reports_configuration_error(self):
        client = InforOlapClient(host="", username="u", password="p")
        result = client.test_connection()
        assert result["success"] is False
        assert "Configuration error" in result["message"]

    def test_manager_success_reports_worker_url(self):
        client = _manager_client(catalog="Planning")
        _install_post(client, [
            _make_response(DATASOURCES_ONE),
            _make_response(CATALOGS_TWO),
        ])
        result = client.test_connection()
        assert result["success"] is True
        assert result["worker_url"] == "http://203.0.113.5:8210/BI/APP/SOAP/OLAPDB/Planning"
        assert "Worker URL" in result["message"]


# ---------------------------------------------------------------------------
# Catalog resolution shortcut (no network round trips per query)
# ---------------------------------------------------------------------------

class TestCatalogShortcut:
    def test_catalog_from_table_name_prefix_without_discovery(self):
        client = _client()
        session = _install_post(client, [_make_response(EXECUTE_OK)])
        df = client.execute_query("SELECT {} ON COLUMNS FROM [GL]", table_name="Finance/GL")
        assert not df.empty
        # Exactly one POST: the Execute itself — no schema re-discovery.
        assert session.post.call_count == 1
        body = (session.post.call_args.kwargs.get("data") or session.post.call_args.args[1]).decode()
        assert "<Catalog>Finance</Catalog>" in body
