"""
Inviting a member with roles and groups in one request.

Contract of ``POST /organizations/{org}/members``:
  - ``role_ids`` (any roles: system or custom) and ``group_ids`` are applied
    in the same request as the invite, for a pending email AND for an email
    that already has an account (registered elsewhere).
  - A pending invitee holds exactly what was chosen once they register.
  - The legacy ``role`` string still works, and a custom role named there
    is honored rather than silently granting nothing.
  - The invite is all-or-nothing: an unknown role/group, or one the caller
    may not grant, rejects the request and leaves no membership behind.
  - The same escalation rule as role assignment applies: a non-full-admin
    may not invite with a role or group wider than their own permissions.
"""
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def _email(prefix="inv"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}@test.com"


# A permission pair a plain member does not hold (see the escalation suite).
EXTRA_PERMS = ["view_members", "manage_members"]


@pytest.fixture
def org(enterprise_license, bootstrap_admin, create_role, create_group):
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    role = create_role(name=_uniq("analyst"), permissions=EXTRA_PERMS,
                       user_token=admin["token"], org_id=org_id)
    assert role.status_code == 200, role.json()
    groups = []
    for _ in range(2):
        g = create_group(name=_uniq("team"), user_token=admin["token"], org_id=org_id)
        assert g.status_code == 200, g.json()
        groups.append(g.json())
    return {"admin": admin, "org_id": org_id, "custom_role": role.json(), "groups": groups}


def _invite(test_client, token, org_id, **body):
    return test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, **body},
        headers=_hdr(token, org_id),
    )


