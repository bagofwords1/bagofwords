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

    ds_a = sqlite_data_source(name="ds_a", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name="ds_b", user_token=admin["token"], org_id=org_id)

    # Force both private so access requires explicit grants.
    for ds in (ds_a, ds_b):
        test_client.put(
            f"/api/data_sources/{ds['id']}",
            json={"is_public": False},
            headers=_hdr(admin["token"], org_id),
        )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_a_manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_b_manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grant_a = grant_resource(
        resource_type="data_source",
        resource_id=ds_a["id"],
        principal_type="user",
        principal_id=ds_a_manager["user_id"],
        permissions=["view", "view_schema", "create_instructions", "manage"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert grant_a.status_code == 200, grant_a.json()

    grant_b = grant_resource(
        resource_type="data_source",
        resource_id=ds_b["id"],
        principal_type="user",
        principal_id=ds_b_manager["user_id"],
        permissions=["view", "view_schema", "create_instructions", "manage"],
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
    """List filtering + list/detail invariant for the cases that *do* work today.

    Specifically:
      - admin (auto-added to DataSourceMembership when creating each DS) sees both
      - member_no_grants sees nothing
      - every DS that a principal *does* see in the list must open via GET /{id}
        (the inverse direction of the invariant — exposed IDs must be reachable)

    The dual to this test — a user with only a ResourceGrant should also
    appear in the list — is captured separately in the xfail'd test below.
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
            # Admin auto-gets DataSourceMembership rows on create, so should see both.
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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known bug surfaced by these tests: data_source_service.get_data_sources "
        "filters the list by the legacy DataSourceMembership table only — it "
        "ignores ResourceGrant rows. As a result, a user with a per-DS RBAC "
        "grant but no DataSourceMembership can open the DS in detail (which "
        "uses the resolver path) but never sees it in the list. The reverse of "
        "the list/detail invariant is broken. See test_data_source_access_matrix "
        "above for the detail-side coverage."
    ),
)
def test_data_source_grant_appears_in_list(test_client, ds_world):
    """A ResourceGrant on a DS should make it visible in the user's list response."""
    org_id = ds_world["org_id"]
    ds_a_id = ds_world["ds_a"]["id"]
    ds_b_id = ds_world["ds_b"]["id"]

    expected = {
        "ds_a_manager": ds_a_id,
        "ds_b_manager": ds_b_id,
    }

    for name, must_see in expected.items():
        p = ds_world["principals"][name]
        list_resp = test_client.get("/api/data_sources", headers=_hdr(p["token"], org_id))
        assert list_resp.status_code == 200, list_resp.text
        got_ids = {d["id"] for d in list_resp.json()}
        assert must_see in got_ids, f"{name}: {must_see!r} missing from list {sorted(got_ids)}"


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
    ds = sqlite_data_source(name="public_ds", user_token=admin["token"], org_id=org_id)
    test_client.put(
        f"/api/data_sources/{ds['id']}",
        json={"is_public": True},
        headers=_hdr(admin["token"], org_id),
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
