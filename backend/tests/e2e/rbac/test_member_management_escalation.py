"""
Member-management escalation guards (pentest WP1).

Invariants:
  - PUT /organizations/{org}/members/{id} follows the same rule as the RBAC
    routes (#1305): a non-full-admin can't change their own role and can't
    grant a role carrying permissions they don't hold. Full admins are
    unrestricted.
  - POST /organizations/{org}/groups/{id}/members only accepts user principals
    with an active membership in *this* organization.
  - GET  /organizations/{org}/members/{id}/invite-link is closed to service
    accounts, like its neighbouring member-management routes.
"""
import uuid

import pytest


PEOPLE_MANAGER_PERMS = ["view_members", "manage_members"]


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def cast(enterprise_license, bootstrap_admin, invite_user_to_org, create_role, assign_role):
    """Org with an admin, a people-manager delegate, and a plain member."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    role = create_role(name=_uniq("people-mgr"), permissions=PEOPLE_MANAGER_PERMS,
                       user_token=admin["token"], org_id=org_id)
    assert role.status_code == 200, role.json()
    delegate = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    resp = assign_role(role_id=role.json()["id"], principal_type="user",
                       principal_id=delegate["user_id"], user_token=admin["token"], org_id=org_id)
    assert resp.status_code == 200, resp.json()
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    return {"admin": admin, "org_id": org_id, "delegate": delegate, "member": member,
            "mgr_role_id": role.json()["id"]}


def _perms(whoami, token, org_id):
    org = next(o for o in whoami(token)["organizations"] if o["id"] == org_id)
    return set(org["permissions"])


def _put_member(test_client, token, org_id, membership_id, body):
    return test_client.put(f"/api/organizations/{org_id}/members/{membership_id}",
                           json=body, headers=_hdr(token, org_id))


def _member_role(test_client, admin_token, org_id, membership_id):
    resp = test_client.get(f"/api/organizations/{org_id}/members", headers=_hdr(admin_token, org_id))
    assert resp.status_code == 200, resp.json()
    return next(m for m in resp.json() if m["id"] == membership_id)["role"]


# ── 828552157: manage_members -> admin via membership update ─────────────

@pytest.mark.e2e
@pytest.mark.parametrize("target", ["self", "other"])
def test_delegate_cannot_promote_to_admin_via_member_update(cast, test_client, whoami, target):
    c = cast
    principal = c["delegate"] if target == "self" else c["member"]
    resp = _put_member(test_client, c["delegate"]["token"], c["org_id"],
                       principal["membership_id"], {"role": "admin"})
    assert resp.status_code == 403, resp.json()
    assert _member_role(test_client, c["admin"]["token"], c["org_id"], principal["membership_id"]) == "member"
    assert "full_admin_access" not in _perms(whoami, principal["token"], c["org_id"])


@pytest.mark.e2e
def test_delegate_cannot_change_own_role_even_non_escalating(cast, test_client):
    c = cast
    resp = _put_member(test_client, c["delegate"]["token"], c["org_id"],
                       c["delegate"]["membership_id"], {"role": "viewer"})
    assert resp.status_code == 403, resp.json()
    assert _member_role(test_client, c["admin"]["token"], c["org_id"], c["delegate"]["membership_id"]) == "member"


@pytest.mark.e2e
def test_admin_can_still_change_any_member_role(cast, test_client, whoami):
    c = cast
    resp = _put_member(test_client, c["admin"]["token"], c["org_id"], c["member"]["membership_id"], {"role": "admin"})
    assert resp.status_code == 200, resp.json()
    assert "full_admin_access" in _perms(whoami, c["member"]["token"], c["org_id"])
    resp = _put_member(test_client, c["admin"]["token"], c["org_id"], c["member"]["membership_id"], {"role": "member"})
    assert resp.status_code == 200, resp.json()
    assert "full_admin_access" not in _perms(whoami, c["member"]["token"], c["org_id"])


@pytest.mark.e2e
def test_delegate_can_still_make_non_escalating_member_edits(cast, test_client):
    c = cast
    resp = _put_member(test_client, c["delegate"]["token"], c["org_id"],
                       c["member"]["membership_id"], {"note": "contractor"})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["note"] == "contractor"
    resp = _put_member(test_client, c["delegate"]["token"], c["org_id"],
                       c["member"]["membership_id"], {"role": "member"})
    assert resp.status_code == 200, resp.json()
    # A note on one's own membership is not a role change.
    resp = _put_member(test_client, c["delegate"]["token"], c["org_id"],
                       c["delegate"]["membership_id"], {"note": "me"})
    assert resp.status_code == 200, resp.json()


# ── 828922733: group add accepts users from other orgs ───────────────────

@pytest.mark.e2e
def test_cannot_add_non_member_user_to_group(cast, test_client, bootstrap_admin, create_group):
    c = cast
    outsider = bootstrap_admin(name_prefix="outsider")
    group = create_group(name=_uniq("g"), user_token=c["admin"]["token"], org_id=c["org_id"])
    assert group.status_code == 200, group.json()
    gid = group.json()["id"]
    resp = test_client.post(f"/api/organizations/{c['org_id']}/groups/{gid}/members",
                            json={"user_id": outsider["user_id"]}, headers=_hdr(c["admin"]["token"], c["org_id"]))
    assert resp.status_code == 404, resp.json()
    members = test_client.get(f"/api/organizations/{c['org_id']}/groups/{gid}/members",
                              headers=_hdr(c["admin"]["token"], c["org_id"]))
    assert members.status_code == 200
    assert all(m.get("user_id") != outsider["user_id"] for m in members.json())


@pytest.mark.e2e
def test_can_still_add_org_member_to_group(cast, test_client, create_group):
    c = cast
    group = create_group(name=_uniq("g"), user_token=c["admin"]["token"], org_id=c["org_id"])
    gid = group.json()["id"]
    resp = test_client.post(f"/api/organizations/{c['org_id']}/groups/{gid}/members",
                            json={"user_id": c["member"]["user_id"]}, headers=_hdr(c["delegate"]["token"], c["org_id"]))
    assert resp.status_code == 201, resp.json()
    members = test_client.get(f"/api/organizations/{c['org_id']}/groups/{gid}/members",
                              headers=_hdr(c["admin"]["token"], c["org_id"]))
    assert any(m.get("user_id") == c["member"]["user_id"] for m in members.json())


# ── 829374573: service account reads invite links ────────────────────────

@pytest.fixture
def invite_cast(cast, test_client):
    c = cast
    sa = test_client.post("/api/service_accounts", json={"name": _uniq("sa"), "role_id": c["mgr_role_id"]},
                          headers=_hdr(c["admin"]["token"], c["org_id"]))
    assert sa.status_code == 200, sa.text
    key = test_client.post(f"/api/service_accounts/{sa.json()['id']}/keys", json={"name": "k"},
                           headers=_hdr(c["admin"]["token"], c["org_id"]))
    assert key.status_code == 200, key.text
    pending = test_client.post(
        f"/api/organizations/{c['org_id']}/members",
        json={"organization_id": c["org_id"], "email": f"{_uniq('p')}@test.com", "role": "member"},
        headers=_hdr(c["admin"]["token"], c["org_id"]),
    )
    assert pending.status_code == 200, pending.json()
    return {**c, "sa_key": key.json()["key"], "pending_id": pending.json()["id"]}


@pytest.mark.e2e
def test_service_account_cannot_get_invite_link(invite_cast, test_client):
    c = invite_cast
    resp = test_client.get(f"/api/organizations/{c['org_id']}/members/{c['pending_id']}/invite-link",
                           headers=_hdr(c["sa_key"], c["org_id"]))
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
def test_human_admin_can_still_get_invite_link(invite_cast, test_client):
    c = invite_cast
    resp = test_client.get(f"/api/organizations/{c['org_id']}/members/{c['pending_id']}/invite-link",
                           headers=_hdr(c["admin"]["token"], c["org_id"]))
    assert resp.status_code == 200, resp.json()
