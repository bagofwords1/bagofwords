"""Unit tests for PowerBIReportServerClient.

Covers:
- URL normalization for different server_url shapes
- NTLM username composition with optional domain
- RDL XML parsing (CommandText, fields, parameters)
- get_schemas with mocked REST responses
- execute_query routing (pbix -> NotImplementedError, KPI -> ValueError)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.data_sources.clients.powerbi_report_server_client import (
    PowerBIReportServerClient,
    _clr_to_dtype,
    _strip_ns,
    _summarize_upstream,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------- URL / auth plumbing ---------- #


class TestUrlNormalization:
    def test_root_url(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        assert c._api_base() == "http://pbi/Reports/api/v2.0"
        assert c._report_server_root() == "http://pbi"

    def test_reports_suffix(self):
        c = PowerBIReportServerClient("http://pbi/Reports", "u", "p")
        assert c._api_base() == "http://pbi/Reports/api/v2.0"
        assert c._report_server_root() == "http://pbi"

    def test_full_api_url(self):
        c = PowerBIReportServerClient("http://pbi/Reports/api/v2.0", "u", "p")
        assert c._api_base() == "http://pbi/Reports/api/v2.0"
        assert c._report_server_root() == "http://pbi"

    def test_trailing_slash_stripped(self):
        c = PowerBIReportServerClient("http://pbi/", "u", "p")
        assert c._api_base() == "http://pbi/Reports/api/v2.0"

    def test_https_preserved(self):
        c = PowerBIReportServerClient("https://pbi.corp.example.com", "u", "p")
        assert c._api_base() == "https://pbi.corp.example.com/Reports/api/v2.0"


class TestNtlmUser:
    def test_domain_prepended_when_missing(self):
        c = PowerBIReportServerClient("http://pbi", "alice", "p", domain="CORP")
        assert c._ntlm_user() == "CORP\\alice"

    def test_domain_ignored_when_username_has_backslash(self):
        c = PowerBIReportServerClient("http://pbi", "CORP\\alice", "p", domain="OTHER")
        assert c._ntlm_user() == "CORP\\alice"

    def test_domain_ignored_when_username_has_upn(self):
        c = PowerBIReportServerClient("http://pbi", "alice@corp.example", "p", domain="X")
        assert c._ntlm_user() == "alice@corp.example"

    def test_no_domain(self):
        c = PowerBIReportServerClient("http://pbi", "alice", "p")
        assert c._ntlm_user() == "alice"


class TestConstructorValidation:
    def test_requires_server_url(self):
        with pytest.raises(ValueError, match="server_url"):
            PowerBIReportServerClient("", "u", "p")

    def test_requires_username(self):
        with pytest.raises(ValueError, match="username"):
            PowerBIReportServerClient("http://pbi", "", "p")

    def test_requires_password(self):
        with pytest.raises(ValueError, match="password"):
            PowerBIReportServerClient("http://pbi", "u", None)


# ---------- helpers ---------- #


class TestHelpers:
    def test_strip_ns(self):
        assert _strip_ns("{http://x}Report") == "Report"
        assert _strip_ns("Report") == "Report"
        assert _strip_ns("") == ""

    def test_clr_to_dtype_numeric(self):
        assert _clr_to_dtype("System.Int32") == "int"
        assert _clr_to_dtype("System.Int16") == "int"
        assert _clr_to_dtype("System.Decimal") == "decimal"
        assert _clr_to_dtype("System.Double") == "float"

    def test_clr_to_dtype_misc(self):
        assert _clr_to_dtype("System.String") == "string"
        assert _clr_to_dtype("System.Boolean") == "bool"
        assert _clr_to_dtype("System.DateTime") == "datetime"
        assert _clr_to_dtype(None) == "unknown"
        assert _clr_to_dtype("") == "unknown"

    def test_summarize_upstream_empty(self):
        assert _summarize_upstream([]) == ""

    def test_summarize_upstream_file(self):
        out = _summarize_upstream([{"kind": "File", "connection_string": "c:\\data\\sales.xlsx"}])
        assert out == "File: c:\\data\\sales.xlsx"

    def test_summarize_upstream_sql(self):
        out = _summarize_upstream([{"kind": "SQL", "connection_string": "Server=dw01;Database=Sales"}])
        assert out == "SQL (Server=dw01;Database=Sales)"

    def test_summarize_upstream_dedups(self):
        srcs = [
            {"kind": "SQL", "connection_string": "Server=dw01"},
            {"kind": "SQL", "connection_string": "Server=dw01"},
            {"kind": "File", "connection_string": "c:\\x.xlsx"},
        ]
        out = _summarize_upstream(srcs)
        assert out == "SQL (Server=dw01); File: c:\\x.xlsx"


# ---------- RDL parser ---------- #


class TestRdlParser:
    def test_parse_sample_rdl(self):
        xml_bytes = (FIXTURES_DIR / "pbirs_sample_report.rdl").read_bytes()
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        parsed = c.parse_rdl_content(xml_bytes)

        assert len(parsed["data_sources"]) == 1
        ds = parsed["data_sources"][0]
        assert ds["name"] == "AdventureWorks"
        assert "AdventureWorks2019" in ds["connection_string"]
        assert ds["data_provider"] == "SQL"

        assert len(parsed["datasets"]) == 2
        sbr = next(d for d in parsed["datasets"] if d["name"] == "SalesByRegion")
        assert sbr["command_type"] == "Text"
        assert sbr["data_source_name"] == "AdventureWorks"
        assert "SUM(Amount)" in sbr["command_text"]
        assert "GROUP BY Region" in sbr["command_text"]

        field_names = {f["name"] for f in sbr["fields"]}
        assert field_names == {"Region", "TotalSales", "OrderCount"}
        dtype_by_name = {f["name"]: f["dtype"] for f in sbr["fields"]}
        assert dtype_by_name["Region"] == "string"
        assert dtype_by_name["TotalSales"] == "decimal"
        assert dtype_by_name["OrderCount"] == "int"

        qp_names = {qp["name"] for qp in sbr["parameters"]}
        assert qp_names == {"@StartDate", "@EndDate"}

        od = next(d for d in parsed["datasets"] if d["name"] == "OrderDetails")
        od_dtypes = {f["name"]: f["dtype"] for f in od["fields"]}
        assert od_dtypes["Quantity"] == "int"
        assert od_dtypes["UnitPrice"] == "float"

        rp_names = {p["name"] for p in parsed["parameters"]}
        assert rp_names == {"StartDate", "EndDate", "OrderId"}
        start = next(p for p in parsed["parameters"] if p["name"] == "StartDate")
        assert start["data_type"] == "DateTime"
        assert start["default_values"] == ["2024-01-01"]

    def test_parse_invalid_xml_raises(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        with pytest.raises(RuntimeError, match="Invalid RDL XML"):
            c.parse_rdl_content(b"not xml")

    def test_parse_empty_report(self):
        xml = b'<?xml version="1.0"?><Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"/>'
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        parsed = c.parse_rdl_content(xml)
        assert parsed == {"data_sources": [], "datasets": [], "parameters": []}


# ---------- get_schemas with mocked HTTP ---------- #


def _mock_response(json_body=None, content=None, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body
    r.content = content or (json.dumps(json_body).encode() if json_body is not None else b"")
    r.text = r.content.decode("utf-8", errors="replace") if isinstance(r.content, bytes) else ""
    return r


class TestGetSchemasMocked:
    def _fake_session(self, responses_by_path):
        """Build a fake session where session.get(url, ...) routes by path suffix.

        Most-specific (longest) suffix wins, so /PowerBIReports(r1)/DataSources
        takes precedence over /DataSources.
        """
        session = MagicMock()
        ordered = sorted(responses_by_path.items(), key=lambda kv: len(kv[0]), reverse=True)

        def _get(url, **kwargs):
            for path, resp in ordered:
                if url.endswith(path):
                    return resp
            return _mock_response(status=404, json_body={"error": "not found"})

        session.get.side_effect = _get
        return session

    def test_pbix_only(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        pbi_reports = [{
            "Id": "r1",
            "Name": "Sales Dashboard",
            "Path": "/Sales Dashboard",
            "Size": 123,
            "ModifiedBy": "alice",
            "ModifiedDate": "2024-01-01T00:00:00Z",
            "CreatedBy": "alice",
            "ParentFolderId": "f1",
        }]
        responses = {
            "/PowerBIReports": _mock_response({"value": pbi_reports}),
            "/Reports": _mock_response({"value": []}),
            "/Datasets": _mock_response({"value": []}),
            "/Kpis": _mock_response({"value": []}),
            "/DataSources": _mock_response({"value": []}),
            "/PowerBIReports(r1)/DataSources": _mock_response({"value": [
                {"Name": "src1", "ConnectionString": "Server=sqlsrv", "DataModelDataSource": {
                    "Type": "Import", "Kind": "SQL", "AuthType": "Windows", "ModelConnectionName": "x"
                }}
            ]}),
            "/PowerBIReports(r1)/DataModelParameters": _mock_response({"value": []}),
            "/PowerBIReports(r1)/DataModelRoles": _mock_response({"value": []}),
        }
        c._session = self._fake_session(responses)

        tables = c.get_schemas()
        assert len(tables) == 1
        t = tables[0]
        assert t.name == "pbix:Sales Dashboard"
        meta = t.metadata_json["powerbi_report_server"]
        assert meta["report_type"] == "PowerBIReport"
        assert meta["queryable"] is False
        assert len(meta["data_sources"]) == 1
        assert meta["data_sources"][0]["type"] == "Import"
        assert meta["data_sources"][0]["kind"] == "SQL"
        assert meta["upstream_source"] == "SQL (Server=sqlsrv)"
        assert "discovery" in meta["query_note"].lower()
        assert "Server=sqlsrv" in meta["query_note"]
        assert "discovery only" in (t.description or "").lower()

    def test_rdl_report_with_datasets(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        rdl_bytes = (FIXTURES_DIR / "pbirs_sample_report.rdl").read_bytes()

        responses = {
            "/PowerBIReports": _mock_response({"value": []}),
            "/Reports": _mock_response({"value": [{
                "Id": "rdl1", "Name": "Sales Report", "Path": "/Sales Report"
            }]}),
            "/Datasets": _mock_response({"value": []}),
            "/Kpis": _mock_response({"value": []}),
            "/DataSources": _mock_response({"value": []}),
            "/CatalogItems(rdl1)/Content/$value": _mock_response(content=rdl_bytes),
        }
        c._session = self._fake_session(responses)

        tables = c.get_schemas()
        assert len(tables) == 2
        names = sorted(t.name for t in tables)
        assert names == ["rdl:Sales Report/OrderDetails", "rdl:Sales Report/SalesByRegion"]

        sbr = next(t for t in tables if "SalesByRegion" in t.name)
        assert {c.name for c in sbr.columns} == {"Region", "TotalSales", "OrderCount"}
        meta = sbr.metadata_json["powerbi_report_server"]
        assert meta["report_type"] == "Report"
        assert meta["queryable"] is True
        assert "SUM(Amount)" in meta["command_text"]
        assert len(meta["report_parameters"]) == 3

    def test_kpi(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        responses = {
            "/PowerBIReports": _mock_response({"value": []}),
            "/Reports": _mock_response({"value": []}),
            "/Datasets": _mock_response({"value": []}),
            "/Kpis": _mock_response({"value": [{
                "Id": "k1", "Name": "Sales KPI", "Path": "/Sales KPI",
                "ValueFormat": "Currency",
                "Visualization": "CylindricalGauge",
                "Values": {"Value": 100, "Goal": 150, "Status": -1},
            }]}),
            "/DataSources": _mock_response({"value": []}),
        }
        c._session = self._fake_session(responses)

        tables = c.get_schemas()
        assert len(tables) == 1
        t = tables[0]
        assert t.name == "kpi:Sales KPI"
        meta = t.metadata_json["powerbi_report_server"]
        assert meta["report_type"] == "Kpi"
        assert meta["queryable"] is False
        assert meta["current_value"] == 100
        assert meta["goal_value"] == 150


# ---------- execute_query routing ---------- #


class TestExecuteQueryRouting:
    def _client_with_schemas(self, tables):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        c.get_schemas = MagicMock(return_value=tables)
        return c

    def test_pbix_raises_not_implemented(self):
        from app.ai.prompt_formatters import Table as PFTable
        tables = [PFTable(
            name="pbix:Sales", description="", columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {
                "report_type": "PowerBIReport",
                "report_id": "r1",
                "upstream_source": "SQL (Server=dw01;Database=Sales)",
                "data_sources": [{
                    "kind": "SQL", "connection_string": "Server=dw01;Database=Sales", "auth_type": "Windows"
                }],
            }},
        )]
        c = self._client_with_schemas(tables)
        with pytest.raises(NotImplementedError) as ei:
            c.execute_query(table_name="pbix:Sales")
        msg = str(ei.value)
        assert "discovery" in msg.lower()
        assert "Server=dw01" in msg

    def test_kpi_raises_value_error(self):
        from app.ai.prompt_formatters import Table as PFTable
        tables = [PFTable(
            name="kpi:Revenue", description="", columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {"report_type": "Kpi", "kpi_id": "k1"}},
        )]
        c = self._client_with_schemas(tables)
        with pytest.raises(ValueError, match="KPI"):
            c.execute_query(table_name="kpi:Revenue")

    def test_no_args_raises(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        with pytest.raises(ValueError, match="requires one of"):
            c.execute_query()


class TestDiscoveryFraming:
    """The client must be clear that it's NOT a queryable data source."""

    def test_description_flags_metadata_only(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        desc = c.description.lower()
        assert "metadata" in desc
        assert "not" in desc and ("queryable" in desc or "query" in desc)
        assert "upstream" in desc

    def test_system_prompt_warns_llm(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        sp = c.system_prompt()
        assert "NOT a queryable data source" in sp
        assert "upstream" in sp.lower()

    def test_registry_description_flags_metadata_only(self):
        from app.schemas.data_source_registry import REGISTRY
        entry = REGISTRY["powerbi_report_server"]
        d = entry.description.lower()
        assert "metadata" in d
        assert "not a queryable" in d
        assert "upstream" in d


# ---------- test_connection error paths ---------- #


class TestTestConnection:
    def test_unauthorized(self):
        c = PowerBIReportServerClient("http://pbi", "u", "badpass")
        c.connect = MagicMock()
        c.get_system_info = MagicMock(side_effect=RuntimeError("HTTP 401 Unauthorized"))
        res = c.test_connection()
        assert res["success"] is False
        assert "Authentication failed" in res["message"]

    def test_unreachable(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        c.connect = MagicMock()
        c.get_system_info = MagicMock(side_effect=RuntimeError("Connection refused"))
        res = c.test_connection()
        assert res["success"] is False
        assert "Cannot reach server" in res["message"]

    def test_auth_ok_but_catalog_list_fails(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        c.connect = MagicMock()
        c.get_system_info = MagicMock(return_value={"ProductName": "PBIRS", "ProductVersion": "1.0"})
        c.list_powerbi_reports = MagicMock(side_effect=RuntimeError("boom"))
        res = c.test_connection()
        assert res["success"] is False
        assert "could not list catalog" in res["message"]
        assert res["connectivity"] is True
