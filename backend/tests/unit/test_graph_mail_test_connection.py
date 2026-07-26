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


def _client(get_impl, user_token=True, token_impl=None):
    client = GraphMailClient.__new__(GraphMailClient)  # skip network/auth setup
    client._get = get_impl
    # Mirrors GraphDriveClient.__init__: True when a delegated user token was
    # supplied, False in admin/service-principal mode.
    client._user_token_provided = user_token
    client._token = token_impl or (lambda: "app-only-token")
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


# --- admin / service-principal mode (no user has signed in yet) --------------
#
# Second fix: the admin's "Test credentials" button runs with app-only client
# credentials. `/me` is delegated-only, so probing it there ALWAYS returned
# HTTP 400 `/me request is only valid with delegated authentication flow`, and
# the raw Graph body was rendered in the connection form as if the credentials
# were bad. Observed live against a real tenant while configuring Outlook Mail.

ME_DELEGATED_ONLY = (
    'Graph https://graph.microsoft.com/v1.0/me?$select=userPrincipalName,displayName → 400 '
    '{"error":{"code":"BadRequest","message":"/me request is only valid with delegated '
    'authentication flow.","innerError":{"date":"2026-07-26T03:10:29",'
    '"request-id":"3089e8a6-524d-49a7-a6aa-589326821ce9"}}}'
)


def test_admin_mode_does_not_probe_me_and_succeeds():
    calls = []

    def _get(path):
        calls.append(path)
        raise ValueError(ME_DELEGATED_ONLY)  # what Graph really does here

    result = _client(_get, user_token=False).test_connection()
    assert result["success"] is True, "verifying service-principal credentials is not a failure"
    assert calls == [], "no /me* call may be made without a delegated token"
    assert "sign in" in result["message"].lower(), "tell the admin what happens next"


def test_admin_mode_surfaces_bad_service_principal_credentials():
    def _token():
        raise ValueError("AADSTS7000215: Invalid client secret provided")

    result = _client(lambda path: None, user_token=False, token_impl=_token).test_connection()
    assert result["success"] is False, "credentials that cannot mint a token must fail"
    assert "AADSTS7000215" in result["message"]


def test_admin_mode_never_leaks_the_raw_graph_body():
    def _get(path):
        raise ValueError(ME_DELEGATED_ONLY)

    msg = _client(_get, user_token=False).test_connection()["message"]
    for leak in ("graph.microsoft.com", "BadRequest", "request-id", "innerError"):
        assert leak not in msg, f"raw Graph detail {leak!r} must not reach the admin"


def test_delegated_mode_still_probes_the_mailbox_after_the_admin_branch():
    """The admin branch must not short-circuit the delegated path (fix 1)."""
    calls = []

    def _get(path):
        calls.append(path)
        return ME if path.startswith("/me?") else {"value": [{"id": "1"}]}

    result = _client(_get, user_token=True).test_connection()
    assert result["success"] is True
    assert any("/me/messages" in c for c in calls)


# --- the same guard is needed on the inventory path -------------------------
#
# `POST /connections/test-params` (the "Test credentials" button) does not stop
# at test_connection: it then counts the inventory via
# _acount_files_for_validation -> client.list_files(). That is another `/me/*`
# call, so guarding only test_connection moved the raw 400 from
# `/me?$select=...` to `/me/messages?$top=25...` instead of removing it.
# GraphDriveClient already returns [] for OneDrive in exactly this situation.


def test_list_files_returns_empty_without_a_delegated_token():
    def _get(path):
        raise AssertionError(f"no /me* call may be made app-only, got {path}")

    assert _client(_get, user_token=False).list_files() == []


def test_list_files_still_reads_the_mailbox_with_a_delegated_token():
    calls = []

    def _get(path):
        calls.append(path)
        return {"value": [{"id": "1", "subject": "Hello", "receivedDateTime": "2026-07-26T00:00:00Z"}]}

    items = _client(_get, user_token=True).list_files()
    assert len(items) == 1 and items[0]["name"] == "Hello"
    assert any("/me/messages" in c for c in calls)
