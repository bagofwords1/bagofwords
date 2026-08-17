"""Connector metadata the query path needs must reach the agent's schema context.

Two classes of metadata were captured at indexing time and then dropped by the
renderers, leaving the agent unable to author a valid query:

1. **Column query identifiers (XMLA).** `analysis_services`, `sap_bw` and
   `infor_olap` all pass statements straight to XMLA Execute — there is no
   caption-to-identifier resolution — so the agent must author MDX/DAX itself.
   `TableColumn.name` is the human *caption* ("Category"), while MDX needs the
   bracketed identifier ("[Product].[Category]"), which is not derivable from
   it. All three clients' system prompts tell the agent to reference
   `metadata.unique_name`, so it has to actually be present.

2. **Table object identifiers.** `TableauClient.execute_query` takes
   `datasource_luid` as a required positional argument, and the SSAS system
   prompt instructs the agent to pick MDX vs DAX from
   `metadata.analysis_services.modelType`. Prometheus's `metric_type` decides
   whether `rate()` is required — a counter queried without it returns
   meaningless monotonic values, which fails *silently*.

Deliberately NOT surfaced (the value is recoverable, so it would be noise):
qlik_sense / sisense / businessobjects resolve their object from the table name
inside `execute_query`; XMLA `cubeUniqueName` is `f"[{cube}]"` where `cube` is
the second segment of `Table.name`; every SQL client builds `name=fqn`.
"""
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.ai.prompt_formatters import Table, TableColumn
from app.schemas.data_source_schema import DataSourceSummarySchema


def _ctx(tables, ds_type="analysis_services"):
    return TablesSchemaContext(data_sources=[
        TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="DS", type=ds_type),
            tables=tables,
        )
    ])


def _cube_table(meta=None):
    """An XMLA cube as xmla_base.get_schemas builds it: caption-named columns
    whose MDX identifier lives only in metadata.unique_name."""
    return Table(
        name="AdventureWorks/Sales",
        description="Sales cube",
        columns=[
            TableColumn(
                name="Category", dtype="dimension", description="Product category",
                metadata={"role": "dimension", "unique_name": "[Product].[Category]"},
            ),
            TableColumn(
                name="Sales Amount", dtype="measure", description="Net sales",
                metadata={"role": "measure", "unique_name": "[Measures].[Sales Amount]"},
            ),
        ],
        pks=[], fks=[], is_active=True, metadata_json=meta,
    )


# ── 1. Column-level: MDX/DAX identifiers ───────────────────────────────────

def test_combined_render_includes_column_unique_names():
    """The agent path (render_combined) is what agent_v2 calls."""
    out = _ctx([_cube_table()]).render_combined(top_k_per_ds=10, index_limit=200)
    assert 'unique_name="[Product].[Category]"' in out
    assert 'unique_name="[Measures].[Sales Amount]"' in out


def test_full_render_includes_column_unique_names():
    out = _ctx([_cube_table()]).render("full")
    assert 'unique_name="[Product].[Category]"' in out
    assert 'unique_name="[Measures].[Sales Amount]"' in out


def test_unique_name_does_not_replace_role_or_caption():
    """unique_name is additive — captions and roles still render."""
    out = _ctx([_cube_table()]).render_combined(top_k_per_ds=10, index_limit=200)
    assert 'name="Category"' in out
    assert 'role="dimension"' in out
    assert 'role="measure"' in out


def test_no_unique_name_attr_when_column_has_none():
    """Columns without the key must not gain an empty attribute."""
    t = Table(
        name="public.orders",
        columns=[TableColumn(name="id", dtype="int")],
        pks=[], fks=[], is_active=True, metadata_json={"schema": "public"},
    )
    out = _ctx([t], ds_type="postgresql").render_combined(top_k_per_ds=10, index_limit=200)
    assert "unique_name" not in out


# ── 2. Table-level: object identifiers ─────────────────────────────────────

def test_ssas_model_type_reaches_agent_context():
    """The SSAS system prompt says to pick MDX vs DAX from modelType."""
    meta = {"analysis_services": {
        "catalog": "AdventureWorks", "cube": "Sales",
        "modelType": "TABULAR", "supportsDax": True, "preferredDialect": "DAX",
    }}
    out = _ctx([_cube_table(meta)]).render_combined(top_k_per_ds=10, index_limit=200)
    assert 'modelType="TABULAR"' in out
    assert 'supportsDax="true"' in out
    assert 'preferredDialect="DAX"' in out


