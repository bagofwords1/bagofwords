"""User memory end to end: API privacy, the org setting gate, the agent's
catalog + <memory> injection, machine turns, saving on noticing, and trace
privacy.

The LLM boundary is stubbed at ``AgentV2.main_execution``: the real completion
path (routes → CompletionService → AgentV2.__init__) runs, and the stub
inspects the agent the way the planner would see it (tool catalog, the
rendered <memory> block via the real PromptBuilderV3) and, where a test needs
the agent to act, invokes the real memory tools with the agent's runtime
context. Services, models and the DB run real.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.ai.agent_v2 import AgentV2

MEMORY_TOOLS = {"create_memory", "edit_memory", "search_memory"}


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def org(monkeypatch, test_client, create_user, login_user, whoami, create_llm_provider_and_models):
    """An org with a (never-called) model, its admin, and one member."""
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    create_llm_provider_and_models(admin_token, org_id)

    def add_member(role="member"):
        email = f"mem_{uuid.uuid4().hex[:8]}@test.com"
        r = test_client.post(
            f"/api/organizations/{org_id}/members",
            json={"organization_id": org_id, "email": email, "role": role},
            headers=_h(admin_token, org_id),
        )
        assert r.status_code == 200, r.json()
        create_user(email=email, password="test123")
        token = login_user(email, "test123")
        return token, whoami(token)["id"]

    member_token, member_id = add_member()
    return {"id": org_id, "admin": admin_token, "member": member_token, "member_id": member_id,
            "add_member": add_member}


def _set_memory_enabled(test_client, org, enabled: bool):
    r = test_client.put(
        "/api/organization/settings",
        json={"config": {"enable_user_memory": {"value": enabled}}},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()


def _add(test_client, token, org_id, **body):
    return test_client.post("/api/users/me/memory", json=body, headers=_h(token, org_id))


def _list(test_client, token, org_id):
    return test_client.get("/api/users/me/memory", headers=_h(token, org_id))


# ---------------------------------------------------------------------------
# API: own memory only
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_member_crud_on_own_memory(test_client, org):
    t, o = org["member"], org["id"]
    r = _add(test_client, t, o, text="Prefers the number first", section="style", tags=["Format"])
    assert r.status_code == 200, r.json()
    entry = r.json()
    assert entry["source"] == "user" and entry["tags"] == ["format"] and entry["handle"].startswith("m")

    ev = _add(test_client, t, o, text="Board meeting", section="events", tags=["board"],
              event_start=(datetime.utcnow() + timedelta(days=3)).date().isoformat())
    assert ev.status_code == 200, ev.json()

    body = _list(test_client, t, o).json()
    assert [e["id"] for e in body["sections"]["style"]] == [entry["id"]]
    assert len(body["sections"]["events"]) == 1
    assert body["total"] == 2
    assert "Prefers the number first" in body["preview"] and "Board meeting" in body["preview"]

    p = test_client.patch(f"/api/users/me/memory/{entry['id']}", json={"text": "Prefers the number first, then context"},
                          headers=_h(t, o))
    assert p.status_code == 200, p.json()
    new_id = p.json()["id"]
    assert new_id != entry["id"]
    styles = _list(test_client, t, o).json()["sections"]["style"]
    assert [e["id"] for e in styles] == [new_id]

    d = test_client.delete(f"/api/users/me/memory/{new_id}", headers=_h(t, o))
    assert d.status_code == 200
    assert _list(test_client, t, o).json()["sections"]["style"] == []

    fa = test_client.delete("/api/users/me/memory", headers=_h(t, o))
    assert fa.status_code == 200 and fa.json()["forgotten"] == 1
    assert _list(test_client, t, o).json()["total"] == 0


@pytest.mark.e2e
@pytest.mark.parametrize("bad,code", [
    ({"text": "Board meeting", "section": "events"}, "memory.event_date_required"),
    ({"text": "x" * 300, "section": "style"}, "memory.text_too_long"),
    ({"text": "Prefers tables", "section": "hobbies"}, "memory.invalid_section"),
    ({"text": "my password: hunter22", "section": "preferences"}, "memory.sensitive"),
])
def test_api_validation_returns_typed_errors(test_client, org, bad, code):
    r = _add(test_client, org["member"], org["id"], **bad)
    assert r.status_code == 400
    assert r.json()["error_code"] == code


@pytest.mark.e2e
def test_other_member_and_admin_cannot_read_or_change_someone_elses_memory(test_client, org):
    o = org["id"]
    mine = _add(test_client, org["member"], o, text="Presents to the CFO monthly", section="role").json()
    other_token, _ = org["add_member"]()
    for token in (other_token, org["admin"]):
        listed = _list(test_client, token, o).json()
        assert all(mine["id"] != e["id"] for sec in listed["sections"].values() for e in sec)
        assert "CFO" not in listed["preview"]
        assert test_client.patch(f"/api/users/me/memory/{mine['id']}", json={"text": "hacked"},
                                 headers=_h(token, o)).status_code == 404
        assert test_client.delete(f"/api/users/me/memory/{mine['id']}", headers=_h(token, o)).status_code == 404
        # "Forget everything" only ever touches the caller's own entries.
        test_client.delete("/api/users/me/memory", headers=_h(token, o))
    still = _list(test_client, org["member"], o).json()["sections"]["role"]
    assert [e["text"] for e in still] == ["Presents to the CFO monthly"]


@pytest.mark.e2e
def test_removing_membership_deletes_its_memory(test_client, org):
    o = org["id"]
    tok, uid = org["add_member"]()
    assert _add(test_client, tok, o, text="Likes cohort charts", section="preferences").status_code == 200
    members = test_client.get(f"/api/organizations/{o}/members", headers=_h(org["admin"], o)).json()
    membership_id = next(m["id"] for m in members if (m.get("user") or {}).get("id") == uid)
    r = test_client.delete(f"/api/organizations/{o}/members/{membership_id}", headers=_h(org["admin"], o))
    assert r.status_code in (200, 204), r.text

    from sqlalchemy import select, func
    from app.dependencies import async_session_maker
    from app.models.memory_entry import MemoryEntry

    async def count():
        async with async_session_maker() as db:
            return (await db.execute(select(func.count()).select_from(MemoryEntry).where(
                MemoryEntry.user_id == uid, MemoryEntry.organization_id == o))).scalar()
    assert asyncio.run(count()) == 0


# ---------------------------------------------------------------------------
# Agent: catalog, <memory> injection, saving on noticing
# ---------------------------------------------------------------------------

def _stub_agent(monkeypatch, captured: list, act=None):
    """Stub the LLM loop: record what the planner would get, optionally act."""
    async def fake_main_execution(self):
        from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
        from app.schemas.ai.planner import PlannerInput

        # Same run bookkeeping the real loop starts with.
        self.current_execution = await self.project_manager.start_agent_execution(
            self.db, completion_id=str(self.system_completion_id), organization_id=str(self.organization.id),
            user_id=str(self.head_completion.user_id), report_id=str(self.report_id),
        )
        name, _note, memory, _attrs = await self._resolve_user_profile()
        built = PromptBuilderV3.build(PlannerInput(user_message="x", user_name=name, user_memory=memory))
        seen = {
            "tools": {t.name for t in self.planner.tool_catalog},
            "user_turn": built.messages[0]["content"],
            "injected_ids": list(self._memory_injected_ids),
            "hint": self._memory_hint() or "",
        }
        if act is not None:
            seen["act"] = await act(self)
        captured.append(seen)
        await self._stamp_memory_trace()
        self.system_completion.status = "success"
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _runtime_ctx(agent):
    return {
        "db": agent.db, "session_maker": agent._session_maker, "organization": agent.organization,
        "user": agent.head_completion.user, "settings": agent.organization_settings, "report": agent.report,
        "head_completion": agent.head_completion, "mode": agent.mode,
        "memory_trace": agent._memory_trace, "memory_injected_ids": agent._memory_injected_ids,
    }


async def _run_tool(tool, args, agent):
    """Run a real memory tool with the agent's runtime context and persist the
    ToolExecution row the way the loop does (only a live LLM run makes these)."""
    from app.models.tool_execution import ToolExecution

    events = [e async for e in tool.run_stream(args, _runtime_ctx(agent))]
    end = [e for e in events if e.type == "tool.end"][-1].payload
    from app.models.completion_block import CompletionBlock

    te = ToolExecution(
        agent_execution_id=str(agent.current_execution.id), tool_name=tool.name, tool_action=tool.name,
        arguments_json=args, result_json=end["output"], status="success" if end["output"]["success"] else "error",
        success=bool(end["output"]["success"]),
    )
    agent.db.add(te)
    await agent.db.flush()
    agent._stub_block_index = getattr(agent, "_stub_block_index", -1) + 1
    agent.db.add(CompletionBlock(
        completion_id=str(agent.system_completion.id), agent_execution_id=str(agent.current_execution.id),
        source_type="tool", tool_execution_id=str(te.id), block_index=agent._stub_block_index,
        title=args.get("title") or tool.name, status="completed",
    ))
    await agent.db.commit()
    return end


def _new_report(create_report, token, org_id, title="Memory test"):
    return create_report(title=title, user_token=token, org_id=org_id, data_sources=[])


def _ask(test_client, report_id, token, org_id, content):
    r = test_client.post(f"/api/reports/{report_id}/completions",
                         json={"prompt": {"content": content, "mentions": [{}]}}, headers=_h(token, org_id))
    assert r.status_code == 200, r.json()


@pytest.mark.e2e
def test_setting_off_removes_tools_block_and_api_but_keeps_entries(
    monkeypatch, test_client, create_report, org,
):
    t, o = org["member"], org["id"]
    assert _add(test_client, t, o, text="Prefers the number first", section="style").status_code == 200
    captured: list = []
    _stub_agent(monkeypatch, captured)
    report = _new_report(create_report, t, o)

    _ask(test_client, report["id"], t, o, "revenue last month")
    assert MEMORY_TOOLS <= captured[-1]["tools"]
    assert "<memory>" in captured[-1]["user_turn"] and "Prefers the number first" in captured[-1]["user_turn"]

    _set_memory_enabled(test_client, org, False)
    _ask(test_client, report["id"], t, o, "revenue this month")
    assert not (MEMORY_TOOLS & captured[-1]["tools"])
    assert "<memory>" not in captured[-1]["user_turn"]
    r = _list(test_client, t, o)
    assert r.status_code == 403 and r.json()["error_code"] == "memory.disabled"
    assert _add(test_client, t, o, text="x", section="style").status_code == 403

    _set_memory_enabled(test_client, org, True)
    body = _list(test_client, t, o).json()
    assert [e["text"] for e in body["sections"]["style"]] == ["Prefers the number first"]


@pytest.mark.e2e
def test_agent_saves_style_correction_with_evidence_and_next_turn_sees_it(
    monkeypatch, test_client, create_report, org,
):
    from app.ai.tools.implementations.create_memory import CreateMemoryTool

    t, o = org["member"], org["id"]
    captured: list = []
    correction = "Too long. Shorter please, and put the number first."

    async def act(agent):
        if "Shorter please" not in (agent.head_completion.prompt or {}).get("content", ""):
            return None
        return await _run_tool(CreateMemoryTool(), {
            "text": "Prefers short answers with the number first", "section": "style",
            "tags": ["format"], "title": "Noting your preferred format",
        }, agent)

    _stub_agent(monkeypatch, captured, act)
    report = _new_report(create_report, t, o)
    _ask(test_client, report["id"], t, o, correction)
    assert captured[-1]["act"]["output"]["success"] is True
    assert "<memory>" not in captured[-1]["user_turn"]  # nothing remembered before

    entry = _list(test_client, t, o).json()["sections"]["style"][0]
    assert entry["source"] == "agent"
    assert entry["evidence"]["report_id"] == report["id"]
    assert entry["evidence"]["report_link"] == f"/reports/{report['id']}"
    assert entry["evidence"]["quote"] and entry["evidence"]["quote"] in correction

    other = _new_report(create_report, t, o, title="Another report")
    _ask(test_client, other["id"], t, o, "revenue by month")
    assert "Prefers short answers with the number first" in captured[-1]["user_turn"]
    assert f"[{entry['handle']}] style:" in captured[-1]["user_turn"]
    # The injected style entry is named next to the ask so the answer applies it.
    assert "<memory_apply>" in captured[-1]["hint"] and f"[{entry['handle']}]" in captured[-1]["hint"]

    # The report timeline never exposes the entry text — only the status line.
    comps = test_client.get(f"/api/reports/{report['id']}/completions", headers=_h(t, o)).text
    assert "Prefers short answers" not in comps


@pytest.mark.e2e
def test_parallel_create_memory_from_different_reports_both_persist(
    monkeypatch, test_client, create_report, org,
):
    from app.ai.tools.implementations.create_memory import CreateMemoryTool

    t, o = org["member"], org["id"]
    agents: list = []

    async def act(agent):
        agents.append(agent)
        return None

    captured: list = []
    _stub_agent(monkeypatch, captured, act)
    r1, r2 = _new_report(create_report, t, o, "A"), _new_report(create_report, t, o, "B")
    _ask(test_client, r1["id"], t, o, "one")
    _ask(test_client, r2["id"], t, o, "two")

    async def both():
        from app.dependencies import async_session_maker
        from app.models.organization import Organization
        from app.models.report import Report
        from app.models.user import User
        from types import SimpleNamespace

        async def one(report_id, text):
            async with async_session_maker() as db:
                ctx = {
                    "db": db, "session_maker": async_session_maker,
                    "organization": await db.get(Organization, o), "user": await db.get(User, org["member_id"]),
                    "settings": None, "report": await db.get(Report, report_id),
                    "head_completion": SimpleNamespace(id=str(uuid.uuid4()), prompt={"content": text},
                                                       trigger_source=None, scheduled_prompt_id=None, webhook_id=None),
                    "mode": "chat", "memory_trace": {}, "memory_injected_ids": [],
                }
                events = [e async for e in CreateMemoryTool().run_stream(
                    {"text": text, "section": "preferences", "tags": ["parallel"]}, ctx)]
                return events[-1].payload["output"]
        return await asyncio.gather(one(r1["id"], "Wants the SQL shown"), one(r2["id"], "Asks before expensive queries"))

    outs = asyncio.run(both())
    assert all(x["success"] for x in outs)
    texts = {e["text"] for e in _list(test_client, t, o).json()["sections"]["preferences"]}
    assert texts == {"Wants the SQL shown", "Asks before expensive queries"}


@pytest.mark.e2e
def test_search_memory_returns_hidden_entries_with_source_link(monkeypatch, test_client, create_report, org):
    from app.ai.tools.implementations.search_memory import SearchMemoryTool

    t, o = org["member"], org["id"]
    _add(test_client, t, o, text="\"my region\" = EMEA", section="vocabulary", tags=["emea"], aliases=["my region"])
    _add(test_client, t, o, text="Investigating churn in the enterprise tier", section="focus", tags=["churn"])
    captured: list = []

    async def act(agent):
        return await _run_tool(SearchMemoryTool(), {"query": "enterprise churn"}, agent)

    _stub_agent(monkeypatch, captured, act)
    report = _new_report(create_report, t, o)
    _ask(test_client, report["id"], t, o, "revenue in my region")
    seen = captured[-1]
    # The alias match put the vocabulary entry in the matched tier...
    assert "← matched" in seen["user_turn"] and "my region" in seen["user_turn"]
    # ...and search_memory finds the hidden focus entry, excluding injected ones.
    found = seen["act"]["observation"]["entries"]
    assert [e["text"] for e in found] == ["Investigating churn in the enterprise tier"]
    assert seen["act"]["observation"]["excluded_injected"] == len(seen["injected_ids"]) >= 1


@pytest.mark.e2e
def test_machine_turns_get_memory_block_but_no_memory_tools(
    monkeypatch, test_client, create_report, create_scheduled_prompt, org,
):
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.machine_turn import run_machine_turn
    from app.services.scheduled_prompt_service import scheduled_prompt_service

    t, o = org["member"], org["id"]
    _add(test_client, t, o, text="Prefers amounts in €M", section="style")
    captured: list = []
    _stub_agent(monkeypatch, captured)
    report = _new_report(create_report, t, o)

    async def machine(trigger):
        async with async_session_maker() as db:
            await run_machine_turn(
                db, report=await db.get(Report, report["id"]), user=await db.get(User, org["member_id"]),
                organization=await db.get(Organization, o), summary="wake", trigger_source=trigger,
                message_type="wait_wake", instruction="continue",
            )

    for trigger in ("wait", "checkin"):
        asyncio.run(machine(trigger))
        assert not (MEMORY_TOOLS & captured[-1]["tools"]), trigger
        assert "Prefers amounts in €M" in captured[-1]["user_turn"], trigger

    sp = create_scheduled_prompt(report["id"], prompt={"content": "weekly revenue"}, user_token=t, org_id=o)
    asyncio.run(scheduled_prompt_service.scheduled_run_prompt(sp["id"], force=True))
    assert not (MEMORY_TOOLS & captured[-1]["tools"])
    assert "Prefers amounts in €M" in captured[-1]["user_turn"]

    # A human turn on the same report does get the tools.
    _ask(test_client, report["id"], t, o, "and last week?")
    assert MEMORY_TOOLS <= captured[-1]["tools"]


@pytest.mark.e2e
def test_trace_shows_memory_text_only_to_its_owner(monkeypatch, test_client, create_report, org):
    from app.ai.tools.implementations.create_memory import CreateMemoryTool

    o, t = org["id"], org["member"]
    _add(test_client, t, o, text="Presents to the CFO monthly", section="role")
    captured: list = []

    async def act(agent):
        await _run_tool(CreateMemoryTool(), {"text": "Revenue means net revenue excluding VAT",
                                             "section": "vocabulary", "tags": ["revenue"]}, agent)
        return await _run_tool(CreateMemoryTool(), {"text": "Prefers amounts in €M", "section": "style",
                                                    "tags": ["currency"]}, agent)

    _stub_agent(monkeypatch, captured, act)
    report = _new_report(create_report, t, o)
    _ask(test_client, report["id"], t, o, "Use €M please. Revenue means net revenue excluding VAT.")

    admin = test_client.get(f"/api/console/reports/{report['id']}/conversation", headers=_h(org["admin"], o))
    assert admin.status_code == 200, admin.text
    mem = admin.json()["turns"][-1]["memory"]
    assert mem["owner_view"] is False
    assert {i["handle"] for i in mem["injected"]} and all(i["text"] is None for i in mem["injected"])
    assert [c["tool"] for c in mem["tool_calls"]] == ["create_memory", "create_memory"]
    assert mem["refusals"] and mem["refusals"][0]["code"] == "memory.looks_like_rule"
    for secret in ("Presents to the CFO", "Prefers amounts in €M", "net revenue excluding VAT"):
        # Not in the memory section, and not anywhere else in the admin's payload
        # (the user's own prompt aside, which the admin legitimately sees).
        assert secret not in str(mem)
    assert "Presents to the CFO" not in admin.text and "Prefers amounts in €M" not in admin.text

    # The per-turn trace (tool arguments/results) is redacted for everyone.
    ae_id = admin.json()["turns"][-1]["agent_execution_id"]
    per_turn = test_client.get(f"/api/console/agent_executions/{ae_id}", headers=_h(org["admin"], o))
    assert per_turn.status_code == 200, per_turn.text
    tool_blocks = [b for b in per_turn.json()["completion_blocks"] if b.get("tool_execution")]
    assert {b["tool_execution"]["tool_name"] for b in tool_blocks} == {"create_memory"}
    assert "Prefers amounts in €M" not in per_turn.text and "net revenue excluding VAT" not in str(tool_blocks)
    # The member's own report timeline is redacted the same way (the tool card
    # renders a status line only; details live in the profile).
    timeline = test_client.get(f"/api/reports/{report['id']}/completions", headers=_h(t, o)).text
    assert "Prefers amounts in €M" not in timeline


@pytest.mark.e2e
def test_owner_sees_memory_text_in_trace(monkeypatch, test_client, create_report, org):
    """The report owner who is also an org admin views their own trace."""
    from app.ai.tools.implementations.create_memory import CreateMemoryTool

    t, o = org["admin"], org["id"]
    _add(test_client, t, o, text="Presents to the CFO monthly", section="role")
    captured: list = []

    async def act(agent):
        await _run_tool(CreateMemoryTool(), {"text": "Always filter out test accounts",
                                             "section": "preferences", "tags": ["filters"]}, agent)

    _stub_agent(monkeypatch, captured, act)
    report = _new_report(create_report, t, o)
    _ask(test_client, report["id"], t, o, "revenue please")
    mem = test_client.get(f"/api/console/reports/{report['id']}/conversation", headers=_h(t, o)).json()["turns"][-1]["memory"]
    assert mem["owner_view"] is True
    assert any(i["text"] == "Presents to the CFO monthly" for i in mem["injected"])
    assert mem["refusals"][0]["text"] == "Always filter out test accounts"
    assert mem["chars"] > 0
