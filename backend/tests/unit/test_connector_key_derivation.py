"""Report-embedded connections must resolve a provider `connector_key` so the UI
renders the brand icon (Monday, Notion, …) instead of the generic MCP glyph.

Regression: `ConnectionEmbedded.connector_key` derived the key only from
`config.catalog_key`, missing the `server_url`→preset fallback the connections
route (`_conn_connector_key`) uses — so a catalog MCP connection that recorded
only its `server_url` came back with `connector_key=None` in report responses,
and the tool-call rows showed the generic stack/MCP icon.
"""
from app.schemas.data_source_schema import ConnectionEmbedded, _connector_key_from_config


# Any still-registered MCP preset works as the example (Gmail's preset was
# removed once the native gmail_mail connector shipped).
MONDAY_SERVER_URL = "https://mcp.monday.com/mcp"


def test_connector_key_from_explicit_catalog_key():
    assert _connector_key_from_config({"catalog_key": "notion"}) == "notion"


def test_connector_key_from_server_url_preset_match():
    # No catalog_key, only server_url → matched against the MCP presets.
    assert _connector_key_from_config({"server_url": MONDAY_SERVER_URL}) == "monday"


def test_connector_key_accepts_json_string_config():
    assert _connector_key_from_config('{"server_url": "%s"}' % MONDAY_SERVER_URL) == "monday"


def test_connector_key_none_for_unknown_or_missing():
    assert _connector_key_from_config({"server_url": "https://example.com/mcp"}) is None
    assert _connector_key_from_config({}) is None
    assert _connector_key_from_config(None) is None


def test_connection_embedded_resolves_key_from_server_url():
    ce = ConnectionEmbedded(
        id="c1", name="Monday", type="mcp",
        config={"server_url": MONDAY_SERVER_URL},
    )
    assert ce.connector_key == "monday"


def test_connection_embedded_explicit_key_wins():
    ce = ConnectionEmbedded(
        id="c2", name="My Notion", type="mcp",
        config={"catalog_key": "notion", "server_url": MONDAY_SERVER_URL},
    )
    assert ce.connector_key == "notion"
