"""
RBAC end-to-end coverage for /api/instructions.

Covers:
  - Per-DS ``manage_instructions`` grantee can create + edit instructions
    on their DS, but not on someone else's.
  - Org-level manage_instructions wildcard works.
  - Member with no grants can still POST /instructions (the route is
    resource_scoped) but only if they pass no data_source_ids; otherwise
    ``check_resource_permissions`` denies them.
  - The list endpoint filters owners' visibility.
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def ins_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    # sqlite_data_source defaults to is_public=False, asserts the flip.
    ds_a = sqlite_data_source(name="ins_ds_a", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name="ins_ds_b", user_token=admin["token"], org_id=org_id)

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_a_author = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    # Per-DS manage_instructions grant for ds_a_author on ds_a only.
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

    return {
        "org_id": org_id,
        "ds_a": ds_a,
        "ds_b": ds_b,
        "principals": {
            "admin": admin,
            "member": member,
            "ds_a_author": ds_a_author,
        },
    }


# ────────────────────────────────────────────────────────────────────
# Create
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_create_instruction_matrix(test_client, ins_world):
    """Validate the create matrix in one shot.

    admin            → can create on ds_a, ds_b, and global (no ds)
    ds_a_author      → can create on ds_a, denied on ds_b, allowed without ds (has grant)
    member           → denied everywhere (no manage_instructions grant at all)
    """
    org_id = ins_world["org_id"]
    ds_a_id = ins_world["ds_a"]["id"]
    ds_b_id = ins_world["ds_b"]["id"]

    cases = [
        # (principal, data_source_ids, expected_status)
        ("admin", [ds_a_id], 200),
        ("admin", [ds_b_id], 200),
        ("admin", [], 200),
        ("ds_a_author", [ds_a_id], 200),
        ("ds_a_author", [ds_b_id], 403),
        ("ds_a_author", [], 200),
        ("member", [ds_a_id], 403),
        ("member", [ds_b_id], 403),
        ("member", [], 403),
    ]

    failures = []
    for principal, ds_ids, want in cases:
        p = ins_world["principals"][principal]
        body = {
            "text": f"{principal} writes about ds={ds_ids}",
            "status": "draft",
            "category": "general",
            "data_source_ids": ds_ids,
        }
        resp = test_client.post(
            "/api/instructions",
            json=body,
            headers=_hdr(p["token"], org_id),
        )
        if resp.status_code != want:
            failures.append(f"{principal} ds={ds_ids}: want {want} got {resp.status_code} ({resp.text[:120]})")

    assert not failures, "\n".join(failures)


# ────────────────────────────────────────────────────────────────────
# Edit / delete by owner vs admin vs other
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_edit_and_delete_permission_layering(test_client, ins_world):
    """Cover the four edit branches in permissions_decorator + service:

      1. Admin can edit any instruction
      2. Owner can edit their own draft instruction
      3. Non-owner non-admin cannot edit someone else's instruction
      4. Per-DS grantee can edit instructions tied to their DS
    """
    org_id = ins_world["org_id"]
    ds_a_id = ins_world["ds_a"]["id"]

    admin = ins_world["principals"]["admin"]
    author = ins_world["principals"]["ds_a_author"]
    member = ins_world["principals"]["member"]

    # Admin creates a global instruction (no DS attachment)
    admin_inst = test_client.post(
        "/api/instructions",
        json={"text": "admin global", "status": "draft", "category": "general", "data_source_ids": []},
        headers=_hdr(admin["token"], org_id),
    )
    assert admin_inst.status_code == 200, admin_inst.text
    admin_inst_id = admin_inst.json()["id"]

    # ds_a_author creates an instruction tied to ds_a
    author_inst = test_client.post(
        "/api/instructions",
        json={"text": "author writes ds_a", "status": "draft", "category": "general", "data_source_ids": [ds_a_id]},
        headers=_hdr(author["token"], org_id),
    )
    assert author_inst.status_code == 200, author_inst.text
    author_inst_id = author_inst.json()["id"]

    # 1. Admin can edit author's instruction
    r = test_client.put(
        f"/api/instructions/{author_inst_id}",
        json={"text": "admin edits author"},
        headers=_hdr(admin["token"], org_id),
    )
    assert r.status_code == 200, r.text

    # 2. ds_a_author can edit their own instruction (owner_edit branch)
    r = test_client.put(
        f"/api/instructions/{author_inst_id}",
        json={"text": "author self-edits"},
        headers=_hdr(author["token"], org_id),
    )
    assert r.status_code == 200, r.text

    # 3. Member (no manage_instructions, not owner, no DS grant) cannot
    #    edit author's instruction.
    r = test_client.put(
        f"/api/instructions/{author_inst_id}",
        json={"text": "hijack"},
        headers=_hdr(member["token"], org_id),
    )
    assert r.status_code == 403, r.text

    # 4. Author cannot edit admin's global instruction (no DS to grant on,
    #    not the owner, not an admin).
    r = test_client.put(
        f"/api/instructions/{admin_inst_id}",
        json={"text": "author hijack admin"},
        headers=_hdr(author["token"], org_id),
    )
    assert r.status_code == 403, r.text

    # 5. Admin can delete the author instruction
    r = test_client.delete(
        f"/api/instructions/{author_inst_id}",
        headers=_hdr(admin["token"], org_id),
    )
    assert r.status_code == 200, r.text


# ────────────────────────────────────────────────────────────────────
# List visibility — service filters per-permissions internally
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_instructions_list_filters_by_visibility(test_client, ins_world):
    """Members never get to see another user's draft instruction."""
    org_id = ins_world["org_id"]
    ds_a_id = ins_world["ds_a"]["id"]

    admin = ins_world["principals"]["admin"]
    member = ins_world["principals"]["member"]
    author = ins_world["principals"]["ds_a_author"]

    # Author creates a draft on ds_a — visible only to themselves until published
    author_inst = test_client.post(
        "/api/instructions",
        json={"text": "draft on ds_a", "status": "draft", "category": "general", "data_source_ids": [ds_a_id]},
        headers=_hdr(author["token"], org_id),
    )
    assert author_inst.status_code == 200, author_inst.text
    author_inst_id = author_inst.json()["id"]

    # Member listing — must NOT see author's draft.
    list_resp = test_client.get("/api/instructions", headers=_hdr(member["token"], org_id))
    assert list_resp.status_code == 200, list_resp.text
    body = list_resp.json()
    items = body["items"] if isinstance(body, dict) and "items" in body else body
    member_seen_ids = {i["id"] for i in items}
    assert author_inst_id not in member_seen_ids

    # Admin listing — sees author's draft when explicitly requesting drafts.
    # (The default list endpoint serves the main build only; non-admin
    # instructions live in a pending_approval build until approved.)
    list_resp_admin = test_client.get(
        "/api/instructions",
        params={"include_drafts": "true"},
        headers=_hdr(admin["token"], org_id),
    )
    assert list_resp_admin.status_code == 200
    body_admin = list_resp_admin.json()
    admin_items = body_admin["items"] if isinstance(body_admin, dict) and "items" in body_admin else body_admin
    admin_seen_ids = {i["id"] for i in admin_items}
    assert author_inst_id in admin_seen_ids


