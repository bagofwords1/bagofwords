"""Regression tests for the Microsoft Graph connector connect path.

Two defects made the OneDrive / SharePoint / Outlook Mail connect flow unusable:

  A. ``ConnectionService._resolve_client_by_type`` narrowed constructor kwargs to
     the client's ``inspect.signature`` params. The Graph clients are thin
     ``__init__(self, **kwargs)`` subclasses, so narrowing stripped every
     credential and the pre-save "Test credentials" always failed with
     "No access_token and no service-principal credentials configured".

  B. ``connection_oauth_service.get_oauth_params`` didn't handle ``outlook_mail``
     at all, so "Sign in with Microsoft" returned
     "OAuth not supported for connection type: outlook_mail".

These assert the general invariants (every ``**kwargs`` Graph client keeps its
credentials; every OAuth-capable Entra connector resolves params with a Graph
scope), not just the one reported scenario.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# --------------------------------------------------------------------------- A

# Every registry client whose constructor swallows **kwargs and forwards to a
# parent that actually consumes the credentials.
VAR_KWARGS_GRAPH_TYPES = ["onedrive", "sharepoint", "outlook_mail"]


@pytest.mark.parametrize("ds_type", VAR_KWARGS_GRAPH_TYPES)
def test_resolve_client_by_type_keeps_credentials_for_var_kwargs_clients(ds_type):
    """The pre-save test path must forward tenant/client/secret to the client.

    Before the fix these were stripped by signature narrowing, so the client
    was built with no auth and raised "No access_token and no service-principal
    credentials configured".
    """
    from app.services.connection_service import ConnectionService

    creds = {
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "22222222-2222-2222-2222-222222222222",
        "client_secret": "shhh-secret",
    }
    client = ConnectionService()._resolve_client_by_type(
        data_source_type=ds_type, config={}, credentials=creds
    )
    assert client.tenant_id == creds["tenant_id"]
    assert client.client_id == creds["client_id"]
    assert client.client_secret == creds["client_secret"]


# --------------------------------------------------------------------------- B

# Connectors that expose per-user Microsoft OAuth ("Sign in with Microsoft").
ENTRA_OAUTH_TYPES = ["onedrive", "sharepoint", "outlook_mail", "powerbi", "ms_fabric"]


def _fake_connection(conn_type: str):
    conn = MagicMock()
    conn.id = "conn-1"
    conn.type = conn_type
    conn.decrypt_credentials.return_value = {
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "22222222-2222-2222-2222-222222222222",
        "client_secret": "shhh-secret",
    }
    return conn


@pytest.mark.parametrize("conn_type", ENTRA_OAUTH_TYPES)
def test_get_oauth_params_resolves_for_entra_connectors(conn_type):
    """Every Entra OAuth connector must resolve params without raising."""
    from app.services.connection_oauth_service import get_oauth_params

    params = get_oauth_params(_fake_connection(conn_type))
    assert params["provider_name"] == "microsoft"
    assert "login.microsoftonline.com" in params["authorize_url"]
    assert params["scopes"]  # non-empty


def test_outlook_mail_oauth_requests_mail_read_scope():
    """Outlook's delegated scope must include Mail.Read so the token can read
    and $search the signed-in user's messages."""
    from app.services.connection_oauth_service import get_oauth_params

    params = get_oauth_params(_fake_connection("outlook_mail"))
    assert "Mail.Read" in params["scopes"]


def test_outlook_mail_is_obo_provisionable():
    """Entra-login OBO auto-provisioning must cover outlook_mail too."""
    from app.services.connection_oauth_service import (
        ENTRA_OBO_CONNECTION_TYPES,
        _OBO_SCOPES,
    )

    assert "outlook_mail" in ENTRA_OBO_CONNECTION_TYPES
    assert "outlook_mail" in _OBO_SCOPES
    assert "graph.microsoft.com" in _OBO_SCOPES["outlook_mail"]


