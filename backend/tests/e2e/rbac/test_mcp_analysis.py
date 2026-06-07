"""Full MCP integration analysis.

Exercises the MCP endpoint across the dimensions that matter for tenancy and
access control:

  * private vs public data sources surfaced by create_report / get_context
  * API-key auth vs OAuth-token auth parity
  * permission-gated tool visibility in tools/list
  * tool execution (tools/call) scoping

Reuses the rbac/ fixtures (bootstrap_admin, sqlite_data_source, invite_user_to_org,
grant_resource, ...) plus the global oauth/api-key/mcp fixtures.
"""

import hashlib
import base64
import json
import secrets
from urllib.parse import urlparse, parse_qs

import pytest


# ── helpers ────────────────────────────────────────────────────────────

def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _oauth_access_token(test_client, *, consent_token, client):
    """Run consent + token exchange, return a bow_oauth_ access token."""
    verifier, challenge = _pkce_pair()
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    auth = test_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "state": "s",
            "scope": "mcp",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        headers={"Authorization": f"Bearer {consent_token}"},
    )
    assert auth.status_code == 200, auth.json()
    code = parse_qs(urlparse(auth.json()["redirect_url"]).query)["code"][0]
    tok = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200, tok.json()
    return tok.json()["access_token"]


def _mcp(test_client, *, method, params=None, api_key=None, bearer=None):
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    resp = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers,
    )
    return resp


