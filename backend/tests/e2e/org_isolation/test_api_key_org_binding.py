"""A personal API key acts only in the organization it was created in.

Contract (multi-organization deployments): the organization a key was issued
in is the only one it can act in, even when its owner belongs to several. The
owner's own session is not narrowed, and a deployment that never enabled
multiple organizations is unaffected.
"""
import pytest

from app.settings.config import settings as bow_settings

from ._shared_member import h, shared_member, two_orgs  # noqa: F401


def _count(test_client, headers):
    return test_client.get("/api/notifications/count", headers=headers)


@pytest.fixture
def key_from_a(shared_member, create_api_key):
    return create_api_key(shared_member["shared_token"], shared_member["a_org"])["key"]


@pytest.mark.parametrize("style", ["x-api-key", "bearer"])
def test_key_is_rejected_for_another_organization_of_its_owner(
    test_client, shared_member, key_from_a, style,
):
    o = shared_member
    auth = {"X-API-Key": key_from_a} if style == "x-api-key" else {"Authorization": f"Bearer {key_from_a}"}

    other = _count(test_client, {**auth, "X-Organization-Id": o["b_org"]})
    assert other.status_code == 403, other.text
    assert "unread" not in other.json()

    # Its own organization works, named explicitly or left to the key.
    assert _count(test_client, {**auth, "X-Organization-Id": o["a_org"]}).status_code == 200
    assert _count(test_client, auth).status_code == 200


def test_owner_session_still_reaches_both_organizations(test_client, shared_member, key_from_a):
    o = shared_member
    assert _count(test_client, h(o["shared_token"], o["a_org"])).status_code == 200
    assert _count(test_client, h(o["shared_token"], o["b_org"])).status_code == 200


def test_binding_is_not_enforced_when_multiple_organizations_are_off(
    test_client, shared_member, key_from_a, monkeypatch,
):
    o = shared_member
    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", False)

    resp = _count(test_client, {"X-API-Key": key_from_a, "X-Organization-Id": o["b_org"]})

    assert resp.status_code == 200, resp.text
