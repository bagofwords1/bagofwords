"""Fix validation: the Outlook Mail connection test must probe the MAILBOX.

`/me` only proves the token maps to a directory user — it says nothing about
whether that user has an Exchange mailbox. A user without a mail license has a
perfectly valid identity, so the identity-only check reported a green
"Connected as <upn>" and every mail tool then failed at runtime with
`MailboxNotEnabledForRESTAPI`. Observed live against a real tenant: an
unlicensed demo user's connection test passed while `/me/messages` 404'd.

Run:
    cd backend && python -m pytest tests/unit/test_graph_mail_test_connection.py -v
"""
import pytest

from app.data_sources.clients.graph_mail_client import GraphMailClient


def _client(get_impl):
    client = GraphMailClient.__new__(GraphMailClient)  # skip network/auth setup
    client._get = get_impl
    return client


ME = {"userPrincipalName": "user@contoso.onmicrosoft.com", "displayName": "User"}
NO_MAILBOX = (
    'Graph https://graph.microsoft.com/v1.0/me/messages → 404 '
    '{"error":{"code":"MailboxNotEnabledForRESTAPI","message":"The mailbox is either '
    'inactive, soft-deleted, or is hosted on-premise."}}'
)


def test_success_when_mailbox_is_readable():
    calls = []

    def _get(path):
        calls.append(path)
        return ME if path.startswith("/me?") else {"value": [{"id": "1"}]}

    result = _client(_get).test_connection()
    assert result["success"] is True
    assert "user@contoso.onmicrosoft.com" in result["message"]
    assert any("/me/messages" in c for c in calls), "the mailbox itself must be probed"


def test_fails_with_actionable_message_when_user_has_no_mailbox():
    def _get(path):
        if path.startswith("/me?"):
            return ME
        raise ValueError(NO_MAILBOX)

    result = _client(_get).test_connection()
    assert result["success"] is False, "an identity without a mailbox is not a usable connection"
    msg = result["message"]
    assert "no Exchange mailbox" in msg
    assert "user@contoso.onmicrosoft.com" in msg, "name the identity that was signed in"
    assert "license" in msg.lower(), "point at the actual remedy"


def test_other_mailbox_errors_still_fail_but_keep_the_detail():
    def _get(path):
        if path.startswith("/me?"):
            return ME
        raise ValueError("Graph … → 403 {\"error\":{\"code\":\"accessDenied\"}}")

    result = _client(_get).test_connection()
    assert result["success"] is False
    assert "accessDenied" in result["message"]


def test_identity_failure_reported_as_before():
    def _get(path):
        raise ValueError("Graph … → 401 InvalidAuthenticationToken")

    result = _client(_get).test_connection()
    assert result["success"] is False
    assert "InvalidAuthenticationToken" in result["message"]
