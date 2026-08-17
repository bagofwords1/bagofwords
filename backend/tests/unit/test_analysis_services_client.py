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

CUBES_TABULAR_MODEL_AND_PERSPECTIVE = _discover_envelope(
    "<row><CUBE_NAME>Retail Model</CUBE_NAME><CUBE_CAPTION>Retail Model</CUBE_CAPTION>"
    "<CUBE_TYPE>CUBE</CUBE_TYPE></row>"
    "<row><CUBE_NAME>Sales Perspective</CUBE_NAME><CUBE_CAPTION>Sales Perspective</CUBE_CAPTION>"
    "<CUBE_TYPE>CUBE</CUBE_TYPE></row>"
)

CSDL_TABULAR = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soap:Body><DiscoverResponse xmlns="urn:schemas-microsoft-com:xml-analysis"><return>'
    '<root xmlns="urn:schemas-microsoft-com:xml-analysis:rowset">'
    '<row><METADATA xmlns:xars="http://schemas.microsoft.com/analysisservices/2003/xmla-rowset">'
    '<Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" '
    'xmlns:bi="http://schemas.microsoft.com/sqlbi/2010/10/edm/extensions" Namespace="Retail">'
    '<EntityContainer Name="Retail">'
    '<EntitySet Name="Product" EntityType="Retail.Product"><bi:EntitySet /></EntitySet>'
    '<EntitySet Name="Internet_Sales" EntityType="Retail.Internet_Sales">'
    '<bi:EntitySet Caption="Internet Sales" ReferenceName="Internet Sales" /></EntitySet>'
    '<AssociationSet Name="Internet_Sales_Product_Product_Product_Id" '
    'Association="Retail.Internet_Sales_Product_Product_Product_Id">'
    '<End EntitySet="Internet_Sales" /><End EntitySet="Product" /><bi:AssociationSet />'
    '</AssociationSet>'
    '<AssociationSet Name="Internet_Sales_Product_Inactive" '
    'Association="Retail.Internet_Sales_Product_Inactive">'
    '<End EntitySet="Internet_Sales" /><End EntitySet="Product" />'
    '<bi:AssociationSet State="Inactive" />'
    '</AssociationSet>'
    '</EntityContainer>'
    '<EntityType Name="Product">'
    '<Key><PropertyRef Name="Product_Id" /></Key>'
    '<Property Name="RowNumber_internal" Type="Int64"><bi:Property Hidden="true" Contents="RowNumber" /></Property>'
    '<Property Name="Product_Id" Type="Int64" Nullable="false" Precision="19" Scale="0">'
    '<bi:Property Caption="Product Id" ReferenceName="Product Id" /></Property>'
    '<Property Name="Product_Name" Type="String" Nullable="true" MaxLength="100" '
    'Unicode="true" FixedLength="false">'
    '<bi:Property Caption="Product Name" ReferenceName="Product Name" '
    'FormatString="General" Stability="Stable" /></Property>'
    '<bi:EntityType Contents="Dimension" /></EntityType>'
    '<EntityType Name="Internet_Sales">'
    '<Property Name="Product_Id" Type="Int64">'
    '<bi:Property Caption="Product Id" ReferenceName="Product Id" /></Property>'
    '<Property Name="Sales_Amount" Type="Decimal">'
    '<bi:Property Caption="Sales Amount" ReferenceName="Sales Amount" /></Property>'
    '<Property Name="Total_Sales" Type="Decimal">'
    '<bi:Measure Caption="Total Sales" ReferenceName="Total Sales" '
    'FormatString="$#,0.00" /></Property>'
    '<bi:EntityType /></EntityType>'
    '<Association Name="Internet_Sales_Product_Product_Product_Id">'
    '<End Role="Internet_Sales_Product_Id" Type="Retail.Internet_Sales" Multiplicity="*" />'
    '<End Role="Product_Product_Id" Type="Retail.Product" Multiplicity="0..1" />'
    '</Association>'
    '<Association Name="Internet_Sales_Product_Inactive">'
    '<End Role="Internet_Sales_Product_Id" Type="Retail.Internet_Sales" Multiplicity="*" />'
    '<End Role="Product_Product_Id2" Type="Retail.Product" Multiplicity="0..1" />'
    '</Association>'
    '</Schema></METADATA></row></root></return></DiscoverResponse></soap:Body></soap:Envelope>'
).encode()

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


def _install_tabular_discovery_router(client: AnalysisServicesClient):
    """Route XMLA requests by their public operation, not internal call order."""
    session = MagicMock()

    def _post(url, data=None, headers=None, timeout=None, verify=None):
        request = (data or b"").decode("utf-8", errors="ignore")
        if "DISCOVER_CSDL_METADATA" in request:
            return _make_response(CSDL_TABULAR)
        if "TMSCHEMA_MODEL" in request:
            return _make_response(TMSCHEMA_MULTIDIM_ERR)
        if "MDSCHEMA_CUBES" in request:
            return _make_response(CUBES_TABULAR_MODEL_AND_PERSPECTIVE)
        if "MDSCHEMA_HIERARCHIES" in request:
            return _make_response(HIERARCHIES_SALES)
        if "MDSCHEMA_MEASURES" in request:
            return _make_response(MEASURES_SALES)
        raise AssertionError(f"Unexpected XMLA request: {request[:300]}")

    session.post.side_effect = _post
    client._http = session
    return session