# --------------------------------------------------------------------------- C
#
# C. A file tool addressed by the connection's *name* on a multi-connection agent
#    was rejected as "not a file source attached to this agent", which the model
#    relayed to the user as "SharePoint is disconnected". The resolver must accept
#    the connection name (not just its id or the data_source name), and a genuine
#    mismatch must read as an invalid SELECTION — never as a disconnection.

from unittest.mock import AsyncMock, patch  # noqa: E402
import types  # noqa: E402


def _fc_conn(cid, name, typ="sharepoint"):
    c = MagicMock()
    c.id, c.name, c.type, c.is_active = cid, name, typ, True
    return c


def _fc_ds(did, name, conns):
    d = MagicMock()
    d.id, d.name, d.is_active, d.connections = did, name, True, conns
    return d


def _two_sharepoint_agent_ctx():
    """An agent (report) with TWO SharePoint connections, so the sole-connection
    fallback does not apply and the identifier must actually match."""
    ca = _fc_conn("06697b27-20f8-4b69-bac5-386d5c461513", "Employees SharePoint")
    cb = _fc_conn("11111111-1111-1111-1111-111111111111", "Finance SharePoint")
    report = MagicMock()
    report.data_sources = [_fc_ds("aaaa", "Employees", [ca]), _fc_ds("bbbb", "Finance", [cb])]
    return {"db": MagicMock(), "organization": MagicMock(), "report": report, "user": None}, ca


@pytest.mark.asyncio
async def test_resolve_file_client_accepts_connection_name():
    """Addressing a file source by its connection name resolves to that
    connection (regression: multi-connection agent used to reject the name)."""
    from app.ai.tools.implementations import _file_tool_common as fc
    from app.data_sources.clients.base import Capability

    ctx, ca = _two_sharepoint_agent_ctx()
    fake_client = types.SimpleNamespace(capabilities={Capability.READ_FILE})
    with patch(
        "app.services.connection_service.ConnectionService.construct_client",
        new=AsyncMock(return_value=fake_client),
    ):
        client, err = await fc.resolve_file_client(ctx, "Employees SharePoint", Capability.READ_FILE)
    assert err is None
    assert str(client._bow_connection.id) == str(ca.id)


@pytest.mark.asyncio
async def test_resolve_file_client_bad_selection_is_not_a_disconnection():
    """A genuinely unknown identifier reads as an invalid selection (with the
    valid choices), NOT as a disconnection — so the model retries instead of
    telling the user to reconnect a source that is still connected."""
    from app.ai.tools.implementations import _file_tool_common as fc
    from app.data_sources.clients.base import Capability

    ctx, _ = _two_sharepoint_agent_ctx()
    client, err = await fc.resolve_file_client(ctx, "Totally Bogus", Capability.READ_FILE)
    assert client is None
    assert "Invalid file-source selection" in err
    assert "NOT a disconnection" in err
    # Names AND ids are offered so the model can self-correct.
    assert "Employees SharePoint" in err and "06697b27-20f8-4b69-bac5-386d5c461513" in err


@pytest.mark.asyncio
async def test_resolve_file_data_source_accepts_names():
    """list_files' resolver accepts the connection name and the data_source name
    (previously id-only), with the same non-alarming error on a real mismatch."""
    from app.ai.tools.implementations import _file_tool_common as fc

    ctx, ca = _two_sharepoint_agent_ctx()
    ds1, e1 = await fc.resolve_file_data_source(ctx, "Employees SharePoint")  # connection name
    ds2, e2 = await fc.resolve_file_data_source(ctx, "Finance")               # data_source name
    ds3, e3 = await fc.resolve_file_data_source(ctx, "06697b27-20f8-4b69-bac5-386d5c461513")  # id
    assert e1 is None and ds1.name == "Employees"
    assert e2 is None and ds2.name == "Finance"
    assert e3 is None and ds3.name == "Employees"
    _, e4 = await fc.resolve_file_data_source(ctx, "nope")
    assert "Invalid file-source selection" in e4 and "NOT a disconnection" in e4
