"""Who may list connected applications for the instruction channel picker.

Anyone who may edit instructions somewhere (org-level or on one agent) sees
the org's app names, so a per-agent manager can scope their rules to an app;
managing the apps themselves stays behind manage_settings. A member who can't
edit instructions anywhere gets nothing.
"""
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.mark.e2e
def test_instruction_editors_list_application_targets_without_settings_access(
        test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    app = test_client.post("/api/connected_applications", headers=_hdr(admin["token"], org_id),
                           json={"name": f"ERP {uuid.uuid4().hex[:6]}"})
    assert app.status_code == 201, app.text

    ds = sqlite_data_source(name=f"at_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    granted = grant_resource(resource_type="data_source", resource_id=ds["id"], principal_type="user",
                             principal_id=manager["user_id"], permissions=["manage_instructions"],
                             user_token=admin["token"], org_id=org_id)
    assert granted.status_code == 200, granted.text
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    for who in (admin, manager):
        r = test_client.get("/api/instructions/application-targets", headers=_hdr(who["token"], org_id))
        assert r.status_code == 200, r.text
        assert app.json()["id"] in {a["id"] for a in r.json()}

    assert test_client.get("/api/connected_applications",
                           headers=_hdr(manager["token"], org_id)).status_code == 403
    assert test_client.get("/api/instructions/application-targets",
                           headers=_hdr(member["token"], org_id)).status_code == 403
