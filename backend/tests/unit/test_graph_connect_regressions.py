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
