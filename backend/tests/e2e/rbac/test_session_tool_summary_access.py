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
