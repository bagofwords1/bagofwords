"""End-to-end tests for OAuth 2.1 Authorization Server (MCP + Claude Web)."""

import hashlib
import base64
import secrets

import pytest


def _pkce_pair():
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ============================================================================
# Well-known metadata endpoints
# ============================================================================

@pytest.mark.e2e
def test_well_known_protected_resource(test_client, create_user, login_user):
    """/.well-known/oauth-protected-resource returns correct metadata."""
    create_user()
    response = test_client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200

    data = response.json()
    assert "resource" in data
    assert data["resource"].endswith("/api/mcp")
    assert "authorization_servers" in data
    assert len(data["authorization_servers"]) >= 1
    assert "scopes_supported" in data
    assert "mcp" in data["scopes_supported"]


@pytest.mark.e2e
def test_well_known_authorization_server(test_client, create_user, login_user):
    """/.well-known/oauth-authorization-server returns correct metadata."""
    create_user()
    response = test_client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200

    data = response.json()
    assert "issuer" in data
    assert "authorization_endpoint" in data
    assert data["authorization_endpoint"].endswith("/authorize")
    assert "token_endpoint" in data
    assert data["response_types_supported"] == ["code"]
    assert "authorization_code" in data["grant_types_supported"]
    assert "refresh_token" in data["grant_types_supported"]
    assert data["code_challenge_methods_supported"] == ["S256"]
    assert "mcp" in data["scopes_supported"]


# ============================================================================
# OAuth client CRUD
# ============================================================================

@pytest.mark.e2e
def test_create_oauth_client(
    create_oauth_client,
    create_user,
    login_user,
    whoami,
):
    """Create an OAuth client and verify response fields."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    client = create_oauth_client(user_token=token, org_id=org_id, name="Claude Web")

    assert "client_id" in client
    assert client["client_id"].startswith("bow_client_")
    assert "client_secret" in client
    assert client["client_secret"].startswith("bow_secret_")
    assert client["name"] == "Claude Web"
    assert "redirect_uris" in client
    assert len(client["redirect_uris"]) > 0


@pytest.mark.e2e
def test_list_oauth_clients(
    create_oauth_client,
    list_oauth_clients,
    create_user,
    login_user,
    whoami,
):
    """List OAuth clients returns created clients (without secrets)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    create_oauth_client(user_token=token, org_id=org_id)
    clients = list_oauth_clients(user_token=token, org_id=org_id)

    assert len(clients) >= 1
    assert "client_id" in clients[0]
    assert "client_secret" not in clients[0]  # Secret should not be in list


@pytest.mark.e2e
def test_rotate_oauth_secret(
    create_oauth_client,
    rotate_oauth_secret,
    create_user,
    login_user,
    whoami,
):
    """Rotating secret returns a new secret and the old one stops working."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    client = create_oauth_client(user_token=token, org_id=org_id)
    old_secret = client["client_secret"]

    rotated = rotate_oauth_secret(
        client_db_id=client["id"],
        user_token=token,
        org_id=org_id,
    )
    new_secret = rotated["client_secret"]

    assert new_secret != old_secret
    assert new_secret.startswith("bow_secret_")


@pytest.mark.e2e
def test_get_client_info_public(
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Public client info endpoint returns name only."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    client = create_oauth_client(user_token=token, org_id=org_id, name="My App")

    # No auth needed
    response = test_client.get(f"/api/oauth/clients/{client['client_id']}/info")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "My App"
    assert "client_secret" not in data


# ============================================================================
# Full OAuth Authorization Code + PKCE flow
# ============================================================================

@pytest.mark.e2e
def test_full_oauth_flow(
    enable_mcp,
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Complete OAuth flow: create client → authorize → token → MCP request."""
    # Setup
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    enable_mcp(user_token=token, org_id=org_id)

    # 1. Create OAuth client
    client = create_oauth_client(user_token=token, org_id=org_id)
    client_id = client["client_id"]
    client_secret = client["client_secret"]
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"

    # 2. Generate PKCE pair
    code_verifier, code_challenge = _pkce_pair()

    # 3. Simulate user approving the consent (POST /api/oauth/authorize)
    authorize_response = test_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "test_state_123",
            "scope": "mcp",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": str(org_id),
        },
    )
    assert authorize_response.status_code == 200, authorize_response.json()
    authorize_data = authorize_response.json()
    assert "redirect_url" in authorize_data

    # Extract code from redirect URL
    redirect_url = authorize_data["redirect_url"]
    assert "code=" in redirect_url
    assert "state=test_state_123" in redirect_url

    # Parse the authorization code from the URL
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(redirect_url)
    params = parse_qs(parsed.query)
    auth_code = params["code"][0]

    # 4. Exchange code for tokens (POST /api/oauth/token)
    token_response = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        },
    )
    assert token_response.status_code == 200, token_response.json()
    token_data = token_response.json()
    assert "access_token" in token_data
    assert token_data["access_token"].startswith("bow_oauth_")
    assert token_data["token_type"] == "Bearer"
    assert "refresh_token" in token_data
    assert token_data["refresh_token"].startswith("bow_rt_")
    assert token_data["expires_in"] > 0

    # 5. Use the OAuth access token to call MCP endpoint
    access_token = token_data["access_token"]
    mcp_response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert mcp_response.status_code == 200, mcp_response.json()

    mcp_data = mcp_response.json()
    assert mcp_data["jsonrpc"] == "2.0"
    assert "result" in mcp_data
    assert "tools" in mcp_data["result"]
    assert len(mcp_data["result"]["tools"]) > 0

    # Verify MCP-Protocol-Version header
    assert mcp_response.headers.get("mcp-protocol-version") == "2025-11-25"


