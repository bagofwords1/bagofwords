"""Per-agent Slack channel scope: the union rule and how it reaches the client.

An agent may narrow the shared Slack connection to its channels (and choose
whether it reads the user's DMs). A report with several agents on the same
connection reads the union of their choices — like attaching several agents
to a report widens what it can read.
"""
from types import SimpleNamespace

import pytest

from app.data_sources.clients.base import Capability
from app.services.slack_channel_scope import (
    client_overrides,
    combine_scopes,
    describe_scope,
    normalize_scope,
)

SUPPORT = {"id": "C0SUPPORT1", "name": "support"}
SALES = {"id": "C0SALES001", "name": "sales"}


def test_no_choice_means_every_channel_and_dms():
    assert normalize_scope(None) == {"allowed_channels": [], "include_direct_messages": True}
    assert describe_scope(normalize_scope(None)) is None
    assert client_overrides(normalize_scope(None)) == {"include_direct_messages": True}


def test_normalize_drops_junk_and_duplicates():
    scope = normalize_scope({"allowed_channels": [SUPPORT, {"id": ""}, "C0SALES001", SUPPORT, {"id": "C0X", "name": "#ops"}],
                             "include_direct_messages": False})
    assert scope == {"allowed_channels": [SUPPORT, {"id": "C0SALES001", "name": ""}, {"id": "C0X", "name": "ops"}],
                     "include_direct_messages": False}


def test_union_of_two_scoped_agents():
    combined = combine_scopes([
        {"allowed_channels": [SUPPORT], "include_direct_messages": False},
        {"allowed_channels": [SALES, SUPPORT], "include_direct_messages": False},
    ])
    assert combined == {"allowed_channels": [SUPPORT, SALES], "include_direct_messages": False}
    assert client_overrides(combined) == {"allowed_channels": ["C0SUPPORT1", "C0SALES001"],
                                          "include_direct_messages": False}


def test_an_unscoped_agent_makes_the_union_unrestricted():
    combined = combine_scopes([{"allowed_channels": [SUPPORT], "include_direct_messages": False}, None])
    assert combined["allowed_channels"] == [] and combined["include_direct_messages"] is True


def test_dms_are_in_when_any_agent_includes_them():
    combined = combine_scopes([{"allowed_channels": [SUPPORT], "include_direct_messages": False},
                               {"allowed_channels": [SALES], "include_direct_messages": True}])
    assert combined["include_direct_messages"] is True and len(combined["allowed_channels"]) == 2


def test_the_planner_note_names_the_channels():
    assert describe_scope({"allowed_channels": [SUPPORT, SALES], "include_direct_messages": False}) == (
        "This agent reads only these Slack channels: #support, #sales. Other channels are not readable here.")
    assert "and the user's DMs" in describe_scope({"allowed_channels": [SUPPORT], "include_direct_messages": True})
    assert describe_scope({"allowed_channels": [], "include_direct_messages": False}) == (
        "This agent reads the user's Slack channels but not their DMs.")


# ------------------------------------------------- wiring into the chat tools


@pytest.mark.asyncio
async def test_chat_tools_build_the_slack_client_with_the_union_of_the_reports_agents(monkeypatch):
    from app.ai.tools.implementations import _file_tool_common as common
    from app.data_sources.clients.slack_messages_client import SlackMessagesClient
    from app.services import slack_channel_scope
    from app.services.connection_service import ConnectionService

    slack = SimpleNamespace(id="conn-slack", type="slack_messages", name="Slack", is_active=True)
    support_agent = SimpleNamespace(id="ds-support", name="Support", is_active=True, connections=[slack])
    sales_agent = SimpleNamespace(id="ds-sales", name="Sales", is_active=True, connections=[slack])
    report = SimpleNamespace(data_sources=[support_agent, sales_agent])
    runtime_ctx = {"db": object(), "organization": SimpleNamespace(id="org"), "report": report, "user": None}

    asked = {}

    async def fake_agents_on_connection(db, connection_id):
        return ["ds-support", "ds-sales", "ds-pinned", "ds-joined"]

    async def fake_scope_for_agents(db, connection_id, ds_ids):
        asked["agents"] = list(ds_ids)
        return {"allowed_channels": [SUPPORT, SALES], "include_direct_messages": False}

    built = {}

    async def fake_construct(self, db, connection, current_user=None, config_overrides=None, **kw):
        built["overrides"] = config_overrides
        return SlackMessagesClient(access_token="xoxp-user", **(config_overrides or {}))

    monkeypatch.setattr(slack_channel_scope, "scope_for_agents", fake_scope_for_agents)
    monkeypatch.setattr(slack_channel_scope, "agents_on_connection", fake_agents_on_connection)
    monkeypatch.setattr(ConnectionService, "construct_client", fake_construct)

    client, err = await common.resolve_file_client(runtime_ctx, "conn-slack", Capability.LIST_CHAT_MESSAGES)
    assert err is None
    assert asked["agents"] == ["ds-support", "ds-sales"]  # every agent in the report on this connection
    assert built["overrides"] == {"allowed_channels": ["C0SUPPORT1", "C0SALES001"], "include_direct_messages": False}
    assert client.allowed == ["C0SUPPORT1", "C0SALES001"] and client.include_dms is False


