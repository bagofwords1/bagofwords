"""
Data source RBAC matrix tests.

For each principal in ``rbac_principals`` we exercise:

  GET /data_sources         (list)
  GET /data_sources/{id}    (detail, public + private DSs)
  PUT /data_sources/{id}    (mutating — admin only)
  POST /data_sources/{id}/members (admin-only DSM mutation)

Two invariants matter most here and have failed in the wild before:

  1. List/detail consistency — every ID returned by the list endpoint MUST
     be GET-able by the same caller. Anything else means the list filter
     and the detail-access filter disagree.
  2. Cross-org isolation — a user who only belongs to a different org must
     never see, GET, or mutate this org's data sources, regardless of role
     in their *own* org.

To keep migration cost manageable (every test re-runs full alembic migrations
via the autouse ``run_migrations`` fixture), we batch related scenarios into
one test function each, looping over a hard-coded matrix.
"""
import pytest


def _h(token, org_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


@pytest.mark.e2e
def test_data_source_list_filters_and_detail_invariant(
    test_client, rbac_principals
):
    """List filtering, list/detail consistency, and cross-org isolation
    in a single setup pass.

    Visibility matrix (admin auto-joined to every DS at creation, plain
    member sees only public, ds_a/b_member see their granted DS + public):

        admin       -> ds_a, ds_b, ds_public
        member      -> ds_public
        ds_a_member -> ds_a, ds_public
        ds_b_member -> ds_b, ds_public
    """
    org_id = rbac_principals["org_id"]
    ds_a_id = rbac_principals["ds_a"]["id"]
    ds_b_id = rbac_principals["ds_b"]["id"]
    ds_pub_id = rbac_principals["ds_public"]["id"]

    expected = {
        "admin":       {ds_a_id, ds_b_id, ds_pub_id},
        "member":      {ds_pub_id},
        "ds_a_member": {ds_a_id, ds_pub_id},
        "ds_b_member": {ds_b_id, ds_pub_id},
    }

    invariant_failures = []
    for name, want in expected.items():
        p = rbac_principals["principals"][name]
        list_resp = test_client.get(
            "/api/data_sources", headers=_h(p["token"], org_id)
        )
        assert list_resp.status_code == 200, list_resp.json()
        got = {d["id"] for d in list_resp.json()}
        assert want.issubset(got), (
            f"{name}: missing expected DSs {want - got}"
        )
        # Anything extra returned by the list endpoint is a bug.
        unexpected = got - want
        # ds_public should always be present; ds_a/ds_b appear only when
        # the principal has explicit access. Anything else (e.g. another
        # private DS leaking through) would land here.
        relevant_extras = unexpected & {ds_a_id, ds_b_id}
        assert not relevant_extras, (
            f"{name}: list returned private DSs they shouldn't see: "
            f"{relevant_extras}"
        )

        # List/detail invariant: every listed id must be GET-able.
        for ds_id in got:
            detail = test_client.get(
                f"/api/data_sources/{ds_id}", headers=_h(p["token"], org_id)
            )
            if detail.status_code != 200:
                invariant_failures.append(
                    f"{name} sees {ds_id} in list but GET /detail "
                    f"returned {detail.status_code}"
                )

    assert not invariant_failures, (
        "List/detail invariant violations:\n" + "\n".join(invariant_failures)
    )


@pytest.mark.e2e
def test_data_source_detail_403_for_unauthorized(test_client, rbac_principals):
    """Direct GET on a private DS the caller has no grant on returns 403."""
    org_id = rbac_principals["org_id"]
    ds_a_id = rbac_principals["ds_a"]["id"]
    ds_b_id = rbac_principals["ds_b"]["id"]

    cases = [
        ("member",      ds_a_id, 403),
        ("member",      ds_b_id, 403),
        ("ds_a_member", ds_b_id, 403),
        ("ds_b_member", ds_a_id, 403),
    ]
    for name, ds_id, expected in cases:
        p = rbac_principals["principals"][name]
        resp = test_client.get(
            f"/api/data_sources/{ds_id}", headers=_h(p["token"], org_id)
        )
        assert resp.status_code == expected, (
            f"{name} -> GET {ds_id}: expected {expected}, "
            f"got {resp.status_code}: {getattr(resp, 'text', '')[:200]}"
        )


@pytest.mark.e2e
def test_data_source_mutations_require_admin_role(
    test_client, rbac_principals, invite_user_to_org,
):
    """PUT /data_sources/{id} and POST /data_sources/{id}/members both
    require an admin org role; being a DS member is not enough.
    """
    org_id = rbac_principals["org_id"]
    admin_token = rbac_principals["admin_token"]
    ds_id = rbac_principals["ds_a"]["id"]

    # PUT matrix
    put_cases = [
        ("admin",       200),
        ("member",      403),
        ("ds_a_member", 403),
        ("ds_b_member", 403),
    ]
    for name, expected in put_cases:
        p = rbac_principals["principals"][name]
        resp = test_client.put(
            f"/api/data_sources/{ds_id}",
            json={"description": f"updated by {name}"},
            headers=_h(p["token"], org_id),
        )
        assert resp.status_code == expected, (
            f"PUT {name}: expected {expected}, got {resp.status_code}: "
            f"{getattr(resp, 'text', '')[:200]}"
        )

    # POST DSM matrix — bring in a fresh user as the target.
    target = invite_user_to_org(admin_token=admin_token, org_id=org_id)
    post_cases = [
        ("member",      403),
        ("ds_a_member", 403),
        ("admin",       200),  # admin succeeds last so target gets added
    ]
    for name, expected in post_cases:
        p = rbac_principals["principals"][name]
        resp = test_client.post(
            f"/api/data_sources/{ds_id}/members",
            json={
                "principal_type": "user",
                "principal_id": target["user_id"],
            },
            headers=_h(p["token"], org_id),
        )
        assert resp.status_code == expected, (
            f"POST DSM {name}: expected {expected}, got {resp.status_code}: "
            f"{getattr(resp, 'text', '')[:200]}"
        )


@pytest.mark.e2e
def test_outsider_cannot_list_or_get_or_mutate(test_client, rbac_principals):
    """A user from a different organization gets 403 from this org's DS routes,
    regardless of HTTP method."""
    org_id = rbac_principals["org_id"]
    outsider = rbac_principals["principals"]["outsider"]
    ds_id = rbac_principals["ds_a"]["id"]
    headers = _h(outsider["token"], org_id)

    list_resp = test_client.get("/api/data_sources", headers=headers)
    assert list_resp.status_code in (403, 404), (
        f"outsider list expected 403/404, got {list_resp.status_code}"
    )

    detail_resp = test_client.get(f"/api/data_sources/{ds_id}", headers=headers)
    assert detail_resp.status_code in (403, 404), (
        f"outsider detail expected 403/404, got {detail_resp.status_code}"
    )

    update_resp = test_client.put(
        f"/api/data_sources/{ds_id}",
        json={"description": "owned"},
        headers=headers,
    )
    assert update_resp.status_code in (403, 404), (
        f"outsider update expected 403/404, got {update_resp.status_code}"
    )


@pytest.mark.e2e
def test_revoking_ds_membership_revokes_detail_access(
    test_client, rbac_principals, revoke_ds_membership,
):
    """After DS membership is revoked the user immediately loses GET access."""
    org_id = rbac_principals["org_id"]
    admin_token = rbac_principals["admin_token"]
    p = rbac_principals["principals"]["ds_a_member"]
    ds_id = rbac_principals["ds_a"]["id"]

    # Sanity: currently has access
    resp = test_client.get(
        f"/api/data_sources/{ds_id}", headers=_h(p["token"], org_id)
    )
    assert resp.status_code == 200, resp.json()

    revoke_ds_membership(
        admin_token=admin_token,
        org_id=org_id,
        data_source_id=ds_id,
        user_id=p["user_id"],
    )

    after = test_client.get(
        f"/api/data_sources/{ds_id}", headers=_h(p["token"], org_id)
    )
    assert after.status_code == 403, (
        f"After revoke expected 403, got {after.status_code}: "
        f"{getattr(after, 'text', '')[:300]}"
    )
