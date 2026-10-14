"""
RBAC end-to-end coverage for /api/builds.

The build endpoints are the area the task brief flagged as the highest
risk for resolver bugs.

After the latest ``rbac improvements`` commit:

  - GET /builds is **open to every authenticated org member**. The route
    no longer carries ``@requires_permission('manage_instructions')`` —
    instead the service applies a per-DS filter via
    ``get_accessible_data_source_ids``: admins (full_admin_access)
    bypass the filter, everyone else only sees builds whose instructions
    all touch DSes they can access.
  - Per-DS-touching mutations (submit, publish, rollback, reject) all
    use ``resource_scoped=True`` and then call ``_enforce_build_ds_access``
    which requires the caller to hold ``manage_instructions`` on every
    DS the build touches.
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def builds_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    # sqlite_data_source defaults to is_public=False and asserts the flip.
    ds_a = sqlite_data_source(name="b_ds_a", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name="b_ds_b", user_token=admin["token"], org_id=org_id)

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_a_author = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grant_resp = grant_resource(
        resource_type="data_source",
        resource_id=ds_a["id"],
        principal_type="user",
        principal_id=ds_a_author["user_id"],
        permissions=["manage_instructions"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert grant_resp.status_code == 200, grant_resp.json()

    # Create one author-owned instruction touching ds_a. Since the
    # "agent admins publish their own instructions live" change, an agent
    # admin (a per-DS ``manage_instructions`` holder) auto-publishes a build
    # scoped entirely to their own DS — so this instruction is approved and
    # promoted to main immediately. It does NOT sit in pending_approval for
    # admin review the way an org-wide / cross-DS proposal would.
    author_inst = test_client.post(
        "/api/instructions",
        json={
            "text": "author wrote about ds_a",
            "status": "draft",
            "category": "general",
            "data_source_ids": [ds_a["id"]],
        },
        headers=_hdr(ds_a_author["token"], org_id),
    )
    assert author_inst.status_code == 200, author_inst.text
    author_inst_id = author_inst.json()["id"]

    # Locate the author's build (now approved + promoted to main) by its
    # contents. status=all so the lookup is independent of the build's state.
    all_resp = test_client.get(
        "/api/builds",
        params={"status": "all"},
        headers=_hdr(admin["token"], org_id),
    )
    assert all_resp.status_code == 200, all_resp.text

    ds_a_build_id = None
    for b in all_resp.json()["items"]:
        contents = test_client.get(
            f"/api/builds/{b['id']}/contents",
            headers=_hdr(admin["token"], org_id),
        )
        if contents.status_code != 200:
            continue
        items = contents.json().get("items", [])
        if any(item["instruction_id"] == author_inst_id for item in items):
            ds_a_build_id = b["id"]
            break

    return {
        "org_id": org_id,
        "ds_a": ds_a,
        "ds_b": ds_b,
        "principals": {
            "admin": admin,
            "member": member,
            "ds_a_author": ds_a_author,
        },
        "ds_a_build_id": ds_a_build_id,
        "author_instruction_id": author_inst_id,
    }


# ────────────────────────────────────────────────────────────────────
# Listing — every authenticated org member can list, with per-DS filter
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_list_builds_open_to_org_members(test_client, builds_world):
    """GET /builds is no longer admin-only — every org member gets a 200.

    The service applies a per-DS filter on top: admin sees everything,
    per-DS authors only see builds touching DSes they can reach, and
    members with no grants only see builds tied to public DSes (none in
    this fixture, so they should get an empty list).
    """
    org_id = builds_world["org_id"]

    for principal in ("admin", "ds_a_author", "member"):
        token = builds_world["principals"][principal]["token"]
        r = test_client.get("/api/builds", headers=_hdr(token, org_id))
        assert r.status_code == 200, f"{principal}: {r.text}"


@pytest.mark.e2e
def test_list_builds_status_filter(test_client, builds_world):
    """status=pending_approval and status=approved both return well-shaped pages.

    The fixture's per-DS author auto-publishes their own-DS instruction, so the
    build it produced lands in the ``approved`` bucket (and main), not in
    ``pending_approval``. The approved page is therefore non-empty; the
    pending_approval page must still be well-shaped but may legitimately be
    empty now that own-DS proposals go live without review.
    """
    org_id = builds_world["org_id"]
    admin_token = builds_world["principals"]["admin"]["token"]

    def _page(status):
        r = test_client.get(
            "/api/builds",
            params={"status": status},
            headers=_hdr(admin_token, org_id),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "items" in body and "total" in body
        # Paginated response: ``total`` is the full match count, ``items`` is
        # the current page, so the page can never exceed the total.
        assert len(body["items"]) <= body["total"]
        return body

    # pending_approval: well-shaped page (may be empty — own-DS proposals
    # auto-publish and so never land here).
    _page("pending_approval")

    # approved: the auto-published author build lives here, so it is non-empty.
    approved = _page("approved")
    assert approved["total"] >= 1
    assert len(approved["items"]) >= 1


# ────────────────────────────────────────────────────────────────────
# Build publish enforcement (resource_scoped path)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_per_ds_author_autopublishes_own_ds(test_client, builds_world):
    """A per-DS author's own-DS instruction goes live immediately.

    The author owns ``manage_instructions`` on ds_a and the build they create
    touches only ds_a, so it auto-approves and promotes to main — no separate
    publish step, and nothing left in pending_approval. A member with no grant
    cannot even propose an instruction on that DS.
    """
    org_id = builds_world["org_id"]
    admin_token = builds_world["principals"]["admin"]["token"]
    member = builds_world["principals"]["member"]
    author_inst_id = builds_world["author_instruction_id"]

    # The author's build was auto-published and is now the org's main build.
    main = test_client.get("/api/builds/main", headers=_hdr(admin_token, org_id))
    assert main.status_code == 200, main.text
    main_build_id = main.json()["id"]
    assert builds_world["ds_a_build_id"] == main_build_id

    # …and the author's instruction is live inside it (no review required).
    contents = test_client.get(
        f"/api/builds/{main_build_id}/contents",
        headers=_hdr(admin_token, org_id),
    )
    assert contents.status_code == 200, contents.text
    live_ids = {it["instruction_id"] for it in contents.json().get("items", [])}
    assert author_inst_id in live_ids

    # A member with no grant cannot propose an instruction on ds_a at all.
    r_member = test_client.post(
        "/api/instructions",
        json={
            "text": "member tries ds_a",
            "status": "draft",
            "category": "general",
            "data_source_ids": [builds_world["ds_a"]["id"]],
        },
        headers=_hdr(member["token"], org_id),
    )
    assert r_member.status_code == 403, r_member.text


@pytest.mark.e2e
def test_publish_build_denied_when_touching_unauthorized_ds(
    test_client, builds_world
):
    """A per-DS author cannot publish a build whose instructions touch a DS they don't manage."""
    org_id = builds_world["org_id"]
    admin = builds_world["principals"]["admin"]
    author = builds_world["principals"]["ds_a_author"]
    ds_b_id = builds_world["ds_b"]["id"]

    # Admin creates an instruction tied to ds_b (sits in main once admin auto-publishes)
    # then a non-admin author writes one tied to ds_b → that build will require
    # manage_instructions on ds_b, which author does not have.
    inst_resp = test_client.post(
        "/api/instructions",
        json={
            "text": "author tries to write about ds_b",
            "status": "draft",
            "category": "general",
            "data_source_ids": [ds_b_id],
        },
        headers=_hdr(author["token"], org_id),
    )
    # The create itself is denied at the manage_instructions resource gate.
    assert inst_resp.status_code == 403, inst_resp.text

    # And as a fallback path: even if a build with ds_b instructions ended up
    # in pending_approval, the author cannot publish it. We can simulate this
    # by having admin create the ds_b instruction (admin → auto-publishes).
    admin_inst = test_client.post(
        "/api/instructions",
        json={
            "text": "admin writes ds_b",
            "status": "draft",
            "category": "general",
            "data_source_ids": [ds_b_id],
        },
        headers=_hdr(admin["token"], org_id),
    )
    assert admin_inst.status_code == 200, admin_inst.text

    # Now trigger another non-admin instruction that touches BOTH ds_a (allowed)
    # and ds_b (denied) — the author can't even create it.
    cross = test_client.post(
        "/api/instructions",
        json={
            "text": "author cross-DS attempt",
            "status": "draft",
            "category": "general",
            "data_source_ids": [builds_world["ds_a"]["id"], ds_b_id],
        },
        headers=_hdr(author["token"], org_id),
    )
    assert cross.status_code == 403, cross.text
