"""e2e tests for the sso_only "admin-only local login" break-glass.

When ``auth.mode = sso_only`` SSO is the source of truth for accounts.
The local username/password form is still exposed (hidden in the UI behind
``?local=true``) so that admins can sign in when SSO is misconfigured or
down — but ``UserManager.authenticate`` rejects non-admins, otherwise the
local route would be a bypass around SSO for regular users.
"""
import uuid

import pytest


def _login(test_client, email: str, password: str):
    return test_client.post(
        "/api/auth/jwt/login",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"username": email, "password": password},
    )


@pytest.mark.e2e
def test_admin_can_local_login_in_sso_only_mode(test_client, create_user, login_user, whoami):
    """The first user is the org admin and must still be able to log in locally."""
    from app.settings.config import settings as _bow_settings
    prev = _bow_settings.bow_config.auth.mode
    _bow_settings.bow_config.auth.mode = "sso_only"
    try:
        admin = create_user()  # first user → admin
        # Sanity: they hold the admin role
        admin_token = login_user(admin["email"], admin["password"])
        assert admin_token, "admin should be able to log in"
        info = whoami(admin_token)
        assert info["organizations"][0]["role"] == "admin"
    finally:
        _bow_settings.bow_config.auth.mode = prev


@pytest.mark.e2e
def test_non_admin_cannot_local_login_in_sso_only_mode(test_client, create_user, login_user, whoami):
    """A regular member must NOT be able to use the local form when SSO-only is on."""
    from app.settings.config import settings as _bow_settings

    # Set up admin + invited regular member while still in hybrid mode so the
    # registration flow works.
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    member_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id},
    )
    assert invite.status_code == 200, invite.json()
    member_password = "test123"
    create_user(email=member_email, password=member_password)

    # Sanity in hybrid: member can log in.
    pre = _login(test_client, member_email, member_password)
    assert pre.status_code == 200, pre.text

    # Now flip to sso_only and try again.
    prev = _bow_settings.bow_config.auth.mode
    _bow_settings.bow_config.auth.mode = "sso_only"
    try:
        denied = _login(test_client, member_email, member_password)
        assert denied.status_code in (400, 401, 403), denied.text

        # Admin still succeeds (break-glass works).
        ok = _login(test_client, admin["email"], admin["password"])
        assert ok.status_code == 200, ok.text
    finally:
        _bow_settings.bow_config.auth.mode = prev


@pytest.mark.e2e
def test_wrong_password_still_rejected_in_sso_only_mode(test_client, create_user, login_user):
    """The admin gate runs AFTER password verification — bad passwords stay rejected."""
    from app.settings.config import settings as _bow_settings
    admin = create_user()
    prev = _bow_settings.bow_config.auth.mode
    _bow_settings.bow_config.auth.mode = "sso_only"
    try:
        bad = _login(test_client, admin["email"], "wrong-password")
        assert bad.status_code in (400, 401, 403), bad.text
    finally:
        _bow_settings.bow_config.auth.mode = prev
