"""A saved instruction edit stays a success when its build fails to finalize.

Contract: ``update_instruction`` commits the edit before it versions it and
auto-finalizes the build. When finalizing fails, ``_auto_finalize_build``
rolls the session back and returns False. The edit is already saved, so the
caller is told it succeeded (MCP ``success: true``, REST 200), the new text is
what a later read returns, and a warning names the instruction whose build did
not finalize.

Real: routes, auth, MCP tools, services, DB. The one stub is
``_auto_finalize_build`` itself, made to fail the way it does under a held
``instruction_builds`` lock: roll back, return False.
"""
import json
import logging
import types
import uuid

import pytest
from sqlalchemy import inspect as sa_inspect, select

from app.models.instruction_build import InstructionBuild
from app.services.instruction_service import InstructionService

ORIGINAL = "Revenue excludes cancelled orders. Use net amounts for all revenue metrics."
EDITED = "Revenue excludes refunded orders. Use net amounts for all revenue metrics."


def _capture_app_logs(monkeypatch, caplog):
    """Collect every ``app.*`` record, DEBUG and up (see test_mcp_request_logging)."""
    for name, lg in list(logging.root.manager.loggerDict.items()):
        if (name == "app" or name.startswith("app.")) and isinstance(lg, logging.Logger):
            monkeypatch.setattr(lg, "disabled", False)
    app_logger = logging.getLogger("app")
    monkeypatch.setattr(app_logger, "handlers", [caplog.handler])
    monkeypatch.setattr(app_logger, "propagate", False)
    caplog.set_level(logging.DEBUG, logger="app")
    logging.getLogger("app.services.instruction_service").info("capture is active")
    assert "capture is active" in caplog.text
    caplog.clear()


def _fail_finalize(monkeypatch):
    calls = []

    async def failing_finalize(self, db, build, current_user, user_permissions, *args, **kwargs):
        # The real finalize fails mid-transaction (submit/approve/promote have
        # already run statements), so the rollback ends a live transaction and
        # expires every object in the session. A rollback with no statement
        # since the last commit would be a no-op and prove nothing.
        await db.execute(select(InstructionBuild.id).where(InstructionBuild.id == str(build.id)))
        await db.rollback()
        calls.append(sa_inspect(build).expired)
        return False

    # The REST routes share one module-level service. A test that patched
    # that instance leaves a bound original in its __dict__ when undone
    # (monkeypatch restores what getattr returned), which would shadow a
    # class patch; so patch the instance too. Instance first: patched after
    # the class, its undo would restore the stub itself.
    from app.routes.instruction import instruction_service as route_service
    monkeypatch.setattr(route_service, "_auto_finalize_build",
                        types.MethodType(failing_finalize, route_service))
    monkeypatch.setattr(InstructionService, "_auto_finalize_build", failing_finalize)
    return calls


def _finalize_warnings(caplog, instruction_id):
    return [
        r for r in caplog.records
        if r.name == "app.services.instruction_service"
        and r.levelno == logging.WARNING
        and instruction_id in r.getMessage()
        and "finaliz" in r.getMessage().lower()
    ]


def _warnings(caplog):
    return [f"{r.name}:{r.levelname}: {r.getMessage()[:200]}" for r in caplog.records
            if r.levelno >= logging.WARNING]


@pytest.fixture
def admin(create_user, login_user, whoami):
    email = f"finalize_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    org_id = whoami(token)["organizations"][0]["id"]
    return {"token": token, "org_id": org_id}


def _headers(admin):
    return {"Authorization": f"Bearer {admin['token']}", "X-Organization-Id": str(admin["org_id"])}


def _read_text(test_client, admin, instruction_id):
    response = test_client.get(f"/api/instructions/{instruction_id}", headers=_headers(admin))
    assert response.status_code == 200, response.text
    return response.json()["text"]


@pytest.mark.e2e
def test_mcp_edit_is_reported_saved_when_finalize_fails(
    monkeypatch, caplog, test_client, admin, create_global_instruction, enable_mcp, create_api_key,
):
    instr = create_global_instruction(
        text=ORIGINAL, status="published", user_token=admin["token"], org_id=admin["org_id"],
    )
    enable_mcp(user_token=admin["token"], org_id=admin["org_id"])
    api_key = create_api_key(user_token=admin["token"], org_id=admin["org_id"])["key"]
    calls = _fail_finalize(monkeypatch)
    _capture_app_logs(monkeypatch, caplog)

    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {
            "name": "edit_instruction",
            "arguments": {"instruction_id": instr["id"], "old_text": "cancelled", "text": "refunded"},
        }},
        headers={"X-API-Key": api_key},
    )

    assert calls == [True], (
        f"finalize was not reached, or its rollback expired nothing: {calls}; "
        f"warnings: {_warnings(caplog)}"
    )
    assert response.status_code == 200, response.text
    envelope = response.json()["result"]
    assert envelope["isError"] is False, \
        f"the tool raised after the edit was saved: {envelope['content'][0]['text'][:300]}"
    result = json.loads(envelope["content"][0]["text"])
    assert result.get("success") is True, f"a saved edit was reported as failed: {result}"
    assert _read_text(test_client, admin, instr["id"]) == EDITED
    assert _finalize_warnings(caplog, instr["id"]), _warnings(caplog)


@pytest.mark.e2e
def test_rest_edit_is_reported_saved_when_finalize_fails(
    monkeypatch, caplog, test_client, admin, create_global_instruction,
):
    instr = create_global_instruction(
        text=ORIGINAL, status="published", user_token=admin["token"], org_id=admin["org_id"],
    )
    calls = _fail_finalize(monkeypatch)
    _capture_app_logs(monkeypatch, caplog)

    response = test_client.put(
        f"/api/instructions/{instr['id']}", json={"text": EDITED}, headers=_headers(admin),
    )

    assert calls == [True], (
        f"finalize was not reached, or its rollback expired nothing: {calls}; "
        f"warnings: {_warnings(caplog)}"
    )
    assert response.status_code == 200, f"a saved edit was reported as failed: {response.text[:300]}"
    assert response.json()["text"] == EDITED
    assert _read_text(test_client, admin, instr["id"]) == EDITED
    assert _finalize_warnings(caplog, instr["id"]), _warnings(caplog)
