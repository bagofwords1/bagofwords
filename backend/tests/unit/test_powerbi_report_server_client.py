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
    _AUTO_DATE_TABLE_RE,
    _clr_to_dtype,
    _dax_to_dtype,
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

    def test_dax_to_dtype(self):
        assert _dax_to_dtype("Int64") == "int"
        assert _dax_to_dtype("Float64") == "float"
        assert _dax_to_dtype("String") == "string"
        assert _dax_to_dtype("DateTime") == "datetime"
        assert _dax_to_dtype("Boolean") == "bool"
        assert _dax_to_dtype(None) == "unknown"

    def test_auto_date_table_regex(self):
        assert _AUTO_DATE_TABLE_RE.match("LocalDateTable_12345678-aaaa-bbbb-cccc-123456789012")
        assert _AUTO_DATE_TABLE_RE.match("DateTableTemplate_deadbeef-0000-1111-2222-333333333333")
        assert not _AUTO_DATE_TABLE_RE.match("Orders")
        assert not _AUTO_DATE_TABLE_RE.match("DateTable")

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
        c = PowerBIReportServerClient("http://pbi", "u", "p", extract_pbix_schemas=False)
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

    def test_pbix_schema_uplift(self):
        """When pbix schema extraction returns data, emit one Table per internal model table
        with real columns + measures, plus the umbrella discovery row."""
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        pbi_reports = [{
            "Id": "r1",
            "Name": "Sales Dashboard",
            "Path": "/Sales Dashboard",
            "ModifiedDate": "2024-01-01T00:00:00Z",
        }]
        responses = {
            "/PowerBIReports": _mock_response({"value": pbi_reports}),
            "/Reports": _mock_response({"value": []}),
            "/Datasets": _mock_response({"value": []}),
            "/Kpis": _mock_response({"value": []}),
            "/DataSources": _mock_response({"value": []}),
            "/PowerBIReports(r1)/DataSources": _mock_response({"value": []}),
            "/PowerBIReports(r1)/DataModelParameters": _mock_response({"value": []}),
            "/PowerBIReports(r1)/DataModelRoles": _mock_response({"value": []}),
        }
        c._session = self._fake_session(responses)

        fake_schema = {
            "source": "pbixray",
            "report_id": "r1",
            "report_name": "Sales Dashboard",
            "tables": [
                {
                    "name": "Orders",
                    "columns": [
                        {"name": "OrderID", "dtype": "int"},
                        {"name": "CustomerID", "dtype": "int"},
                        {"name": "Amount", "dtype": "float"},
                    ],
                    "measures": [
                        {"name": "Total Sales", "expression": "SUM(Orders[Amount])",
                         "display_folder": "", "description": ""}
                    ],
                },
                {
                    "name": "Customers",
                    "columns": [
                        {"name": "CustomerID", "dtype": "int"},
                        {"name": "Name", "dtype": "string"},
                    ],
                    "measures": [],
                },
            ],
            "relationships": [
                {"from_table": "Orders", "from_column": "CustomerID",
                 "to_table": "Customers", "to_column": "CustomerID",
                 "is_active": True, "cardinality": "many_to_one"},
            ],
        }
        c.extract_pbix_schema = MagicMock(return_value=fake_schema)

        tables = c.get_schemas()
        names = sorted(t.name for t in tables)
        assert names == [
            "pbix:Sales Dashboard",
            "pbix:Sales Dashboard/Customers",
            "pbix:Sales Dashboard/Orders",
        ]

        umbrella = next(t for t in tables if t.name == "pbix:Sales Dashboard")
        umb_meta = umbrella.metadata_json["powerbi_report_server"]
        assert umb_meta["schema_source"] == "pbixray"
        assert set(umb_meta["model_tables"]) == {"Orders", "Customers"}
        assert len(umb_meta["model_relationships"]) == 1

        orders = next(t for t in tables if t.name.endswith("/Orders"))
        assert {col.name for col in orders.columns} == {"OrderID", "CustomerID", "Amount"}
        assert len(orders.fks) == 1
        assert orders.fks[0].references_name == "pbix:Sales Dashboard/Customers"
        assert orders.fks[0].column.name == "CustomerID"
        orders_meta = orders.metadata_json["powerbi_report_server"]
        assert orders_meta["report_type"] == "PowerBIReportTable"
        assert orders_meta["model_table"] == "Orders"
        assert orders_meta["measures"][0]["name"] == "Total Sales"
        # queryable flips True under the default enable_pbix_query=True
        assert orders_meta["queryable"] is True

        customers = next(t for t in tables if t.name.endswith("/Customers"))
        assert {col.name for col in customers.columns} == {"CustomerID", "Name"}
        assert customers.fks == []

    def test_pbix_schema_extraction_failure_falls_back(self):
        """When extraction returns None (download failed, parse failed, etc.),
        only the umbrella discovery row is emitted."""
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        pbi_reports = [{"Id": "r1", "Name": "Broken", "Path": "/Broken"}]
        responses = {
            "/PowerBIReports": _mock_response({"value": pbi_reports}),
            "/Reports": _mock_response({"value": []}),
            "/Datasets": _mock_response({"value": []}),
            "/Kpis": _mock_response({"value": []}),
            "/DataSources": _mock_response({"value": []}),
            "/PowerBIReports(r1)/DataSources": _mock_response({"value": []}),
            "/PowerBIReports(r1)/DataModelParameters": _mock_response({"value": []}),
            "/PowerBIReports(r1)/DataModelRoles": _mock_response({"value": []}),
        }
        c._session = self._fake_session(responses)
        c.extract_pbix_schema = MagicMock(return_value=None)

        tables = c.get_schemas()
        assert len(tables) == 1
        assert tables[0].name == "pbix:Broken"

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
    def _client_with_schemas(self, tables, **kwargs):
        c = PowerBIReportServerClient("http://pbi", "u", "p", **kwargs)
        c.get_schemas = MagicMock(return_value=tables)
        return c

    def test_pbix_raises_not_implemented_when_disabled(self):
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
        c = self._client_with_schemas(tables, enable_pbix_query=False)
        with pytest.raises(NotImplementedError) as ei:
            c.execute_query(table_name="pbix:Sales")
        msg = str(ei.value)
        assert "disabled" in msg.lower()
        assert "Server=dw01" in msg

    def test_pbix_requires_query_when_enabled(self):
        from app.ai.prompt_formatters import Table as PFTable
        tables = [PFTable(
            name="pbix:Sales/Orders", description="", columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {
                "report_type": "PowerBIReportTable",
                "report_id": "r1",
                "report_name": "Sales",
                "modified_date": "2024-01-01T00:00:00Z",
                "model_table": "Orders",
            }},
        )]
        c = self._client_with_schemas(tables)
        with pytest.raises(ValueError) as ei:
            c.execute_query(table_name="pbix:Sales/Orders")
        assert "query" in str(ei.value).lower()

    def test_pbix_query_routes_to_duckdb(self, tmp_path, monkeypatch):
        """execute_query on pbix:<Report>/<Table> with a query runs DuckDB over the
        Parquet cache, with all same-report tables registered as views."""
        import pandas as pd
        from app.ai.prompt_formatters import Table as PFTable

        # Build two small parquet files to stand in for pbix model tables.
        orders = pd.DataFrame({"OrderID": [1, 2, 3], "CustomerID": [10, 10, 20], "Amount": [5.0, 7.5, 2.0]})
        customers = pd.DataFrame({"CustomerID": [10, 20], "Name": ["Alice", "Bob"]})
        orders_path = tmp_path / "orders.parquet"
        customers_path = tmp_path / "customers.parquet"
        orders.to_parquet(orders_path)
        customers.to_parquet(customers_path)

        tables = [PFTable(
            name="pbix:Sales/Orders", description="", columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {
                "report_type": "PowerBIReportTable",
                "report_id": "r1",
                "report_name": "Sales",
                "modified_date": "2024-01-01T00:00:00Z",
                "model_table": "Orders",
            }},
        )]
        c = self._client_with_schemas(tables)

        # Short-circuit the parquet materializer — we pre-built the files.
        c.ensure_pbix_parquets = MagicMock(return_value={
            "Orders": orders_path,
            "Customers": customers_path,
        })

        df = c.execute_query(
            table_name="pbix:Sales/Orders",
            query=(
                "SELECT c.Name, SUM(o.Amount) AS total "
                "FROM Orders o JOIN Customers c ON o.CustomerID = c.CustomerID "
                "GROUP BY c.Name ORDER BY c.Name"
            ),
        )
        assert list(df["Name"]) == ["Alice", "Bob"]
        assert list(df["total"]) == [12.5, 2.0]

    def test_pbix_query_missing_report_id(self):
        from app.ai.prompt_formatters import Table as PFTable
        tables = [PFTable(
            name="pbix:Broken", description="", columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {
                "report_type": "PowerBIReport",
                # report_id missing
                "modified_date": "2024-01-01",
            }},
        )]
        c = self._client_with_schemas(tables)
        with pytest.raises(ValueError, match="report_id"):
            c.execute_query(table_name="pbix:Broken", query="SELECT 1")

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