def _tool_call(test_client, *, name, arguments, **auth):
    resp = _mcp(test_client, method="tools/call",
                params={"name": name, "arguments": arguments}, **auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "result" in body, body
    # tools/call wraps the tool result as text content
    text = body["result"]["content"][0]["text"]
    return json.loads(text)


# ── fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def mcp_org(bootstrap_admin, sqlite_data_source, invite_user_to_org):
    """An org with a public DS, a private DS, an admin, and a plain member who
    has NOT been granted the private DS."""
    admin = bootstrap_admin("mcpadmin")
    public_ds = sqlite_data_source(
        name="Public DS", user_token=admin["token"], org_id=admin["org_id"],
        is_public=True,
    )
    private_ds = sqlite_data_source(
        name="Private DS", user_token=admin["token"], org_id=admin["org_id"],
        is_public=False,
    )
    member = invite_user_to_org(
        org_id=admin["org_id"], admin_token=admin["token"], role="member",
    )
    return {
        "admin": admin,
        "member": member,
        "org_id": admin["org_id"],
        "public_ds": public_ds,
        "private_ds": private_ds,
    }


# ── private vs public ──────────────────────────────────────────────────

@pytest.mark.e2e
def test_create_report_hides_private_ds_from_member_via_api_key(
    mcp_org, create_api_key, test_client,
):
    """A member without access to the private DS must NOT get it attached to a
    report created over MCP (API-key auth)."""
    member = mcp_org["member"]
    key = create_api_key(user_token=member["token"], org_id=mcp_org["org_id"])["key"]

    result = _tool_call(
        test_client, name="create_report",
        arguments={"title": "member session"},
        api_key=key,
    )
    names = {ds["name"] for ds in result["data_sources"]}
    assert "Public DS" in names
    assert "Private DS" not in names, (
        f"Private DS leaked to a non-member over MCP create_report: {names}"
    )


@pytest.mark.e2e
def test_create_report_hides_private_ds_from_member_via_oauth(
    mcp_org, create_oauth_client, test_client,
):
    """Same as above but over OAuth-token auth — must behave identically."""
    admin = mcp_org["admin"]
    member = mcp_org["member"]
    client = create_oauth_client(
        user_token=admin["token"], org_id=mcp_org["org_id"], name="Claude Web",
    )
    access = _oauth_access_token(test_client, consent_token=member["token"], client=client)

    result = _tool_call(
        test_client, name="create_report",
        arguments={"title": "member session"},
        bearer=access,
    )
    names = {ds["name"] for ds in result["data_sources"]}
    assert "Public DS" in names
    assert "Private DS" not in names, (
        f"Private DS leaked to a non-member over MCP create_report (OAuth): {names}"
    )


@pytest.mark.e2e
def test_get_context_refilters_shared_report_by_visibility(
    mcp_org, create_api_key, test_client,
):
    """Defense-in-depth: even if a report already has a private DS attached
    (e.g. created by the admin), a member reading that report's context must not
    see the private DS schema."""
    admin = mcp_org["admin"]
    member = mcp_org["member"]
    admin_key = create_api_key(user_token=admin["token"], org_id=mcp_org["org_id"])["key"]
    member_key = create_api_key(user_token=member["token"], org_id=mcp_org["org_id"])["key"]

    # Admin creates a report — it gets both Public and Private DS attached.
    rep = _tool_call(test_client, name="create_report",
                     arguments={"title": "shared"}, api_key=admin_key)
    assert {"Public DS", "Private DS"} <= {d["name"] for d in rep["data_sources"]}

    # Member opens the same report's context.
    ctx = _tool_call(test_client, name="get_context",
                     arguments={"report_id": rep["report_id"]}, api_key=member_key)
    names = {d["name"] for d in ctx["data_sources"]}
    assert "Private DS" not in names, f"private DS schema leaked via shared report: {names}"


@pytest.mark.e2e
def test_admin_sees_private_ds(mcp_org, create_api_key, test_client):
    """The admin (full access) should see both data sources — sanity check that
    the hiding above is about permissions, not a broken fixture."""
    admin = mcp_org["admin"]
    key = create_api_key(user_token=admin["token"], org_id=mcp_org["org_id"])["key"]
    result = _tool_call(
        test_client, name="create_report",
        arguments={"title": "admin session"}, api_key=key,
    )
    names = {ds["name"] for ds in result["data_sources"]}
    assert {"Public DS", "Private DS"} <= names, names


# ── API key vs OAuth parity ────────────────────────────────────────────

@pytest.mark.e2e
def test_api_key_and_oauth_resolve_same_context(
    mcp_org, create_api_key, create_oauth_client, test_client,
):
    """For the same user, API-key and OAuth auth must surface the same data
    sources in get_context."""
    admin = mcp_org["admin"]
    key = create_api_key(user_token=admin["token"], org_id=mcp_org["org_id"])["key"]
    client = create_oauth_client(
        user_token=admin["token"], org_id=mcp_org["org_id"], name="Claude Web",
    )
    access = _oauth_access_token(test_client, consent_token=admin["token"], client=client)

    rep_key = _tool_call(test_client, name="create_report",
                         arguments={"title": "k"}, api_key=key)
    ctx_key = _tool_call(test_client, name="get_context",
                         arguments={"report_id": rep_key["report_id"]}, api_key=key)

    rep_oauth = _tool_call(test_client, name="create_report",
                           arguments={"title": "o"}, bearer=access)
    ctx_oauth = _tool_call(test_client, name="get_context",
                           arguments={"report_id": rep_oauth["report_id"]}, bearer=access)

    assert {d["name"] for d in ctx_key["data_sources"]} == \
           {d["name"] for d in ctx_oauth["data_sources"]}


# ── permission-gated tool visibility ───────────────────────────────────

@pytest.mark.e2e
def test_tools_list_gated_by_permission(
    mcp_org, create_api_key, test_client,
):
    """create_instruction/delete_instruction require manage_instructions, so a
    plain member should not see them while the admin should."""
    admin = mcp_org["admin"]
    member = mcp_org["member"]
    admin_key = create_api_key(user_token=admin["token"], org_id=mcp_org["org_id"])["key"]
    member_key = create_api_key(user_token=member["token"], org_id=mcp_org["org_id"])["key"]

    def tool_names(key):
        r = _mcp(test_client, method="tools/list", api_key=key)
        assert r.status_code == 200, r.text
        return {t["name"] for t in r.json()["result"]["tools"]}

    admin_tools = tool_names(admin_key)
    member_tools = tool_names(member_key)

    assert "create_instruction" in admin_tools
    assert "create_instruction" not in member_tools, (
        f"member saw privileged tools: {member_tools - admin_tools | (member_tools & {'create_instruction'})}"
    )
