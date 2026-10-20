"""e2e: OAuth callback failures redirect with a fixed code, never reflected text.

The callback used to put the provider's ``error_description`` (or an exception
message) into ``/users/sign-in?error=<text>``, which the page rendered with
``v-html``. The provider-error branch runs before any state check, so anyone
could craft a callback link that put script on the sign-in page.
"""
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.routes import auth as auth_routes

pytestmark = pytest.mark.e2e

ALLOWED_CODES = {"invitation_required", "oauth_email_missing", "oauth_login_failed"}
PAYLOADS = ['<img src=x onerror=alert(1)>', '"><script>alert(1)</script>', "Call +1-555-0100 to unlock"]


@pytest.fixture
def sso_client():
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api")
    return TestClient(app)


def _assert_fixed_code_redirect(resp, payload: str):
    assert resp.status_code == 303
    location = resp.headers["location"]
    parsed = urlparse(location)
    assert parsed.path == "/users/sign-in"
    params = parse_qs(parsed.query)
    assert set(params) == {"error_code"}
    assert params["error_code"][0] in ALLOWED_CODES
    for fragment in (payload, payload.split()[0], "onerror", "script"):
        assert fragment not in location


@pytest.mark.parametrize("provider", ["google", "some-oidc"])
@pytest.mark.parametrize("payload", PAYLOADS)
def test_provider_error_is_not_reflected(sso_client, provider, payload):
    resp = sso_client.get(
        f"/api/auth/{provider}/callback",
        params={"error": "access_denied", "error_description": payload},
        follow_redirects=False,
    )
    _assert_fixed_code_redirect(resp, payload)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_failed_callback_does_not_reflect_request_input(sso_client, payload):
    # No provider error: the flow itself fails (bad state / unknown provider).
    resp = sso_client.get(
        f"/api/auth/google/callback",
        params={"code": payload, "state": payload},
        follow_redirects=False,
    )
    _assert_fixed_code_redirect(resp, payload)