def test_ssas_tabular_semantics_reach_agent_context():
    """Formats, sort order, hierarchies and inactive joins change valid DAX."""
    table = Table(
        name="AdventureWorks/Date",
        columns=[TableColumn(
            name="Month", dtype="String", metadata={
                "role": "column",
                "format_string": "MMMM",
                "sort_by_column": "Month Number",
                "data_category": "Time",
            },
        )],
        pks=[], fks=[], is_active=True,
        metadata_json={"analysis_services": {
            "modelType": "TABULAR", "supportsDax": True, "preferredDialect": "DAX",
            "entity_contents": "Time",
            "hierarchies": [{
                "name": "Calendar", "levels": [
                    {"name": "Month", "column": "Month", "ordinal": 0},
                ],
            }],
            "relationships": [{
                "name": "Ship Date", "state": "inactive", "fromColumn": "Ship Date Key",
                "toTable": "Date", "toColumn": "Date Key",
            }],
        }},
    )

    out = _ctx([table]).render_combined(top_k_per_ds=10, index_limit=200)

    assert 'format_string="MMMM"' in out
    assert 'sort_by_column="Month Number"' in out
    assert 'entity_contents="Time"' in out
    assert '<hierarchy name="Calendar">' in out
    assert "<level " in out
    assert 'name="Month"' in out
    assert 'column="Month"' in out
    assert 'ordinal="0"' in out
    assert 'state="inactive"' in out
    assert 'to_table="Date"' in out


def test_tableau_luid_reaches_the_agent_path():
    """TableauClient.execute_query takes datasource_luid as a required arg.

    It already rendered via render(); render_combined() — the path agent_v2
    actually uses — dropped it.
    """
    t = Table(
        name="Finance/Superstore", description="Sales datasource",
        columns=[TableColumn(name="Sales", dtype="REAL", metadata={"role": "MEASURE"})],
        pks=[], fks=[], is_active=True,
        metadata_json={"tableau": {"datasourceLuid": "abc-123", "projectName": "Finance"}},
    )
    combined = _ctx([t], ds_type="tableau").render_combined(top_k_per_ds=10, index_limit=200)
    assert 'datasourceLuid="abc-123"' in combined
    # and the pre-existing full-render behaviour is preserved
    assert 'datasourceLuid="abc-123"' in _ctx([t], ds_type="tableau").render("full")


def test_prometheus_metric_type_and_unit_reach_context():
    """counter vs gauge decides whether rate() is required; the failure is silent."""
    t = Table(
        name="http_requests_total", description="Total HTTP requests",
        columns=[TableColumn(name="value", dtype="float")],
        pks=[], fks=[], is_active=True,
        metadata_json={"metric_type": "counter", "unit": "seconds", "source": "prometheus"},
    )
    out = _ctx([t], ds_type="prometheus").render_combined(top_k_per_ds=10, index_limit=200)
    assert 'metric_type="counter"' in out
    assert 'unit="seconds"' in out


# ── 3. Restraint: no block for sources that don't need one ─────────────────

def test_no_source_meta_block_for_plain_sql_tables():
    """Every SQL client builds name=fqn, so {"schema": ...} is duplication."""
    t = Table(
        name="public.orders", columns=[TableColumn(name="id", dtype="int")],
        pks=[], fks=[], is_active=True, metadata_json={"schema": "public"},
    )
    out = _ctx([t], ds_type="postgresql").render_combined(top_k_per_ds=10, index_limit=200)
    assert "<source_meta" not in out
    assert "<analysis_services" not in out


def test_redundant_ids_are_not_surfaced():
    """qlik/sisense/businessobjects resolve from the table name — surfacing
    their ids would be noise, so they must stay out of the context."""
    for ns, blob in (
        ("qlik_sense", {"appId": "app-1", "appName": "Sales"}),
        ("sisense", {"datamodelId": "dm-1", "datamodelTitle": "SalesModel"}),
        ("businessobjects", {"universe_id": "u-1", "universe_name": "Sales"}),
    ):
        t = Table(
            name="Sales/Orders", columns=[TableColumn(name="id", dtype="int")],
            pks=[], fks=[], is_active=True, metadata_json={ns: blob},
        )
        out = _ctx([t], ds_type=ns).render_combined(top_k_per_ds=10, index_limit=200)
        assert f"<{ns}" not in out, f"{ns} id should not be surfaced (recoverable from name)"


def test_malformed_metadata_does_not_break_rendering():
    """metadata_json shapes vary across connectors; rendering must not raise."""
    for bad in (None, {}, {"tableau": "not-a-dict"}, {"analysis_services": []}):
        t = Table(
            name="x", columns=[TableColumn(name="id", dtype="int")],
            pks=[], fks=[], is_active=True, metadata_json=bad,
        )
        out = _ctx([t]).render_combined(top_k_per_ds=10, index_limit=200)
        assert "<table" in out
