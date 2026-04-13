"""
E2E tests for the report sharing / visibility system.

Covers:
- PUT /reports/{id}/visibility/{type}  (set_visibility)
- GET /reports/{id}/shares/{type}      (get_shares)
- Access control on public endpoints   (/r/ and /c/)
- GET /reports/{id} visibility fields
"""
import pytest
import uuid


def _setup_two_users(create_user, login_user, whoami, test_client):
    """Helper: create two users in the same org."""
    email1 = f"share_owner_{uuid.uuid4().hex[:6]}@test.com"
    user1 = create_user(email=email1, password="test123")
    token1 = login_user(email=email1, password="test123")
    me1 = whoami(token1)
    org_id = me1["organizations"][0]["id"]

    email2 = f"share_member_{uuid.uuid4().hex[:6]}@test.com"
    # Invite user2 into the org
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email2, "role": "member"},
        headers={"Authorization": f"Bearer {token1}", "X-Organization-Id": org_id},
    )
    user2 = create_user(email=email2, password="test123")
    token2 = login_user(email=email2, password="test123")
    me2 = whoami(token2)
    user2_id = me2["id"]

    return token1, token2, org_id, user2_id


# ---------------------------------------------------------------------------
# Basic visibility cycling
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_set_artifact_visibility_levels(
    test_client, create_report, get_report, set_visibility,
    create_user, login_user, whoami,
):
    """Cycle through all artifact visibility levels and verify persistence."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Vis Levels", user_token=token, org_id=org_id, data_sources=[])

    for level in ("none", "internal", "public", "none"):
        resp = set_visibility(report["id"], "artifact", level, user_token=token, org_id=org_id)
        data = resp.json()
        assert data["visibility"] == level
        assert data["share_type"] == "artifact"

        fetched = get_report(report["id"], user_token=token, org_id=org_id)
        assert fetched["artifact_visibility"] == level


@pytest.mark.e2e
def test_set_conversation_visibility_levels(
    test_client, create_report, get_report, set_visibility,
    create_user, login_user, whoami,
):
    """Cycle through all conversation visibility levels and verify persistence."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Conv Vis", user_token=token, org_id=org_id, data_sources=[])

    for level in ("none", "internal", "public", "none"):
        resp = set_visibility(report["id"], "conversation", level, user_token=token, org_id=org_id)
        data = resp.json()
        assert data["visibility"] == level
        assert data["share_type"] == "conversation"

        fetched = get_report(report["id"], user_token=token, org_id=org_id)
        assert fetched["conversation_visibility"] == level


@pytest.mark.e2e
def test_conversation_visibility_generates_token(
    test_client, create_report, get_report, set_visibility,
    create_user, login_user, whoami,
):
    """Setting conversation visibility to non-none creates a share token."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Token Gen", user_token=token, org_id=org_id, data_sources=[])

    # Initially no token
    fetched = get_report(report["id"], user_token=token, org_id=org_id)
    assert fetched["conversation_share_token"] is None

    # Enable conversation sharing
    resp = set_visibility(report["id"], "conversation", "public", user_token=token, org_id=org_id)
    data = resp.json()
    assert data["conversation_share_token"] is not None
    assert len(data["conversation_share_token"]) > 0

    # Token persists on GET
    fetched = get_report(report["id"], user_token=token, org_id=org_id)
    assert fetched["conversation_share_token"] == data["conversation_share_token"]


# ---------------------------------------------------------------------------
# Sharing with specific users
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_share_with_specific_users(
    test_client, create_report, set_visibility, get_shares,
    create_user, login_user, whoami,
):
    """Set visibility to 'shared' with user IDs, then verify via get_shares."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Shared Report", user_token=token1, org_id=org_id, data_sources=[])

    resp = set_visibility(
        report["id"], "artifact", "shared",
        user_token=token1, org_id=org_id, shared_user_ids=[user2_id],
    )
    data = resp.json()
    assert data["visibility"] == "shared"
    assert user2_id in data["shared_user_ids"]

    shares = get_shares(report["id"], "artifact", user_token=token1, org_id=org_id)
    assert len(shares) == 1
    assert shares[0]["user_id"] == user2_id


