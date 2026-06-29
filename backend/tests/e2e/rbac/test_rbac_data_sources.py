"""
RBAC end-to-end coverage for /api/data_sources.

The pytest fixture infrastructure runs full alembic up/down migrations
between every test, so each test costs ~30 s of fixed overhead. To stay
fast we collapse what would otherwise be a parametrized matrix into a
small number of high-density tests that exercise all principals + all
actions in one shot.

Principals built per test:

    admin            — full_admin_access (bootstrap owner)
    ds_a_manager     — direct user resource-grant on ds_a
    ds_b_manager     — direct user resource-grant on ds_b
    member_no_grants — invited member, no grants at all
    outsider         — admin of a completely separate org
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


# ────────────────────────────────────────────────────────────────────
# Shared world (one org, two DSes, matrix of principals)
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def ds_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    # The sqlite_data_source fixture defaults to is_public=False, asserting
    # the flip succeeds — access here therefore requires explicit grants.
    ds_a = sqlite_data_source(name="ds_a", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name="ds_b", user_token=admin["token"], org_id=org_id)

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_a_manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_b_manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grant_a = grant_resource(
        resource_type="data_source",
        resource_id=ds_a["id"],
        principal_type="user",
        principal_id=ds_a_manager["user_id"],
        permissions=["manage_instructions", "manage"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert grant_a.status_code == 200, grant_a.json()

    grant_b = grant_resource(
        resource_type="data_source",
        resource_id=ds_b["id"],
        principal_type="user",
        principal_id=ds_b_manager["user_id"],
        permissions=["manage_instructions", "manage"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert grant_b.status_code == 200, grant_b.json()

    outsider = bootstrap_admin("outsider")

    return {
        "org_id": org_id,
        "ds_a": ds_a,
        "ds_b": ds_b,
        "principals": {
            "admin": admin,
            "member_no_grants": member,
            "ds_a_manager": ds_a_manager,
            "ds_b_manager": ds_b_manager,
            "outsider": outsider,
        },
    }


# ────────────────────────────────────────────────────────────────────
# Detail GET / update / list — one big test, many assertions
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_data_source_access_matrix(test_client, ds_world):
    """End-to-end matrix: every principal × every action × every DS in one go."""
    org_id = ds_world["org_id"]
    ds_a_id = ds_world["ds_a"]["id"]
    ds_b_id = ds_world["ds_b"]["id"]

    # Expected: principal -> {detail_a, detail_b, put_a, put_b}
    expected = {
        "admin":            {"detail_a": 200, "detail_b": 200, "put_a": 200, "put_b": 200},
        "ds_a_manager":     {"detail_a": 200, "detail_b": 403, "put_a": 200, "put_b": 403},
        "ds_b_manager":     {"detail_a": 403, "detail_b": 200, "put_a": 403, "put_b": 200},
        "member_no_grants": {"detail_a": 403, "detail_b": 403, "put_a": 403, "put_b": 403},
        # outsider runs against the wrong org → org-membership check denies first.
        "outsider":         {"detail_a": 403, "detail_b": 403, "put_a": 403, "put_b": 403},
    }

    failures = []
    for name, want in expected.items():
        p = ds_world["principals"][name]

        for ds_label, ds_id in (("a", ds_a_id), ("b", ds_b_id)):
            # GET /data_sources/{id}
            got = test_client.get(
                f"/api/data_sources/{ds_id}",
                headers=_hdr(p["token"], org_id),
            )
            key = f"detail_{ds_label}"
            if got.status_code != want[key]:
                failures.append(f"{name} GET ds_{ds_label}: want {want[key]} got {got.status_code}")

            # PUT /data_sources/{id}
            got = test_client.put(
                f"/api/data_sources/{ds_id}",
                json={"description": f"rename-by-{name}"},
                headers=_hdr(p["token"], org_id),
            )
            key = f"put_{ds_label}"
            if got.status_code != want[key]:
                failures.append(f"{name} PUT ds_{ds_label}: want {want[key]} got {got.status_code}")

    assert not failures, "\n".join(failures)


@pytest.mark.e2e
def test_data_source_list_basic_filter_and_invariant(test_client, ds_world):
    """List filtering + forward list/detail invariant.

    Specifically:
      - admin sees both DSes because they CREATED them (creator-as-member
        rule). The list endpoint does NOT bypass for full_admin_access —
        admins only see DSes they explicitly belong to. See
        ``test_admin_does_not_auto_see_other_admins_data_source`` for the
        non-creator admin case.
      - member_no_grants sees nothing
      - every DS that a principal does see in the list must open via GET /{id}

    The dual direction — per-DS grantees must see *their* DS in the list —
    is covered by ``test_data_source_grant_appears_in_list`` below.
    """
    org_id = ds_world["org_id"]
    ds_a_id = ds_world["ds_a"]["id"]
    ds_b_id = ds_world["ds_b"]["id"]

    failures = []
    for name in ("admin", "member_no_grants"):
        p = ds_world["principals"][name]
        list_resp = test_client.get("/api/data_sources", headers=_hdr(p["token"], org_id))
        if list_resp.status_code != 200:
            failures.append(f"{name} list returned {list_resp.status_code}")
            continue
        got_ids = {d["id"] for d in list_resp.json()}

        if name == "admin":
            # Admin created both DSes → creator-as-member auto-write → visible.
            for ds_id in (ds_a_id, ds_b_id):
                if ds_id not in got_ids:
                    failures.append(f"admin list missing: {ds_id}")
        elif name == "member_no_grants":
            for ds_id in (ds_a_id, ds_b_id):
                if ds_id in got_ids:
                    failures.append(f"member_no_grants list leaks: {ds_id}")

        # Forward invariant: every DS exposed in the list MUST open in detail.
        for ds_id in got_ids:
            detail = test_client.get(
                f"/api/data_sources/{ds_id}",
                headers=_hdr(p["token"], org_id),
            )
            if detail.status_code != 200:
                failures.append(
                    f"{name}: listed {ds_id} but GET returned {detail.status_code}"
                )

    assert not failures, "\n".join(failures)


@pytest.mark.e2e
def test_data_source_grant_appears_in_list(test_client, ds_world):
    """A ResourceGrant on a DS must make it visible in the holder's list response.

    This used to be xfail'd against the previous revision of the service
    layer which filtered /data_sources by the legacy DataSourceMembership
    table only. The fix landed in the latest ``rbac improvements`` commit
    (data_source_service now delegates to ``get_accessible_data_source_ids``
    which unions ResourceGrant with DataSourceMembership), and this test
    is the regression guard against it regressing again.

    ds_a_manager sees ds_a only (never ds_b).
    ds_b_manager sees ds_b only (never ds_a).
    """
    org_id = ds_world["org_id"]
    ds_a_id = ds_world["ds_a"]["id"]
    ds_b_id = ds_world["ds_b"]["id"]

    expectations = {
        "ds_a_manager": {"must_see": {ds_a_id}, "must_not_see": {ds_b_id}},
        "ds_b_manager": {"must_see": {ds_b_id}, "must_not_see": {ds_a_id}},
    }

    failures = []
    for name, want in expectations.items():
        p = ds_world["principals"][name]
        list_resp = test_client.get("/api/data_sources", headers=_hdr(p["token"], org_id))
        if list_resp.status_code != 200:
            failures.append(f"{name}: list returned {list_resp.status_code}")
            continue
        got_ids = {d["id"] for d in list_resp.json()}
        missing = want["must_see"] - got_ids
        leaked = want["must_not_see"] & got_ids
        if missing:
            failures.append(f"{name}: list missing {missing}")
        if leaked:
            failures.append(f"{name}: list leaked {leaked}")

    assert not failures, "\n".join(failures)


@pytest.mark.e2e
def test_admin_does_not_auto_see_other_admins_data_source(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source
):
    """A second admin who didn't create the DS and wasn't added to it
    should NOT see it in the default list — only the creator does. This
    is the noise-reduction guarantee: full_admin_access grants capability
    (direct GET still works), not list membership.
    """
    creator = bootstrap_admin("creator")
    org_id = creator["org_id"]

    ds = sqlite_data_source(name="creator_ds", user_token=creator["token"], org_id=org_id)
    ds_id = ds["id"]

    second_admin = invite_user_to_org(
        org_id=org_id, admin_token=creator["token"], role="admin"
    )

    # The default list MUST NOT include a DS the admin neither created
    # nor was added to.
    list_resp = test_client.get(
        "/api/data_sources", headers=_hdr(second_admin["token"], org_id)
    )
    assert list_resp.status_code == 200, list_resp.json()
    assert ds_id not in {d["id"] for d in list_resp.json()}, (
        f"second admin should not auto-see {ds_id}; got {list_resp.json()}"
    )

    # Capability bypass is preserved: direct GET still succeeds for admins.
    detail = test_client.get(
        f"/api/data_sources/{ds_id}", headers=_hdr(second_admin["token"], org_id)
    )
    assert detail.status_code == 200, detail.json()


@pytest.mark.e2e
def test_admin_show_all_reveals_private_data_source(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source
):
    """show_all=true lets a full admin see a private DS they neither created
    nor joined, flagged admin_only=true. A plain member's show_all is ignored
    (no org-wide governance capability), and the admin's default list is
    still scoped (admin_only never set without the toggle).
    """
    creator = bootstrap_admin("creator")
    org_id = creator["org_id"]

    ds = sqlite_data_source(name="creator_ds", user_token=creator["token"], org_id=org_id)
    ds_id = ds["id"]

    second_admin = invite_user_to_org(
        org_id=org_id, admin_token=creator["token"], role="admin"
    )
    member = invite_user_to_org(org_id=org_id, admin_token=creator["token"])

    # Default list (no toggle) still hides it from the second admin.
    default_resp = test_client.get(
        "/api/data_sources", headers=_hdr(second_admin["token"], org_id)
    )
    assert default_resp.status_code == 200, default_resp.json()
    assert ds_id not in {d["id"] for d in default_resp.json()}

    # show_all=true reveals it for the admin, flagged admin_only.
    show_all_resp = test_client.get(
        "/api/data_sources?show_all=true", headers=_hdr(second_admin["token"], org_id)
    )
    assert show_all_resp.status_code == 200, show_all_resp.json()
    revealed = {d["id"]: d for d in show_all_resp.json()}
    assert ds_id in revealed, f"show_all should reveal {ds_id}; got {revealed.keys()}"
    assert revealed[ds_id]["admin_only"] is True

    # A plain member without governance capability gets the toggle ignored.
    member_resp = test_client.get(
        "/api/data_sources?show_all=true", headers=_hdr(member["token"], org_id)
    )
    assert member_resp.status_code == 200, member_resp.json()
    assert ds_id not in {d["id"] for d in member_resp.json()}


# ────────────────────────────────────────────────────────────────────
# Public DS visibility
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_public_data_source_visibility(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source
):
    """is_public DSes are readable by every member but writes still need manage."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    ds = sqlite_data_source(
        name="public_ds",
        user_token=admin["token"],
        org_id=org_id,
        is_public=True,
    )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    list_resp = test_client.get("/api/data_sources", headers=_hdr(member["token"], org_id))
    assert list_resp.status_code == 200
    assert ds["id"] in [d["id"] for d in list_resp.json()]

    detail = test_client.get(
        f"/api/data_sources/{ds['id']}",
        headers=_hdr(member["token"], org_id),
    )
    assert detail.status_code == 200, detail.text

    put_resp = test_client.put(
        f"/api/data_sources/{ds['id']}",
        json={"description": "hijack"},
        headers=_hdr(member["token"], org_id),
    )
    assert put_resp.status_code == 403, put_resp.text


