"""POST /api/tools/session_summary as a non-admin member.

A member gets the same answer as the admin for an agent they can use, and a
report they can't see is a 404, like the other per-user report endpoints.
"""

import pytest

from tests.e2e.test_session_tool_summary import (
    WRITE_TOOL, _agent_with_tools, _headers, _report, _summary,
)


@pytest.mark.e2e
def test_member_gets_the_answer_for_a_shared_agent_but_not_for_a_private_report(
    test_client, create_user, login_user, whoami, invite_user_to_org,
    create_mcp_connection, create_domain_from_connection,
):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    ds_id = _agent_with_tools(
        test_client, create_mcp_connection, create_domain_from_connection, admin_token, org_id, [WRITE_TOOL],
    )
    private_report = _report(test_client, admin_token, org_id, [ds_id])

    member = invite_user_to_org(org_id=org_id, admin_token=admin_token)

    shared = _summary(test_client, member["token"], org_id, data_source_ids=[ds_id])
    assert shared.status_code == 200, shared.text
    assert shared.json()["has_non_read_tools"] is True

    hidden = _summary(test_client, member["token"], org_id, report_id=private_report)
    assert hidden.status_code == 404


@pytest.mark.e2e
def test_a_folder_scoped_report_counts_the_tools_of_every_folder_it_reaches(
    test_client, create_user, login_user, whoami, invite_user_to_org,
    create_mcp_connection, create_domain_from_connection,
):
    """A report scoped to folders (not pinned agents) is answered from the
    agents in all its folders, the additional ones included; another member
    still gets a 404 for it."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    headers = _headers(admin_token, org_id)
    ds_id = _agent_with_tools(
        test_client, create_mcp_connection, create_domain_from_connection, admin_token, org_id, [WRITE_TOOL],
    )
    first = test_client.post("/api/folders", headers=headers, json={"name": "Finance"})
    second = test_client.post("/api/folders", headers=headers, json={"name": "Operations"})
    assert first.is_success and second.is_success, (first.text, second.text)
    first, second = first.json()["id"], second.json()["id"]
    # The write-tool agent sits only in the report's second (additional) folder.
    moved = test_client.put(f"/api/folders/items/agent/{ds_id}/placement", headers=headers,
                            json={"parent_id": second})
    assert moved.is_success, moved.text
    created = test_client.post("/api/reports", headers=headers,
                               json={"title": "Folder chip", "agent_folder_ids": [first, second]})
    assert created.status_code == 200, created.text
    report_id = created.json()["id"]
    scope = test_client.get(f"/api/reports/{report_id}", headers=headers)
    assert scope.status_code == 200, scope.text
    assert scope.json()["agent_folder_ids"] == [first, second]

    in_folders = _summary(test_client, admin_token, org_id, report_id=report_id)
    assert in_folders.status_code == 200, in_folders.text
    assert in_folders.json()["has_non_read_tools"] is True

    member = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    hidden = _summary(test_client, member["token"], org_id, report_id=report_id)
    assert hidden.status_code == 404
