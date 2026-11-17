"""A Slack connection must reach the planner as a chat source, like Teams.

The planner picks tools from the schema context: a Slack scope must name the
chat tools (never the file tools) and, when the connection is limited to
specific channels, say which ones so the agent doesn't hunt elsewhere.
"""
from app.ai.context.agent_roster import CHAT_CONNECTION_TYPES
from app.ai.context.sections.tables_schema_section import FileScopeItem, TablesSchemaContext
from app.ai.tools.implementations.chat_tools import _conversation_from_thread_text
from app.ai.tools.implementations.connection_catalog_common import TOKEN_SCOPED_TYPES
from app.schemas.data_source_schema import DataSourceSummarySchema


def _render(note=None) -> str:
    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="slack-agent", name="Slack", type="slack_messages"),
        tables=[],
        file_scopes=[FileScopeItem(
            connection_id="conn-slack", name="Slack", type="slack_messages",
            base=None, file_count=0, sample=[], index_mode="content",
            supports_search=True, per_user=True, enforces_scope=False, note=note,
        )],
    )
    return TablesSchemaContext(data_sources=[ds]).render_combined(top_k_per_ds=10, index_limit=200)


def _slack_block(out: str) -> str:
    block = out[out.index('type="slack_messages"'):]
    return block[: block.index("</connection>")]


def test_slack_scope_names_the_chat_tools_and_never_the_file_tools():
    block = _slack_block(_render())
    assert 'kind="messages"' in block
    for tool in ("search_chat_messages", "read_chat_thread", "list_chat_messages"):
        assert tool in block
    assert "search_files" not in block and "read_file" not in block
    assert "channels and DMs they belong to" in block
    assert "<index " not in block  # live: no catalog


def test_allowed_channels_note_reaches_the_planner():
    block = _slack_block(_render(note="This connection is limited to these Slack channels: #support."))
    assert "limited to these Slack channels: #support" in block


def test_slack_is_registered_as_a_chat_and_token_scoped_source():
    assert "slack_messages" in CHAT_CONNECTION_TYPES
    assert "slack_messages" in TOKEN_SCOPED_TYPES


def test_thread_card_is_titled_after_the_slack_conversation():
    assert _conversation_from_thread_text("Slack channel: #support\nThread started by…") == "#support"
    assert _conversation_from_thread_text("Teams channel: Sales › General") == "Sales › General"


def test_allowed_channels_are_formatted_for_the_note():
    from app.data_sources.clients.slack_messages_client import format_allowed_channels, parse_allowed_channels

    assert format_allowed_channels(parse_allowed_channels("#Support, C0SALES001")) == "#support, C0SALES001"


def test_message_rows_carry_their_link_and_the_tool_surfaces_hints():
    from app.ai.tools.implementations.chat_tools import SearchChatMessagesTool, _message_row
    from app.ai.tools.schemas.chat_tools import SearchChatMessagesInput

    row = _message_row({"id": "slack:C1/1.2", "sender": "Dana", "preview": "hi",
                        "web_url": "https://acme.slack.com/archives/C1/p12"})
    assert "link: https://acme.slack.com/archives/C1/p12" in row

    tool = SearchChatMessagesTool()
    data = SearchChatMessagesInput(connection_id="c", query="support")
    end = tool._end(data, {"items": [], "next_cursor": None, "hint": "'support' is the name of a conversation."})
    assert "is the name of a conversation" in end.payload["observation"]["summary"]

