"""A Teams connection must reach the planner in chat vocabulary.

The planner picks tools from the schema context. A Teams scope rendered like a
file share told it to call list_files/search_files (tools a Teams client does
not back) and gave no hint that the team's discussions hold numbers,
definitions and decisions — so a question about a dashboard figure was
answered "no data source" without ever searching the conversations.
"""
from app.ai.context.sections.tables_schema_section import FileScopeItem, TablesSchemaContext
from app.schemas.data_source_schema import DataSourceSummarySchema


def _render(mode: str) -> str:
    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="teams-agent", name="Microsoft Teams", type="teams_messages"),
        tables=[],
        file_scopes=[FileScopeItem(
            connection_id="conn-teams", name="Microsoft Teams", type="teams_messages",
            base=None, file_count=0, sample=[], index_mode="content",
            supports_search=True, per_user=True, enforces_scope=False,
        )],
    )
    ctx = TablesSchemaContext(data_sources=[ds])
    return ctx.render_combined(top_k_per_ds=10, index_limit=200) if mode == "combined" else ctx.render("full")


def test_teams_scope_names_the_chat_tools_and_never_the_file_tools():
    for mode in ("combined", "full"):
        out = _render(mode)
        assert 'kind="messages"' in out and 'id="conn-teams"' in out
        for tool in ("search_chat_messages", "read_chat_thread", "list_chat_messages"):
            assert tool in out
        teams_block = out[out.index('type="teams_messages"'):]
        teams_block = teams_block[: teams_block.index("</connection>")]
        assert "search_files" not in teams_block and "read_file" not in teams_block


def test_teams_scope_says_what_the_conversations_hold_and_that_empty_is_not_absent():
    out = _render("combined")
    assert "decisions" in out and "definitions" in out
    assert "does NOT mean no messages" in out
