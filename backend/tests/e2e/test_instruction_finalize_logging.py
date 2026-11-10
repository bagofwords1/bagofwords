"""A failed build finalize logs no instruction text.

Contract: ``_auto_finalize_build`` logs a failure by the build's id and the
error's shape (``describe_for_log``), never the error's text: a DB error from
``promote_build`` carries the edited instruction text as a bound parameter.

What the edit reports to its caller after a failed finalize is out of scope
here (it moves to a separate instruction-edit PR); this file asserts only
the log invariant.

Real: routes, auth, MCP tools, services, DB. The one stub is
``BuildService.promote_build`` raising a DB error that carries the edited text.
"""
import logging
import uuid

import pytest
from sqlalchemy import select

from app.models.instruction_build import InstructionBuild

ORIGINAL = "Revenue excludes cancelled orders. Use net amounts for all revenue metrics."


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


@pytest.mark.e2e
def test_mcp_edit_whose_promote_fails_logs_no_edited_text(
    monkeypatch, caplog, test_client, admin, create_global_instruction, enable_mcp, create_api_key,
):
    """A DB error inside promote_build carries the edited text as a bound
    parameter (the engines do not hide parameters); the finalize warning names
    the build and the error's shape, never that text. No ``app.*`` record
    carries it either. The edit's reported outcome is not asserted."""
    from sqlalchemy import exc as sa_exc
    from app.services.build_service import BuildService

    marker = f"client-content-{uuid.uuid4().hex[:12]}"
    instr = create_global_instruction(
        text=ORIGINAL, status="published", user_token=admin["token"], org_id=admin["org_id"],
    )
    enable_mcp(user_token=admin["token"], org_id=admin["org_id"])
    api_key = create_api_key(user_token=admin["token"], org_id=admin["org_id"])["key"]

    promoted = []

    async def failing_promote(self, db, build_id, *args, **kwargs):
        # Run a statement first so the finalize rollback ends a live transaction.
        await db.execute(select(InstructionBuild.id).where(InstructionBuild.id == str(build_id)))
        promoted.append(str(build_id))
        raise sa_exc.OperationalError(
            "UPDATE instructions SET text=:text WHERE id=:id",
            {"text": f"refunded {marker}", "id": instr["id"]},
            Exception(f"lock timeout while writing refunded {marker}"),
        )

    monkeypatch.setattr(BuildService, "promote_build", failing_promote)
    _capture_app_logs(monkeypatch, caplog)

    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 22, "method": "tools/call", "params": {
            "name": "edit_instruction",
            "arguments": {"instruction_id": instr["id"], "old_text": "cancelled",
                          "text": f"refunded {marker}"},
        }},
        headers={"X-API-Key": api_key},
    )

    assert response.status_code == 200, response.text
    assert len(promoted) == 1, f"promote_build was not reached: {promoted}; warnings: {_warnings(caplog)}"
    leaked = [f"{r.name}:{r.levelname}" for r in caplog.records if marker in r.getMessage()]
    assert not leaked, f"the edited text reached the logs in: {leaked}"
    failed = [
        r for r in caplog.records
        if r.name == "app.services.instruction_service" and r.levelno == logging.WARNING
        and "auto-finalize" in r.getMessage() and promoted[0] in r.getMessage()
    ]
    assert failed, f"no finalize-failure warning naming the build: {_warnings(caplog)}"
    assert "OperationalError" in failed[0].getMessage(), failed[0].getMessage()
