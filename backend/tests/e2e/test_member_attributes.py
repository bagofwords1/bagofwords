"""Admin-set member attributes (Settings → Members).

Contract under test: an admin sets, edits and imports a member's attributes;
a member can neither set them (their own included) nor read them; and
``membership.attr:<key>`` sources (MCP header/metadata forwarding) resolve the
admin's value ahead of an IdP-synced one.
"""

import io
import uuid

import pytest


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def org(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    def invite(custom_attributes=None, register=True):
        email = f"mem_{uuid.uuid4().hex[:8]}@test.com"
        body = {"organization_id": org_id, "email": email, "role": "member"}
        if custom_attributes is not None:
            body["custom_attributes"] = custom_attributes
        r = test_client.post(f"/api/organizations/{org_id}/members", json=body, headers=_h(admin_token, org_id))
        assert r.status_code == 200, r.json()
        member = {"email": email, "membership_id": r.json()["id"], "invite": r.json()}
        if register:
            create_user(email=email, password="test123")
            member["token"] = login_user(email, "test123")
            member["user_id"] = whoami(member["token"])["id"]
        return member

    return {"id": org_id, "admin": admin_token, "admin_user_id": whoami(admin_token)["id"], "invite": invite}


def _members(test_client, org):
    r = test_client.get(f"/api/organizations/{org['id']}/members", headers=_h(org["admin"], org["id"]))
    assert r.status_code == 200, r.json()
    return {m["id"]: m for m in r.json()}



# ---------------------------------------------------------------------------
# Member attributes
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_admin_sets_member_attributes_on_invite_and_edit(test_client, org):
    member = org["invite"]({"qlik_user_id": " dana ", "pg_role": "sales_team"})
    listed = _members(test_client, org)[member["membership_id"]]
    assert listed["custom_attributes"] == {"qlik_user_id": "dana", "pg_role": "sales_team"}

    # An edit replaces the set; an empty value drops that attribute.
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{member['membership_id']}",
        json={"custom_attributes": {"pg_role": "finance_team", "qlik_user_id": ""}},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    assert _members(test_client, org)[member["membership_id"]]["custom_attributes"] == {"pg_role": "finance_team"}

    # Editing only the note leaves attributes alone.
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{member['membership_id']}",
        json={"note": "Finance analyst"},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    assert _members(test_client, org)[member["membership_id"]]["custom_attributes"] == {"pg_role": "finance_team"}


@pytest.mark.e2e
@pytest.mark.parametrize("bad", [{"1starts_with_digit": "x"}, {"has space": "x"}, {"k": "v" * 300}, {"k": ["a"]}])
def test_invalid_member_attributes_are_rejected(test_client, org, bad):
    email = f"bad_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post(
        f"/api/organizations/{org['id']}/members",
        json={"organization_id": org["id"], "email": email, "custom_attributes": bad},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code in (400, 422), r.json()


@pytest.mark.e2e
def test_members_cannot_set_attributes_not_even_their_own(test_client, org):
    member = org["invite"]({"pg_role": "sales_team"})
    for target in (member["membership_id"],):
        r = test_client.put(
            f"/api/organizations/{org['id']}/members/{target}",
            json={"custom_attributes": {"pg_role": "finance_team"}},
            headers=_h(member["token"], org["id"]),
        )
        assert r.status_code == 403, r.json()
    assert _members(test_client, org)[member["membership_id"]]["custom_attributes"] == {"pg_role": "sales_team"}


@pytest.mark.e2e
def test_plain_members_cannot_read_attributes(test_client, org):
    member = org["invite"]({"pg_role": "sales_team"})
    other = org["invite"]({"pg_role": "finance_team"})
    r = test_client.get(f"/api/organizations/{org['id']}/members", headers=_h(member["token"], org["id"]))
    assert r.status_code == 200, r.json()
    assert all(not m.get("custom_attributes") for m in r.json())
    assert _members(test_client, org)[other["membership_id"]]["custom_attributes"] == {"pg_role": "finance_team"}


@pytest.mark.e2e
def test_member_managers_who_are_not_full_admins_cannot_set_or_read_attributes(test_client, org):
    """Attributes can name the account a member acts as elsewhere, so they
    are full-admin only — a custom role with manage_members may still invite
    and edit members, but not their attributes."""
    r = test_client.post(
        f"/api/organizations/{org['id']}/roles",
        json={"name": f"people-ops-{uuid.uuid4().hex[:6]}", "permissions": ["manage_members", "view_members"]},
        headers=_h(org["admin"], org["id"]),
    )
    if r.status_code == 402:
        pytest.skip("custom roles need an enterprise license in this environment")
    assert r.status_code == 200, r.json()
    manager = org["invite"]({})
    target = org["invite"]({"qlik_user_id": "dana"})
    r = test_client.post(
        f"/api/organizations/{org['id']}/role-assignments",
        json={"role_id": r.json()["id"], "principal_type": "user", "principal_id": manager["user_id"]},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    mh = _h(manager["token"], org["id"])

    # Managing members still works...
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{target['membership_id']}", json={"note": "ok"}, headers=mh,
    )
    assert r.status_code == 200, r.json()
    # ...attributes do not: not on edit, invite or import, and not in the list.
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{target['membership_id']}",
        json={"custom_attributes": {"qlik_user_id": "ceo"}}, headers=mh,
    )
    assert r.status_code == 403
    r = test_client.post(
        f"/api/organizations/{org['id']}/members",
        json={"organization_id": org["id"], "email": f"x_{uuid.uuid4().hex[:6]}@test.com",
              "custom_attributes": {"qlik_user_id": "ceo"}},
        headers=mh,
    )
    assert r.status_code == 403
    r = test_client.post(
        f"/api/organizations/{org['id']}/members/import?dry_run=true",
        files={"file": ("m.csv", io.BytesIO(f"email,attr.qlik_user_id\n{target['email']},ceo\n".encode()), "text/csv")},
        headers=mh,
    )
    assert r.status_code == 403
    listed = test_client.get(f"/api/organizations/{org['id']}/members", headers=mh).json()
    assert all(not m.get("custom_attributes") for m in listed)
    assert _members(test_client, org)[target["membership_id"]]["custom_attributes"] == {"qlik_user_id": "dana"}


