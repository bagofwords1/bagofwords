"""Remote connector lifecycle and policy boundaries through the real API."""

import os
import sys

import pytest


@pytest.fixture
def ssh_driver(tmp_path, monkeypatch):
    # External SSH executable boundary. Everything in BOW, including policy/DB, is real.
    driver = tmp_path / "ssh"
    marker = tmp_path / "executed"
    driver.write_text(f"""#!{sys.executable}
import json,sys
from pathlib import Path
if "-O" in sys.argv: sys.exit(0)
raw=sys.stdin.read()
if raw:
 print(json.dumps({{"success":True,"content_type":"json","data":{{"protocol":1,"roots":["/logs"],"operations":["read_file","list_directory","search_file"]}}}}))
else:
 Path({str(marker)!r}).write_text("executed")
 print("executed")
""")
    driver.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return marker


def test_ssh_connection_discovers_tools_and_blocks_unapproved_shell(
    ssh_driver,
    create_user,
    login_user,
    whoami,
    create_connection,
    create_domain_from_connection,
    invite_user_to_org,
    create_organization,
    test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": "Bearer " + token, "X-Organization-Id": org}
    conn = create_connection(
        name="Remote diagnostics",
        type="remote_ssh",
        config={
            "host": "127.0.0.1",
            "host_public_key": "ssh-ed25519 YWJj",
            "auth_type": "password",
            "allowed_paths": ["/logs"],
            "enable_shell": True,
            "shell_policy": "strict",
        },
        credentials={"username": "reader", "password": "test-secret"},
        user_token=token,
        org_id=org,
    )
    response = test_client.get(f"/api/connections/{conn['id']}/tools", headers=headers)
    assert response.status_code == 200, response.text
    tools = response.json()
    shell = next(t for t in tools if t["name"] == "execute_shell")
    assert shell["policy"] == "ask"
    assert "test-secret" not in response.text
    result = test_client.post(
        "/api/connections/test-tool",
        headers=headers,
        json={
            "type": "remote_ssh",
            "connection_id": conn["id"],
            "tool_name": "execute_shell",
            "arguments": {"command": "id", "working_directory": "/"},
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["success"] is False
    agent = create_domain_from_connection(
        name="Server operator", connection_id=conn["id"], user_token=token, org_id=org
    )
    tools_url = f"/api/data_sources/{agent['id']}/tools"
    response = test_client.get(tools_url, headers=headers)
    assert response.status_code == 200, response.text
    shell = next(t for t in response.json() if t["name"] == "execute_shell")
    assert shell["metadata"]["remote_shell"] is True
    # Strict mode keeps human approval even when a personal Allow is saved.
    preference = test_client.put(f"{tools_url}/{shell['id']}/my_policy", headers=headers, json={"policy": "allow"})
    assert preference.status_code == 200, preference.text
    assert preference.json()["effective_policy"] == "ask"
    member = invite_user_to_org(org_id=org, admin_token=token)
    member_token = member["token"]
    member_headers = {"Authorization": "Bearer " + member_token, "X-Organization-Id": org}
    preference = test_client.put(
        f"{tools_url}/{shell['id']}/my_policy", headers=member_headers, json={"policy": "allow"}
    )
    assert preference.status_code == 200, preference.text
    assert preference.json()["effective_policy"] == "ask"
    denied = test_client.put(
        f"/api/connections/{conn['id']}/tools/{shell['id']}", headers=member_headers, json={"policy": "allow"}
    )
    assert denied.status_code in (403, 404)
    # Persistent rules belong only to the current user, survive later requests,
    # and remain editable by an ordinary member without connection-admin access.
    rules_url = f"{tools_url}/{shell['id']}/my_shell_rules"
    rule = {"pattern": "docker ps*", "action": "allow", "match": "glob", "working_directory": "/srv"}
    response = test_client.put(rules_url, headers=member_headers, json=[rule])
    assert response.status_code == 200, response.text
    assert test_client.get(rules_url, headers=member_headers).json() == [rule]
    assert test_client.get(rules_url, headers=headers).json() == []
    # Connection-wide Allow applies even to a member who saved Deny.
    response = test_client.put(f"{tools_url}/{shell['id']}/my_policy", headers=member_headers, json={"policy": "deny"})
    assert response.status_code == 200
    response = test_client.put(
        f"/api/connections/{conn['id']}",
        headers=headers,
        json={
            "config": {
                "host": "127.0.0.1",
                "host_public_key": "ssh-ed25519 YWJj",
                "auth_type": "password",
                "enable_shell": True,
                "shell_policy": "allow",
            }
        },
    )
    assert response.status_code == 200, response.text
    rows = test_client.get(tools_url, headers=member_headers).json()
    assert next(t for t in rows if t["name"] == "execute_shell")["effective_policy"] == "allow"
    assert test_client.delete(f"{tools_url}/{shell['id']}/my_policy", headers=member_headers).status_code == 200
    assert test_client.get(rules_url, headers=member_headers).json() == []
    # The external gateway honors the same connection-wide setting for members.
    import asyncio

    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.connection_tool_gateway import ConnectionToolGateway

    member_id = whoami(member_token)["id"]

    async def invoke_gateway():
        async with async_session_maker() as db:
            actor = (await db.execute(select(User).where(User.id == member_id))).scalar_one()
            organization = (await db.execute(select(Organization).where(Organization.id == org))).scalar_one()
            return await ConnectionToolGateway().execute(
                db,
                organization,
                data_source_id=agent["id"],
                tool_name="execute_shell",
                arguments={"command": "printf fixture", "working_directory": "/tmp"},
                current_user=actor,
            )

    assert asyncio.run(invoke_gateway()).success
    assert ssh_driver.exists()
    ssh_driver.unlink()
    response = test_client.put(
        f"/api/connections/{conn['id']}/tools/{shell['id']}", headers=headers, json={"is_enabled": False}
    )
    assert response.status_code == 200, response.text
    assert not asyncio.run(invoke_gateway()).success
    assert not ssh_driver.exists()
    # Another organization cannot use the saved credential via test-tool.
    other = create_user(email="remote-other@example.com")
    other_token = login_user(other["email"], other["password"])
    other_org = create_organization(name="Other remote organization", user_token=other_token)
    denied = test_client.post(
        "/api/connections/test-tool",
        headers={"Authorization": "Bearer " + other_token, "X-Organization-Id": other_org},
        json={
            "type": "remote_ssh",
            "connection_id": conn["id"],
            "tool_name": "read_file",
            "arguments": {"path": "/logs/a"},
        },
    )
    assert denied.status_code in (403, 404)


@pytest.mark.parametrize(
    "decision",
    ["allow", "deny", "headless", "changed_target", "global_allow", "user_allow", "user_deny", "global_overrides_user"],
)
def test_remote_shell_execution_is_gated_by_exact_human_decision(
    decision,
    ssh_driver,
    create_user,
    login_user,
    whoami,
    create_connection,
    create_domain_from_connection,
    test_client,
):
    import asyncio

    from sqlalchemy import select

    from app.ai.tools.confirmation import resolve_confirmation
    from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.tool_confirmation_service import ToolConfirmationService

    user = create_user()
    token = login_user(user["email"], user["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    conn = create_connection(
        name="Maintenance target",
        type="remote_ssh",
        config={
            "host": "127.0.0.1",
            "host_public_key": "ssh-ed25519 YWJj",
            "auth_type": "password",
            "allowed_paths": ["/logs"],
            "enable_shell": True,
            "shell_policy": "allow" if decision in ("global_allow", "global_overrides_user") else "policy",
        },
        credentials={"username": "operator", "password": "fixture-secret"},
        user_token=token,
        org_id=org_id,
    )
    agent = create_domain_from_connection(name="Operator", connection_id=conn["id"], user_token=token, org_id=org_id)

    if decision in ("user_allow", "user_deny", "global_overrides_user"):
        url = f"/api/data_sources/{agent['id']}/tools"
        headers = {"Authorization": "Bearer " + token, "X-Organization-Id": org_id}
        shell = next(t for t in test_client.get(url, headers=headers).json() if t["name"] == "execute_shell")
        response = test_client.put(
            f"{url}/{shell['id']}/my_policy",
            headers=headers,
            json={"policy": "allow" if decision == "user_allow" else "deny"},
        )
        assert response.status_code == 200

    async def exercise():
        async with async_session_maker() as db:
            actor = (await db.execute(select(User).where(User.id == info["id"]))).scalar_one()
            org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
            ctx = {"db": db, "user": actor, "organization": org, "run_agent_ids": [agent["id"]]}
            if decision == "headless":
                ctx["is_eval_run"] = True
            events = []
            async for event in ExecuteMCPTool().run_stream(
                {
                    "connection_id": conn["id"],
                    "tool_name": "execute_shell",
                    "arguments": {"command": "printf test", "working_directory": "/tmp"},
                },
                ctx,
            ):
                events.append(event)
                if event.type == "tool.confirmation":
                    assert not ssh_driver.exists()
                    assert event.payload["arguments"]["_execution_target"]["username"] == "operator"
                    if decision == "changed_target":
                        response = test_client.put(
                            f"/api/connections/{conn['id']}",
                            headers={"Authorization": "Bearer " + token, "X-Organization-Id": org_id},
                            json={
                                "config": {
                                    "host": "127.0.0.2",
                                    "host_public_key": "ssh-ed25519 YWJj",
                                    "auth_type": "password",
                                    "allowed_paths": ["/logs"],
                                    "enable_shell": True,
                                }
                            },
                        )
                        assert response.status_code == 200, response.text
                    cid = event.payload["confirmation_id"]
                    approved = decision != "deny"
                    await ToolConfirmationService().resolve(
                        db, confirmation_id=cid, approved=approved, remember=False, user_id=actor.id
                    )
                    resolve_confirmation(cid, {"approved": approved, "remember": False})
            return events

    events = asyncio.run(exercise())
    final = [e for e in events if e.type == "tool.end"][-1]
    assert bool(final.payload["output"]["success"]) == (
        decision in ("allow", "global_allow", "user_allow", "global_overrides_user")
    ), final.payload
    assert ssh_driver.exists() == (decision in ("allow", "global_allow", "user_allow", "global_overrides_user"))
    assert any(e.type == "tool.confirmation" for e in events) == (decision in ("allow", "deny", "changed_target"))


@pytest.mark.parametrize("scope", ["user", "session"])
def test_shell_approval_persists_scoped_rules_without_sharing_between_users(
    scope,
    ssh_driver,
    create_user,
    login_user,
    whoami,
    create_connection,
    create_domain_from_connection,
    create_report,
    invite_user_to_org,
    test_client,
):
    import asyncio
    from uuid import uuid4

    from app.dependencies import async_session_maker
    from app.services.remote_shell_policy import user_shell_rules
    from app.services.tool_confirmation_service import ToolConfirmationService

    user = create_user()
    token = login_user(user["email"], user["password"])
    info = whoami(token)
    org = info["organizations"][0]["id"]
    headers = {"Authorization": "Bearer " + token, "X-Organization-Id": org}
    conn = create_connection(
        name="Rules target",
        type="remote_ssh",
        config={
            "host": "127.0.0.1",
            "host_public_key": "ssh-ed25519 YWJj",
            "auth_type": "password",
            "enable_shell": True,
        },
        credentials={"username": "reader", "password": "fixture-secret"},
        user_token=token,
        org_id=org,
    )
    agent = create_domain_from_connection(name="Rules agent", connection_id=conn["id"], user_token=token, org_id=org)
    report = create_report(user_token=token, org_id=org)
    tools_url = f"/api/data_sources/{agent['id']}/tools"
    shell = next(t for t in test_client.get(tools_url, headers=headers).json() if t["name"] == "execute_shell")
    cid, completion_id = str(uuid4()), str(uuid4())

    async def pending():
        # Confirmation creation is an internal executor API, not an HTTP endpoint.
        async with async_session_maker() as db:
            await ToolConfirmationService().create(
                db,
                confirmation_id=cid,
                organization_id=org,
                report_id=report["id"],
                system_completion_id=completion_id,
                user_id=info["id"],
                connection_id=conn["id"],
                connection_tool_id=shell["id"],
                tool_name="execute_shell",
                arguments={
                    "command": "docker ps -a",
                    "working_directory": "/srv",
                    "_execution_target": {"shell_permissions": True, "mandatory_approval": False},
                },
            )

    asyncio.run(pending())
    member = invite_user_to_org(org_id=org, admin_token=token)
    member_headers = {"Authorization": "Bearer " + member["token"], "X-Organization-Id": org}
    url = f"/api/completions/{completion_id}/mcp_tool_confirmations/{cid}"
    body = {"approved": True, "permission_scope": scope, "command_pattern": "docker ps*", "command_match": "glob"}
    assert test_client.post(url, headers=member_headers, json=body).status_code == 403
    result = test_client.post(url, headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["remembered"] is True
    # Repeated delivery does not append another rule.
    duplicate = test_client.post(url, headers=headers, json={**body, "approved": False})
    assert duplicate.status_code == 200 and duplicate.json()["approved"] is True

    async def stored():
        async with async_session_maker() as db:
            current = await user_shell_rules(db, info["id"], shell["id"], report["id"])
            other = await user_shell_rules(db, info["id"], shell["id"], str(uuid4()))
            return current, other

    current, other = asyncio.run(stored())
    assert len(current) == 1 and current[0]["pattern"] == "docker ps*"
    assert bool(other) == (scope == "user")
    assert test_client.get(f"{tools_url}/{shell['id']}/my_shell_rules", headers=member_headers).json() == []
