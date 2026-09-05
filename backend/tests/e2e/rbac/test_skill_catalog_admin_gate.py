"""Only an org admin may enable a pre-built skill.

An installed skill is a global row: it is advertised on every request in the
org and the planner acts on it. So enabling one is org-level
`manage_instructions` authority, the same gate as POST /instructions/global —
never something a plain member can do.
"""
import uuid

import pytest

from app.ai.skills.catalog import list_prebuilt_skills


def _headers(token: str, org_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _a_skill_key() -> str:
    """Any catalog entry — the gate does not depend on mode scoping."""
    return list_prebuilt_skills()[0].key


@pytest.fixture
def admin_bundle(bootstrap_admin):
    return bootstrap_admin("skilladmin")


@pytest.mark.e2e
def test_member_cannot_install_or_uninstall(
    test_client, admin_bundle, invite_user_to_org
):
    org_id = admin_bundle["org_id"]
    key = _a_skill_key()
    member = invite_user_to_org(org_id=org_id, admin_token=admin_bundle["token"])

    install = test_client.post(
        f"/api/instructions/skill-catalog/{key}/install",
        headers=_headers(member["token"], org_id),
    )
    assert install.status_code == 403

    # Nothing was created by the refused call.
    admin_view = test_client.get(
        "/api/instructions/skill-catalog", headers=_headers(admin_bundle["token"], org_id)
    ).json()
    assert {e["key"]: e for e in admin_view}[key]["installed"] is False

    # The admin installs it; the member still may not remove or update it.
    assert test_client.post(
        f"/api/instructions/skill-catalog/{key}/install",
        headers=_headers(admin_bundle["token"], org_id),
    ).status_code == 200

    assert test_client.delete(
        f"/api/instructions/skill-catalog/{key}",
        headers=_headers(member["token"], org_id),
    ).status_code == 403
    assert test_client.post(
        f"/api/instructions/skill-catalog/{key}/update",
        headers=_headers(member["token"], org_id),
    ).status_code == 403

    # Still installed — the refusals changed nothing.
    admin_view = test_client.get(
        "/api/instructions/skill-catalog", headers=_headers(admin_bundle["token"], org_id)
    ).json()
    assert {e["key"]: e for e in admin_view}[key]["installed"] is True


@pytest.mark.e2e
def test_member_can_browse_the_catalog(test_client, admin_bundle, invite_user_to_org):
    """Listing is read-only product content — gating it would hide the catalog
    from the people who ask for the skills."""
    org_id = admin_bundle["org_id"]
    member = invite_user_to_org(org_id=org_id, admin_token=admin_bundle["token"])

    resp = test_client.get(
        "/api/instructions/skill-catalog", headers=_headers(member["token"], org_id)
    )
    assert resp.status_code == 200
    assert len(resp.json()) == len(list_prebuilt_skills())


@pytest.mark.e2e
def test_install_is_scoped_to_the_installing_org(
    test_client, admin_bundle, bootstrap_admin
):
    """Enabling a skill in one org must not leak it into another."""
    key = _a_skill_key()
    assert test_client.post(
        f"/api/instructions/skill-catalog/{key}/install",
        headers=_headers(admin_bundle["token"], admin_bundle["org_id"]),
    ).status_code == 200

    other = bootstrap_admin("otherorg")

    entries = {
        e["key"]: e
        for e in test_client.get(
            "/api/instructions/skill-catalog",
            headers=_headers(other["token"], other["org_id"]),
        ).json()
    }
    assert entries[key]["installed"] is False
    assert entries[key]["instruction_id"] is None
