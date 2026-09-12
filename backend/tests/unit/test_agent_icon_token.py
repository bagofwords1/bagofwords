"""One agent, one icon — on every payload that names it.

The bug these cover: an agent's icon used to be recomputed by each surface that
drew one, from a different pair of fields. ``type`` came from ``connections[0]``
while ``connector_key`` scanned for the first connection resolving a brand, so an
agent wired to ``[postgresql, notion-mcp]`` showed notion's logo in the agents
explorer and postgres' in the data-tool ticker.
"""

import datetime as dt

import pytest

from app.schemas.agent_icon import resolve_agent_icon_token
from app.schemas.completion_v2_schema import ToolExecutionDataSourceSchema
from app.schemas.data_source_schema import (
    DataSourceListItemSchema,
    DataSourceMinimalSchema,
    DataSourceReportSchema,
)

NOW = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

PG = {"id": "c1", "name": "warehouse", "type": "postgresql"}
NOTION = {"id": "c2", "name": "notion", "type": "mcp", "config": {"catalog_key": "notion"}}


@pytest.mark.parametrize(
    "icon, connections, expected",
    [
        # A single ordinary connection: the connection's type.
        (None, [PG], "type:postgresql"),
        # The reported bug. A brand anywhere in the list wins, whichever position
        # it is in — so both orderings agree, and so does every surface.
        (None, [PG, NOTION], "type:notion"),
        (None, [NOTION, PG], "type:notion"),
        # An explicit override outranks anything derived.
        ("emoji:\U0001f4ca", [PG, NOTION], "emoji:\U0001f4ca"),
        ("type:snowflake", [PG, NOTION], "type:snowflake"),
        # 'preset:' is reserved but not yet renderable, and a malformed token is
        # not a reason to show a broken image: both fall through to the derived
        # icon (mirrors the frontend's parseAgentIcon).
        ("preset:pretty", [PG], "type:postgresql"),
        ("nonsense", [PG], "type:postgresql"),
        ("emoji:", [PG], "type:postgresql"),
        # Nothing to draw.
        (None, [], None),
        (None, None, None),
    ],
)
def test_resolve_agent_icon_token(icon, connections, expected):
    assert resolve_agent_icon_token(icon, connections) == expected


def test_connector_key_derived_from_server_url_not_only_catalog_key():
    """A catalog connection records its server_url and not always a catalog_key."""
    conn = {"type": "mcp", "config": {"server_url": "https://mcp.notion.com/mcp"}}
    assert resolve_agent_icon_token(None, [conn]) == "type:notion"


def test_already_derived_connector_key_is_preferred_over_config():
    """Embedded connection schemas have derived connector_key themselves; the
    resolver must use it rather than re-parsing a config it may not carry."""
    conn = {"type": "mcp", "connector_key": "linear", "config": None}
    assert resolve_agent_icon_token(None, [conn]) == "type:linear"


def _list_item(**kw):
    return DataSourceListItemSchema(
        id=kw.pop("id", "a1"),
        name=kw.pop("name", "FKA"),
        description=None,
        status="active",
        created_at=NOW,
        **kw,
    )


def _report_item(**kw):
    return DataSourceReportSchema(
        id=kw.pop("id", "a1"),
        name=kw.pop("name", "FKA"),
        organization_id="o1",
        created_at=NOW,
        updated_at=NOW,
        description=None,
        summary=None,
        is_active=True,
        **kw,
    )


@pytest.mark.parametrize("connections, expected", [
    ([PG], "type:postgresql"),
    ([PG, NOTION], "type:notion"),
    ([], None),
])
def test_every_agent_facing_schema_agrees(connections, expected):
    """The agents list (explorer, pickers), the report payload (agent panel) and
    the instruction/entity chip shape must not disagree about one agent."""
    tokens = {
        "list": _list_item(connections=connections).icon_token,
        "report": _report_item(connections=connections).icon_token,
        "minimal": DataSourceMinimalSchema(
            id="a1", name="FKA", connections=connections
        ).icon_token,
        # What the data tools receive, resolved from the same helper.
        "tool_execution": ToolExecutionDataSourceSchema(
            id="a1",
            name="FKA",
            type=(connections[0]["type"] if connections else None),
            icon_token=resolve_agent_icon_token(None, connections),
        ).icon_token,
    }
    assert set(tokens.values()) == {expected}, tokens


def test_legacy_type_field_does_not_change_the_token():
    """``type`` is the first connection's; the token may legitimately differ from
    it (a brand later in the list). Both stay on the payload, and the client must
    render the token — so the token must not be contaminated by ``type``."""
    item = _list_item(connections=[PG, NOTION], type="postgresql", connector_key="notion")
    assert item.type == "postgresql"
    assert item.icon_token == "type:notion"


def test_minimal_schema_does_not_serialize_its_input_only_connections():
    """``connections`` exists on the chip shape only so the token can be resolved;
    leaking it would widen a deliberately slim payload."""
    dumped = DataSourceMinimalSchema(id="a1", name="FKA", connections=[PG, NOTION]).model_dump()
    assert dumped["icon_token"] == "type:notion"
    assert "connections" not in dumped


def test_explicit_icon_token_is_not_overwritten():
    """Call sites that resolved the token themselves (raw SQL paths) pass it in."""
    item = _list_item(connections=[PG], icon_token="type:already_set")
    assert item.icon_token == "type:already_set"
