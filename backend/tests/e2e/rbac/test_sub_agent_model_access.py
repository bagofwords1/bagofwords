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
import time
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


def _delegate(org_id, user_id, report_id, model_spec, *, profile="investigate", parent_model_id=None, small_model_id=None):
    from app.ai.tools.schemas.sub_agents import DelegateInput
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.sub_agent_service import sub_agent_service

    async def _go():
        from app.models.llm_model import LLMModel
        async with async_session_maker() as db:
            parent_model = await db.get(LLMModel, parent_model_id) if parent_model_id else None
            small_model = await db.get(LLMModel, small_model_id) if small_model_id else None
            receipt = await sub_agent_service.record_delegation(
                db,
                organization=await db.get(Organization, org_id),
                user=await db.get(User, user_id),
                report=await db.get(Report, report_id),
                parent_head_completion=None, parent_system_completion=None,
                parent_execution_id=None, tool_execution_id=None, plan_decision_id=None,
                data=DelegateInput(title="Model check", goal="Check which model the child gets.", model=model_spec, profile=profile),
                settings=None, parent_mode=None, parent_model=parent_model, small_model=small_model,
            )
            from app.models.sub_agent import SubAgentRun, SubAgentWave
            row = await db.get(SubAgentRun, receipt["sub_agent_id"]) if receipt.get("sub_agent_id") else None
            # No parent completion here, so every call would join one
            # collecting wave and hit the per-wave cap. Close it (direct write:
            # launch never runs in these tests) so each call stands alone.
            if receipt.get("wave_id"):
                wave = await db.get(SubAgentWave, receipt["wave_id"])
                wave.status = "stopped"
                await db.commit()
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


@pytest.mark.e2e
@pytest.mark.parametrize("profile", ["investigate", "validate"])
def test_default_child_model_is_the_parent_conversation_model(enterprise_license, test_client, twin_models, profile):
    """With no override and sub_agent_model=auto, every profile runs on the
    model the main conversation ran on — not the org's small default."""
    t = twin_models
    resp = test_client.get("/api/llm/models", headers=_headers(t["admin"]["token"], t["org_id"]))
    small = next(m for m in resp.json() if m["is_small_default"])
    parent = t["rows"][1]
    assert parent["id"] != small["id"]

    receipt, child_model_id = _delegate(
        t["org_id"], t["member"]["user_id"], t["report_id"], None,
        profile=profile, parent_model_id=parent["id"], small_model_id=small["id"],
    )

    assert receipt["status"] == "running"
    assert child_model_id == parent["id"]


# ── the delegate tool's model list ───────────────────────────────────────


def _grant(test_client, t, model_row_id, user_id):
    resp = test_client.post(
        f"/api/llm/models/{model_row_id}/access",
        json={"principal_type": "user", "principal_id": user_id},
        headers=_headers(t["admin"]["token"], t["org_id"]),
    )
    assert resp.status_code in (200, 201), resp.json()


def _options(org_id, user_id):
    """[(label, model_row_id)] the delegate tool would offer this user."""
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.sub_agent_service import delegate_model_options

    async def _go():
        async with async_session_maker() as db:
            opts = await delegate_model_options(
                db, await db.get(Organization, org_id), await db.get(User, user_id),
            )
            return [(label, str(m.id)) for label, m in opts]

    return asyncio.run(_go())


def _create_bedrock_provider(test_client, token, org_id, model_id, display_name):
    # Provider creation does not test the connection, so dummy creds are fine.
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"bedrock-{uuid.uuid4().hex[:6]}",
            "provider_type": "bedrock",
            "credentials": {"region": "us-east-1", "auth_mode": "api_key", "api_key": "dummy-key"},
            "models": [{"model_id": model_id, "name": display_name, "is_custom": True}],
        },
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()


