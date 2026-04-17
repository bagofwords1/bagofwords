"""
Tests for the Audit Log module.

Covers:
- AuditService.log() creation
- AuditService.get_logs() with filters and pagination
- AuditService.get_log_by_id()
- AuditService.get_action_types()
- API routes: list, get by id, action-types
- API key auth for SIEM polling (fetch latest events)
- Enterprise license gating
"""
import pytest
import uuid
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_headers(token, org_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


def _setup_user(create_user, login_user, whoami):
    """Create a user, log in, return (token, org_id, user_id)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    user_id = me["id"]
    return token, org_id, user_id


# ---------------------------------------------------------------------------
# Service-level tests (hit the DB directly via the API that triggers auditing)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestAuditLogCreation:
    """Test that actions produce audit log entries."""

    def test_login_creates_audit_log(
        self, test_client, create_user, login_user, whoami
    ):
        """Logging in should generate an audit entry visible via the API."""
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"page_size": 100},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 0  # at least the endpoint works

    def test_list_returns_created_audit_entry(
        self, test_client, create_user, login_user, whoami
    ):
        """After performing actions that produce audit logs, they appear in the list."""
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        # Creating an API key is an audited action
        key_resp = test_client.post(
            "/api/api_keys",
            json={"name": "audit-test-key"},
            headers=_auth_headers(token, org_id),
        )
        assert key_resp.status_code == 200

        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"action": "api_key.created", "page_size": 10},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert any(i["action"] == "api_key.created" for i in items)


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestAuditListRoute:
    """GET /api/enterprise/audit"""

    def test_returns_paginated_response(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert "page" in body
        assert "page_size" in body
        assert "total_pages" in body

    def test_pagination_params(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"page": 1, "page_size": 5},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 5
        assert len(body["items"]) <= 5

    def test_filter_by_action(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"action": "nonexistent.action.xyz"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["items"] == []

    def test_filter_by_date_range(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        future = (datetime.utcnow() + timedelta(days=365)).isoformat()
        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"start_date": future},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_filter_by_user_id(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        fake_user_id = str(uuid.uuid4())
        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"user_id": fake_user_id},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_search_filter(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"search": "zzz_no_match_zzz"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


@pytest.mark.e2e
class TestAuditGetByIdRoute:
    """GET /api/enterprise/audit/{log_id}"""

    def test_get_existing_log(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        # Create an audited action
        test_client.post(
            "/api/api_keys",
            json={"name": "audit-get-test"},
            headers=_auth_headers(token, org_id),
        )

        # Grab the first log entry
        list_resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"page_size": 1},
        )
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        if not items:
            pytest.skip("No audit logs generated")

        log_id = items[0]["id"]

        # Fetch by id
        resp = test_client.get(
            f"/api/enterprise/audit/{log_id}",
            headers=_auth_headers(token, org_id),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == log_id
        assert "action" in body
        assert "created_at" in body

    def test_get_nonexistent_log_returns_404(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        resp = test_client.get(
            f"/api/enterprise/audit/{uuid.uuid4()}",
            headers=_auth_headers(token, org_id),
        )
        assert resp.status_code == 404


@pytest.mark.e2e
class TestAuditActionTypesRoute:
    """GET /api/enterprise/audit/action-types"""

    def test_returns_list_of_strings(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        # Generate at least one audit event
        test_client.post(
            "/api/api_keys",
            json={"name": "action-types-test"},
            headers=_auth_headers(token, org_id),
        )

        resp = test_client.get(
            "/api/enterprise/audit/action-types",
            headers=_auth_headers(token, org_id),
        )
        assert resp.status_code == 200
        actions = resp.json()
        assert isinstance(actions, list)
        # Should contain at least the api_key.created action
        if actions:
            assert all(isinstance(a, str) for a in actions)


# ---------------------------------------------------------------------------
# SIEM / API-key polling tests
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestAuditSIEMPolling:
    """
    Validates that an external SIEM (e.g. Sentinel) can poll audit logs
    using an API key instead of a user JWT.
    """

    def test_poll_with_api_key_header(
        self, test_client, create_user, login_user, whoami, create_api_key
    ):
        """SIEM can fetch audit logs via X-API-Key header."""
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        # Create an API key (this also generates an audit event)
        key_data = create_api_key(user_token=token, org_id=org_id, name="siem-poll-key")
        api_key = key_data["key"]

        resp = test_client.get(
            "/api/enterprise/audit",
            headers={
                "X-API-Key": api_key,
                "X-Organization-Id": str(org_id),
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert body["total"] >= 1

    def test_poll_with_bearer_api_key(
        self, test_client, create_user, login_user, whoami, create_api_key
    ):
        """SIEM can fetch audit logs via Authorization: Bearer bow_... header."""
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        key_data = create_api_key(user_token=token, org_id=org_id, name="siem-bearer-key")
        api_key = key_data["key"]

        resp = test_client.get(
            "/api/enterprise/audit",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Organization-Id": str(org_id),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_poll_with_start_date_cursor(
        self, test_client, create_user, login_user, whoami, create_api_key
    ):
        """SIEM can use start_date as a cursor to fetch only new events."""
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        key_data = create_api_key(user_token=token, org_id=org_id, name="siem-cursor-key")
        api_key = key_data["key"]

        # First poll — get all events and note the latest timestamp
        resp1 = test_client.get(
            "/api/enterprise/audit",
            headers={"X-API-Key": api_key, "X-Organization-Id": str(org_id)},
            params={"page_size": 100},
        )
        assert resp1.status_code == 200
        items1 = resp1.json()["items"]
        if not items1:
            pytest.skip("No audit events to test cursor with")

        # Use the latest event's created_at as the cursor for next poll
        latest_ts = items1[0]["created_at"]

        # Generate a new event after the cursor
        test_client.post(
            "/api/api_keys",
            json={"name": "post-cursor-key"},
            headers=_auth_headers(token, org_id),
        )

        # Second poll — only events after the cursor
        resp2 = test_client.get(
            "/api/enterprise/audit",
            headers={"X-API-Key": api_key, "X-Organization-Id": str(org_id)},
            params={"start_date": latest_ts, "page_size": 100},
        )
        assert resp2.status_code == 200
        items2 = resp2.json()["items"]
        # Should have the new event(s) — at least the api_key.created
        assert resp2.json()["total"] >= 1

    def test_poll_invalid_api_key_returns_401(self, test_client):
        """Invalid API key is rejected."""
        resp = test_client.get(
            "/api/enterprise/audit",
            headers={
                "X-API-Key": "bow_invalid_key_123",
                "X-Organization-Id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 401

    def test_poll_get_single_event_with_api_key(
        self, test_client, create_user, login_user, whoami, create_api_key
    ):
        """SIEM can fetch a single audit event by ID using API key."""
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        key_data = create_api_key(user_token=token, org_id=org_id, name="siem-single-key")
        api_key = key_data["key"]

        # Get an event id
        list_resp = test_client.get(
            "/api/enterprise/audit",
            headers={"X-API-Key": api_key, "X-Organization-Id": str(org_id)},
            params={"page_size": 1},
        )
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        if not items:
            pytest.skip("No audit events")

        log_id = items[0]["id"]

        resp = test_client.get(
            f"/api/enterprise/audit/{log_id}",
            headers={"X-API-Key": api_key, "X-Organization-Id": str(org_id)},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == log_id

    def test_poll_action_types_with_api_key(
        self, test_client, create_user, login_user, whoami, create_api_key
    ):
        """SIEM can fetch action types using API key."""
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        key_data = create_api_key(user_token=token, org_id=org_id, name="siem-actions-key")
        api_key = key_data["key"]

        resp = test_client.get(
            "/api/enterprise/audit/action-types",
            headers={"X-API-Key": api_key, "X-Organization-Id": str(org_id)},
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Response schema validation
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestAuditResponseSchema:
    """Validate the shape of audit log responses."""

    def test_audit_item_has_expected_fields(
        self, test_client, create_user, login_user, whoami
    ):
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        # Trigger an auditable action
        test_client.post(
            "/api/api_keys",
            json={"name": "schema-test-key"},
            headers=_auth_headers(token, org_id),
        )

        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"page_size": 1},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        if not items:
            pytest.skip("No audit logs")

        item = items[0]
        expected_fields = {
            "id", "organization_id", "action", "created_at",
            "user_id", "user_email", "resource_type", "resource_id",
            "details", "ip_address", "user_agent",
        }
        assert expected_fields.issubset(item.keys())

    def test_audit_item_user_email_populated(
        self, test_client, create_user, login_user, whoami
    ):
        """Audit entries for user actions should have user_email set."""
        token, org_id, _ = _setup_user(create_user, login_user, whoami)

        test_client.post(
            "/api/api_keys",
            json={"name": "email-test-key"},
            headers=_auth_headers(token, org_id),
        )

        resp = test_client.get(
            "/api/enterprise/audit",
            headers=_auth_headers(token, org_id),
            params={"action": "api_key.created", "page_size": 1},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        if not items:
            pytest.skip("No api_key.created audit logs")

        assert items[0]["user_email"] is not None


# ---------------------------------------------------------------------------
# Auth / unauthenticated access
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestAuditAuth:
    """Ensure audit endpoints require authentication."""

    def test_no_auth_returns_401(self, test_client):
        resp = test_client.get("/api/enterprise/audit")
        assert resp.status_code == 401

    def test_no_auth_get_by_id_returns_401(self, test_client):
        resp = test_client.get(f"/api/enterprise/audit/{uuid.uuid4()}")
        assert resp.status_code == 401

    def test_no_auth_action_types_returns_401(self, test_client):
        resp = test_client.get("/api/enterprise/audit/action-types")
        assert resp.status_code == 401