@pytest.mark.asyncio
async def test_an_explicitly_chosen_agent_wins_over_a_connector_that_joined_on_its_own(monkeypatch):
    """The report pins "Support (#support only)"; the org's unscoped "Slack"
    connector on the SAME connection joins every run by itself (a personal
    connector). It must not reopen the scope the user picked."""
    from app.ai.tools.implementations import _file_tool_common as common
    from app.data_sources.clients.slack_messages_client import SlackMessagesClient
    from app.services import slack_channel_scope
    from app.services.connection_service import ConnectionService

    conn = SimpleNamespace(id="conn-slack", type="slack_messages", name="Slack", is_active=True)
    pinned = SimpleNamespace(id="ds-pinned", name="slack", is_active=True, connections=[conn])
    joined = SimpleNamespace(id="ds-joined", name="Slack", is_active=True, connections=[conn])
    report = SimpleNamespace(data_sources=[pinned])
    runtime_ctx = {"db": object(), "organization": SimpleNamespace(id="org"), "report": report, "user": None,
                   "run_agents": [pinned, joined]}

    asked = {}

    async def fake_agents_on_connection(db, connection_id):
        return ["ds-support", "ds-sales", "ds-pinned", "ds-joined"]

    async def fake_scope_for_agents(db, connection_id, ds_ids):
        asked["agents"] = list(ds_ids)
        return {"allowed_channels": [SUPPORT], "include_direct_messages": False}

    async def fake_construct(self, db, connection, current_user=None, config_overrides=None, **kw):
        return SlackMessagesClient(access_token="xoxp-user", **(config_overrides or {}))

    monkeypatch.setattr(slack_channel_scope, "scope_for_agents", fake_scope_for_agents)
    monkeypatch.setattr(slack_channel_scope, "agents_on_connection", fake_agents_on_connection)
    monkeypatch.setattr(ConnectionService, "construct_client", fake_construct)

    client, err = await common.resolve_file_client(runtime_ctx, "conn-slack", Capability.LIST_CHAT_MESSAGES)
    assert err is None and asked["agents"] == ["ds-pinned"]
    assert client.allowed == ["C0SUPPORT1"]

    # Auto (nothing pinned): every agent on the connection counts.
    runtime_ctx["report"] = SimpleNamespace(data_sources=[])
    await common.resolve_file_client(runtime_ctx, "conn-slack", Capability.LIST_CHAT_MESSAGES)
    assert asked["agents"] == ["ds-pinned", "ds-joined"]



