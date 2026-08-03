"""Probe: who can DELETE / re-scope a GLOBAL instruction (no attached agents)?

Global instructions apply to every agent. Creating one requires org-level
manage_instructions (create_global uses require_org_permission). This probe
checks that DELETE and agent-attachment changes are gated consistently — a
per-agent manager (manage on one agent only) must NOT be able to delete or
re-scope a global instruction created by an admin. Ported from PR #732.
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name="g_ds_a", user_token=admin["token"], org_id=org_id)
    author = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant_resource(
        resource_type="data_source", resource_id=ds_a["id"],
        principal_type="user", principal_id=author["user_id"],
        permissions=["manage_instructions"], user_token=admin["token"], org_id=org_id,
    )
    return {"org_id": org_id, "admin": admin, "author": author, "ds_a": ds_a}


@pytest.mark.e2e
def test_per_agent_manager_cannot_delete_or_rescope_global(test_client, world):
    org_id = world["org_id"]
    admin = world["admin"]
    author = world["author"]  # manage_instructions on ds_a only
    ds_a_id = world["ds_a"]["id"]

    r = test_client.post(
        "/api/instructions",
        json={"text": "GLOBAL rule", "status": "published", "category": "general", "data_source_ids": []},
        headers=_hdr(admin["token"], org_id),
    )
    assert r.status_code == 200, r.text
    gid = r.json()["id"]

    r_edit = test_client.put(
        f"/api/instructions/{gid}", json={"text": "hijack global"},
        headers=_hdr(author["token"], org_id),
    )
    assert r_edit.status_code == 403, f"edit global: got {r_edit.status_code}"

    r_scope = test_client.put(
        f"/api/instructions/{gid}", json={"data_source_ids": [ds_a_id]},
        headers=_hdr(author["token"], org_id),
    )
    assert r_scope.status_code == 403, f"rescope global: got {r_scope.status_code}"

    r_del = test_client.delete(f"/api/instructions/{gid}", headers=_hdr(author["token"], org_id))
    assert r_del.status_code == 403, f"delete global: got {r_del.status_code}"

    r_admin_del = test_client.delete(f"/api/instructions/{gid}", headers=_hdr(admin["token"], org_id))
    assert r_admin_del.status_code == 200, r_admin_del.text


@pytest.mark.e2e
def test_owner_can_delete_their_own_global_but_others_cannot(
    test_client, world, invite_user_to_org, grant_resource
):
    """Anti-zombie: a per-agent manager who OWNS a global can delete their own;
    a DIFFERENT per-agent manager (non-owner) cannot."""
    org_id = world["org_id"]
    author = world["author"]
    ds_a_id = world["ds_a"]["id"]

    other = invite_user_to_org(org_id=org_id, admin_token=world["admin"]["token"])
    grant_resource(
        resource_type="data_source", resource_id=ds_a_id,
        principal_type="user", principal_id=other["user_id"],
        permissions=["manage_instructions"], user_token=world["admin"]["token"], org_id=org_id,
    )

    r = test_client.post(
        "/api/instructions",
        json={"text": "author global", "status": "draft", "category": "general", "data_source_ids": []},
        headers=_hdr(author["token"], org_id),
    )
    assert r.status_code == 200, r.text
    gid = r.json()["id"]

    r_other = test_client.delete(f"/api/instructions/{gid}", headers=_hdr(other["token"], org_id))
    assert r_other.status_code == 403, f"non-owner delete global: got {r_other.status_code}"

    r_owner = test_client.delete(f"/api/instructions/{gid}", headers=_hdr(author["token"], org_id))
    assert r_owner.status_code == 200, f"owner delete own global: got {r_owner.status_code} {r_owner.text}"
