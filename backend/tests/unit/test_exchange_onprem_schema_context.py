"""An Exchange Server connection must reach the planner in mail vocabulary,
and say whose mail it is.

The same tools back a personal mailbox read with each user's own sign-in and a
team mailbox read through one service account. A live run showed the planner
calling a shared support mailbox "your Inbox" when nothing told it otherwise.
"""
import pytest

from app.ai.context.sections.tables_schema_section import FileScopeItem, TablesSchemaContext
from app.data_sources.clients.exchange_onprem_client import planner_scope_note
from app.schemas.data_source_schema import DataSourceSummarySchema

SHARED = "support@example.com, sales@example.com"


def _render(mode: str, note: str) -> str:
    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="mail-agent", name="Team mail", type="exchange_onprem"),
        tables=[],
        file_scopes=[FileScopeItem(
            connection_id="conn-mail", name="Team mail", type="exchange_onprem",
            base="https://mail.example.com", file_count=0, sample=[], index_mode="content",
            supports_search=True, enforces_scope=False, note=note,
        )],
    )
    ctx = TablesSchemaContext(data_sources=[ds])
    out = ctx.render_combined(top_k_per_ds=10, index_limit=200) if mode == "combined" else ctx.render("full")
    block = out[out.index('type="exchange_onprem"'):]
    return block[: block.index("</connection>")]


@pytest.mark.parametrize("mode", ["combined", "full"])
def test_scope_names_the_mail_tools_and_never_the_file_tools(mode):
    block = _render(mode, planner_scope_note({"shared_mailboxes": SHARED}, per_user=False))
    assert 'kind="mailbox"' in block and 'id="conn-mail"' in block
    for tool in ("search_email", "list_emails", "read_email"):
        assert tool in block
    for wrong in ("search_files", "read_file", "list_files"):
        assert wrong not in block
    assert "<index " not in block, "a live mailbox has no index to describe"


@pytest.mark.parametrize("config", [
    {"shared_mailboxes": SHARED},
    {"shared_mailboxes": "support@example.com", "include_own_mailbox": False},
    {},
])
def test_a_service_account_connection_is_never_presented_as_the_users_own_mail(config):
    note = planner_scope_note(config, per_user=False)
    assert "service account" in note and "NOT the asking user's personal mailbox" in note
    for address in [a.strip() for a in (config.get("shared_mailboxes") or "").split(",") if a.strip()]:
        assert address in note
    if config.get("include_own_mailbox") is False:
        assert "default mailbox is support@example.com" in note
    assert "does NOT mean no mail" in _render("combined", note)


@pytest.mark.parametrize("config", [{}, {"shared_mailboxes": SHARED}])
def test_a_per_user_connection_says_the_default_mailbox_is_the_users_own(config):
    note = planner_scope_note(config, per_user=True)
    assert "own Windows sign-in" in note and "asking user's own mailbox" in note
    assert "service account" not in note
    if config:
        assert "only sees the ones their account may open" in note


def test_a_broken_shared_mailbox_setting_cannot_break_the_prompt():
    assert planner_scope_note({"shared_mailboxes": "not an address"}, per_user=False)
    assert planner_scope_note(None, per_user=True)
