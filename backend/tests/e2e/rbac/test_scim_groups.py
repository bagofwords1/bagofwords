"""
E2E tests for SCIM 2.0 Groups (/scim/v2/Groups).

Three layers:

1. **Entra provisioning engine** — tests/mocks/entra_scim_provisioner.py drives
   the endpoint exactly the way Microsoft Entra ID does (match by filter, create
   without members, PATCH Add/Remove deltas, DELETE on unassign, soft-disable of
   users leaving scope). These assert what an Entra admin observes: the groups
   and members land, follow directory changes, and *change what people can do*
   in BOW through roles assigned to the group.
2. **Protocol** — Okta's payload shapes, filters, paging, attribute selection,
   PATCH atomicity and RFC 7644 error bodies.
3. **Isolation** — the SCIM surface never reaches another org or a group it
   does not own, and the admin API cannot edit a group the IdP owns.
"""
import json
import uuid
from urllib.parse import quote

import pytest

from tests.mocks.entra_scim_provisioner import (
    EntraProvisioningApp,
    EntraTenant,
)

SCIM_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_OP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _whoami_perms(whoami, token, org_id):
    info = whoami(token)
    return set(next(o for o in info["organizations"] if o["id"] == org_id)["permissions"])


def _uniq(prefix):
    return f"{prefix} {uuid.uuid4().hex[:6]}"


@pytest.fixture
def scim_org(test_client, bootstrap_admin, enterprise_license):
    """A fresh org with an admin and a SCIM bearer token, plus request helpers."""

    def _make():
        admin = bootstrap_admin()
        resp = test_client.post(
            "/api/enterprise/scim/tokens", json={"name": "Entra"},
            headers=_hdr(admin["token"], admin["org_id"]),
        )
        assert resp.status_code == 201, resp.text
        token = resp.json()["token"]

        def scim(method, path, body=None):
            headers = {"Authorization": f"Bearer {token}"}
            content = None
            if body is not None:
                headers["Content-Type"] = "application/scim+json"
                content = json.dumps(body)
            return test_client.request(method, f"/scim/v2{path}", content=content, headers=headers)

        def admin_groups():
            r = test_client.get(
                f"/api/organizations/{admin['org_id']}/groups",
                headers=_hdr(admin["token"], admin["org_id"]),
            )
            assert r.status_code == 200, r.text
            return {g["id"]: g for g in r.json()}

        return {
            "admin": admin,
            "org_id": admin["org_id"],
            "admin_token": admin["token"],
            "scim_token": token,
            "scim": scim,
            "admin_groups": admin_groups,
        }

    return _make


def _patch(scim, group_id, *operations):
    return scim("PATCH", f"/Groups/{group_id}", {"schemas": [PATCH_OP], "Operations": list(operations)})


def _member_ids(group_json):
    return {m["value"] for m in group_json.get("members", [])}


def _assert_scim_error(resp, status, scim_type=None):
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/scim+json")
    body = resp.json()
    assert SCIM_ERROR in body["schemas"]
    assert body["status"] == str(status)
    if scim_type:
        assert body.get("scimType") == scim_type, body


# ════════════════════════════════════════════════════════════════════════
# 1. Entra provisioning engine
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def entra_cast(test_client, scim_org, invite_user_to_org):
    """An org whose existing members Alice and Bob also live in an Entra tenant,
    alongside Carol who exists only in Entra.

    Groups: "Data Team" (Alice, Bob, Carol) and "All Staff" (same three) —
    All Staff keeps everyone in provisioning scope, so membership changes in
    Data Team can be observed through permissions without the user being
    deactivated for leaving scope.
    """
    org = scim_org()
    alice = invite_user_to_org(org_id=org["org_id"], admin_token=org["admin_token"])
    bob = invite_user_to_org(org_id=org["org_id"], admin_token=org["admin_token"])

    tenant = EntraTenant(domain=f"t{uuid.uuid4().hex[:6]}.example.com")
    d_alice = tenant.add_user("Alice", "Analyst", mail=alice["email"])
    d_bob = tenant.add_user("Bob", "Builder", mail=bob["email"])
    d_carol = tenant.add_user("Carol", "Chen")
    data_team = tenant.add_group(_uniq("Data Team"), [d_alice, d_bob, d_carol])
    all_staff = tenant.add_group(_uniq("All Staff"), [d_alice, d_bob, d_carol])

    return {
        **org,
        "alice": alice, "bob": bob,
        "tenant": tenant,
        "d_alice": d_alice, "d_bob": d_bob, "d_carol": d_carol,
        "data_team": data_team, "all_staff": all_staff,
    }