@pytest.mark.e2e
def test_remove_shared_user(
    test_client, create_report, set_visibility, get_shares,
    create_user, login_user, whoami,
):
    """Share with user, then remove them by re-setting shared_user_ids without them."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Remove Share", user_token=token1, org_id=org_id, data_sources=[])

    # Share with user2
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=token1, org_id=org_id, shared_user_ids=[user2_id],
    )
    shares = get_shares(report["id"], "artifact", user_token=token1, org_id=org_id)
    assert len(shares) == 1

    # Remove user2 by setting empty list
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=token1, org_id=org_id, shared_user_ids=[],
    )
    shares = get_shares(report["id"], "artifact", user_token=token1, org_id=org_id)
    assert len(shares) == 0


@pytest.mark.e2e
def test_shares_cleared_when_visibility_changes_from_shared(
    test_client, create_report, set_visibility, get_shares,
    create_user, login_user, whoami,
):
    """Switching from 'shared' to 'internal' should clear share records."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Clear Shares", user_token=token1, org_id=org_id, data_sources=[])

    set_visibility(
        report["id"], "artifact", "shared",
        user_token=token1, org_id=org_id, shared_user_ids=[user2_id],
    )
    assert len(get_shares(report["id"], "artifact", user_token=token1, org_id=org_id)) == 1

    # Switch to internal
    set_visibility(report["id"], "artifact", "internal", user_token=token1, org_id=org_id)
    shares = get_shares(report["id"], "artifact", user_token=token1, org_id=org_id)
    assert len(shares) == 0


# ---------------------------------------------------------------------------
# Visibility field on GET /reports/{id}
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_get_report_includes_visibility_fields(
    test_client, create_report, get_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /reports/{id} should include artifact_visibility and conversation_visibility."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Vis Fields", user_token=token, org_id=org_id, data_sources=[])

    fetched = get_report(report["id"], user_token=token, org_id=org_id)
    assert fetched["artifact_visibility"] == "none"
    assert fetched["conversation_visibility"] == "none"

    set_visibility(report["id"], "artifact", "public", user_token=token, org_id=org_id)
    set_visibility(report["id"], "conversation", "internal", user_token=token, org_id=org_id)

    fetched = get_report(report["id"], user_token=token, org_id=org_id)
    assert fetched["artifact_visibility"] == "public"
    assert fetched["conversation_visibility"] == "internal"


# ---------------------------------------------------------------------------
# Legacy backward compatibility
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_artifact_visibility_syncs_publish_status(
    test_client, create_report, get_report, set_visibility,
    create_user, login_user, whoami,
):
    """Setting artifact_visibility to public should set status='published' (legacy sync)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Legacy Sync", user_token=token, org_id=org_id, data_sources=[])

    # Default is draft
    fetched = get_report(report["id"], user_token=token, org_id=org_id)
    assert fetched["status"] == "draft"

    # Set to public -> should become published
    set_visibility(report["id"], "artifact", "public", user_token=token, org_id=org_id)
    fetched = get_report(report["id"], user_token=token, org_id=org_id)
    assert fetched["status"] == "published"

    # Set back to none -> should become draft
    set_visibility(report["id"], "artifact", "none", user_token=token, org_id=org_id)
    fetched = get_report(report["id"], user_token=token, org_id=org_id)
    assert fetched["status"] == "draft"


# ---------------------------------------------------------------------------
# Invalid inputs
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_invalid_share_type_returns_400(
    test_client, create_report,
    create_user, login_user, whoami,
):
    """PUT with an invalid share_type returns 400."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Bad Type", user_token=token, org_id=org_id, data_sources=[])

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    resp = test_client.put(
        f"/api/reports/{report['id']}/visibility/invalid_type",
        json={"visibility": "public"},
        headers=headers,
    )
    assert resp.status_code == 400