@pytest.fixture
def bedrock_model(test_client, twin_models):
    """A restricted Bedrock model whose model_id is an inference-profile ARN."""
    t = twin_models
    suffix = uuid.uuid4().hex[:8]
    arn = f"arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/{suffix}"
    display = f"Opus Bedrock {suffix}"
    _create_bedrock_provider(test_client, t["admin"]["token"], t["org_id"], arn, display)
    row = _rows_for(test_client, t["admin"]["token"], t["org_id"], arn)[0]
    resp = test_client.put(
        f"/api/llm/models/{row['id']}/restricted", json={"is_restricted": True},
        headers=_headers(t["admin"]["token"], t["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    return {"arn": arn, "display": display, "row": row}


@pytest.mark.e2e
@pytest.mark.parametrize("spec_kind", ["arn", "display_name", "option_label"])
def test_bedrock_arn_model_resolves_only_for_granted_members(
    enterprise_license, test_client, twin_models, bedrock_model, invite_user_to_org, spec_kind,
):
    """An ARN model_id holds '/' and ':' — it must still resolve, by the ARN,
    the display name or the offered label, for a member granted the model; a
    member without the grant is refused by every spelling."""
    t, b = twin_models, bedrock_model
    granted_uid = t["member"]["user_id"]
    other = invite_user_to_org(org_id=t["org_id"], admin_token=t["admin"]["token"])
    _grant(test_client, t, b["row"]["id"], granted_uid)

    label = next(lbl for lbl, mid in _options(t["org_id"], granted_uid) if mid == b["row"]["id"])
    spec = {"arn": b["arn"], "display_name": b["display"], "option_label": label}[spec_kind]

    receipt, child_model_id = _delegate(t["org_id"], granted_uid, t["report_id"], spec)
    assert not receipt.get("refused")
    assert child_model_id == b["row"]["id"]

    other_report = test_client.post(
        "/api/reports", json={"title": "other", "widget": None, "files": [], "data_sources": []},
        headers=_headers(other["token"], t["org_id"]),
    ).json()
    receipt, child_model_id = _delegate(t["org_id"], other["user_id"], other_report["id"], spec)
    assert receipt["refused"]
    assert child_model_id != b["row"]["id"]


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["member", "admin"])
def test_every_offered_model_is_usable_and_resolves_back_to_itself(
    enterprise_license, test_client, twin_models, bedrock_model, who,
):
    """The offered list is exactly the user's usable models, labels are
    unique, and delegating with any label lands on that same model."""
    t, b = twin_models, bedrock_model
    granted = t["rows"][1]
    _grant(test_client, t, granted["id"], t["member"]["user_id"])
    uid = t[who]["user_id"]

    report_id = t["report_id"] if who == "member" else test_client.post(
        "/api/reports", json={"title": "admin", "widget": None, "files": [], "data_sources": []},
        headers=_headers(t["admin"]["token"], t["org_id"]),
    ).json()["id"]

    opts = _options(t["org_id"], uid)
    labels = [lbl for lbl, _ in opts]
    ids = {mid for _, mid in opts}
    assert len(labels) == len({lbl.lower() for lbl in labels})

    restricted_ids = {t["rows"][0]["id"], t["rows"][1]["id"], b["row"]["id"]}
    expected_restricted = restricted_ids if who == "admin" else {granted["id"]}
    assert ids & restricted_ids == expected_restricted

    for label, mid in opts:
        receipt, child_model_id = _delegate(t["org_id"], uid, report_id, label)
        assert not receipt.get("refused"), label
        assert child_model_id == mid, label


# ── live planner turn: the list reaches the LLM and works end to end ─────


def _scripted_planner(seen: dict, pick_contains: str):
    """LLM boundary stub. The parent reads the delegate tool's `model` enum
    from the schema it was sent and delegates with the option whose label
    contains `pick_contains`; the child finishes; the fan-in answers."""
    from app.ai.llm.types import (
        MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
    )

    def _text(messages) -> str:
        out = []
        for m in messages or []:
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict):
                        out.append(str(b.get("text") or b.get("content") or ""))
        return "\n".join(out)

    def _has_tool_result(messages) -> bool:
        for m in messages or []:
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                return True
        return False

    def _tool(name, args):
        tid = f"tu_{uuid.uuid4().hex[:8]}"
        return [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args)]

    def _end(reason="end_turn"):
        return [MessageStopEvent(stop_reason=reason, raw_stop_reason=reason), UsageEvent(input_tokens=10, output_tokens=5)]

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        sys_text = system or ""
        text = _text(messages)
        by_name = {getattr(t, "name", None): t for t in (tools or [])}
        events = []
        if "Mode: Sub-agent" in sys_text:
            seen.setdefault("child_model_ids", []).append(getattr(getattr(self, "model", None), "model_id", None))
            events += _tool("finish_subtask", {
                "outcome": "refuted", "confidence": 0.5, "summary": "Checked; nothing found.",
                "findings": [], "ruled_out": [], "evidence": [], "caveats": [], "unknown": None,
            }) + _end("tool_use")
        elif "<sub_agent_results" in text:
            events += [TextDeltaEvent(text="Done.")] + _end()
        elif "delegate" in by_name and not _has_tool_result(messages):
            schema = by_name["delegate"].input_schema
            seen["delegate_schema"] = schema
            enum = (schema.get("properties", {}).get("model") or {}).get("enum") or []
            pick = next((v for v in enum if pick_contains in v), None)
            seen["picked"] = pick
            args = {"goal": "Count paid orders per day for September.", "title": "Paid orders check"}
            if pick:
                args["model"] = pick
            events += _tool("delegate", args) + _end("tool_use")
        else:
            events += [TextDeltaEvent(text="Launched.")] + _end()
        for e in events:
            yield e

    return scripted