# ────────────────────────────────────────────────────────────────────
# Table-selection edits require `manage` (not the read-tier `view_schema`)
# ────────────────────────────────────────────────────────────────────


def _table_mutation_calls(client, ds_id, org_id, token):
    """Hit each table-mutation endpoint; return {endpoint: status_code}."""
    h = _hdr(token, org_id)
    return {
        "update_schema": client.put(
            f"/api/data_sources/{ds_id}/update_schema", json=[], headers=h
        ).status_code,
        "bulk_update_tables": client.post(
            f"/api/data_sources/{ds_id}/bulk_update_tables",
            json={"action": "deactivate", "filter": {}},
            headers=h,
        ).status_code,
        "update_tables_status": client.put(
            f"/api/data_sources/{ds_id}/update_tables_status",
            json={"activate": [], "deactivate": []},
            headers=h,
        ).status_code,
    }


@pytest.mark.e2e
def test_table_edits_on_private_ds_require_manage(test_client, ds_world):
    """On a private agent, only `manage` holders may edit table selection.

    Previously these endpoints were gated on the read-tier `view_schema`,
    so anyone with *any* grant (e.g. a view-only member) could mutate the
    table set. They now require `manage`, matching name/status/member edits.

    - admin (full_admin)            → allowed (not 403)
    - ds_a_manager (manage on ds_a) → allowed on ds_a, denied on ds_b
    - member_no_grants              → denied
    """
    org_id = ds_world["org_id"]
    ds_a_id = ds_world["ds_a"]["id"]

    admin = ds_world["principals"]["admin"]
    manager = ds_world["principals"]["ds_a_manager"]
    member = ds_world["principals"]["member_no_grants"]

    # Manager + admin can edit ds_a's tables (manage grant / full_admin).
    for name, p in (("admin", admin), ("ds_a_manager", manager)):
        for endpoint, code in _table_mutation_calls(test_client, ds_a_id, org_id, p["token"]).items():
            assert code != 403, f"{name} unexpectedly denied on {endpoint}: {code}"

    # A member with no grant is denied on every table-mutation endpoint.
    for endpoint, code in _table_mutation_calls(test_client, ds_a_id, org_id, member["token"]).items():
        assert code == 403, f"member_no_grants should be denied on {endpoint}; got {code}"

    # The ds_a manager has no grant on ds_b → denied there too.
    ds_b_id = ds_world["ds_b"]["id"]
    for endpoint, code in _table_mutation_calls(test_client, ds_b_id, org_id, manager["token"]).items():
        assert code == 403, f"ds_a_manager should be denied on ds_b {endpoint}; got {code}"


