"""User-visible lifecycle through the real tool, policy, confirmation and DB paths."""

import asyncio

import pytest

from tests.e2e.rbac.test_remote_server import ssh_driver  # noqa: F401 — shared external SSH fixture


@pytest.mark.parametrize("role", ["admin", "member"])
@pytest.mark.parametrize(
    "outcome",
    [
        "cancelled",
        "cancelled_after_approval",
        "cancelled_before_dispatch",
        "denied",
        "approval_expired",
        "execution_timed_out",
        "command_failed",
        "invalid_arguments",
        "success",
    ],
)
def test_shell_lifecycle(
    outcome,
    role,
    ssh_driver,  # noqa: F811 — pytest injects the imported fixture
    create_user,
    login_user,
    whoami,
    create_connection,
    create_domain_from_connection,
    create_report,
    enable_mcp,
    invite_user_to_org,
    test_client,
    monkeypatch,
):
    from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool
    from app.data_sources.clients.remote_server_client import RemoteServerClient
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.tool_confirmation_service import ToolConfirmationService

    account = create_user()
    token = login_user(account["email"], account["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    enable_mcp(user_token=token, org_id=org_id)
    conn = create_connection(
        name="Fixture SSH",
        type="remote_ssh",
        config={
            "host": "127.0.0.1",
            "host_public_key": "ssh-ed25519 YWJj",
            "auth_type": "password",
            "enable_shell": True,
        },
        credentials={"username": "fixture", "password": "fixture-secret"},
        user_token=token,
        org_id=org_id,
    )
    agent = create_domain_from_connection(
        name="Fixture operator", connection_id=conn["id"], user_token=token, org_id=org_id
    )
    if role == "member":
        member = invite_user_to_org(org_id=org_id, admin_token=token)
        token = member["token"]
        info = whoami(token)
    report = create_report(user_token=token, org_id=org_id)
    if outcome == "approval_expired":
        monkeypatch.setattr(ExecuteMCPTool, "_ASK_TIMEOUT_S", 0)
    if outcome == "execution_timed_out":
        # The external wire fails after approval; no real host/clock dependency.
        def timeout(*args, **kwargs):
            raise TimeoutError(
                "Remote operation timed out; remote shell work may still be running. Check its state before retrying."
            )

        monkeypatch.setattr(RemoteServerClient, "_ssh", timeout)

    if outcome == "command_failed":
        monkeypatch.setattr(RemoteServerClient, "_ssh", lambda *a, **k: (7, b"partial output", b"fixture error"))

    async def run():
        async with async_session_maker() as db:
            stop = asyncio.Event()
            ctx = {
                "db": db,
                "user": await db.get(User, info["id"]),
                "organization": await db.get(Organization, org_id),
                "report": await db.get(Report, report["id"]),
                "run_agent_ids": [agent["id"]],
                "sigkill_event": stop,
            }
            events = []
            arguments = {"command": "printf ready", "working_directory": "/srv"}
            if outcome == "invalid_arguments":
                del arguments["working_directory"]
            async for event in ExecuteMCPTool().run_stream(
                {
                    "connection_id": conn["id"],
                    "tool_name": "execute_shell",
                    "arguments": arguments,
                },
                ctx,
            ):
                events.append(event)
                if (
                    outcome == "cancelled_before_dispatch"
                    and event.type == "tool.progress"
                    and event.payload.get("stage") == "calling_tool"
                ):
                    stop.set()
                if event.type == "tool.confirmation":
                    cid = event.payload["confirmation_id"]
                    if outcome in ("cancelled", "cancelled_after_approval"):
                        stop.set()
                    if outcome not in ("cancelled", "approval_expired"):
                        async with async_session_maker() as responder:
                            await ToolConfirmationService().resolve(
                                responder,
                                confirmation_id=cid,
                                approved=outcome != "denied",
                                remember=False,
                                user_id=info["id"],
                            )
            return next(e.payload for e in reversed(events) if e.type == "tool.end")

    payload = asyncio.run(run())
    output = payload["output"]
    assert output["connection_type"] == "remote_ssh"
    assert output["execution_target"]["server"] == "127.0.0.1"
    if outcome == "success":
        assert output["success"] is True
        assert ssh_driver.exists()
    else:
        expected = (
            "cancelled"
            if outcome.startswith("cancelled")
            else "failed"
            if outcome in ("command_failed", "invalid_arguments")
            else outcome
        )
        assert output["outcome"] == expected
        assert output["success"] is False
        assert not ssh_driver.exists()
        if expected == "cancelled":
            assert output["approval"]["cancelled"] is True
            assert output["approval"]["timed_out"] is False
        if outcome == "command_failed":
            assert output["execution_result"]["exit_code"] == 7
            assert output["execution_result"]["stderr"]
        if expected == "execution_timed_out":
            assert output["execution_state"] == "unknown"
            assert output["approval"]["approved"] is True
            assert "input_schema" not in payload["observation"]
