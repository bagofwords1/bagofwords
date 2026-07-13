"""Power BI (cloud) metadata must be rendered into the agent's schema context.

The executeQueries endpoint is addressed by dataset GUID, so the agent needs
each table's datasetId/workspaceId (or the exact schema table name) at query
time. These GUIDs are captured at indexing time in metadata_json.powerbi, but
were historically stripped by every renderer — leaving the agent no way to
resolve a dataset and prompting it to ask the user for the GUID.
"""
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter, TableFormatter


PBI_META = {
    "powerbi": {
        "datasetId": "11111111-aaaa-bbbb-cccc-000000000001",
        "workspaceId": "22222222-aaaa-bbbb-cccc-000000000002",
        "workspaceName": "Sales WS",
        "datasetName": "SalesModel",
        "tableName": "Customers",
        "reports": [],
    }
}

PBIRS_META = {
    "powerbi_report_server": {
        "report_type": "Dataset",
        "dataset_id": "33333333-aaaa-bbbb-cccc-000000000003",
        "queryable": True,
        "query_note": "Execute via execute_query(table_name=...).",
    }
}


def _pbi_table(name="SalesModel/Customers", meta=PBI_META):
    return Table(
        name=name,
        columns=[TableColumn(name="id", dtype="int")],
        pks=[], fks=[], is_active=True,
        metadata_json=meta,
    )


def _ctx(tables):
    return TablesSchemaContext(data_sources=[
        TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="PBI", type="powerbi"),
            tables=tables,
        )
    ])


def test_full_render_includes_dataset_and_workspace_guids():
    out = _ctx([_pbi_table()]).render("full")
    assert '<powerbi ' in out
    assert 'datasetId="11111111-aaaa-bbbb-cccc-000000000001"' in out
    assert 'workspaceId="22222222-aaaa-bbbb-cccc-000000000002"' in out
    assert 'datasetName="SalesModel"' in out


def test_combined_render_includes_dataset_and_workspace_guids():
    out = _ctx([_pbi_table()]).render_combined(top_k_per_ds=10, index_limit=200)
    assert 'datasetId="11111111-aaaa-bbbb-cccc-000000000001"' in out
    assert 'workspaceId="22222222-aaaa-bbbb-cccc-000000000002"' in out


def test_no_powerbi_block_for_other_sources():
    out = _ctx([_pbi_table(name="orders", meta={"schema": "public"})]).render("full")
    assert '<powerbi ' not in out


def test_pbirs_render_includes_report_and_dataset_ids():
    out = _ctx([_pbi_table(name="dataset:Sales", meta=PBIRS_META)]).render("full")
    assert 'dataset_id="33333333-aaaa-bbbb-cccc-000000000003"' in out


def test_service_formatter_includes_guids():
    out = ServiceFormatter([_pbi_table()]).table_str
    assert "datasetId=11111111-aaaa-bbbb-cccc-000000000001" in out
    assert "workspaceId=22222222-aaaa-bbbb-cccc-000000000002" in out


def test_table_formatter_includes_guids():
    out = TableFormatter([_pbi_table()]).table_str
    assert "datasetId=11111111-aaaa-bbbb-cccc-000000000001" in out
    assert "workspaceId=22222222-aaaa-bbbb-cccc-000000000002" in out
