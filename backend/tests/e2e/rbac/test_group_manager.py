"""Group managers: delegated administration scoped to one group.

Contract (docs/feedback-loops/group-manager.md):
  - Nobody manages a group until an org admin (or a manager of that group)
    promotes a registered member of it. A plain member can't touch any group.
  - A manager can list, add and remove members of the groups they manage and
    promote a member to manager. They cannot demote or remove another manager,
    act on any other group, or edit the group itself.
  - Only users who are members of the group can hold the role; the grant is not
    accepted through the generic resource-grant or role routes, and it confers
    nothing on a group imported from an identity provider.
  - Leaving the group, leaving the org or deleting the group ends the role.
  - Membership added by a manager carries the group's access like any other.
"""
import uuid

import pytest

from tests.e2e.test_smart_instructions_planner_prompt import env, llm  # noqa: F401 (planner fixtures for sqlite agents)


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def org(enterprise_license, test_client, bootstrap_admin, invite_user_to_org, create_group,
        add_user_to_group, sqlite_data_source):
    """Admin; users lead/peer/other/outsider; groups sales+finance; a private
    agent in a folder that the sales group can query."""
    admin = bootstrap_admin("gm")
    org_id = admin["org_id"]

    def call(actor, method, path, body=None):
        return test_client.request(method, "/api/" + path.lstrip("/"), json=body,
                                   headers=_hdr(actor["token"], org_id))

    people = {n: invite_user_to_org(org_id=org_id, admin_token=admin["token"])
              for n in ("lead", "peer", "other", "outsider")}
    groups = {}
    for name in ("sales", "finance"):
        resp = create_group(name=_uniq(name), user_token=admin["token"], org_id=org_id)
        assert resp.status_code == 200, resp.text
        groups[name] = resp.json()["id"]
    for who, grp in (("lead", "sales"), ("peer", "sales"), ("other", "finance")):
        resp = add_user_to_group(group_id=groups[grp], user_id=people[who]["user_id"],
                                 user_token=admin["token"], org_id=org_id)
        assert resp.status_code == 201, resp.text

    folder = call(admin, "POST", "folders", {"name": _uniq("Sales")}).json()["id"]
    agent = sqlite_data_source(name=_uniq("pipeline"), user_token=admin["token"], org_id=org_id)
    assert call(admin, "PUT", f"folders/items/agent/{agent['id']}/placement",
                {"parent_id": folder}).status_code == 200
    resp = call(admin, "PUT", f"folders/{folder}/share",
                {"principal_type": "group", "principal_id": groups["sales"], "role": "Viewer"})
    assert resp.status_code == 200, resp.text

    return {"admin": admin, "org_id": org_id, "call": call, "groups": groups,
            "agent_id": agent["id"], **people}


# ── helpers ──────────────────────────────────────────────────────────────

def _members(o, group):
    resp = o["call"](o["admin"], "GET", f"organizations/{o['org_id']}/groups/{o['groups'][group]}/members")
    assert resp.status_code == 200, resp.text
    return {m["user_id"] for m in resp.json() if m.get("user_id")}


def _group(o, group, actor=None):
    resp = o["call"](actor or o["admin"], "GET", f"organizations/{o['org_id']}/groups")
    assert resp.status_code == 200, resp.text
    return next(g for g in resp.json() if g["id"] == o["groups"][group])


def _managers(o, group):
    return set(_group(o, group)["manager_user_ids"])


def _promote(o, actor, group, who):
    return o["call"](actor, "PUT", f"organizations/{o['org_id']}/groups/{o['groups'][group]}/managers/{o[who]['user_id']}")


def _demote(o, actor, group, who):
    return o["call"](actor, "DELETE", f"organizations/{o['org_id']}/groups/{o['groups'][group]}/managers/{o[who]['user_id']}")


def _add(o, actor, group, who):
    return o["call"](actor, "POST", f"organizations/{o['org_id']}/groups/{o['groups'][group]}/members",
                     {"user_id": o[who]["user_id"]})


def _remove(o, actor, group, who):
    return o["call"](actor, "DELETE", f"organizations/{o['org_id']}/groups/{o['groups'][group]}/members/{o[who]['user_id']}")


def _list(o, actor, group):
    return o["call"](actor, "GET", f"organizations/{o['org_id']}/groups/{o['groups'][group]}/members")