@pytest.fixture
def live_client():
    """One event loop for the whole test, so the sub-agent launcher and the
    child run a request spawns are not cancelled when the request returns."""
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as client:
        yield client


@pytest.mark.e2e
def test_planner_sees_only_usable_models_and_delegates_to_a_bedrock_arn_model(
    enterprise_license, live_client, twin_models, bedrock_model, monkeypatch,
):
    from sqlalchemy import select
    from app.ai.llm.llm import LLM
    from app.models.sub_agent import SubAgentRun

    t, b = twin_models, bedrock_model
    member = t["member"]
    _grant(live_client, t, b["row"]["id"], member["user_id"])
    _grant(live_client, t, t["rows"][0]["id"], member["user_id"])  # twin 1 granted, twin 2 not

    seen: dict = {}
    monkeypatch.setattr(LLM, "inference_stream_v2", _scripted_planner(seen, b["display"]))

    async def _no_stream(self, prompt, **kw):
        yield "Paid orders"
    monkeypatch.setattr(LLM, "inference_stream", _no_stream)
    monkeypatch.setattr(LLM, "inference", lambda self, *a, **k: "ok")

    headers = _headers(member["token"], t["org_id"])
    r = live_client.post(
        f"/api/reports/{t['report_id']}/completions",
        json={"prompt": {"content": "Use the Bedrock Opus model in sub-agents to check paid orders.", "widget_id": None, "step_id": None, "mentions": [{}]}},
        headers=headers, params={"background": False},
    )
    assert r.status_code == 200, r.text

    # What the LLM was offered: the member's usable models, nothing else.
    model_prop = seen["delegate_schema"]["properties"]["model"]
    offered = {lbl: mid for lbl, mid in _options(t["org_id"], member["user_id"])}
    assert set(model_prop["enum"]) == set(offered)
    assert b["row"]["id"] in offered.values()
    assert t["rows"][0]["id"] in offered.values()
    assert t["rows"][1]["id"] not in offered.values()
    assert seen["picked"]


    # What happened: the child was recorded and run on the Bedrock model.
    async def _child():
        async with async_session_maker() as db:
            return (await db.execute(
                select(SubAgentRun).where(SubAgentRun.report_id == t["report_id"])
            )).scalars().all()

    deadline = time.monotonic() + 120
    rows = []
    while time.monotonic() < deadline:
        rows = asyncio.run(_child())
        if rows and all(row.status in ("finished", "failed", "timed_out", "interrupted") for row in rows):
            break
        time.sleep(0.5)
    assert len(rows) == 1
    assert rows[0].model_id == b["row"]["id"]
    assert rows[0].status == "finished", rows[0].error if hasattr(rows[0], "error") else rows[0].status
    assert b["arn"] in (seen.get("child_model_ids") or [])


@pytest.mark.e2e
def test_planner_gets_no_model_choice_when_overrides_are_disabled(
    enterprise_license, live_client, twin_models, monkeypatch, update_organization_settings,
):
    from app.ai.llm.llm import LLM

    t = twin_models
    update_organization_settings(
        {"sub_agent_allow_model_override": {"value": False}}, user_token=t["admin"]["token"], org_id=t["org_id"],
    )
    seen: dict = {}
    monkeypatch.setattr(LLM, "inference_stream_v2", _scripted_planner(seen, "nothing"))

    async def _no_stream(self, prompt, **kw):
        yield "x"
    monkeypatch.setattr(LLM, "inference_stream", _no_stream)
    monkeypatch.setattr(LLM, "inference", lambda self, *a, **k: "ok")

    r = live_client.post(
        f"/api/reports/{t['report_id']}/completions",
        json={"prompt": {"content": "Check paid orders with sub-agents.", "widget_id": None, "step_id": None, "mentions": [{}]}},
        headers=_headers(t["member"]["token"], t["org_id"]), params={"background": False},
    )
    assert r.status_code == 200, r.text
    assert "model" not in seen["delegate_schema"]["properties"]
