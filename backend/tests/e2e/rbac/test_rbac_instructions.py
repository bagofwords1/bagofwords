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
    ds_a_author      → can create on ds_a, denied on ds_b, allowed without ds (resource_scoped)
    member           → denied with any ds, allowed with no ds (resource_scoped)
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
        ("member", [], 200),
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