def _can_see_agent(o, who):
    resp = o["call"](o[who], "GET", "data_sources/active")
    assert resp.status_code == 200, resp.text
    return o["agent_id"] in {d["id"] for d in resp.json()}


def _session_group_perms(whoami, o, who):
    info = whoami(o[who]["token"])
    org = next(x for x in info["organizations"] if x["id"] == o["org_id"])
    return {k: v for k, v in (org.get("resource_permissions") or {}).items() if k.startswith("group:")}


def _make_manager(o, who="lead", group="sales"):
    resp = _promote(o, o["admin"], group, who)
    assert resp.status_code == 204, resp.text


# ── default: nobody manages anything ─────────────────────────────────────

@pytest.mark.e2e
def test_groups_start_with_no_managers_and_members_cannot_manage(org, whoami):
    o = org
    assert _managers(o, "sales") == set() and _managers(o, "finance") == set()
    for who in ("lead", "outsider"):
        assert _list(o, o[who], "sales").status_code == 403
        assert _add(o, o[who], "sales", "outsider" if who == "lead" else "lead").status_code == 403
        assert _remove(o, o[who], "sales", "peer").status_code == 403
        assert _promote(o, o[who], "sales", "peer").status_code == 403
        assert _session_group_perms(whoami, o, who) == {}
    assert _members(o, "sales") == {o["lead"]["user_id"], o["peer"]["user_id"]}
    assert _managers(o, "sales") == set()


# ── what a manager can do ────────────────────────────────────────────────

@pytest.mark.e2e
def test_manager_adds_and_removes_members_who_gain_and_lose_group_access(org, whoami):
    o = org
    _make_manager(o)
    assert _session_group_perms(whoami, o, "lead") == {f"group:{o['groups']['sales']}": ["manage_members"]}
    assert _list(o, o["lead"], "sales").status_code == 200

    assert not _can_see_agent(o, "outsider")
    assert _add(o, o["lead"], "sales", "outsider").status_code == 201
    assert o["outsider"]["user_id"] in _members(o, "sales")
    assert _can_see_agent(o, "outsider")

    assert _remove(o, o["lead"], "sales", "outsider").status_code == 204
    assert o["outsider"]["user_id"] not in _members(o, "sales")
    assert not _can_see_agent(o, "outsider")


@pytest.mark.e2e
def test_manager_can_promote_a_member_who_then_manages(org):
    o = org
    _make_manager(o)
    assert _promote(o, o["lead"], "sales", "peer").status_code == 204
    assert _managers(o, "sales") == {o["lead"]["user_id"], o["peer"]["user_id"]}
    assert _add(o, o["peer"], "sales", "outsider").status_code == 201


@pytest.mark.e2e
def test_manager_can_leave_and_rejoining_does_not_restore_the_role(org, whoami):
    o = org
    _make_manager(o)
    assert _remove(o, o["lead"], "sales", "lead").status_code == 204
    assert _managers(o, "sales") == set()
    assert _session_group_perms(whoami, o, "lead") == {}
    assert _add(o, o["admin"], "sales", "lead").status_code == 201
    assert _managers(o, "sales") == set()
    assert _list(o, o["lead"], "sales").status_code == 403


# ── what a manager cannot do ─────────────────────────────────────────────

@pytest.mark.e2e
def test_manager_cannot_act_on_a_group_they_do_not_manage(org):
    o = org
    _make_manager(o)
    before = _members(o, "finance")
    assert _list(o, o["lead"], "finance").status_code == 403
    assert _add(o, o["lead"], "finance", "outsider").status_code == 403
    assert _add(o, o["lead"], "finance", "lead").status_code == 403
    assert _remove(o, o["lead"], "finance", "other").status_code == 403
    assert _promote(o, o["lead"], "finance", "other").status_code == 403
    assert _members(o, "finance") == before
    assert _managers(o, "finance") == set()


@pytest.mark.e2e
def test_manager_cannot_demote_or_remove_another_manager(org):
    o = org
    _make_manager(o, "lead")
    _make_manager(o, "peer")
    assert _demote(o, o["lead"], "sales", "peer").status_code == 403
    assert _remove(o, o["lead"], "sales", "peer").status_code == 403
    assert _managers(o, "sales") == {o["lead"]["user_id"], o["peer"]["user_id"]}
    assert o["peer"]["user_id"] in _members(o, "sales")
    # The org admin can.
    assert _demote(o, o["admin"], "sales", "peer").status_code == 204
    assert _managers(o, "sales") == {o["lead"]["user_id"]}
    assert o["peer"]["user_id"] in _members(o, "sales"), "demoting keeps the membership"