def _entra(cast, test_client, legacy_patch=False):
    return EntraProvisioningApp(cast["tenant"], test_client, cast["scim_token"], legacy_patch=legacy_patch)


@pytest.mark.e2e
@pytest.mark.parametrize("legacy_patch", [False, True], ids=["scim-compliant", "legacy-aadOptscim062020-off"])
def test_entra_provisioning_lifecycle_drives_groups_and_access(
    entra_cast, test_client, whoami, create_role, assign_role, list_role_assignments, legacy_patch,
):
    """Assign → change → unassign in Entra; BOW groups, members and the
    permissions granted through the group follow at every step."""
    cast = entra_cast
    org_id, admin_token = cast["org_id"], cast["admin_token"]
    app = _entra(cast, test_client, legacy_patch=legacy_patch)

    app.test_connection()
    app.assign_group(cast["data_team"])
    app.assign_group(cast["all_staff"])

    # ── Initial cycle ────────────────────────────────────────────────────
    report = app.run_cycle()
    assert report.ok, report.errors
    assert report.users_matched == 2   # existing BOW members are adopted, not duplicated
    assert report.users_created == 1   # Carol is new
    assert report.groups_created == 2

    alice_id = cast["alice"]["user_id"]
    bob_id = cast["bob"]["user_id"]
    carol_id = app.user_ids[cast["d_carol"].object_id]
    assert app.user_ids[cast["d_alice"].object_id] == alice_id
    assert app.user_ids[cast["d_bob"].object_id] == bob_id

    groups = cast["admin_groups"]()
    team_id = app.group_ids[cast["data_team"].object_id]
    team = groups[team_id]
    assert team["name"] == cast["data_team"].display_name
    assert team["external_provider"] == "scim"
    assert team["external_id"] == cast["data_team"].object_id
    assert set(team["member_user_ids"]) == {alice_id, bob_id, carol_id}

    # Every response Entra consumed was SCIM-typed.
    assert all(
        e["content_type"].startswith("application/scim+json")
        for e in app.wire_log if e["response"] is not None
    )

    # ── Admin gives the IdP group a role → members gain it ───────────────
    role = create_role(name=_uniq("conn-mgr"), permissions=["manage_connections"],
                       user_token=admin_token, org_id=org_id)
    assert role.status_code == 200, role.text
    assigned = assign_role(role_id=role.json()["id"], principal_type="group", principal_id=team_id,
                           user_token=admin_token, org_id=org_id)
    assert assigned.status_code == 200, assigned.text
    assert "manage_connections" in _whoami_perms(whoami, cast["alice"]["token"], org_id)
    assert "manage_connections" in _whoami_perms(whoami, cast["bob"]["token"], org_id)

    # ── Steady state: a cycle with no directory changes writes nothing ───
    before = len(app.wire_log)
    report = app.run_cycle()
    assert report.ok, report.errors
    assert [e["method"] for e in app.wire_log[before:] if e["method"] != "GET"] == []

    # ── Directory change: Alice leaves Data Team, the team is renamed ────
    cast["data_team"].members.discard(cast["d_alice"].object_id)
    cast["data_team"].display_name = _uniq("Data Platform")
    report = app.run_cycle()
    assert report.ok, report.errors
    assert report.members_removed == 1 and report.groups_updated == 1

    team = cast["admin_groups"]()[team_id]
    assert team["name"] == cast["data_team"].display_name
    assert set(team["member_user_ids"]) == {bob_id, carol_id}
    assert "manage_connections" not in _whoami_perms(whoami, cast["alice"]["token"], org_id)
    assert "manage_connections" in _whoami_perms(whoami, cast["bob"]["token"], org_id)

    # ── Alice rejoins → access returns ───────────────────────────────────
    cast["data_team"].members.add(cast["d_alice"].object_id)
    report = app.run_cycle()
    assert report.ok, report.errors
    assert report.members_added == 1
    assert "manage_connections" in _whoami_perms(whoami, cast["alice"]["token"], org_id)

    # ── Unassign the group in Entra → DELETE → group and its access gone ─
    app.unassign_group(cast["data_team"])
    report = app.run_cycle()
    assert report.ok, report.errors
    assert report.groups_deleted == 1
    assert team_id not in cast["admin_groups"]()
    assert list_role_assignments(user_token=admin_token, org_id=org_id,
                                 principal_type="group", principal_id=team_id) == []
    assert "manage_connections" not in _whoami_perms(whoami, cast["bob"]["token"], org_id)
    # Still in All Staff → still in scope → never deactivated.
    assert app.user_active[cast["d_bob"].object_id] is True

    # ── A user leaving all assigned groups is soft-deleted (active=false) ─
    cast["all_staff"].members.discard(cast["d_carol"].object_id)
    report = app.run_cycle()
    assert report.ok, report.errors
    assert report.users_disabled == 1
    carol = cast["scim"]("GET", f"/Users/{carol_id}").json()
    assert carol["active"] is False
    staff = cast["admin_groups"]()[app.group_ids[cast["all_staff"].object_id]]
    assert carol_id not in staff["member_user_ids"]


