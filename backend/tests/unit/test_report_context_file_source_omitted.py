"""Regression tests for the file-source / partial-connection context fix.

Bug (now fixed): `TablesSchemaContext.render_combined` — the render the main
planner path uses to build the schema context (`app/ai/agent_v2.py:1515`) —
decided whether to include a data source using ONLY relational tables / index /
MCP tools and never rendered `file_scopes`. So a data source whose active
content is files (network_dir / s3 / sharepoint / drive), or a multi-connection
agent whose relational connection is dead, produced an EMPTY `<data_sources>`
block and the model reported it as not connected (never calling list_files).

Fix (`tables_schema_section.py:render_combined`, `schema_context_builder.py`):
  * `render_combined` now renders `file_scopes` and counts them (plus any
    unavailable-connection note) when deciding whether to keep a source, so a
    source is dropped only when it has NOTHING to contribute.
  * The schema builder collects connections it withheld because they are
    unhealthy (`Connection.is_active` False) and, as long as a sibling
    connection is still live, surfaces them as `<unavailable_connections>` so a
    dead DB connection never takes its healthy file sibling — or the whole agent
    — down with it.
"""
from app.ai.context.sections.tables_schema_section import (
    TablesSchemaContext,
    FileScopeItem,
)
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.ai.prompt_formatters import Table, TableColumn


def _file_only_ds(name, unhealthy=None):
    """A data source whose active content is a network-files connection:
    file_scopes present, zero relational tables. Optionally carrying a list of
    withheld (unhealthy) connections."""
    return TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id=name, name=name, type="network_dir"),
        tables=[],
        file_scopes=[FileScopeItem(
            connection_id="c1", name="NetFiles", type="network_dir",
            base="/mnt/share", file_count=42, sample=["report.txt", "data.csv"],
            index_mode="content", supports_search=True,
        )],
        unhealthy_connections=unhealthy or [],
    )


def test_full_render_includes_the_file_source():
    ctx = TablesSchemaContext(data_sources=[_file_only_ds("Files Agent")])
    out = ctx.render("full")
    assert 'name="Files Agent"' in out


def test_combined_render_keeps_the_file_source():
    """A files-only agent is now present in the planner's schema context, with its
    file scope rendered so the model knows to call list_files/search_files."""
    ctx = TablesSchemaContext(data_sources=[_file_only_ds("Files Agent")])
    out = ctx.render_combined(top_k_per_ds=10, index_limit=200)
    assert 'name="Files Agent"' in out
    assert 'kind="files"' in out            # the file scope is rendered
    assert "list_files" in out              # with retrieval-tool guidance


def test_combined_render_keeps_file_source_alongside_a_normal_one():
    normal = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="db", name="Music Store", type="sqlite"),
        tables=[Table(name="Album", columns=[TableColumn(name="id", dtype="int")],
                      pks=[], fks=[], is_active=True)],
    )
    ctx = TablesSchemaContext(data_sources=[normal, _file_only_ds("Files Agent")])
    out = ctx.render_combined(top_k_per_ds=10, index_limit=200)
    assert 'name="Music Store"' in out
    assert 'name="Files Agent"' in out


def test_partial_connection_agent_kept_and_dead_connection_flagged():
    """Multi-connection agent: one connection is live (files), one is withheld
    as unhealthy. The agent stays in context, the live side renders, and the
    dead side is flagged as unavailable rather than silently dropped."""
    ds = _file_only_ds(
        "Procurement Agent",
        unhealthy=[{"name": "Procurement DB", "type": "sqlite"}],
    )
    out = TablesSchemaContext(data_sources=[ds]).render_combined(
        top_k_per_ds=10, index_limit=200
    )
    assert 'name="Procurement Agent"' in out
    assert 'kind="files"' in out                          # healthy side rendered
    assert "<unavailable_connections>" in out             # dead side surfaced
    assert 'name="Procurement DB"' in out
    assert "do not attempt to query" in out


def test_agent_with_no_content_and_no_dead_connection_is_still_dropped():
    """An agent that truly contributes nothing — no tables, no file scopes, no
    unhealthy connection to report — is still omitted (unchanged behavior)."""
    empty = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="e", name="Empty Agent", type="sqlite"),
        tables=[],
    )
    out = TablesSchemaContext(data_sources=[empty]).render_combined(
        top_k_per_ds=10, index_limit=200
    )
    assert 'name="Empty Agent"' not in out