@pytest.mark.e2e
def test_instructions_list_filters_by_data_source_access(
    test_client, ins_world, sqlite_data_source
):
    """A member must not see published instructions tied to a data source they
    cannot access.

    Visibility rules for a non-admin member:
      - global instructions (no data source)        → visible
      - instructions on a public data source        → visible
      - instructions on a private DS they're not in → hidden
      - instructions on a private DS they ARE in    → visible (per-DS grantee)
    """
    org_id = ins_world["org_id"]
    ds_a_id = ins_world["ds_a"]["id"]  # private; ds_a_author has a grant on it

    admin = ins_world["principals"]["admin"]
    member = ins_world["principals"]["member"]
    author = ins_world["principals"]["ds_a_author"]

    # A public data source that every member can access.
    ds_pub = sqlite_data_source(
        name="ins_ds_public", user_token=admin["token"], org_id=org_id, is_public=True
    )

    def _create(data_source_ids):
        resp = test_client.post(
            "/api/instructions",
            json={
                "text": f"published on ds={data_source_ids}",
                "status": "published",
                "category": "general",
                "data_source_ids": data_source_ids,
            },
            headers=_hdr(admin["token"], org_id),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    # Admin (auto-promoted to main) publishes across the three scopes.
    global_id = _create([])
    public_id = _create([ds_pub["id"]])
    private_a_id = _create([ds_a_id])

    # Member: sees global + public, never the private-ds_a instruction.
    member_list = test_client.get("/api/instructions", headers=_hdr(member["token"], org_id))
    assert member_list.status_code == 200, member_list.text
    member_body = member_list.json()
    member_items = member_body["items"] if isinstance(member_body, dict) and "items" in member_body else member_body
    member_ids = {i["id"] for i in member_items}
    assert global_id in member_ids
    assert public_id in member_ids
    assert private_a_id not in member_ids

    # ds_a_author: has a grant on ds_a, so additionally sees the ds_a instruction.
    author_list = test_client.get("/api/instructions", headers=_hdr(author["token"], org_id))
    assert author_list.status_code == 200, author_list.text
    author_body = author_list.json()
    author_items = author_body["items"] if isinstance(author_body, dict) and "items" in author_body else author_body
    author_ids = {i["id"] for i in author_items}
    assert global_id in author_ids
    assert public_id in author_ids
    assert private_a_id in author_ids


@pytest.mark.e2e
def test_admin_does_not_see_instructions_for_unjoined_data_source(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source
):
    """Even a full org admin only sees instructions for data sources they are an
    explicit member of — mirroring the default data-sources list. An admin must
    NOT see instructions tied to a private DS they never joined; global
    instructions stay visible to everyone.
    """
    admin1 = bootstrap_admin("admin")
    org_id = admin1["org_id"]
    # Second full admin who will own a data source admin1 never joins.
    admin2 = invite_user_to_org(org_id=org_id, admin_token=admin1["token"], role="admin")

    ds_private = sqlite_data_source(name="adm2_ds", user_token=admin2["token"], org_id=org_id)

    def _post(token, body):
        r = test_client.post("/api/instructions", json=body, headers=_hdr(token, org_id))
        assert r.status_code == 200, r.text
        return r.json()["id"]

    priv_id = _post(admin2["token"], {
        "text": "adm2 private inst", "status": "published",
        "category": "general", "data_source_ids": [ds_private["id"]],
    })
    global_id = _post(admin2["token"], {
        "text": "adm2 global inst", "status": "published",
        "category": "general", "data_source_ids": [],
    })

    # admin1 is a full admin but is NOT a member of ds_private.
    resp = test_client.get("/api/instructions", headers=_hdr(admin1["token"], org_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"] if isinstance(body, dict) and "items" in body else body
    seen = {i["id"] for i in items}
    assert priv_id not in seen, \
        "admin must NOT see instructions for a private data source they never joined"
    assert global_id in seen, "global instructions remain visible to everyone"

    # And it must not be reachable by id either — the detail modal, version
    # history, and pending-builds endpoints all gate on view access, so an admin
    # can't open an instruction for an agent they never joined.
    assert test_client.get(
        f"/api/instructions/{priv_id}", headers=_hdr(admin1["token"], org_id)
    ).status_code == 404
    assert test_client.get(
        f"/api/instructions/{priv_id}/versions", headers=_hdr(admin1["token"], org_id)
    ).status_code == 404
    # Global instruction stays openable by id.
    assert test_client.get(
        f"/api/instructions/{global_id}", headers=_hdr(admin1["token"], org_id)
    ).status_code == 200
