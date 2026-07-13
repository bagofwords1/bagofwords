"""Reproduction (confirmed root cause): a report-attached agent with zero
*active* tables is silently omitted from the agent's schema context.

Reported symptom: an agent (data source) is selected in the prompt box and
attached to the report, the connection is live, the user is an admin — yet the
model answers that it is "connected to 2 data sources" and never mentions the
third. Mentioning the agent works, because @mention uses a different context
path.

Confirmed mechanism (end-to-end, see docs/feedback-loops/report-context-agent-
missing-inactive-tables.md):

1. A newly created agent indexes its tables but activates NONE of them:
   ``create_data_source`` → ``sync_domain_tables_from_connection(
   max_auto_select=ONBOARDING_MAX_TABLES)`` and
   ``ONBOARDING_MAX_TABLES == 0`` (data_source_service.py). With
   ``max_auto_select == 0`` the activation logic sets
   ``should_activate = total_tables <= 0`` → False for every table, and the
   smart-select fallback ``if needs_smart_selection and max_auto_select:`` is
   skipped because ``0`` is falsy. Result: 0 active tables.

2. ``SchemaContextBuilder.build(active_only=True)`` returns that data source
   with an empty ``tables`` list (schema_context_builder.py:86-87).

3. ``TablesSchemaContext.render_combined`` then DROPS the entire
   ``<data_source>`` element when it renders no tables, index, or MCP tools
   (tables_schema_section.py:582-583) — so the agent's prompt never mentions it.

This test pins step 3 (the silent omission) at the render layer, which is the
invariant a fix must change: an attached data source should not vanish from the
context with no signal just because its tables aren't active yet.
"""
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.ai.prompt_formatters import Table, TableColumn


def _ds(name, *, with_table):
    tables = []
    if with_table:
        tables = [Table(name="orders", columns=[TableColumn(name="id", dtype="int")],
                        pks=[], fks=[], is_active=True)]
    return TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id=name, name=name, type="sqlite"),
        tables=tables,
    )


def test_agent_with_no_active_tables_is_omitted_from_combined_context():
    """An attached agent that renders zero tables disappears from the prompt,
    while its siblings with tables remain — the exact 'connected to 2 of 3'
    reproduction."""
    ctx = TablesSchemaContext(data_sources=[
        _ds("Music Store", with_table=True),        # demo: tables pre-activated
        _ds("Financial Market Agent", with_table=True),
        _ds("Sales Agent", with_table=False),        # fresh custom agent: 0 active
    ])
    out = ctx.render_combined(top_k_per_ds=10, index_limit=200)

    assert 'name="Music Store"' in out
    assert 'name="Financial Market Agent"' in out
    # THE BUG: the attached agent with no active tables is silently absent.
    assert 'name="Sales Agent"' not in out, (
        "an attached data source with zero active tables must not vanish from "
        "the agent context without any signal"
    )


def test_full_render_also_drops_the_empty_sources_tables():
    """The full render emits no <table> rows for the zero-active-tables agent,
    so the model has nothing to work with and reports it as not connected."""
    ctx = TablesSchemaContext(data_sources=[_ds("Sales Agent", with_table=False)])
    combined = ctx.render_combined(top_k_per_ds=10, index_limit=200)
    # No sample/index content is produced → the whole source is dropped.
    assert "Sales Agent" not in combined