class TestEnsurePbixParquets:
    """Direct tests for the Parquet materialization + caching path."""

    def test_cache_hit_skips_download(self, tmp_path, monkeypatch):
        """When manifest.json already exists, no pbix download is attempted."""
        import app.data_sources.clients.powerbi_report_server_client as mod

        monkeypatch.setattr(mod, "_PBIX_DATA_CACHE_DIR", tmp_path)
        cache_dir = mod._pbix_data_cache_dir("rX", "2024-01-01T00:00:00Z")
        cache_dir.mkdir(parents=True)
        (cache_dir / "orders.parquet").write_bytes(b"not really a parquet")
        (cache_dir / "manifest.json").write_text(json.dumps({"Orders": "orders.parquet"}))

        c = PowerBIReportServerClient("http://pbi", "u", "p")
        # download_catalog_item_content should NOT be called — raise if it is.
        c.download_catalog_item_content = MagicMock(
            side_effect=AssertionError("download should not run on cache hit")
        )

        paths = c.ensure_pbix_parquets("rX", "2024-01-01T00:00:00Z", report_name="X")
        assert set(paths.keys()) == {"Orders"}
        assert paths["Orders"].name == "orders.parquet"

    def test_size_cap_rejects_oversized_pbix(self, tmp_path, monkeypatch):
        import app.data_sources.clients.powerbi_report_server_client as mod

        monkeypatch.setattr(mod, "_PBIX_DATA_CACHE_DIR", tmp_path)
        monkeypatch.setattr(mod, "_PBIX_MAX_BYTES", 10)

        c = PowerBIReportServerClient("http://pbi", "u", "p")
        c.download_catalog_item_content = MagicMock(return_value=b"x" * 100)

        with pytest.raises(RuntimeError, match="refusing to materialize"):
            c.ensure_pbix_parquets("rBig", "mdate", report_name="Huge")


class TestDiscoveryFraming:
    """Framing must accurately describe pbix queryability and warn about snapshot staleness."""

    def test_description_default_flags_cached_snapshot(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        desc = c.description.lower()
        assert "parquet" in desc or "snapshot" in desc
        assert "upstream" in desc

    def test_description_when_query_disabled_flags_metadata_only(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p", enable_pbix_query=False)
        desc = c.description.lower()
        assert "not queryable" in desc

    def test_system_prompt_explains_pbix_query(self):
        c = PowerBIReportServerClient("http://pbi", "u", "p")
        sp = c.system_prompt()
        assert "DuckDB" in sp
        assert "snapshot" in sp.lower() or "cached" in sp.lower()
        assert "upstream" in sp.lower()

    def test_registry_description_explains_connector(self):
        from app.schemas.data_source_registry import REGISTRY
        entry = REGISTRY["powerbi_report_server"]
        d = entry.description.lower()
        assert "power bi report server" in d
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