@pytest.mark.e2e
def test_invalid_visibility_value_returns_422(
    test_client, create_report,
    create_user, login_user, whoami,
):
    """PUT with an invalid visibility value returns 422."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Bad Vis", user_token=token, org_id=org_id, data_sources=[])

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    resp = test_client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "bogus_value"},
        headers=headers,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Public endpoint access control
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_public_report_accessible_when_public(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /r/{id} succeeds when artifact_visibility is 'public'."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Public Access", user_token=token, org_id=org_id, data_sources=[])
    set_visibility(report["id"], "artifact", "public", user_token=token, org_id=org_id)

    resp = test_client.get(f"/api/r/{report['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Public Access"


@pytest.mark.e2e
def test_public_report_blocked_when_private(
    test_client, create_report,
    create_user, login_user, whoami,
):
    """GET /r/{id} returns 404 when artifact_visibility is 'none'."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Private Report", user_token=token, org_id=org_id, data_sources=[])
    # Default is 'none' — don't set visibility

    resp = test_client.get(f"/api/r/{report['id']}")
    assert resp.status_code == 404


@pytest.mark.e2e
def test_public_report_internal_requires_auth(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /r/{id} with internal visibility returns 401 for unauthenticated users."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Internal Report", user_token=token, org_id=org_id, data_sources=[])
    set_visibility(report["id"], "artifact", "internal", user_token=token, org_id=org_id)

    # No auth header
    resp = test_client.get(f"/api/r/{report['id']}")
    assert resp.status_code == 401


@pytest.mark.e2e
def test_shared_report_accessible_by_shared_user(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /r/{id} succeeds for a user the report is shared with."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Shared Access", user_token=token1, org_id=org_id, data_sources=[])
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=token1, org_id=org_id, shared_user_ids=[user2_id],
    )

    # user2 should be able to access
    resp = test_client.get(
        f"/api/r/{report['id']}",
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert resp.status_code == 200


@pytest.mark.e2e
def test_shared_report_blocked_for_non_shared_user(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /r/{id} returns 403 for a user NOT in the share list."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Not Shared", user_token=token1, org_id=org_id, data_sources=[])
    # Share with nobody (empty list)
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=token1, org_id=org_id, shared_user_ids=[],
    )

    resp = test_client.get(
        f"/api/r/{report['id']}",
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Conversation public endpoint access control
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_public_conversation_accessible_when_public(
    test_client, create_report, get_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /c/{token} succeeds when conversation_visibility is 'public'."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Public Conv", user_token=token, org_id=org_id, data_sources=[])
    vis_resp = set_visibility(report["id"], "conversation", "public", user_token=token, org_id=org_id)
    share_token = vis_resp.json()["conversation_share_token"]
    assert share_token is not None

    resp = test_client.get(f"/api/c/{share_token}")
    assert resp.status_code == 200


@pytest.mark.e2e
def test_public_conversation_blocked_when_private(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """Need a token to access conversation, but private reports have no active share."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Private Conv", user_token=token, org_id=org_id, data_sources=[])

    # Enable then disable to get a token that should be blocked
    vis_resp = set_visibility(report["id"], "conversation", "public", user_token=token, org_id=org_id)
    share_token = vis_resp.json()["conversation_share_token"]
    set_visibility(report["id"], "conversation", "none", user_token=token, org_id=org_id)

    resp = test_client.get(f"/api/c/{share_token}")
    # Should be blocked — either 404 or 403
    assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Independent artifact / conversation visibility
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_artifact_and_conversation_visibility_independent(
    test_client, create_report, get_report, set_visibility,
    create_user, login_user, whoami,
):
    """Setting artifact visibility should NOT affect conversation visibility and vice versa."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Independent", user_token=token, org_id=org_id, data_sources=[])

    set_visibility(report["id"], "artifact", "public", user_token=token, org_id=org_id)
    fetched = get_report(report["id"], user_token=token, org_id=org_id)
    assert fetched["artifact_visibility"] == "public"
    assert fetched["conversation_visibility"] == "none"

    set_visibility(report["id"], "conversation", "internal", user_token=token, org_id=org_id)
    fetched = get_report(report["id"], user_token=token, org_id=org_id)
    assert fetched["artifact_visibility"] == "public"
    assert fetched["conversation_visibility"] == "internal"


# ---------------------------------------------------------------------------
# GET /reports?filter=shared  ("Shared with me" listing)
# ---------------------------------------------------------------------------

def _get_reports_filtered(test_client, token, org_id, filter_value):
    """Helper: call GET /reports with a filter param and return the reports list."""
    resp = test_client.get(
        "/api/reports",
        params={"filter": filter_value},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": str(org_id),
        },
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    return data.get("reports", data) if isinstance(data, dict) else data


@pytest.mark.e2e
def test_shared_filter_shows_shared_reports(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /reports?filter=shared returns reports shared with the user."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Shared Listing", user_token=token1, org_id=org_id, data_sources=[])
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=token1, org_id=org_id, shared_user_ids=[user2_id],
    )

    # user2 should see it in their "shared" list
    reports = _get_reports_filtered(test_client, token2, org_id, "shared")
    ids = [r["id"] for r in reports]
    assert report["id"] in ids