@pytest.mark.e2e
def test_table_edits_on_public_ds_require_manage(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source
):
    """Public agents are readable by every member, but editing the table
    selection still requires `manage` — a plain member is denied even though
    the agent is public (the old `view_schema` gate let them through).
    """
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    ds = sqlite_data_source(
        name="public_tables_ds", user_token=admin["token"], org_id=org_id, is_public=True
    )
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    # Member can read the schema (view tier) but cannot mutate it.
    read = test_client.get(
        f"/api/data_sources/{ds['id']}/full_schema", headers=_hdr(member["token"], org_id)
    )
    assert read.status_code == 200, read.text

    for endpoint, code in _table_mutation_calls(test_client, ds["id"], org_id, member["token"]).items():
        assert code == 403, f"public-DS member should be denied on {endpoint}; got {code}"

    # The creator (manage grant) can still edit.
    for endpoint, code in _table_mutation_calls(test_client, ds["id"], org_id, admin["token"]).items():
        assert code != 403, f"admin unexpectedly denied on {endpoint}: {code}"


# ────────────────────────────────────────────────────────────────────
# Org-isolation: detail of *their own* org's DS via wrong org header
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_outsider_cannot_see_other_orgs_data_source(test_client, ds_world):
    """An admin of org B cannot read org A's DS even with their valid token."""
    outsider = ds_world["principals"]["outsider"]

    # Sending the foreign org_id header should be denied (membership check).
    resp = test_client.get(
        f"/api/data_sources/{ds_world['ds_a']['id']}",
        headers=_hdr(outsider["token"], ds_world["org_id"]),
    )
    assert resp.status_code in (403, 404), resp.text

    # And listing under the foreign org header is also denied.
    list_resp = test_client.get(
        "/api/data_sources",
        headers=_hdr(outsider["token"], ds_world["org_id"]),
    )
    assert list_resp.status_code != 200 or all(
        d["id"] not in (ds_world["ds_a"]["id"], ds_world["ds_b"]["id"])
        for d in list_resp.json()
    )