@pytest.mark.e2e
def test_only_registered_members_of_the_group_can_be_promoted(org):
    o = org
    _make_manager(o)
    for actor in (o["admin"], o["lead"]):
        # Org member outside the group, and a member of another group.
        assert _promote(o, actor, "sales", "outsider").status_code == 422
        assert _promote(o, actor, "sales", "other").status_code == 422
    ghost = o["call"](o["admin"], "PUT",
                      f"organizations/{o['org_id']}/groups/{o['groups']['sales']}/managers/{uuid.uuid4()}")
    assert ghost.status_code == 422
    assert _managers(o, "sales") == {o["lead"]["user_id"]}


@pytest.mark.e2e
def test_manager_cannot_edit_or_delete_the_group_or_its_access(org, create_role, assign_role):
    o = org
    _make_manager(o)
    gid, oid = o["groups"]["sales"], o["org_id"]
    assert o["call"](o["lead"], "PUT", f"organizations/{oid}/groups/{gid}", {"name": "pwned"}).status_code == 403
    assert o["call"](o["lead"], "DELETE", f"organizations/{oid}/groups/{gid}").status_code == 403
    assert o["call"](o["lead"], "POST", f"organizations/{oid}/groups", {"name": _uniq("mine")}).status_code == 403
    role = create_role(name=_uniq("big"), permissions=["manage_settings"], user_token=o["admin"]["token"], org_id=oid)
    assert role.status_code == 200
    resp = assign_role(role_id=role.json()["id"], principal_type="group", principal_id=gid,
                       user_token=o["lead"]["token"], org_id=oid)
    assert resp.status_code == 403
    assert _group(o, "sales")["name"] != "pwned"


@pytest.mark.e2e
def test_manager_session_cannot_reach_another_org(org, bootstrap_admin, create_group):
    o = org
    _make_manager(o)
    other_admin = bootstrap_admin("gm2")
    g2 = create_group(name=_uniq("x"), user_token=other_admin["token"], org_id=other_admin["org_id"]).json()["id"]
    # Foreign group id under the manager's own org: not found.
    resp = o["call"](o["lead"], "GET", f"organizations/{o['org_id']}/groups/{g2}/members")
    assert resp.status_code in (403, 404)
    resp = o["call"](o["admin"], "PUT", f"organizations/{o['org_id']}/groups/{g2}/managers/{o['lead']['user_id']}")
    assert resp.status_code == 404


# ── the grant can't be minted or smuggled another way ────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("principal", ["user", "group"])
def test_generic_resource_grant_route_rejects_group_manager_grants(org, grant_resource, whoami, principal):
    o = org
    pid = o["lead"]["user_id"] if principal == "user" else o["groups"]["sales"]
    resp = grant_resource(resource_type="group", resource_id=o["groups"]["finance"], principal_type=principal,
                          principal_id=pid, permissions=["manage_members"],
                          user_token=o["admin"]["token"], org_id=o["org_id"])
    assert resp.status_code == 422, resp.text
    assert _managers(o, "finance") == set()
    assert _session_group_perms(whoami, o, "lead") == {}


@pytest.mark.e2e
def test_role_editor_rejects_group_manager_grants(org, create_role, test_client, whoami):
    o = org
    role = create_role(name=_uniq("r"), permissions=[], user_token=o["admin"]["token"], org_id=o["org_id"])
    assert role.status_code == 200
    resp = test_client.put(
        f"/api/organizations/{o['org_id']}/roles/{role.json()['id']}",
        json={"resource_grants": [{"resource_type": "group", "resource_id": o["groups"]["sales"],
                                   "permissions": ["manage_members"]}]},
        headers=_hdr(o["admin"]["token"], o["org_id"]),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["scim", "ldap", "oidc"])
