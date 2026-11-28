"""E2E: the agent-sandbox feature gate.

The feature ships dark. With the deployment flag unset — which is how CI and
every existing deployment run — BoW must behave exactly as it did before the
feature existed: the settings payload says it is off, every sandbox route
answers "feature disabled", and the role editor does not offer the sandbox
permissions. The flag is the first of three levels; the tests at the bottom
cover what turning it on does and does not unlock by itself.

The first block deliberately does NOT pin the flag off: it runs against the
default, so it also catches the default changing.
"""
import pytest

from app.services.sandbox.feature import SANDBOX_ORG_PERMISSIONS, SANDBOX_RESOURCE_TYPES

pytestmark = pytest.mark.e2e

# One representative per HTTP method the router serves; grown as routes land.
_ROUTES = [
    ("get", "/api/sandbox/status"),
    ("get", "/api/sandbox/environments"),
    ("post", "/api/sandbox/environments"),
    ("put", "/api/sandbox/environments/any-id"),
    ("delete", "/api/sandbox/environments/any-id"),
    ("put", "/api/sandbox/environments/any-id/access"),
    ("get", "/api/sandbox/compute-types"),
    ("get", "/api/sandbox/computes"),
    ("post", "/api/sandbox/computes"),
    ("post", "/api/sandbox/computes/any-id/test"),
    ("post", "/api/sandbox/computes/any-id/bootstrap"),
    ("delete", "/api/sandbox/computes/any-id"),
    ("get", "/api/sandbox/settings"),
    ("put", "/api/sandbox/settings"),
]


def _registry(test_client, headers):
    resp = test_client.get("/api/permissions/registry", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _offered_permissions(registry: dict) -> set:
    return {p for perms in registry["categories"].values() for p in perms}


# ── Flag off (the default) ───────────────────────────────────────────────


def test_settings_payload_says_the_feature_is_off_by_default(test_client):
    resp = test_client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["agent_sandboxes"] == {"enabled": False}


@pytest.mark.parametrize("method, path", _ROUTES)
@pytest.mark.parametrize("who", ["admin", "member"])
def test_sandbox_routes_answer_feature_disabled_by_default(test_client, sandbox_cast, method, path, who):
    resp = getattr(test_client, method)(path, headers=sandbox_cast[who]["headers"])
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "agent_sandboxes.disabled"


def test_role_editor_does_not_offer_sandbox_permissions_by_default(test_client, sandbox_cast):
    registry = _registry(test_client, sandbox_cast["admin"]["headers"])
    assert not (_offered_permissions(registry) & SANDBOX_ORG_PERMISSIONS)
    assert not (set(registry["resource_permissions"]) & SANDBOX_RESOURCE_TYPES)
    assert not (set(registry["resource_scoped_groups"]) & SANDBOX_RESOURCE_TYPES)
    # Hiding them must not disturb what was there before.
    assert {"view_code", "run_custom_code"} <= _offered_permissions(registry)
    assert {"manage_settings", "manage_llm"} <= _offered_permissions(registry)
    for merged, members in registry["merged_categories"].items():
        assert set(members) <= set(registry["categories"]), merged


# ── Flag on: level 1 only ────────────────────────────────────────────────


def test_settings_payload_follows_the_deployment_flag(test_client, agent_sandboxes_on):
    assert test_client.get("/api/settings").json()["agent_sandboxes"] == {"enabled": True}


def test_role_editor_offers_sandbox_permissions_when_the_flag_is_on(test_client, sandbox_cast, agent_sandboxes_on):
    registry = _registry(test_client, sandbox_cast["admin"]["headers"])
    assert SANDBOX_ORG_PERMISSIONS <= _offered_permissions(registry)
    assert registry["resource_permissions"]["sandbox_environment"] == ["use"]
    # They join the existing rows rather than adding new ones.
    assert "use_agent_sandbox" in registry["categories"]["Code"]
    assert "manage_sandboxes" in registry["categories"]["Settings"]


def test_flag_on_alone_leaves_the_organization_switch_off(test_client, sandbox_cast, agent_sandboxes_on):
    resp = test_client.get("/api/sandbox/status", headers=sandbox_cast["admin"]["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["feature_enabled"] is True
    assert resp.json()["enabled"] is False


def test_sandbox_admin_routes_need_manage_sandboxes(test_client, sandbox_cast, agent_sandboxes_on):
    resp = test_client.get("/api/sandbox/status", headers=sandbox_cast["member"]["headers"])
    assert resp.status_code == 403


def test_generic_settings_writer_cannot_touch_sandbox_settings(test_client, sandbox_cast, agent_sandboxes_on):
    """The Sandbox settings tab is written only through the sandbox routes (manage_sandboxes)."""
    admin = sandbox_cast["admin"]["headers"]
    resp = test_client.put(
        "/api/organization/settings", json={"config": {"agent_sandboxes": {"enabled": True}}}, headers=admin,
    )
    assert resp.status_code == 403
    assert test_client.get("/api/sandbox/status", headers=admin).json()["enabled"] is False
