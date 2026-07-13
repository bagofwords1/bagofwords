"""Reproduction (confirmed root cause, variant 2): a file-source agent is
invisible to the planner's schema context.

Reported symptom: an agent (data source) with a **network files** connection
(and a SQLite connection whose relational tables aren't the active content) is
absent from the model's context — the agent even denies any file connection
exists — while it is attached to the report and has active files.

Confirmed mechanism (see docs/feedback-loops/report-context-agent-missing-inactive-tables.md):

`TablesSchemaContext.render_combined` — the render the main planner path uses to
build the schema context (`app/ai/agent_v2.py:1515`) — decides whether to include
a data source using ONLY relational tables / index / MCP tools, and **never
renders `file_scopes` at all** (`tables_schema_section.py:575-611`, the drop at
`:582-583`):

    if not (sample_xml or index_xml or mcp_xml):   # file_scopes NOT considered
        continue

So a data source whose active content is files (network_dir / s3 / sharepoint /
drive) — i.e. it contributes `file_scopes` but no active relational tables — is
dropped from the combined context entirely. The full `render("full")` DOES emit
`file_scopes` (`:346-348`) and never skips a source, which is why the same agent
is visible on other paths and via `@mention`.

These tests pin both facts: full render includes the file source; combined
render omits it.
"""
from app.ai.context.sections.tables_schema_section import (
    TablesSchemaContext,
    FileScopeItem,
)
from app.schemas.data_source_schema import DataSourceSummarySchema


def _file_only_ds(name):
    """A data source whose only active content is a network-files connection:
    file_scopes present, zero relational tables."""
    return TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id=name, name=name, type="network_dir"),
        tables=[],
        file_scopes=[FileScopeItem(
            connection_id="c1", name="NetFiles", type="network_dir",
            base="/mnt/share", file_count=42, sample=["report.txt", "data.csv"],
            index_mode="content", supports_search=True,
        )],
    )


def test_full_render_includes_the_file_source():
    """Sanity: the full render surfaces the file-source agent (so it is NOT that
    the data is missing — it is the combined render that drops it)."""
    ctx = TablesSchemaContext(data_sources=[_file_only_ds("Files Agent")])
    out = ctx.render("full")
    assert 'name="Files Agent"' in out
    assert "NetFiles" in out or "file" in out.lower()


def test_combined_render_drops_the_file_source():
    """THE BUG: render_combined (the planner's schema context) omits a
    file-source agent entirely — no file_scopes are rendered and the whole
    <data_source> is skipped, so the model reports it as not connected."""
    ctx = TablesSchemaContext(data_sources=[_file_only_ds("Files Agent")])
    out = ctx.render_combined(top_k_per_ds=10, index_limit=200)
    assert 'name="Files Agent"' not in out, (
        "a data source contributing only file_scopes must not vanish from the "
        "planner's schema context"
    )


def test_combined_render_drops_file_source_even_alongside_a_normal_one():
    """The multi-source reproduction: the relational agent survives, the
    file-source agent is silently dropped from the same combined context."""
    from app.ai.prompt_formatters import Table, TableColumn
    normal = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="db", name="Music Store", type="sqlite"),
        tables=[Table(name="Album", columns=[TableColumn(name="id", dtype="int")],
                      pks=[], fks=[], is_active=True)],
    )
    ctx = TablesSchemaContext(data_sources=[normal, _file_only_ds("Files Agent")])
    out = ctx.render_combined(top_k_per_ds=10, index_limit=200)
    assert 'name="Music Store"' in out
    assert 'name="Files Agent"' not in out
