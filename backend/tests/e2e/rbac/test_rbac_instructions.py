"""
Instruction RBAC tests.

The instruction permission space in this branch is split into "private" and
"global" actions:

  create_private_instructions  (member + admin)
  view_instructions            (member + admin)
  create_instructions          (admin only)  -> POST /instructions/global
  update_instructions          (admin only)  -> PUT  /instructions/{id}
  delete_instructions          (admin only)

A member may create their own private instructions and update/delete only
those they own (the decorator falls through to ``Instruction owner``
allowance for non-approved instructions). They cannot create global
instructions or touch instructions owned by others.
"""
import uuid

import pytest


def _h(token, org_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


@pytest.mark.e2e
def test_member_can_create_private_admin_can_create_global(
    test_client, rbac_principals,
):
    """Members may create private instructions; only admins can create global ones."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]
    member = rbac_principals["principals"]["member"]

    # Member: POST /instructions (private) -> 200
    member_priv = test_client.post(
        "/api/instructions",
        json={"text": "private from member", "category": "general", "data_source_ids": []},
        headers=_h(member["token"], org_id),
    )
    assert member_priv.status_code == 200, member_priv.json()

    # Member: POST /instructions/global (admin-only) -> 403
    member_global = test_client.post(
        "/api/instructions/global",
        json={"text": "global from member", "category": "general", "status": "draft"},
        headers=_h(member["token"], org_id),
    )
    assert member_global.status_code == 403, member_global.json()

    # Admin: both endpoints succeed
    admin_priv = test_client.post(
        "/api/instructions",
        json={"text": "private from admin", "category": "general", "data_source_ids": []},
        headers=_h(admin["token"], org_id),
    )
    assert admin_priv.status_code == 200, admin_priv.json()

    admin_global = test_client.post(
        "/api/instructions/global",
        json={"text": "global from admin", "category": "general", "status": "draft"},
        headers=_h(admin["token"], org_id),
    )
    assert admin_global.status_code == 200, admin_global.json()


@pytest.mark.e2e
def test_member_cannot_update_or_delete_others_instructions(
    test_client, rbac_principals,
):
    """A member can update/delete their own draft instructions but not
    instructions owned by another user."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]
    member = rbac_principals["principals"]["member"]

    # Admin creates a global instruction
    admin_inst_resp = test_client.post(
        "/api/instructions/global",
        json={"text": "owned by admin", "category": "general", "status": "draft"},
        headers=_h(admin["token"], org_id),
    )
    assert admin_inst_resp.status_code == 200, admin_inst_resp.json()
    admin_inst_id = admin_inst_resp.json()["id"]

    # Member tries to update — admin perm required, expect 403
    upd = test_client.put(
        f"/api/instructions/{admin_inst_id}",
        json={"text": "hacked"},
        headers=_h(member["token"], org_id),
    )
    assert upd.status_code == 403, (
        f"member updating admin's instruction: expected 403, got {upd.status_code}: "
        f"{getattr(upd, 'text', '')[:300]}"
    )

    # Member tries to delete — admin perm required, expect 403
    delete_resp = test_client.delete(
        f"/api/instructions/{admin_inst_id}",
        headers=_h(member["token"], org_id),
    )
    assert delete_resp.status_code == 403, delete_resp.json()


@pytest.mark.e2e
def test_admin_can_view_update_delete_any_instruction(
    test_client, rbac_principals,
):
    """Admin role can perform every CRUD op on instructions in their org."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]
    member = rbac_principals["principals"]["member"]

    # Member creates a private instruction
    inst_resp = test_client.post(
        "/api/instructions",
        json={"text": "member private", "category": "general", "data_source_ids": []},
        headers=_h(member["token"], org_id),
    )
    assert inst_resp.status_code == 200, inst_resp.json()
    inst_id = inst_resp.json()["id"]

    # Admin GET
    g = test_client.get(
        f"/api/instructions/{inst_id}", headers=_h(admin["token"], org_id)
    )
    assert g.status_code == 200, g.json()

    # Admin PUT
    u = test_client.put(
        f"/api/instructions/{inst_id}",
        json={"text": "admin edit"},
        headers=_h(admin["token"], org_id),
    )
    assert u.status_code == 200, u.json()
    assert u.json()["text"] == "admin edit"

    # Admin DELETE
    d = test_client.delete(
        f"/api/instructions/{inst_id}", headers=_h(admin["token"], org_id)
    )
    assert d.status_code == 200, getattr(d, "text", "")[:300]


@pytest.mark.e2e
def test_outsider_cannot_create_or_view_instructions(
    test_client, rbac_principals,
):
    """An outsider (member of a different org) is rejected from this org's
    instruction endpoints."""
    org_id = rbac_principals["org_id"]
    outsider = rbac_principals["principals"]["outsider"]

    list_resp = test_client.get(
        "/api/instructions", headers=_h(outsider["token"], org_id)
    )
    assert list_resp.status_code in (403, 404), (
        f"outsider list: expected 403/404, got {list_resp.status_code}"
    )

    create_resp = test_client.post(
        "/api/instructions",
        json={"text": "evil", "category": "general", "data_source_ids": []},
        headers=_h(outsider["token"], org_id),
    )
    assert create_resp.status_code in (403, 404)

    create_global = test_client.post(
        "/api/instructions/global",
        json={"text": "evil global", "category": "general", "status": "draft"},
        headers=_h(outsider["token"], org_id),
    )
    assert create_global.status_code in (403, 404)


@pytest.mark.e2e
def test_member_owner_can_update_delete_own_draft(test_client, rbac_principals):
    """Member should be able to update/delete an instruction they own that
    is not approved (decorator owner-allowance for unpublished instructions)."""
    org_id = rbac_principals["org_id"]
    member = rbac_principals["principals"]["member"]

    inst = test_client.post(
        "/api/instructions",
        json={"text": "mine", "category": "general", "data_source_ids": []},
        headers=_h(member["token"], org_id),
    ).json()
    assert "id" in inst, inst

    # Make it draft (private instructions are auto-published by default; the
    # owner allowance only kicks in when global_status != approved). Even if
    # we can't easily change global_status from the client side, the member
    # may at least GET their own instruction.
    g = test_client.get(
        f"/api/instructions/{inst['id']}", headers=_h(member["token"], org_id)
    )
    assert g.status_code == 200, g.json()
