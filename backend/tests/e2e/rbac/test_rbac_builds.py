"""
Build RBAC tests.

Permission map for builds in this branch:

    GET /builds              view_builds          (member + admin)
    GET /builds/main         view_instructions    (member + admin)
    GET /builds/{id}         view_builds          (member + admin)
    GET /builds/{id}/contents view_builds         (member + admin)
    GET /builds/{id}/diff    view_builds          (member + admin)
    GET /builds/{id}/diff/details view_instructions (member + admin)
    POST /builds             create_instructions  (admin only)
    POST /builds/{id}/submit create_builds        (admin only)
    POST /builds/{id}/reject create_builds        (admin only)
    POST /builds/{id}/rollback create_builds      (admin only)
    POST /builds/{id}/publish create_builds       (admin only)

The decisive bug class here is "list shows builds the user can't open" — a
view_builds caller hitting GET /builds should always see builds GET-able
by GET /builds/{id}. Cross-org isolation matters too.
"""
import pytest


def _h(token, org_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


@pytest.mark.e2e
def test_member_can_view_builds_but_not_create_or_rollback(
    test_client, rbac_principals,
):
    """Members can list/get builds; create/rollback/publish require admin."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]
    member = rbac_principals["principals"]["member"]

    # Admin creates a build (POST /builds requires create_instructions)
    create_resp = test_client.post(
        "/api/builds",
        json={"source": "manual"},
        headers=_h(admin["token"], org_id),
    )
    assert create_resp.status_code == 200, create_resp.json()
    build_id = create_resp.json()["id"]

    # Member can list and get builds
    list_resp = test_client.get(
        "/api/builds?status=all", headers=_h(member["token"], org_id)
    )
    assert list_resp.status_code == 200, list_resp.json()

    detail_resp = test_client.get(
        f"/api/builds/{build_id}", headers=_h(member["token"], org_id)
    )
    assert detail_resp.status_code == 200, detail_resp.json()

    contents_resp = test_client.get(
        f"/api/builds/{build_id}/contents", headers=_h(member["token"], org_id)
    )
    assert contents_resp.status_code == 200, contents_resp.json()

    # Member cannot CREATE a build (requires create_instructions, admin only)
    member_create = test_client.post(
        "/api/builds",
        json={"source": "manual"},
        headers=_h(member["token"], org_id),
    )
    assert member_create.status_code == 403, member_create.json()

    # Member cannot rollback / publish (requires create_builds, admin only)
    rollback_resp = test_client.post(
        f"/api/builds/{build_id}/rollback", headers=_h(member["token"], org_id)
    )
    assert rollback_resp.status_code == 403, rollback_resp.json()

    publish_resp = test_client.post(
        f"/api/builds/{build_id}/publish",
        json={},
        headers=_h(member["token"], org_id),
    )
    assert publish_resp.status_code == 403, publish_resp.json()


@pytest.mark.e2e
def test_build_list_detail_invariant(test_client, rbac_principals):
    """Every build returned by GET /builds must be GET-able for that caller."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]

    # Seed a couple of builds
    for _ in range(2):
        r = test_client.post(
            "/api/builds",
            json={"source": "manual"},
            headers=_h(admin["token"], org_id),
        )
        assert r.status_code == 200, r.json()

    failures = []
    for name in ("admin", "member", "ds_a_member", "ds_b_member"):
        p = rbac_principals["principals"][name]
        list_resp = test_client.get(
            "/api/builds?status=all", headers=_h(p["token"], org_id)
        )
        assert list_resp.status_code == 200, (
            f"{name} list builds: {list_resp.status_code}"
        )
        items = list_resp.json().get("items", [])
        for b in items:
            d = test_client.get(
                f"/api/builds/{b['id']}", headers=_h(p["token"], org_id)
            )
            if d.status_code != 200:
                failures.append(
                    f"{name} sees build {b['id']} in list but GET returned {d.status_code}"
                )

    assert not failures, "Build list/detail invariant violations:\n" + "\n".join(
        failures
    )


@pytest.mark.e2e
def test_outsider_cannot_access_builds(test_client, rbac_principals):
    """Outsider gets 403 listing/creating builds in this org."""
    org_id = rbac_principals["org_id"]
    outsider = rbac_principals["principals"]["outsider"]
    admin = rbac_principals["principals"]["admin"]
    headers = _h(outsider["token"], org_id)

    # Seed a build via admin so the outsider has a real id to attempt
    create = test_client.post(
        "/api/builds",
        json={"source": "manual"},
        headers=_h(admin["token"], org_id),
    )
    assert create.status_code == 200, create.json()
    build_id = create.json()["id"]

    list_resp = test_client.get("/api/builds?status=all", headers=headers)
    assert list_resp.status_code in (403, 404), list_resp.status_code

    detail_resp = test_client.get(f"/api/builds/{build_id}", headers=headers)
    assert detail_resp.status_code in (403, 404), detail_resp.status_code

    rollback = test_client.post(
        f"/api/builds/{build_id}/rollback", headers=headers
    )
    assert rollback.status_code in (403, 404), rollback.status_code


@pytest.mark.e2e
def test_build_belongs_to_org_check(
    test_client, rbac_principals,
):
    """A build id from another org should not be GET-able even if the
    user is a valid admin in their own org. The route enforces
    organization match in addition to permission."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]
    outsider = rbac_principals["principals"]["outsider"]
    outsider_org = outsider["own_org_id"]

    # Build in primary org
    primary_build = test_client.post(
        "/api/builds",
        json={"source": "manual"},
        headers=_h(admin["token"], org_id),
    )
    assert primary_build.status_code == 200, primary_build.json()
    primary_build_id = primary_build.json()["id"]

    # Outsider tries to GET that build through THEIR org header — should
    # 403 (build does not belong to this organization) or 404.
    resp = test_client.get(
        f"/api/builds/{primary_build_id}",
        headers=_h(outsider["token"], outsider_org),
    )
    assert resp.status_code in (403, 404), (
        f"cross-org build access: expected 403/404, got {resp.status_code}: "
        f"{getattr(resp, 'text', '')[:300]}"
    )