@pytest.mark.e2e
def test_oauth_refresh_token(
    enable_mcp,
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Refresh token exchange returns new access + refresh tokens."""
    # Setup
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    enable_mcp(user_token=token, org_id=org_id)

    # Create client, authorize, get tokens
    client = create_oauth_client(user_token=token, org_id=org_id)
    client_id = client["client_id"]
    client_secret = client["client_secret"]
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    code_verifier, code_challenge = _pkce_pair()

    authorize_response = test_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "s",
            "scope": "mcp",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": str(org_id),
        },
    )
    from urllib.parse import urlparse, parse_qs
    auth_code = parse_qs(urlparse(authorize_response.json()["redirect_url"]).query)["code"][0]

    token_response = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        },
    )
    token_data = token_response.json()
    refresh_token = token_data["refresh_token"]
    old_access = token_data["access_token"]

    # Use refresh token
    refresh_response = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert refresh_response.status_code == 200, refresh_response.json()
    refresh_data = refresh_response.json()
    assert refresh_data["access_token"] != old_access
    assert refresh_data["access_token"].startswith("bow_oauth_")
    assert "refresh_token" in refresh_data

    # New access token should work
    mcp_response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {refresh_data['access_token']}"},
    )
    assert mcp_response.status_code == 200


# ============================================================================
# Error cases
# ============================================================================

@pytest.mark.e2e
def test_oauth_invalid_pkce_rejected(
    enable_mcp,
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Token exchange with wrong code_verifier is rejected."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    enable_mcp(user_token=token, org_id=org_id)

    client = create_oauth_client(user_token=token, org_id=org_id)
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    _, code_challenge = _pkce_pair()

    # Authorize
    authorize_response = test_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "state": "s",
            "scope": "mcp",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": str(org_id),
        },
    )
    from urllib.parse import urlparse, parse_qs
    auth_code = parse_qs(urlparse(authorize_response.json()["redirect_url"]).query)["code"][0]

    # Exchange with WRONG verifier
    token_response = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code_verifier": "wrong_verifier_that_doesnt_match",
        },
    )
    assert token_response.status_code == 400
    assert token_response.json()["error"] == "invalid_grant"


@pytest.mark.e2e
def test_oauth_code_single_use(
    enable_mcp,
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Authorization code can only be used once."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    enable_mcp(user_token=token, org_id=org_id)

    client = create_oauth_client(user_token=token, org_id=org_id)
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    code_verifier, code_challenge = _pkce_pair()

    authorize_response = test_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "state": "s",
            "scope": "mcp",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": str(org_id),
        },
    )
    from urllib.parse import urlparse, parse_qs
    auth_code = parse_qs(urlparse(authorize_response.json()["redirect_url"]).query)["code"][0]

    token_data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": redirect_uri,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "code_verifier": code_verifier,
    }

    # First exchange succeeds
    r1 = test_client.post("/api/oauth/token", data=token_data)
    assert r1.status_code == 200

    # Second exchange with same code fails
    r2 = test_client.post("/api/oauth/token", data=token_data)
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


@pytest.mark.e2e
def test_oauth_invalid_redirect_uri_rejected(
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Authorization with non-allowlisted redirect_uri is rejected."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    client = create_oauth_client(user_token=token, org_id=org_id)
    _, code_challenge = _pkce_pair()

    response = test_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client["client_id"],
            "redirect_uri": "https://evil.example.com/callback",
            "state": "s",
            "scope": "mcp",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": str(org_id),
        },
    )
    assert response.status_code == 400
    assert "redirect_uri" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_mcp_401_has_www_authenticate(test_client, create_user, login_user):
    """Unauthenticated MCP request returns 401 with WWW-Authenticate header."""
    create_user()
    response = test_client.get("/api/mcp")
    assert response.status_code == 401
    www_auth = response.headers.get("www-authenticate", "")
    assert "resource_metadata" in www_auth
    assert ".well-known/oauth-protected-resource" in www_auth
