"""create_memory / edit_memory / search_memory / suggest_personal_instruction
through their public run_stream.

Contracts: validation errors come back as a failed tool.end (the agent handles
them); rules — definitions or how-to-answer preferences — are refused by
memory with a pointer to instructions and the refusal is recorded for the
trace; evidence is filled by code; entries the user typed are protected unless
the user's message directly asks for the change; search_memory excludes
entries already injected this turn; suggest_personal_instruction saves
nothing and reports when the user's Custom instructions already hold the rule.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.ai.tools.implementations.create_memory import CreateMemoryTool
from app.ai.tools.implementations.edit_memory import EditMemoryTool
from app.ai.tools.implementations.search_memory import SearchMemoryTool
from app.ai.tools.implementations.suggest_personal_instruction import SuggestPersonalInstructionTool
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.services.memory_service import memory_service


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def owner(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    who = whoami(token)
    return who["id"], who["organizations"][0]["id"]


async def _call(tool, tool_input, uid, org, *, message="", trace=None, injected=None, mode="chat", **head_kw):
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        organization = await db.get(Organization, org)
        head = SimpleNamespace(
            id="head-1", prompt={"content": message}, trigger_source=head_kw.get("trigger_source"),
            scheduled_prompt_id=None, webhook_id=None,
        )
        ctx = {
            "db": db, "session_maker": async_session_maker, "user": user, "organization": organization,
            "settings": None, "report": SimpleNamespace(id="rep-1"), "head_completion": head, "mode": mode,
            "memory_trace": trace if trace is not None else {}, "memory_injected_ids": injected or [],
        }
        events = [e async for e in tool.run_stream(tool_input, ctx)]
    end = [e for e in events if e.type == "tool.end"][-1].payload
    return end


def _ok(end):
    assert end["output"]["success"] is True, end
    return end


def _err(end):
    assert end["output"]["success"] is False, end
    return end["observation"]["error"]


@pytest.mark.parametrize("bad,code", [
    ({"text": "Board meeting", "tags": ["board"], "date": "next thursday"}, "memory.invalid_date"),
    ({"text": "Owns the tables project", "tags": ["a", "b", "c", "d", "e"]}, "memory.too_many_tags"),
    ({"text": "x" * 281, "tags": ["a"]}, "memory.text_too_long"),
    ({"text": "Owns the tables project", "tags": []}, "memory.tags_required"),
    ({"text": "api key is sk-proj-abcdefghijklmnopqrstuv123", "tags": ["k"]}, "memory.sensitive"),
])
def test_create_memory_validation_rejections(owner, bad, code):
    uid, org = owner
    err = _err(_run(_call(CreateMemoryTool(), bad, uid, org)))
    assert err["type"] == code
    async def count():
        async with async_session_maker() as db:
            return len(await memory_service.list_entries(db, org, uid))
    assert _run(count()) == 0


@pytest.mark.parametrize("text", [
    "Active customers are those who paid in the last 90 days",
    "Always filter out test accounts",
    "EMEA includes Turkey",
    "Revenue means net revenue excluding VAT",
    "Prefers the number first, then one line of context",
    "Amounts in thousands of dollars with one decimal",
])
def test_create_memory_refuses_rules_and_records_refusal(owner, text):
    uid, org = owner
    trace: dict = {}
    err = _err(_run(_call(CreateMemoryTool(), {"text": text, "tags": ["def"]},
                          uid, org, trace=trace)))
    assert err["type"] == "memory.looks_like_rule"
    assert "instruction" in err["message"].lower()
    assert trace["refusals"][0]["text"] == text


@pytest.mark.parametrize("text,aliases", [
    ("When I say my region I mean EMEA", ["my region"]),
    ("'the board deck' = report Q3 Board Pack", ["board deck"]),
])
def test_create_memory_accepts_personal_shorthand_with_evidence(owner, text, aliases):
    uid, org = owner
    msg = f"Quick note: {text}. Now show me Q3 revenue."
    end = _ok(_run(_call(CreateMemoryTool(), {"text": text, "tags": ["Shorthand"],
                                               "aliases": aliases}, uid, org, message=msg)))
    handle = end["output"]["handle"]

    async def get():
        async with async_session_maker() as db:
            return await memory_service.resolve_handle(db, org, uid, handle)
    e = _run(get())
    assert e.source == "agent" and e.tags == ["shorthand"]
    assert e.evidence["report_id"] == "rep-1" and e.evidence["completion_id"] == "head-1"
    assert e.evidence["quote"] and e.evidence["quote"] in msg


def test_create_memory_dedupes_into_existing(owner):
    uid, org = owner
    args = {"text": "Leads the Q3 churn project", "tags": ["churn"]}
    first = _ok(_run(_call(CreateMemoryTool(), args, uid, org)))
    second = _ok(_run(_call(CreateMemoryTool(), {**args, "text": "leads the Q3 CHURN project."}, uid, org)))
    assert second["output"]["deduped_into"] == first["output"]["handle"]


def test_edit_memory_unknown_handle(owner):
    uid, org = owner
    err = _err(_run(_call(EditMemoryTool(), {"handle": "m404", "action": "delete"}, uid, org)))
    assert err["type"] == "not_found"


def _seed_user_entry(uid, org):
    async def go():
        async with async_session_maker() as db:
            r = await memory_service.create(db, organization_id=org, user_id=uid, text="Reports to the board in €M", tags=["board"], source="user")
            return r.entry.handle
    return _run(go())


@pytest.mark.parametrize("message", ["What was revenue last month?", "Show churn by region"])
def test_edit_memory_protects_user_entries_without_direct_request(owner, message):
    uid, org = owner
    h = _seed_user_entry(uid, org)
    for args in ({"handle": h, "action": "delete"}, {"handle": h, "action": "update", "text": "Reports to the board in USD"}):
        err = _err(_run(_call(EditMemoryTool(), args, uid, org, message=message)))
        assert err["type"] == "memory.user_authored"


def test_edit_memory_allows_user_entry_change_on_direct_request(owner):
    uid, org = owner
    h = _seed_user_entry(uid, org)
    end = _ok(_run(_call(EditMemoryTool(), {"handle": h, "action": "update", "text": "Reports to the board in USD"},
                         uid, org, message="Please change it: I report to the board in USD now, not €M")))
    assert end["output"]["handle"] != h


def test_search_memory_excludes_injected_and_says_so(owner):
    uid, org = owner

    async def seed():
        async with async_session_maker() as db:
            a = await memory_service.create(db, organization_id=org, user_id=uid, text="\"my region\" = EMEA", tags=["emea"], aliases=["my region"], source="agent")
            b = await memory_service.create(db, organization_id=org, user_id=uid, text="\"my region deck\" = EMEA pack", tags=["emea"], source="agent")
            return str(a.entry.id), a.entry.handle, b.entry.handle
    aid, ah, bh = _run(seed())
    end = _ok(_run(_call(SearchMemoryTool(), {"query": "my region"}, uid, org, injected=[aid])))
    handles = [e["handle"] for e in end["observation"]["entries"]]
    assert ah not in handles and bh in handles
    assert end["observation"]["excluded_injected"] == 1


@pytest.mark.parametrize("tool,args", [
    (CreateMemoryTool(), {"text": "Owns the tables project", "tags": ["t"]}),
    (SearchMemoryTool(), {"query": "tables"}),
])
def test_memory_tools_refuse_machine_turns_and_training(owner, tool, args):
    uid, org = owner
    assert _err(_run(_call(tool, args, uid, org, trigger_source="wait")))["type"] == "unavailable"
    assert _err(_run(_call(tool, args, uid, org, mode="training")))["type"] == "unavailable"


def test_edit_memory_refuses_appending_a_different_fact(owner):
    uid, org = owner
    created = _ok(_run(_call(CreateMemoryTool(), {"text": "Leads the Q3 churn project",
                                                   "tags": ["churn"]}, uid, org)))
    h = created["output"]["handle"]
    trace: dict = {}
    err = _err(_run(_call(EditMemoryTool(), {
        "handle": h, "action": "update",
        "text": "Leads the Q3 churn project; also owns the APAC launch plan"}, uid, org, trace=trace)))
    assert err["type"] == "memory.one_fact_per_entry" and "create_memory" in err["message"]
    assert trace["refusals"][0]["code"] == "memory.one_fact_per_entry"
    # A real change of the same fact is still an edit.
    _ok(_run(_call(EditMemoryTool(), {"handle": h, "action": "update", "text": "Leads the Q4 churn project"},
                   uid, org)))



def _set_note(uid, org, note):
    from sqlalchemy import update
    from app.models.membership import Membership

    async def go():
        async with async_session_maker() as db:
            await db.execute(update(Membership).where(Membership.user_id == uid, Membership.organization_id == org)
                             .values(note=note))
            await db.commit()
    _run(go())


def test_suggest_personal_instruction_saves_nothing_and_knows_what_is_already_there(owner):
    uid, org = owner
    rule = "Lead with the number, then one line of context."
    end = _ok(_run(_call(SuggestPersonalInstructionTool(), {"text": rule}, uid, org)))
    assert end["output"]["already_saved"] is False and end["output"]["text"] == rule

    async def memory_count():
        async with async_session_maker() as db:
            return len(await memory_service.list_entries(db, org, uid))
    assert _run(memory_count()) == 0  # a rule never becomes memory

    _set_note(uid, org, "I'm the CFO.\n- lead with the number, then one line of context")
    end = _ok(_run(_call(SuggestPersonalInstructionTool(), {"text": rule}, uid, org)))
    assert end["output"]["already_saved"] is True


@pytest.mark.parametrize("bad", ["", "x" * 201, "my password: hunter22"])
def test_suggest_personal_instruction_validation(owner, bad):
    uid, org = owner
    _err(_run(_call(SuggestPersonalInstructionTool(), {"text": bad}, uid, org)))


def test_suggest_personal_instruction_unavailable_on_machine_turns(owner):
    uid, org = owner
    err = _err(_run(_call(SuggestPersonalInstructionTool(), {"text": "Lead with the number"}, uid, org,
                          trigger_source="schedule")))
    assert err["type"] == "unavailable"