@pytest.mark.e2e
def test_shared_filter_excludes_owned_reports(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /reports?filter=shared should NOT include reports owned by the user."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    own_report = create_report(title="My Own", user_token=token1, org_id=org_id, data_sources=[])
    set_visibility(
        own_report["id"], "artifact", "public",
        user_token=token1, org_id=org_id,
    )

    # owner's "shared" tab should NOT include their own report
    reports = _get_reports_filtered(test_client, token1, org_id, "shared")
    ids = [r["id"] for r in reports]
    assert own_report["id"] not in ids


@pytest.mark.e2e
def test_shared_filter_shows_internal_reports(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /reports?filter=shared returns org-internal reports to org members."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Internal Listing", user_token=token1, org_id=org_id, data_sources=[])
    set_visibility(report["id"], "artifact", "internal", user_token=token1, org_id=org_id)

    # user2 (org member) should see it
    reports = _get_reports_filtered(test_client, token2, org_id, "shared")
    ids = [r["id"] for r in reports]
    assert report["id"] in ids


@pytest.mark.e2e
def test_shared_filter_shows_public_reports(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """GET /reports?filter=shared returns public reports (not owned by user)."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Public Listing", user_token=token1, org_id=org_id, data_sources=[])
    set_visibility(report["id"], "artifact", "public", user_token=token1, org_id=org_id)

    # user2 should see it
    reports = _get_reports_filtered(test_client, token2, org_id, "shared")
    ids = [r["id"] for r in reports]
    assert report["id"] in ids


@pytest.mark.e2e
def test_shared_filter_empty_when_nothing_shared(
    test_client, create_report,
    create_user, login_user, whoami,
):
    """GET /reports?filter=shared returns empty when no reports are shared with user."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    create_report(title="Private Only", user_token=token1, org_id=org_id, data_sources=[])

    # user2 should see nothing
    reports = _get_reports_filtered(test_client, token2, org_id, "shared")
    assert len(reports) == 0


@pytest.mark.e2e
def test_shared_filter_hides_after_unshare(
    test_client, create_report, set_visibility,
    create_user, login_user, whoami,
):
    """Report disappears from shared list after visibility set back to 'none'."""
    token1, token2, org_id, user2_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    report = create_report(title="Unshare Test", user_token=token1, org_id=org_id, data_sources=[])
    set_visibility(
        report["id"], "artifact", "shared",
        user_token=token1, org_id=org_id, shared_user_ids=[user2_id],
    )

    # Visible
    reports = _get_reports_filtered(test_client, token2, org_id, "shared")
    assert report["id"] in [r["id"] for r in reports]

    # Unshare
    set_visibility(report["id"], "artifact", "none", user_token=token1, org_id=org_id)

    # Gone
    reports = _get_reports_filtered(test_client, token2, org_id, "shared")
    assert report["id"] not in [r["id"] for r in reports]