@pytest.mark.e2e
def test_entra_with_lost_escrow_matches_existing_groups_instead_of_duplicating(entra_cast, test_client):
    """A re-created provisioning job (no stored target ids) finds the groups it
    made by displayName and converges on the same rows."""
    cast = entra_cast
    first = _entra(cast, test_client)
    first.assign_group(cast["data_team"])
    assert first.run_cycle().ok

    second = _entra(cast, test_client)
    second.assign_group(cast["data_team"])
    report = second.run_cycle()
    assert report.ok, report.errors
    assert report.groups_matched == 1 and report.groups_created == 0
    assert second.group_ids == first.group_ids

    scim_groups = [g for g in cast["admin_groups"]().values() if g["external_provider"] == "scim"]
    assert len(scim_groups) == 1
    assert len(scim_groups[0]["member_user_ids"]) == 3


@pytest.mark.e2e
def test_entra_recreates_a_group_removed_on_the_target(entra_cast, test_client):
    cast = entra_cast
    app = _entra(cast, test_client)
    app.assign_group(cast["data_team"])
    assert app.run_cycle().ok
    old_id = app.group_ids[cast["data_team"].object_id]

    assert cast["scim"]("DELETE", f"/Groups/{old_id}").status_code == 204
    report = app.run_cycle()
    assert report.ok, report.errors
    new_id = app.group_ids[cast["data_team"].object_id]
    assert new_id != old_id
    assert len(cast["admin_groups"]()[new_id]["member_user_ids"]) == 3


@pytest.mark.e2e
def test_entra_group_named_like_a_manual_group_is_refused_not_adopted(
    entra_cast, test_client, create_group, add_user_to_group,
):
    """An admin's hand-made group is never handed to the IdP: Entra's lookup
    does not see it and its create fails with 409 uniqueness, leaving the
    manual group's members untouched."""
    cast = entra_cast
    manual = create_group(name=cast["data_team"].display_name,
                          user_token=cast["admin_token"], org_id=cast["org_id"])
    assert manual.status_code == 200, manual.text
    manual_id = manual.json()["id"]
    added = add_user_to_group(group_id=manual_id, user_id=cast["alice"]["user_id"],
                              user_token=cast["admin_token"], org_id=cast["org_id"])
    assert added.status_code in (200, 201), added.text

    app = _entra(cast, test_client)
    app.assign_group(cast["data_team"])
    report = app.run_cycle()

    assert [(e.step, e.status) for e in report.errors] == [("CreateGroup", 409)]
    assert report.errors[0].detail["scimType"] == "uniqueness"
    lookup = next(e for e in app.wire_log if e["method"] == "GET" and "/Groups?" in e["path"])
    assert lookup["response"]["totalResults"] == 0

    manual_after = cast["admin_groups"]()[manual_id]
    assert manual_after["external_provider"] is None
    assert manual_after["member_user_ids"] == [cast["alice"]["user_id"]]