async def test_imported_groups_have_no_managers(org, whoami, provider):
    o = org
    _make_manager(o)
    # Groups only become imported through a directory sync; flip the flag
    # directly to model "this group is now owned by the IdP" for each provider.
    from sqlalchemy import update
    from app.dependencies import async_session_maker
    from app.models.group import Group
    async with async_session_maker() as db:
        for gid in (o["groups"]["sales"], o["groups"]["finance"]):
            await db.execute(update(Group).where(Group.id == gid).values(external_provider=provider, external_id=gid))
        await db.commit()

    # The existing grant confers nothing any more…
    assert _session_group_perms(whoami, o, "lead") == {}
    assert _add(o, o["lead"], "sales", "outsider").status_code == 403
    assert _remove(o, o["lead"], "sales", "peer").status_code == 403
    assert o["outsider"]["user_id"] not in _members(o, "sales")
    # …and nobody can be made manager of an imported group.
    assert _promote(o, o["admin"], "finance", "other").status_code == 409
    assert _managers(o, "finance") == set()


@pytest.mark.e2e
def test_leaving_the_org_or_deleting_the_group_ends_the_role(org, whoami, invite_user_to_org):
    o = org
    _make_manager(o, "lead")
    _make_manager(o, "other", "finance")

    resp = o["call"](o["admin"], "DELETE", f"organizations/{o['org_id']}/members/{o['lead']['membership_id']}")
    assert resp.status_code in (200, 204), resp.text
    assert o["lead"]["user_id"] not in _managers(o, "sales")

    gid = o["groups"]["finance"]
    assert o["call"](o["admin"], "DELETE", f"organizations/{o['org_id']}/groups/{gid}").status_code == 204
    assert _session_group_perms(whoami, o, "other") == {}


# ── audit ────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_promotion_demotion_and_denials_are_audited(org):
    o = org
    _make_manager(o)
    assert _promote(o, o["lead"], "sales", "peer").status_code == 204
    assert _demote(o, o["admin"], "sales", "peer").status_code == 204
    assert _add(o, o["lead"], "finance", "outsider").status_code == 403
    resp = o["call"](o["admin"], "GET", "enterprise/audit?limit=100")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    actions = [e["action"] for e in body.get("items", body if isinstance(body, list) else [])]
    assert actions.count("group.manager_promoted") == 2
    assert "group.manager_demoted" in actions
    assert any(a.startswith("group.") and a.endswith(".denied") for a in actions)


# ── review follow-ups ────────────────────────────────────────────────────

@pytest.fixture
def delegate(org, create_role, assign_role, invite_user_to_org):
    """An org-wide people manager (`manage_members`) who holds nothing else."""
    o = org
    role = create_role(name=_uniq("people"), permissions=["view_members", "manage_members"],
                       user_token=o["admin"]["token"], org_id=o["org_id"])
    assert role.status_code == 200, role.text
    person = invite_user_to_org(org_id=o["org_id"], admin_token=o["admin"]["token"])
    resp = assign_role(role_id=role.json()["id"], principal_type="user", principal_id=person["user_id"],
                       user_token=o["admin"]["token"], org_id=o["org_id"])
    assert resp.status_code == 200, resp.text
    return person


@pytest.mark.e2e
@pytest.mark.parametrize("carried", ["folder_access", "role"])
def test_promotion_is_held_to_the_same_rule_as_adding_a_member(org, delegate, create_role, assign_role, carried):
    """Whoever can't add someone to a group directly can't make a manager who could."""
    o = org
    o["delegate"] = delegate
    if carried == "folder_access":
        group, member = "sales", "lead"          # sales can query a folder the delegate can't manage
    else:
        group, member = "finance", "other"       # finance carries a role the delegate doesn't hold
        role = create_role(name=_uniq("settings"), permissions=["manage_settings"],
                           user_token=o["admin"]["token"], org_id=o["org_id"])
        assert assign_role(role_id=role.json()["id"], principal_type="group", principal_id=o["groups"]["finance"],
                           user_token=o["admin"]["token"], org_id=o["org_id"]).status_code == 200

    assert _add(o, o["delegate"], group, "outsider").status_code == 403
    assert _promote(o, o["delegate"], group, member).status_code == 403
    assert _managers(o, group) == set()
    # The org admin is not bounded, and the person they promote can manage.
    assert _promote(o, o["admin"], group, member).status_code == 204
    assert _add(o, o[member], group, "outsider").status_code == 201