def _members(test_client, token, org_id):
    resp = test_client.get(f"/api/organizations/{org_id}/members", headers=_hdr(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _member_by_email(test_client, token, org_id, email, user_id=None):
    for m in _members(test_client, token, org_id):
        if m.get("email") == email or (user_id and m.get("user_id") == user_id):
            return m
    return None


def _group_member_ids(test_client, token, org_id, group_id):
    resp = test_client.get(f"/api/organizations/{org_id}/groups", headers=_hdr(token, org_id))
    assert resp.status_code == 200, resp.json()
    g = next(g for g in resp.json() if g["id"] == group_id)
    return set(g["member_user_ids"]) | set(g["member_membership_ids"])


def _org_entry(whoami, token, org_id):
    return next(o for o in whoami(token)["organizations"] if o["id"] == org_id)


# ── Pending invite: several roles + several groups ───────────────────────

@pytest.mark.e2e
def test_pending_invite_applies_all_roles_and_groups(org, test_client, get_system_role,
                                                     create_user, login_user, whoami):
    admin, org_id = org["admin"], org["org_id"]
    member_role = get_system_role("member", user_token=admin["token"], org_id=org_id)
    role_ids = [member_role["id"], org["custom_role"]["id"]]
    group_ids = [g["id"] for g in org["groups"]]
    email = _email()

    resp = _invite(test_client, admin["token"], org_id, email=email,
                   role_ids=role_ids, group_ids=group_ids)
    assert resp.status_code == 200, resp.json()
    membership_id = resp.json()["id"]
    assert resp.json()["user_id"] is None

    pending = _member_by_email(test_client, admin["token"], org_id, email)
    direct = {r["id"] for r in pending["roles"] if r.get("source") in (None, "direct")}
    assert direct == set(role_ids)
    for gid in group_ids:
        assert membership_id in _group_member_ids(test_client, admin["token"], org_id, gid)

    # Registering turns the pre-assigned access into the user's real access.
    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    entry = _org_entry(whoami, token, org_id)
    assert set(EXTRA_PERMS) <= set(entry["permissions"])
    assert org["custom_role"]["name"] in entry["roles"]
    user_id = whoami(token)["id"]
    for gid in group_ids:
        assert user_id in _group_member_ids(test_client, admin["token"], org_id, gid)


@pytest.mark.e2e
def test_pending_invite_with_only_a_custom_role_holds_that_role(org, test_client, create_user,
                                                                login_user, whoami):
    """Choosing only a custom role must not be silently swapped for nothing."""
    admin, org_id = org["admin"], org["org_id"]
    email = _email()
    resp = _invite(test_client, admin["token"], org_id, email=email,
                   role_ids=[org["custom_role"]["id"]])
    assert resp.status_code == 200, resp.json()

    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    entry = _org_entry(whoami, token, org_id)
    assert set(EXTRA_PERMS) <= set(entry["permissions"])
    assert "full_admin_access" not in entry["permissions"]


# ── Legacy `role` string naming a custom role ────────────────────────────

@pytest.mark.e2e
def test_legacy_role_string_naming_a_custom_role_is_granted(org, test_client, create_user,
                                                           login_user, whoami):
    admin, org_id = org["admin"], org["org_id"]
    email = _email()
    resp = _invite(test_client, admin["token"], org_id, email=email,
                   role=org["custom_role"]["name"])
    assert resp.status_code == 200, resp.json()

    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    entry = _org_entry(whoami, token, org_id)
    assert set(EXTRA_PERMS) <= set(entry["permissions"])


@pytest.mark.e2e
def test_unknown_role_name_is_rejected(org, test_client):
    admin, org_id = org["admin"], org["org_id"]
    email = _email()
    resp = _invite(test_client, admin["token"], org_id, email=email, role=_uniq("no-such-role"))
    assert resp.status_code in (400, 404), resp.json()
    assert _member_by_email(test_client, admin["token"], org_id, email) is None


# ── Existing account (registered elsewhere) ──────────────────────────────

@pytest.mark.e2e
def test_inviting_existing_account_applies_roles_and_groups(org, test_client, bootstrap_admin, whoami):
    """An email with an account in another org is added as a registered member;
    its roles and groups must land on the *user*, not be rejected."""
    admin, org_id = org["admin"], org["org_id"]
    other = bootstrap_admin("elsewhere")  # has an account + its own org
    group_ids = [g["id"] for g in org["groups"]]

    resp = _invite(test_client, admin["token"], org_id, email=other["email"],
                   role_ids=[org["custom_role"]["id"]], group_ids=group_ids)
    assert resp.status_code == 200, resp.json()
    assert resp.json()["user_id"] == other["user_id"]

    for gid in group_ids:
        assert other["user_id"] in _group_member_ids(test_client, admin["token"], org_id, gid)
    entry = _org_entry(whoami, other["token"], org_id)
    assert set(EXTRA_PERMS) <= set(entry["permissions"])


# ── All-or-nothing ────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("bad", ["role", "group"])
def test_unknown_role_or_group_rejects_invite_without_side_effects(org, test_client, bad):
    admin, org_id = org["admin"], org["org_id"]
    email = _email()
    body = {"email": email, "role_ids": [org["custom_role"]["id"]],
            "group_ids": [org["groups"][0]["id"]]}
    if bad == "role":
        body["role_ids"].append(str(uuid.uuid4()))
    else:
        body["group_ids"].append(str(uuid.uuid4()))
    resp = _invite(test_client, admin["token"], org_id, **body)
    assert resp.status_code == 404, resp.json()
    assert _member_by_email(test_client, admin["token"], org_id, email) is None
    assert _group_member_ids(test_client, admin["token"], org_id, org["groups"][0]["id"]) == set()


# ── Escalation guard (non-admin delegate) ────────────────────────────────

@pytest.fixture
def delegate(org, test_client, invite_user_to_org, create_role, assign_role):
    admin, org_id = org["admin"], org["org_id"]
    role = create_role(name=_uniq("people-mgr"), permissions=["view_members", "manage_members"],
                       user_token=admin["token"], org_id=org_id)
    assert role.status_code == 200, role.json()
    d = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    resp = assign_role(role_id=role.json()["id"], principal_type="user",
                       principal_id=d["user_id"], user_token=admin["token"], org_id=org_id)
    assert resp.status_code == 200, resp.json()
    return d


@pytest.mark.e2e
def test_delegate_cannot_invite_with_admin_in_role_ids(org, delegate, test_client, get_system_role):
    admin, org_id = org["admin"], org["org_id"]
    admin_role = get_system_role("admin", user_token=admin["token"], org_id=org_id)
    member_role = get_system_role("member", user_token=admin["token"], org_id=org_id)
    email = _email()
    resp = _invite(test_client, delegate["token"], org_id, email=email,
                   role_ids=[member_role["id"], admin_role["id"]])
    assert resp.status_code == 403, resp.json()
    assert _member_by_email(test_client, admin["token"], org_id, email) is None


@pytest.mark.e2e
def test_delegate_cannot_invite_into_group_carrying_admin(org, delegate, test_client,
                                                          get_system_role, assign_role):
    admin, org_id = org["admin"], org["org_id"]
    admin_role = get_system_role("admin", user_token=admin["token"], org_id=org_id)
    group_id = org["groups"][0]["id"]
    r = assign_role(role_id=admin_role["id"], principal_type="group", principal_id=group_id,
                    user_token=admin["token"], org_id=org_id)
    assert r.status_code == 200, r.json()
    email = _email()
    resp = _invite(test_client, delegate["token"], org_id, email=email, group_ids=[group_id])
    assert resp.status_code == 403, resp.json()
    assert _member_by_email(test_client, admin["token"], org_id, email) is None


@pytest.mark.e2e
def test_delegate_can_invite_with_roles_and_groups_they_hold(org, delegate, test_client):
    """The guard is a subset check, not a blanket ban on role_ids/group_ids."""
    admin, org_id = org["admin"], org["org_id"]
    email = _email()
    resp = _invite(test_client, delegate["token"], org_id, email=email,
                   role_ids=[org["custom_role"]["id"]], group_ids=[org["groups"][1]["id"]])
    assert resp.status_code == 200, resp.json()
    assert resp.json()["id"] in _group_member_ids(test_client, admin["token"], org_id, org["groups"][1]["id"])