@pytest.mark.asyncio
async def test_a_call_with_no_report_gets_the_scope_of_every_agent_on_the_connection(monkeypatch):
    """A standalone tool call (no report) must not read wider than the agents
    on the connection allow — it gets their union."""
    from app.ai.tools.implementations import _file_tool_common as common
    from app.data_sources.clients.slack_messages_client import SlackMessagesClient
    from app.services import slack_channel_scope
    from app.services.connection_service import ConnectionService

    slack = SimpleNamespace(id="conn-slack", type="slack_messages", name="Slack", is_active=True)

    class _Result:
        def scalar_one_or_none(self):
            return slack

    class _DB:
        async def execute(self, *a, **k):
            return _Result()

    asked = {}

    async def fake_agents_on_connection(db, connection_id):
        return ["ds-a", "ds-b"]

    async def fake_scope_for_agents(db, connection_id, ds_ids):
        asked["agents"] = list(ds_ids)
        return {"allowed_channels": [SUPPORT], "include_direct_messages": False}

    async def fake_construct(self, db, connection, current_user=None, config_overrides=None, **kw):
        return SlackMessagesClient(access_token="xoxp-user", **(config_overrides or {}))

    monkeypatch.setattr(slack_channel_scope, "agents_on_connection", fake_agents_on_connection)
    monkeypatch.setattr(slack_channel_scope, "scope_for_agents", fake_scope_for_agents)
    monkeypatch.setattr(ConnectionService, "construct_client", fake_construct)

    runtime_ctx = {"db": _DB(), "organization": SimpleNamespace(id="org"), "report": None, "user": None}
    client, err = await common.resolve_file_client(runtime_ctx, "conn-slack", Capability.LIST_CHAT_MESSAGES)
    assert err is None and asked["agents"] == ["ds-a", "ds-b"]
    assert client.allowed == ["C0SUPPORT1"] and client.include_dms is False


@pytest.mark.asyncio
async def test_run_scope_picks_pinned_agents_then_falls_back_to_the_connections_agents(monkeypatch):
    from app.services import slack_channel_scope as scope

    async def on_conn(db, cid):
        return ["a", "b", "c"]

    chosen = {}

    async def scopes(db, cid, ids):
        chosen["ids"] = list(ids)
        return normalize_scope(None)

    monkeypatch.setattr(scope, "agents_on_connection", on_conn)
    monkeypatch.setattr(scope, "scope_for_agents", scopes)
    await scope.run_scope(None, "c1", ["a", "b", "x"], pinned_ids=["b"])
    assert chosen["ids"] == ["b"]                 # pinned wins
    await scope.run_scope(None, "c1", ["a", "b"], pinned_ids=[])
    assert chosen["ids"] == ["a", "b"]            # union of the run's agents on the connection
    await scope.run_scope(None, "c1", None)
    assert chosen["ids"] == ["a", "b", "c"]       # no run context: every agent on the connection
    await scope.run_scope(None, "c1", ["x"])
    assert chosen["ids"] == ["a", "b", "c"]       # none of the run's agents carry it: same


@pytest.mark.asyncio
async def test_a_file_reference_is_materialized_with_the_reports_scope(monkeypatch):
    """A Slack message attached as a file reference is read into the report
    with the report's agents' scope: an out-of-scope message is refused."""
    from app.services import file_reference_service, slack_channel_scope
    from app.services.connection_service import ConnectionService
    from app.data_sources.clients.slack_messages_client import SlackMessagesClient

    conn = SimpleNamespace(id="conn-slack", type="slack_messages")

    class _DB:
        async def get(self, model, key):
            return conn

    async def fake_run_scope(db, connection_id, run_ids, pinned):
        assert run_ids == ["ds-support"] and pinned == ["ds-support"]
        return {"allowed_channels": [SUPPORT], "include_direct_messages": False}

    built = {}

    async def fake_construct(self, db, connection, current_user=None, config_overrides=None, **kw):
        built["overrides"] = config_overrides
        client = SlackMessagesClient(access_token="xoxp-user", **(config_overrides or {}))
        client._conversations = [{"id": "slack:C0SUPPORT1", "channel_id": "C0SUPPORT1", "name": "#support",
                                  "channel": "support", "kind": "channel"}]
        return client

    monkeypatch.setattr(slack_channel_scope, "run_scope", fake_run_scope)
    monkeypatch.setattr(ConnectionService, "construct_client", fake_construct)

    report = SimpleNamespace(data_sources=[SimpleNamespace(id="ds-support")])
    ref = SimpleNamespace(connection_id="conn-slack", external_file_id="slack:C0SALES001/1700000100.000100",
                          name="msg", mime_type=None)
    with pytest.raises(ValueError, match="this agent reads"):
        await file_reference_service.ensure_materialized(_DB(), ref, None, report, None)
    assert built["overrides"] == {"allowed_channels": ["C0SUPPORT1"], "include_direct_messages": False}