@pytest.mark.e2e
def test_org_delegate_can_promote_in_a_group_they_could_add_to(org, delegate):
    o = org
    o["delegate"] = delegate
    # finance carries no access, so adding to it grants nothing the delegate lacks.
    assert _add(o, o["delegate"], "finance", "outsider").status_code == 201
    assert _promote(o, o["delegate"], "finance", "other").status_code == 204
    assert _managers(o, "finance") == {o["other"]["user_id"]}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_rejoining_never_restores_a_leftover_manager_grant(org):
    o = org
    # A grant row that outlived its membership (what a promotion racing a
    # removal used to leave behind). The API can't produce one, so write it.
    from app.dependencies import async_session_maker
    from app.models.resource_grant import ResourceGrant
    async with async_session_maker() as db:
        db.add(ResourceGrant(organization_id=o["org_id"], resource_type="group", resource_id=o["groups"]["sales"],
                             principal_type="user", principal_id=o["outsider"]["user_id"],
                             permissions=["manage_members"]))
        await db.commit()
    assert _managers(o, "sales") == set(), "a non-member holding a grant is not a manager"
    assert _add(o, o["admin"], "sales", "outsider").status_code == 201
    assert _managers(o, "sales") == set()
    assert _add(o, o["outsider"], "sales", "other").status_code == 403


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_promotion_and_removal_of_the_same_member_serialize(org):
    """Promotion holds the member row until it commits; a removal of that member
    waits for it and then deletes the new grant too, so none survives.

    Drives the real service methods on two connections and pauses the promotion
    just before its commit. PostgreSQL only: SQLite has no row locks.
    """
    import asyncio
    from sqlalchemy import select, text
    from app.dependencies import async_session_maker
    from app.models.resource_grant import ResourceGrant
    from app.services.rbac_service import rbac_service

    o = org
    async with async_session_maker() as probe:
        if probe.bind.dialect.name != "postgresql":
            pytest.skip("row locking is a PostgreSQL behaviour")

    org_id, gid, uid = o["org_id"], o["groups"]["sales"], o["peer"]["user_id"]
    gate, paused = asyncio.Event(), asyncio.Event()

    async def promote():
        async with async_session_maker() as db:
            real_commit = db.commit

            async def held_commit():
                paused.set()
                await gate.wait()
                await real_commit()
            db.commit = held_commit
            await rbac_service.promote_group_manager(db, org_id, gid, uid, actor_id=None)

    async def remove():
        async with async_session_maker() as db:
            await rbac_service.remove_group_member(db, org_id, gid, uid)

    promoting = asyncio.create_task(promote())
    await asyncio.wait_for(paused.wait(), timeout=30)
    removing = asyncio.create_task(remove())

    # The removal must be blocked on the promotion's row lock, not finished.
    async with async_session_maker() as probe:
        async def removal_waits_on_a_lock():
            while True:
                waiting = (await probe.execute(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                ))).scalar()
                if waiting:
                    return
                assert not removing.done(), "removal finished while the promotion held the member row"
                await asyncio.sleep(0.05)
        await asyncio.wait_for(removal_waits_on_a_lock(), timeout=30)

    gate.set()
    await asyncio.wait_for(asyncio.gather(promoting, removing), timeout=30)

    async with async_session_maker() as db:
        left = (await db.execute(select(ResourceGrant.id).where(
            ResourceGrant.resource_type == "group", ResourceGrant.resource_id == gid,
            ResourceGrant.principal_id == uid,
        ))).all()
    assert left == [], "a manager grant outlived the membership it was granted on"
    assert uid not in _members(o, "sales")


@pytest.mark.e2e
def test_session_reports_managed_groups_per_organization(org, whoami, test_client, create_group, add_user_to_group):
    """whoami resolves every org at once; each org lists only the groups managed there."""
    o = org
    _make_manager(o)
    other_org = test_client.post("/api/organizations", json={"name": _uniq("second")},
                                 headers={"Authorization": f"Bearer {o['lead']['token']}"})
    assert other_org.status_code == 200, other_org.text
    other_id = other_org.json()["id"]
    g = create_group(name=_uniq("own"), user_token=o["lead"]["token"], org_id=other_id).json()["id"]
    assert add_user_to_group(group_id=g, user_id=o["lead"]["user_id"], user_token=o["lead"]["token"],
                             org_id=other_id).status_code == 201

    orgs = {x["id"]: x for x in whoami(o["lead"]["token"])["organizations"]}
    groups_in = lambda oid: {k for k in (orgs[oid].get("resource_permissions") or {}) if k.startswith("group:")}
    assert groups_in(o["org_id"]) == {f"group:{o['groups']['sales']}"}
    assert groups_in(other_id) == set(), "admin of the second org, but manager of none of its groups"