@pytest.mark.e2e
def test_import_merges_attribute_columns_without_touching_notes(test_client, org):
    existing = org["invite"]({"pg_role": "sales_team", "region": "EMEA"})
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{existing['membership_id']}",
        json={"note": "keep me"},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200
    new_email = f"imp_{uuid.uuid4().hex[:8]}@test.com"
    csv_text = (
        "Email,ATTR.pg_role,attr.qlik_user_id\n"
        f"{existing['email']},finance_team,\n"
        f"{new_email},sales_team,noa\n"
    )

    def _import(dry_run):
        return test_client.post(
            f"/api/organizations/{org['id']}/members/import?dry_run={'true' if dry_run else 'false'}",
            files={"file": ("members.csv", io.BytesIO(csv_text.encode()), "text/csv")},
            headers=_h(org["admin"], org["id"]),
        )

    preview = _import(True)
    assert preview.status_code == 200, preview.json()
    assert preview.json()["summary"]["updated"] == 1 and preview.json()["summary"]["created"] == 1
    # A dry run writes nothing.
    assert _members(test_client, org)[existing["membership_id"]]["custom_attributes"]["pg_role"] == "sales_team"

    committed = _import(False)
    assert committed.status_code == 200, committed.json()
    members = _members(test_client, org)
    updated = members[existing["membership_id"]]
    # Merged: pg_role replaced, region kept, the empty qlik_user_id cell changes nothing.
    assert updated["custom_attributes"] == {"pg_role": "finance_team", "region": "EMEA"}
    assert updated["note"] == "keep me"
    created = next(m for m in members.values() if (m.get("email") or "") == new_email)
    assert created["custom_attributes"] == {"pg_role": "sales_team", "qlik_user_id": "noa"}

    # Re-importing the same file is a no-op.
    again = _import(False)
    assert again.json()["summary"]["unchanged"] == 2


def _run(coro_fn):
    import asyncio

    from app.dependencies import async_session_maker

    async def run():
        async with async_session_maker() as db:
            return await coro_fn(db)

    return asyncio.run(run())


@pytest.mark.e2e
def test_every_attribute_change_is_audited_with_before_and_after(test_client, org):
    from sqlalchemy import select

    from app.ee.audit.models import AuditLog

    member = org["invite"]({"region": "EMEA"})
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{member['membership_id']}",
        json={"custom_attributes": {"region": "US", "qlik_user_id": "dana"}},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()

    async def rows(db):
        result = await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == str(org["id"]),
                AuditLog.action == "member.attributes_changed",
                AuditLog.resource_id == member["membership_id"],
            )
        )
        return result.scalars().all()

    logged = _run(rows)
    assert len(logged) == 1
    assert logged[0].details["before"] == {"region": "EMEA"}
    assert logged[0].details["after"] == {"region": "US", "qlik_user_id": "dana"}

    # A save that changes nothing is not an attribute change.
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{member['membership_id']}",
        json={"custom_attributes": {"region": "US", "qlik_user_id": "dana"}},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200
    assert len(_run(rows)) == 1


@pytest.mark.e2e
def test_mcp_forwarding_resolves_the_admin_value_before_the_idp_synced_one(test_client, org):
    """``membership.attr:<key>`` (MCP header forwarding) reads admin-set
    attributes first; a key the admin did not set still falls back to the
    IdP-synced profile."""
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.models.membership import Membership
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.mcp_context_injection import resolve_mcp_context

    member = org["invite"]({"employeeId": "ADMIN-SET"})

    async def seed_synced(db):
        # The IdP profile sync is an external boundary (Entra/Google Graph);
        # tests have no IdP, so its stored result is written directly.
        m = (await db.execute(select(Membership).where(Membership.id == member["membership_id"]))).scalar_one()
        m.profile_attributes = {"employeeId": "IDP-SYNCED", "department": "Sales"}
        await db.commit()

    _run(seed_synced)

    connection = SimpleNamespace(config={
        "header_injection": [
            {"header": "X-Employee", "source": "membership.attr:employeeId"},
            {"header": "X-Dept", "source": "membership.attr:department"},
        ],
    })

    async def resolve(db):
        user = (await db.execute(select(User).where(User.id == member["user_id"]))).scalar_one()
        organization = (await db.execute(select(Organization).where(Organization.id == str(org["id"])))).scalar_one()
        return await resolve_mcp_context(db, connection, user, organization)

    plan = _run(resolve)
    assert plan.headers["X-Employee"] == "ADMIN-SET"
    assert plan.headers["X-Dept"] == "Sales"