@pytest.mark.e2e
def test_entra_legacy_string_boolean_deactivates_user(entra_cast, test_client):
    """Entra apps without aadOptscim062020 send active as the string "False"."""
    cast = entra_cast
    user = cast["scim"]("POST", "/Users", {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": f"legacy_{uuid.uuid4().hex[:8]}@example.com", "active": True,
    }).json()
    for value, expected in (("False", False), ("True", True), ("false", False)):
        resp = cast["scim"]("PATCH", f"/Users/{user['id']}", {
            "schemas": [PATCH_OP], "Operations": [{"op": "Replace", "path": "active", "value": value}],
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["active"] is expected


# ════════════════════════════════════════════════════════════════════════
# 2. Protocol
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def org_with_members(scim_org, invite_user_to_org):
    org = scim_org()
    members = [invite_user_to_org(org_id=org["org_id"], admin_token=org["admin_token"]) for _ in range(3)]
    return {**org, "user_ids": [m["user_id"] for m in members]}


@pytest.mark.e2e
def test_okta_group_push_lifecycle(org_with_members):
    """Okta's shapes: create with members, no-path replace for rename,
    filtered-path remove, lowercase ops, PUT full replace, DELETE."""
    org = org_with_members
    scim = org["scim"]
    u1, u2, u3 = org["user_ids"]

    created = scim("POST", "/Groups", {
        "schemas": [SCIM_GROUP], "displayName": _uniq("Okta Eng"),
        "members": [{"value": u1, "display": "u1"}, {"value": u2, "display": "u2"}],
    })
    assert created.status_code == 201, created.text
    group = created.json()
    assert created.headers["location"] == group["meta"]["location"]
    assert group["meta"]["resourceType"] == "Group"
    assert _member_ids(group) == {u1, u2}
    gid = group["id"]

    new_name = _uniq("Okta Engineering")
    renamed = _patch(scim, gid, {"op": "replace", "value": {"id": gid, "displayName": new_name}})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["displayName"] == new_name

    removed = _patch(scim, gid, {"op": "remove", "path": f'members[value eq "{u1}"]'})
    assert _member_ids(removed.json()) == {u2}

    added = _patch(scim, gid, {"op": "add", "path": "members", "value": [{"value": u3, "display": "u3"}]})
    assert _member_ids(added.json()) == {u2, u3}

    replaced = scim("PUT", f"/Groups/{gid}", {
        "schemas": [SCIM_GROUP], "displayName": new_name, "members": [{"value": u1}],
    })
    assert replaced.status_code == 200, replaced.text
    assert _member_ids(replaced.json()) == {u1}
    assert _member_ids(scim("GET", f"/Groups/{gid}").json()) == {u1}
    assert set(org["admin_groups"]()[gid]["member_user_ids"]) == {u1}

    assert scim("DELETE", f"/Groups/{gid}").status_code == 204
    _assert_scim_error(scim("GET", f"/Groups/{gid}"), 404)
    assert gid not in org["admin_groups"]()


@pytest.mark.e2e
def test_membership_patches_are_idempotent(org_with_members):
    """IdPs retry: re-adding a member or removing an absent one changes nothing."""
    org = org_with_members
    u1, u2, _ = org["user_ids"]
    gid = org["scim"]("POST", "/Groups", {"displayName": _uniq("Retry"), "members": [{"value": u1}]}).json()["id"]

    for _ in range(2):
        r = _patch(org["scim"], gid, {"op": "Add", "path": "members", "value": [{"value": u1}, {"value": u2}]})
        assert r.status_code == 200, r.text
    for _ in range(2):
        r = _patch(org["scim"], gid, {"op": "Remove", "path": "members", "value": [{"value": u2}]})
        assert r.status_code == 200, r.text

    group = org["admin_groups"]()[gid]
    assert group["member_user_ids"] == [u1]
    assert group["member_count"] == 1


@pytest.mark.e2e
def test_patch_is_all_or_nothing(org_with_members):
    """One bad operation rejects the whole PATCH (RFC 7644 §3.5.2)."""
    org = org_with_members
    u1, u2, _ = org["user_ids"]
    name = _uniq("Atomic")
    gid = org["scim"]("POST", "/Groups", {"displayName": name, "members": [{"value": u1}]}).json()["id"]

    resp = _patch(
        org["scim"], gid,
        {"op": "Replace", "path": "displayName", "value": _uniq("Should Not Stick")},
        {"op": "Add", "path": "members", "value": [{"value": u2}, {"value": str(uuid.uuid4())}]},
    )
    _assert_scim_error(resp, 400, "invalidValue")
    after = org["scim"]("GET", f"/Groups/{gid}").json()
    assert after["displayName"] == name
    assert _member_ids(after) == {u1}


@pytest.mark.e2e
@pytest.mark.parametrize("operation,scim_type", [
    ({"op": "Remove", "path": "displayName"}, "mutability"),
    ({"op": "Replace", "path": "nickName", "value": "x"}, "invalidPath"),
    ({"op": "Add", "path": 'members[value eq "x"]', "value": [{"value": "x"}]}, "invalidPath"),
    ({"op": "Move", "path": "displayName", "value": "x"}, "invalidSyntax"),
    ({"op": "Remove"}, "noTarget"),
    ({"op": "Add", "path": "members", "value": [{"display": "no value"}]}, "invalidValue"),
])
def test_invalid_patch_operations_are_scim_400s(org_with_members, operation, scim_type):
    org = org_with_members
    gid = org["scim"]("POST", "/Groups", {"displayName": _uniq("Bad Patch")}).json()["id"]
    _assert_scim_error(_patch(org["scim"], gid, operation), 400, scim_type)


@pytest.mark.e2e
def test_filters(org_with_members):
    org = org_with_members
    scim = org["scim"]
    u1, u2, _ = org["user_ids"]
    quoted = f'R&D "Core" {uuid.uuid4().hex[:4]}'
    a = scim("POST", "/Groups", {"displayName": quoted, "externalId": "ext-A", "members": [{"value": u1}]}).json()
    b = scim("POST", "/Groups", {"displayName": _uniq("Sales"), "externalId": "ext-a", "members": [{"value": u2}]}).json()

    def ids(filter_expr):
        r = scim("GET", "/Groups?filter=" + quote(filter_expr))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["totalResults"] == len(body["Resources"])
        return {g["id"] for g in body["Resources"]}

    escaped = quoted.replace('"', '\\"')
    assert ids(f'displayName eq "{escaped}"') == {a["id"]}
    assert ids(f'displayName eq "{escaped.upper()}"') == {a["id"]}   # caseExact=false
    assert ids('externalId eq "ext-A"') == {a["id"]}                  # caseExact=true
    assert ids('externalId eq "ext-a"') == {b["id"]}
    assert ids(f'id eq "{a["id"]}" and members[value eq "{u1}"]') == {a["id"]}
    assert ids(f'id eq "{a["id"]}" and members[value eq "{u2}"]') == set()
    assert ids(f'members eq "{u2}"') == {b["id"]}
    assert ids('displayName eq "no such group"') == set()

    for unsupported in ('displayName co "Sales"', 'description eq "x"', 'displayName eq Sales',
                        'displayName eq "a" or displayName eq "b"'):
        _assert_scim_error(
            scim("GET", "/Groups?filter=" + quote(unsupported)),
            400, "invalidFilter",
        )


@pytest.mark.e2e
def test_paging_covers_every_group_exactly_once(scim_org):
    org = scim_org()
    created = {
        org["scim"]("POST", "/Groups", {"displayName": _uniq(f"Page {i}")}).json()["id"]
        for i in range(7)
    }
    seen, start = [], 1
    while True:
        page = org["scim"]("GET", f"/Groups?startIndex={start}&count=3").json()
        assert page["totalResults"] == 7
        assert page["itemsPerPage"] == len(page["Resources"])
        if not page["Resources"]:
            break
        seen += [g["id"] for g in page["Resources"]]
        start += len(page["Resources"])
    assert len(seen) == len(set(seen)) == 7
    assert set(seen) == created

    only_count = org["scim"]("GET", "/Groups?count=0").json()
    assert only_count["totalResults"] == 7 and only_count["Resources"] == []


@pytest.mark.e2e
def test_member_attribute_selection(org_with_members):
    org = org_with_members
    u1 = org["user_ids"][0]
    gid = org["scim"]("POST", "/Groups", {"displayName": _uniq("Attrs"), "members": [{"value": u1}]}).json()["id"]

    for query in ("excludedAttributes=members", "attributes=displayName",
                  "excludedAttributes=urn:ietf:params:scim:schemas:core:2.0:Group:members"):
        single = org["scim"]("GET", f"/Groups/{gid}?{query}").json()
        assert "members" not in single and single["displayName"]
        listed = org["scim"]("GET", f"/Groups?{query}").json()["Resources"]
        assert listed and all("members" not in g for g in listed)

    for query in ("", "?attributes=members", "?attributes=displayName,members.value"):
        assert _member_ids(org["scim"]("GET", f"/Groups/{gid}{query}").json()) == {u1}


@pytest.mark.e2e
def test_create_rejections(org_with_members):
    org = org_with_members
    scim = org["scim"]
    name = _uniq("Unique")
    assert scim("POST", "/Groups", {"displayName": name, "externalId": "dup-ext"}).status_code == 201

    _assert_scim_error(scim("POST", "/Groups", {"displayName": name}), 409, "uniqueness")
    _assert_scim_error(scim("POST", "/Groups", {"displayName": _uniq("Other"), "externalId": "dup-ext"}),
                       409, "uniqueness")
    _assert_scim_error(scim("POST", "/Groups", {"schemas": [SCIM_GROUP]}), 400, "invalidValue")
    _assert_scim_error(scim("POST", "/Groups", {"displayName": "   "}), 400, "invalidValue")
    _assert_scim_error(scim("POST", "/Groups", {"displayName": _uniq("Ghost"),
                                                "members": [{"value": str(uuid.uuid4())}]}),
                       400, "invalidValue")
    # Nothing half-created by the rejected requests.
    assert sum(g["external_provider"] == "scim" for g in org["admin_groups"]().values()) == 1


@pytest.mark.e2e
def test_rename_into_a_taken_name_is_a_conflict(scim_org):
    org = scim_org()
    taken = _uniq("Taken")
    org["scim"]("POST", "/Groups", {"displayName": taken})
    gid = org["scim"]("POST", "/Groups", {"displayName": _uniq("Mover")}).json()["id"]
    _assert_scim_error(_patch(org["scim"], gid, {"op": "Replace", "path": "displayName", "value": taken}),
                       409, "uniqueness")
    put = org["scim"]("PUT", f"/Groups/{gid}", {"displayName": taken})
    _assert_scim_error(put, 409, "uniqueness")


@pytest.mark.e2e
def test_names_differing_only_in_case_collide(scim_org, create_group):
    """displayName is caseExact=false: the uniqueness guard matches the filter,
    against manual groups and other SCIM groups alike."""
    org = scim_org()
    manual = _uniq("Engineers")
    assert create_group(name=manual, user_token=org["admin_token"], org_id=org["org_id"]).status_code == 200
    _assert_scim_error(org["scim"]("POST", "/Groups", {"displayName": manual.lower()}), 409, "uniqueness")

    scim_name = _uniq("Platform")
    gid = org["scim"]("POST", "/Groups", {"displayName": scim_name}).json()["id"]
    _assert_scim_error(org["scim"]("POST", "/Groups", {"displayName": scim_name.upper()}), 409, "uniqueness")
    other = org["scim"]("POST", "/Groups", {"displayName": _uniq("Other")}).json()["id"]
    _assert_scim_error(_patch(org["scim"], other, {"op": "Replace", "path": "displayName",
                                                   "value": scim_name.swapcase()}), 409, "uniqueness")

    # Re-casing a group's own name is not a collision with itself.
    recased = _patch(org["scim"], gid, {"op": "Replace", "path": "displayName", "value": scim_name.upper()})
    assert recased.status_code == 200, recased.text
    assert recased.json()["displayName"] == scim_name.upper()


@pytest.mark.e2e
@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_membership_only_change_advances_last_modified(org_with_members, method):
    org = org_with_members
    u1, u2, _ = org["user_ids"]
    name = _uniq("Stamped")
    created = org["scim"]("POST", "/Groups", {"displayName": name, "members": [{"value": u1}]}).json()

    if method == "PUT":
        resp = org["scim"]("PUT", f"/Groups/{created['id']}",
                           {"displayName": name, "members": [{"value": u1}, {"value": u2}]})
    else:
        resp = _patch(org["scim"], created["id"], {"op": "Add", "path": "members", "value": [{"value": u2}]})
    assert resp.status_code == 200, resp.text
    assert _member_ids(resp.json()) == {u1, u2}
    assert resp.json()["meta"]["lastModified"] > created["meta"]["lastModified"]


@pytest.mark.e2e
def test_discovery_advertises_groups_matching_what_the_endpoint_returns(org_with_members):
    org = org_with_members
    scim = org["scim"]
    types = scim("GET", "/ResourceTypes").json()
    group_type = next(t for t in types if t["id"] == "Group")
    assert group_type["endpoint"] == "/scim/v2/Groups"
    assert group_type["schema"] == SCIM_GROUP

    schema = next(s for s in scim("GET", "/Schemas").json() if s["id"] == SCIM_GROUP)
    advertised = {a["name"] for a in schema["attributes"]}
    member_sub = {a["name"] for a in next(a for a in schema["attributes"] if a["name"] == "members")["subAttributes"]}

    group = scim("POST", "/Groups", {"displayName": _uniq("Disco"), "externalId": "x",
                                     "members": [{"value": org["user_ids"][0]}]}).json()
    returned = set(group) - {"schemas", "id", "meta"}
    assert returned == advertised
    assert set(group["members"][0]) == member_sub


@pytest.mark.e2e
def test_errors_on_the_scim_surface_use_the_scim_error_schema(scim_org, test_client):
    org = scim_org()
    _assert_scim_error(org["scim"]("GET", f"/Groups/{uuid.uuid4()}"), 404)
    _assert_scim_error(org["scim"]("GET", f"/Users/{uuid.uuid4()}"), 404)
    _assert_scim_error(test_client.get("/scim/v2/Groups", headers={"Authorization": "Bearer bow_scim_nope"}), 401)
    _assert_scim_error(test_client.get("/scim/v2/Groups"), 401)
    bad_json = test_client.post("/scim/v2/Groups", content="{not json",
                                headers={"Authorization": f"Bearer {org['scim_token']}",
                                         "Content-Type": "application/scim+json"})
    _assert_scim_error(bad_json, 400)


# ════════════════════════════════════════════════════════════════════════
# 3. Isolation & ownership
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
def test_one_org_token_cannot_reach_another_orgs_groups_or_users(scim_org, invite_user_to_org):
    org_a, org_b = scim_org(), scim_org()
    b_user = invite_user_to_org(org_id=org_b["org_id"], admin_token=org_b["admin_token"])
    b_group = org_b["scim"]("POST", "/Groups", {"displayName": _uniq("B only"),
                                                "members": [{"value": b_user["user_id"]}]}).json()

    a = org_a["scim"]
    assert b_group["id"] not in {g["id"] for g in a("GET", "/Groups").json()["Resources"]}
    _assert_scim_error(a("GET", f"/Groups/{b_group['id']}"), 404)
    _assert_scim_error(_patch(a, b_group["id"], {"op": "Add", "path": "members", "value": [{"value": "x"}]}), 404)
    _assert_scim_error(a("PUT", f"/Groups/{b_group['id']}", {"displayName": "hijack"}), 404)
    _assert_scim_error(a("DELETE", f"/Groups/{b_group['id']}"), 404)
    # A user of org B cannot be pulled into an org A group.
    _assert_scim_error(a("POST", "/Groups", {"displayName": _uniq("A"),
                                             "members": [{"value": b_user["user_id"]}]}), 400, "invalidValue")

    after = org_b["scim"]("GET", f"/Groups/{b_group['id']}").json()
    assert after["displayName"] == b_group["displayName"]
    assert _member_ids(after) == {b_user["user_id"]}


@pytest.mark.e2e
def test_scim_surface_never_sees_or_touches_groups_it_does_not_own(
    org_with_members, create_group, add_user_to_group,
):
    org = org_with_members
    manual = create_group(name=_uniq("Manual"), user_token=org["admin_token"], org_id=org["org_id"]).json()
    add_user_to_group(group_id=manual["id"], user_id=org["user_ids"][0],
                      user_token=org["admin_token"], org_id=org["org_id"])

    scim = org["scim"]
    assert manual["id"] not in {g["id"] for g in scim("GET", "/Groups").json()["Resources"]}
    assert scim("GET", "/Groups?filter=" + quote(
        f'displayName eq "{manual["name"]}"')).json()["totalResults"] == 0
    _assert_scim_error(scim("GET", f"/Groups/{manual['id']}"), 404)
    _assert_scim_error(_patch(scim, manual["id"], {"op": "Remove", "path": "members"}), 404)
    _assert_scim_error(scim("DELETE", f"/Groups/{manual['id']}"), 404)

    still = org["admin_groups"]()[manual["id"]]
    assert still["member_user_ids"] == [org["user_ids"][0]]


@pytest.mark.e2e
def test_admin_api_cannot_edit_a_group_the_idp_owns(
    org_with_members, test_client, create_group, create_role, assign_role,
):
    """Name/members/deletion belong to the IdP; roles on the group stay with the admin."""
    org = org_with_members
    org_id, token = org["org_id"], org["admin_token"]
    u1, u2, _ = org["user_ids"]
    gid = org["scim"]("POST", "/Groups", {"displayName": _uniq("Owned"), "members": [{"value": u1}]}).json()["id"]
    base = f"/api/organizations/{org_id}/groups/{gid}"

    attempts = [
        test_client.put(base, json={"name": "renamed by hand"}, headers=_hdr(token, org_id)),
        test_client.post(f"{base}/members", json={"user_id": u2}, headers=_hdr(token, org_id)),
        test_client.delete(f"{base}/members/{u1}", headers=_hdr(token, org_id)),
        test_client.delete(base, headers=_hdr(token, org_id)),
    ]
    for resp in attempts:
        assert resp.status_code == 409, resp.text
        assert resp.json()["error_code"] == "group.managed_by_scim"
    group = org["admin_groups"]()[gid]
    assert group["member_user_ids"] == [u1]

    role = create_role(name=_uniq("r"), permissions=["view_members"], user_token=token, org_id=org_id).json()
    assert assign_role(role_id=role["id"], principal_type="group", principal_id=gid,
                       user_token=token, org_id=org_id).status_code == 200

    # Control: the same calls still work on a hand-made group.
    manual = create_group(name=_uniq("Manual"), user_token=token, org_id=org_id).json()
    assert test_client.post(f"/api/organizations/{org_id}/groups/{manual['id']}/members",
                            json={"user_id": u2}, headers=_hdr(token, org_id)).status_code == 201
    assert test_client.delete(f"/api/organizations/{org_id}/groups/{manual['id']}",
                              headers=_hdr(token, org_id)).status_code == 204


@pytest.mark.e2e
def test_scim_delete_removes_everything_that_points_at_the_group(
    org_with_members, test_client, create_role, assign_role, grant_resource, list_role_assignments,
    create_report, set_visibility, get_shares,
):
    """Role assignments, resource grants, quota assignments and report shares on
    the group go with it — report_shares has a real FK, so on Postgres a leftover
    share would make the DELETE fail outright."""
    org = org_with_members
    org_id, token = org["org_id"], org["admin_token"]
    gid = org["scim"]("POST", "/Groups", {"displayName": _uniq("Doomed"),
                                          "members": [{"value": org["user_ids"][0]}]}).json()["id"]

    role = create_role(name=_uniq("r"), permissions=["view_members"], user_token=token, org_id=org_id).json()
    assert assign_role(role_id=role["id"], principal_type="group", principal_id=gid,
                       user_token=token, org_id=org_id).status_code == 200
    grant = grant_resource(resource_type="data_source", resource_id=str(uuid.uuid4()),
                           principal_type="group", principal_id=gid, permissions=["query"],
                           user_token=token, org_id=org_id)
    assert grant.status_code == 200, grant.text
    report = create_report(title="Shared with IdP group", user_token=token, org_id=org_id, data_sources=[])
    set_visibility(report["id"], "artifact", "shared", user_token=token, org_id=org_id,
                   shared_user_ids=[], shared_group_ids=[gid])
    assert any(s.get("group_id") == gid for s in get_shares(report["id"], "artifact",
                                                             user_token=token, org_id=org_id))
    policy = test_client.post(f"/api/organizations/{org_id}/usage-policies",
                              json={"name": _uniq("Quota"), "monthly_token_limit": 1000, "enabled": True},
                              headers=_hdr(token, org_id))
    assert policy.status_code == 200, policy.text
    quota = test_client.put(f"/api/organizations/{org_id}/usage-policy-assignments/principal",
                            json={"principal_type": "group", "principal_id": gid,
                                  "policy_id": policy.json()["id"]},
                            headers=_hdr(token, org_id))
    assert quota.status_code == 200, quota.text

    def quota_principals():
        policies = test_client.get(f"/api/organizations/{org_id}/usage-policies", headers=_hdr(token, org_id))
        assert policies.status_code == 200, policies.text
        return {a["principal_id"] for p in policies.json() for a in p.get("assignments", [])}

    assert gid in quota_principals()

    assert org["scim"]("DELETE", f"/Groups/{gid}").status_code == 204

    assert gid not in org["admin_groups"]()
    assert list_role_assignments(user_token=token, org_id=org_id, principal_type="group", principal_id=gid) == []
    grants = test_client.get(f"/api/organizations/{org_id}/resource-grants", headers=_hdr(token, org_id)).json()
    assert all(g["principal_id"] != gid for g in grants)
    assert all(s.get("group_id") != gid for s in get_shares(report["id"], "artifact",
                                                             user_token=token, org_id=org_id))
    assert gid not in quota_principals()
