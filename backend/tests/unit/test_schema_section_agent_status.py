"""Unit tests for the agent (data source) status block in the schema context.

The data source's status — connection health (active/inactive) and the
manager-set publishing lifecycle (published/draft/disabled) — is surfaced into
the schema context that PromptBuilderV3 feeds to the planner, each value paired
with a short human-readable explanation so the model understands what the status
MEANS, not just the bare token.
"""
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.ai.prompt_formatters import Table, TableColumn


def _ds(**info_kwargs):
    base = dict(id="1", name="Revenue", type="postgres")
    base.update(info_kwargs)
    return TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(**base),
        tables=[Table(name="orders", columns=[TableColumn(name="id", dtype="int")], is_active=True)],
    )


def test_status_block_includes_value_and_description_in_full_render():
    ctx = TablesSchemaContext(data_sources=[_ds(status="inactive", publish_status="draft")])
    out = ctx.render("full")
    assert "<status>" in out
    assert '<connection_health value="inactive">' in out
    assert "currently unreachable" in out
    assert '<publishing value="draft">' in out
    assert "still being configured" in out


def test_status_block_in_combined_render():
    ctx = TablesSchemaContext(data_sources=[_ds(status="active", publish_status="published")])
    out = ctx.render_combined(top_k_per_ds=10, index_limit=200)
    assert "<status>" in out
    assert '<connection_health value="active">' in out
    assert "healthy" in out
    assert '<publishing value="published">' in out
    assert "available to everyone" in out


def test_status_block_omitted_when_no_status():
    ctx = TablesSchemaContext(data_sources=[_ds()])
    out = ctx.render("full")
    assert "<status>" not in out


def test_unknown_status_value_renders_without_description():
    ctx = TablesSchemaContext(data_sources=[_ds(status="mystery")])
    out = ctx.render("full")
    assert '<connection_health value="mystery">' in out