def _install_admin_tabular_discovery_router(client: AnalysisServicesClient):
    """CSDL baseline plus the query-authoring TMSCHEMA rowsets."""
    rowsets = {
        "TMSCHEMA_TABLES": (
            "<row><ID>10</ID><Name>Product</Name><Description>Products sold</Description>"
            "<DataCategory>Product</DataCategory><IsHidden>false</IsHidden></row>"
            "<row><ID>20</ID><Name>Internet Sales</Name><Description>Sales facts</Description>"
            "<IsHidden>false</IsHidden></row>"
        ),
        "TMSCHEMA_COLUMNS": (
            "<row><ID>100</ID><TableID>10</TableID><Name>Product Id</Name>"
            "<DataType>Int64</DataType><IsHidden>false</IsHidden></row>"
            "<row><ID>101</ID><TableID>10</TableID><Name>Product Name</Name>"
            "<Description>Display product name</Description><DataType>String</DataType>"
            "<DataCategory>Product</DataCategory><DisplayFolder>Catalog</DisplayFolder>"
            "<SortByColumnID>100</SortByColumnID><SummarizeBy>None</SummarizeBy></row>"
            "<row><ID>201</ID><TableID>20</TableID><Name>Product Id</Name>"
            "<DataType>Int64</DataType></row>"
        ),
        "TMSCHEMA_MEASURES": (
            "<row><ID>500</ID><TableID>20</TableID><Name>Total Sales</Name>"
            "<Description>Revenue measure</Description><DataType>Decimal</DataType>"
            "<Expression>SUM(&apos;Internet Sales&apos;[Sales Amount])</Expression>"
            "<FormatString>$#,0.00</FormatString><DisplayFolder>Finance</DisplayFolder>"
            "<IsHidden>false</IsHidden></row>"
        ),
        "TMSCHEMA_RELATIONSHIPS": (
            "<row><Name>Product Sales</Name><FromTableID>20</FromTableID>"
            "<FromColumnID>201</FromColumnID><ToTableID>10</ToTableID>"
            "<ToColumnID>100</ToColumnID><IsActive>true</IsActive>"
            "<CrossFilteringBehavior>OneDirection</CrossFilteringBehavior></row>"
        ),
        "TMSCHEMA_HIERARCHIES": (
            "<row><ID>300</ID><TableID>10</TableID><Name>Products</Name>"
            "<Description>Product drill path</Description><DisplayFolder>Catalog</DisplayFolder>"
            "<IsHidden>false</IsHidden></row>"
        ),
        "TMSCHEMA_LEVELS": (
            "<row><HierarchyID>300</HierarchyID><Name>Product</Name>"
            "<ColumnID>101</ColumnID><Ordinal>0</Ordinal></row>"
        ),
        "TMSCHEMA_PARTITIONS": (
            "<row><TableID>20</TableID><Name>Internet Sales</Name>"
            "<Mode>Import</Mode><State>Ready</State></row>"
        ),
        "TMSCHEMA_ROLES": (
            "<row><Name>Readers</Name><ModelPermission>Read</ModelPermission></row>"
        ),
        "TMSCHEMA_PERSPECTIVES": "<row><Name>Sales</Name></row>",
        "TMSCHEMA_CULTURES": "<row><Name>en-US</Name></row>",
        "TMSCHEMA_KPIS": "",
    }
    session = MagicMock()

    def _post(url, data=None, headers=None, timeout=None, verify=None):
        request = (data or b"").decode("utf-8", errors="ignore")
        if "DISCOVER_CSDL_METADATA" in request:
            return _make_response(CSDL_TABULAR)
        for rowset, rows in rowsets.items():
            if rowset in request:
                return _make_response(_execute_envelope(rows))
        if "MDSCHEMA_CUBES" in request:
            return _make_response(CUBES_TABULAR_MODEL_AND_PERSPECTIVE)
        raise AssertionError(f"Unexpected XMLA request: {request[:300]}")

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

    def test_multidimensional_discovery_keeps_cube_schema(self):
        client = _client(catalog="AdventureWorks")
        _install_post(client, [
            _make_response(TMSCHEMA_MULTIDIM_ERR),  # CSDL unsupported
            _make_response(CUBES_SALES),
            _make_response(HIERARCHIES_SALES),
            _make_response(MEASURES_SALES),
        ])
        tables = client.get_schemas()
        assert [t.name for t in tables] == ["AdventureWorks/Sales"]
        meta = tables[0].metadata_json["analysis_services"]
        assert meta["modelType"] == "MULTIDIMENSIONAL"
        assert meta["supportsDax"] is False
        assert meta["preferredDialect"] == "MDX"
        assert [(c.name, c.dtype) for c in tables[0].columns] == [
            ("Category", "dimension"),
            ("Sales Amount", "measure"),
        ]

    def test_tabular_discovery_returns_physical_tables_for_read_only_user(self):
        client = _client(catalog="Retail")
        _install_tabular_discovery_router(client)

        tables = client.get_schemas()

        assert [table.name for table in tables] == [
            "Retail/Product",
            "Retail/Internet Sales",
        ]
        product, sales = tables
        assert [column.name for column in product.columns] == ["Product Id", "Product Name"]
        assert product.columns[1].metadata["unique_name"] == "'Product'[Product Name]"
        assert [(column.name, column.dtype) for column in sales.columns] == [
            ("Product Id", "Int64"),
            ("Sales Amount", "Decimal"),
            ("Total Sales", "measure"),
        ]
        assert sales.columns[2].metadata["unique_name"] == "[Total Sales]"
        assert sales.columns[2].metadata["format_string"] == "$#,0.00"
        assert product.columns[1].metadata == {
            "role": "column",
            "unique_name": "'Product'[Product Name]",
            "nullable": True,
            "max_length": 100,
            "unicode": True,
            "fixed_length": False,
            "format_string": "General",
            "stability": "Stable",
        }
        assert [column.name for column in product.pks] == ["Product Id"]
        assert len(sales.fks) == 1
        assert sales.fks[0].column.name == "Product Id"
        assert sales.fks[0].references_name == "Retail/Product"
        assert sales.fks[0].references_column.name == "Product Id"
        assert product.metadata_json["analysis_services"]["entity_contents"] == "Dimension"
        relationships = {
            relationship["name"]: relationship
            for relationship in sales.metadata_json["analysis_services"]["relationships"]
        }
        assert relationships["Internet_Sales_Product_Inactive"] == {
            "name": "Internet_Sales_Product_Inactive",
            "state": "inactive",
            "fromTable": "Internet Sales",
            "fromColumn": "Product Id",
            "toTable": "Product",
            "toColumn": "Product Id",
            "fromMultiplicity": "*",
            "toMultiplicity": "0..1",
        }
        assert relationships["Internet_Sales_Product_Product_Product_Id"]["state"] == "active"
        assert {
            key: value
            for key, value in sales.metadata_json["analysis_services"].items()
            if key != "relationships"
        } == {
            "catalog": "Retail",
            "tableName": "Internet Sales",
            "modelType": "TABULAR",
            "supportsDax": True,
            "preferredDialect": "DAX",
            "metadata_source": "CSDL",
        }

    def test_tabular_admin_metadata_enriches_without_replacing_csdl(self):
        client = _client(catalog="Retail")
        _install_admin_tabular_discovery_router(client)

        product, sales = client.get_schemas()

        product_name = next(c for c in product.columns if c.name == "Product Name")
        total_sales = next(c for c in sales.columns if c.name == "Total Sales")
        assert product.description == "Products sold"
        assert product_name.description == "Display product name"
        assert product_name.metadata["sort_by_column"] == "Product Id"
        assert product_name.metadata["display_folder"] == "Catalog"
        assert total_sales.description == "Revenue measure"
        assert total_sales.metadata["expression"] == "SUM('Internet Sales'[Sales Amount])"
        assert total_sales.metadata["display_folder"] == "Finance"
        assert product.metadata_json["analysis_services"]["metadata_source"] == "CSDL+TMSCHEMA"
        assert product.metadata_json["analysis_services"]["hierarchies"] == [{
            "name": "Products",
            "description": "Product drill path",
            "displayFolder": "Catalog",
            "hidden": False,
            "levels": [{"name": "Product", "ordinal": 0, "column": "Product Name"}],
        }]
        assert sales.metadata_json["analysis_services"]["partitions"] == [{
            "name": "Internet Sales", "mode": "Import", "state": "Ready",
        }]
        assert product.metadata_json["analysis_services"]["model_metadata"] == {
            "roles": [{"name": "Readers", "permission": "Read"}],
            "perspectives": ["Sales"],
            "cultures": ["en-US"],
        }


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

    def test_attached_tabular_metadata_avoids_query_time_schema_crawl(self):
        client = _client()
        client.attach_table_metadata([{
            "name": "AdventureWorks/Product",
            "metadata_json": {"analysis_services": {
                "catalog": "AdventureWorks",
                "tableName": "Product",
                "modelType": "TABULAR",
                "supportsDax": True,
                "preferredDialect": "DAX",
            }},
        }])
        session = _install_post(client, [_make_response(EXECUTE_OK)])

        df = client.execute_query("EVALUATE TOPN(3, 'Product')", "AdventureWorks/Product")

        assert len(df) == 2
        assert session.post.call_count == 1
        assert b"<Statement>EVALUATE TOPN(3, 'Product')</Statement>" in session.post.call_args.kwargs["data"]

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

    def test_prompt_does_not_teach_a_fake_sales_cube(self):
        text = _client().system_prompt()
        assert "FROM [Sales]" not in text
        assert "preferredDialect" in text
        assert "exact physical table" in text

    def test_resolve_client_class(self):
        from app.schemas.data_source_registry import resolve_client_class
        assert resolve_client_class("analysis_services") is AnalysisServicesClient