# ────────────────────────────────────────────────────────────────────
# Demo install: creator gets the RBAC manage grant, not just membership
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_demo_installer_gets_manage_grant(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    create_role,
    assign_role,
    install_demo_data_source,
):
    """A non-admin who installs a demo data source must receive the RBAC
    `manage` grant — not just the legacy DataSourceMembership — so they can
    actually manage the data source they created.

    Regression: the demo-install path used to hand-roll only the membership
    row, leaving a non-admin installer able to see the demo in their list
    yet failing every `manage`-gated action on it (e.g. PUT). Admins never
    hit this because full_admin_access bypasses resource checks, so we test
    with a plain member holding only create_data_source.
    """
    admin = bootstrap_admin()
    org_id = admin["org_id"]

    # A member who may create data sources but is NOT an org admin.
    role_resp = create_role(
        name="ds-creator",
        permissions=["create_data_source"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert role_resp.status_code == 200, role_resp.json()
    role_id = role_resp.json()["id"]

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    assign_resp = assign_role(
        role_id=role_id,
        principal_type="user",
        principal_id=member["user_id"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert assign_resp.status_code in (200, 201), assign_resp.json()

    # Member installs the demo (gated by create_data_source).
    result = install_demo_data_source(
        demo_id="chinook", user_token=member["token"], org_id=org_id
    )
    assert result["success"] is True, result
    ds_id = result["data_source_id"]

    # The installer must be able to perform a `manage`-gated update on it.
    # Without the manage grant this returns 403.
    put_resp = test_client.put(
        f"/api/data_sources/{ds_id}",
        json={"description": "updated by installer"},
        headers=_hdr(member["token"], org_id),
    )
    assert put_resp.status_code == 200, (
        f"installer should hold manage on the demo they created; got "
        f"{put_resp.status_code}: {put_resp.text}"
    )


# ────────────────────────────────────────────────────────────────────
# publish_status (publishing lifecycle) — visibility + validation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_publish_status_visibility_and_validation(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
):
    """publish_status gates who sees an agent, independent of access grants.

    - published → visible to every principal with access (default)
    - draft     → visible only to managers (full_admin / per-DS manage)
    - disabled  → never appears in the selector (/data_sources/active)
    """
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    # Public so a plain member would otherwise see it — isolates the
    # publish_status filter from the access/membership filter.
    ds = sqlite_data_source(
        name="published_agent", user_token=admin["token"], org_id=org_id, is_public=True
    )
    ds_id = ds["id"]

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant = grant_resource(
        resource_type="data_source",
        resource_id=ds_id,
        principal_type="user",
        principal_id=manager["user_id"],
        permissions=["manage"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert grant.status_code == 200, grant.json()

    def active_ids(token):
        r = test_client.get(
            "/api/data_sources/active?include_unconnected=true",
            headers=_hdr(token, org_id),
        )
        assert r.status_code == 200, r.text
        return {d["id"] for d in r.json()}

    def detail(token):
        return test_client.get(f"/api/data_sources/{ds_id}", headers=_hdr(token, org_id))

    # Default is published, exposed on the read schema.
    assert detail(admin["token"]).json()["publish_status"] == "published"

    # published → everyone with access sees it.
    assert ds_id in active_ids(admin["token"])
    assert ds_id in active_ids(manager["token"])
    assert ds_id in active_ids(member["token"])

    # Flip to draft (manage-gated). Member can't; admin can.
    assert (
        test_client.put(
            f"/api/data_sources/{ds_id}",
            json={"publish_status": "draft"},
            headers=_hdr(member["token"], org_id),
        ).status_code == 403
    )
    put = test_client.put(
        f"/api/data_sources/{ds_id}",
        json={"publish_status": "draft"},
        headers=_hdr(admin["token"], org_id),
    )
    assert put.status_code == 200, put.text
    assert put.json()["publish_status"] == "draft"

    # draft → only managers (admin + per-DS manage) see it; member does not.
    assert ds_id in active_ids(admin["token"])
    assert ds_id in active_ids(manager["token"])
    assert ds_id not in active_ids(member["token"])

    # disabled → hidden from the normal selector for everyone, even admin.
    assert (
        test_client.put(
            f"/api/data_sources/{ds_id}",
            json={"publish_status": "disabled"},
            headers=_hdr(admin["token"], org_id),
        ).status_code == 200
    )
    assert ds_id not in active_ids(admin["token"])
    assert ds_id not in active_ids(manager["token"])
    assert ds_id not in active_ids(member["token"])

    # ...but the admin "show all" view surfaces disabled agents so a manager
    # can find and re-enable them. A plain member's show_all is still ignored.
    def active_ids_show_all(token):
        r = test_client.get(
            "/api/data_sources/active?include_unconnected=true&show_all=true",
            headers=_hdr(token, org_id),
        )
        assert r.status_code == 200, r.text
        return {d["id"] for d in r.json()}

    assert ds_id in active_ids_show_all(admin["token"])
    assert ds_id not in active_ids_show_all(member["token"])

    # Invalid value is rejected by the schema validator.
    bad = test_client.put(
        f"/api/data_sources/{ds_id}",
        json={"publish_status": "bogus"},
        headers=_hdr(admin["token"], org_id),
    )
    assert bad.status_code == 422, bad.text
