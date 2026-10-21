"""
Sub-agent model overrides honour per-model access grants.

A delegate call names its model by db id, model_id or display name (optionally
'<provider>/'-prefixed). Grants are per model row, and the same model_id often
lives on several rows (one per provider). The override must resolve to a row
the user is granted — never refuse because another, ungranted row with the same
name was looked at first.

Calls `SubAgentService.record_delegation` (the delegate tool's service entry
point) directly: the behavior under test is model resolution, which needs no
LLM run.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _create_provider(test_client, token, org_id, model_id, display_name):
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"prov-{uuid.uuid4().hex[:6]}",
            "provider_type": "anthropic",
            "credentials": {"api_key": "dummy-key"},
            "models": [{"model_id": model_id, "name": display_name, "is_custom": True}],
        },
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()


def _rows_for(test_client, token, org_id, model_id):
    resp = test_client.get("/api/llm/models", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return [m for m in resp.json() if m["model_id"] == model_id]


def _delegate(org_id, user_id, report_id, model_spec):
    from app.ai.tools.schemas.sub_agents import DelegateInput
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.sub_agent_service import sub_agent_service

    async def _go():
        async with async_session_maker() as db:
            receipt = await sub_agent_service.record_delegation(
                db,
                organization=await db.get(Organization, org_id),
                user=await db.get(User, user_id),
                report=await db.get(Report, report_id),
                parent_head_completion=None, parent_system_completion=None,
                parent_execution_id=None, tool_execution_id=None, plan_decision_id=None,
                data=DelegateInput(title="Model check", goal="Check which model the child gets.", model=model_spec),
                settings=None, parent_mode=None, parent_model=None, small_model=None,
            )
            from app.models.sub_agent import SubAgentRun
            row = await db.get(SubAgentRun, receipt["sub_agent_id"]) if receipt.get("sub_agent_id") else None
            return receipt, (row.model_id if row is not None else None)

    return asyncio.run(_go())


@pytest.fixture
def twin_models(test_client, bootstrap_admin, invite_user_to_org, create_report):
    """Same model_id on two providers, both restricted; member granted ONE."""
    admin = bootstrap_admin("subagentmodel")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    org_id, admin_t = admin["org_id"], admin["token"]

    # The org's first provider supplies the defaults, which can't be restricted.
    _create_provider(test_client, admin_t, org_id, f"claude-default-{uuid.uuid4().hex[:6]}", "Default")
    model_id = f"claude-twin-{uuid.uuid4().hex[:6]}"
    display = f"Twin {uuid.uuid4().hex[:4]}"
    _create_provider(test_client, admin_t, org_id, model_id, display)
    _create_provider(test_client, admin_t, org_id, model_id, display)
    rows = _rows_for(test_client, admin_t, org_id, model_id)
    assert len(rows) == 2
    for r in rows:
        assert not r["is_default"] and not r["is_small_default"]
        resp = test_client.put(
            f"/api/llm/models/{r['id']}/restricted", json={"is_restricted": True}, headers=_headers(admin_t, org_id)
        )
        assert resp.status_code == 200, resp.json()

    report = create_report(title="Sub-agent model", user_token=member["token"], org_id=org_id)
    return {
        "admin": admin, "member": member, "org_id": org_id, "rows": rows,
        "model_id": model_id, "display": display, "report_id": report["id"],
    }


@pytest.mark.e2e
@pytest.mark.parametrize("granted_index", [0, 1])
@pytest.mark.parametrize("spec_kind", ["model_id", "provider_model_id", "display_name", "db_id"])
def test_override_resolves_to_the_row_the_user_is_granted(
    enterprise_license, test_client, twin_models, granted_index, spec_kind,
):
    t = twin_models
    granted = t["rows"][granted_index]
    resp = test_client.post(
        f"/api/llm/models/{granted['id']}/access",
        json={"principal_type": "user", "principal_id": t["member"]["user_id"]},
        headers=_headers(t["admin"]["token"], t["org_id"]),
    )
    assert resp.status_code in (200, 201), resp.json()

    spec = {
        "model_id": t["model_id"],
        "provider_model_id": f"anthropic/{t['model_id']}",
        "display_name": t["display"],
        "db_id": granted["id"],
    }[spec_kind]
    receipt, child_model_id = _delegate(t["org_id"], t["member"]["user_id"], t["report_id"], spec)

    assert receipt["status"] == "running"
    assert not receipt.get("refused")
    assert child_model_id == granted["id"]


@pytest.mark.e2e
def test_override_refused_when_user_has_no_grant_on_any_row(enterprise_license, test_client, twin_models):
    t = twin_models
    receipt, child_model_id = _delegate(t["org_id"], t["member"]["user_id"], t["report_id"], t["model_id"])

    assert receipt["refused"]
    assert child_model_id not in {r["id"] for r in t["rows"]}


@pytest.mark.e2e
def test_exact_db_id_of_an_ungranted_row_is_refused(enterprise_license, test_client, twin_models):
    """An exact id names one row; it must not fall through to a granted twin."""
    t = twin_models
    granted, other = t["rows"]
    resp = test_client.post(
        f"/api/llm/models/{granted['id']}/access",
        json={"principal_type": "user", "principal_id": t["member"]["user_id"]},
        headers=_headers(t["admin"]["token"], t["org_id"]),
    )
    assert resp.status_code in (200, 201), resp.json()

    receipt, child_model_id = _delegate(t["org_id"], t["member"]["user_id"], t["report_id"], other["id"])

    assert receipt["refused"]
    assert child_model_id != other["id"]
