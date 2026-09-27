"""create_memory / edit_memory / search_memory through their public run_stream.

Contracts: validation errors come back as a failed tool.end (the agent handles
them), definitions/rules are refused with a pointer to instructions and the
refusal is recorded for the trace, evidence is filled by code, entries the
user typed are protected unless the user's message directly asks for the
change, and search_memory excludes entries already injected this turn.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.ai.tools.implementations.create_memory import CreateMemoryTool
from app.ai.tools.implementations.edit_memory import EditMemoryTool
from app.ai.tools.implementations.search_memory import SearchMemoryTool
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
    ({"text": "Board meeting", "section": "events", "tags": ["board"]}, "memory.event_date_required"),
    ({"text": "Prefers tables", "section": "style", "tags": ["a", "b", "c", "d", "e"]}, "memory.too_many_tags"),
    ({"text": "x" * 281, "section": "style", "tags": ["a"]}, "memory.text_too_long"),
    ({"text": "Prefers tables", "section": "style", "tags": []}, "memory.tags_required"),
    ({"text": "api key is sk-proj-abcdefghijklmnopqrstuv123", "section": "preferences", "tags": ["k"]}, "memory.sensitive"),
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
])
def test_create_memory_refuses_definitions_and_records_refusal(owner, text):
    uid, org = owner
    trace: dict = {}
    err = _err(_run(_call(CreateMemoryTool(), {"text": text, "section": "vocabulary", "tags": ["def"]},
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
    end = _ok(_run(_call(CreateMemoryTool(), {"text": text, "section": "vocabulary", "tags": ["Shorthand"],
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
    args = {"text": "Prefers the number first", "section": "style", "tags": ["format"]}
    first = _ok(_run(_call(CreateMemoryTool(), args, uid, org)))
    second = _ok(_run(_call(CreateMemoryTool(), {**args, "text": "prefers the NUMBER first."}, uid, org)))
    assert second["output"]["deduped_into"] == first["output"]["handle"]


def test_edit_memory_unknown_handle(owner):
    uid, org = owner
    err = _err(_run(_call(EditMemoryTool(), {"handle": "m404", "action": "delete"}, uid, org)))
    assert err["type"] == "not_found"


def _seed_user_entry(uid, org):
    async def go():
        async with async_session_maker() as db:
            r = await memory_service.create(db, organization_id=org, user_id=uid, text="Prefers amounts in €M",
                                            section="style", tags=["currency"], source="user")
            return r.entry.handle
    return _run(go())


@pytest.mark.parametrize("message", ["What was revenue last month?", "Show churn by region"])
def test_edit_memory_protects_user_entries_without_direct_request(owner, message):
    uid, org = owner
    h = _seed_user_entry(uid, org)
    for args in ({"handle": h, "action": "delete"}, {"handle": h, "action": "update", "text": "Prefers $K"}):
        err = _err(_run(_call(EditMemoryTool(), args, uid, org, message=message)))
        assert err["type"] == "memory.user_authored"


def test_edit_memory_allows_user_entry_change_on_direct_request(owner):
    uid, org = owner
    h = _seed_user_entry(uid, org)
    end = _ok(_run(_call(EditMemoryTool(), {"handle": h, "action": "update", "text": "Prefers amounts in $K"},
                         uid, org, message="Please change my currency preference: amounts in $K, not €M")))
    assert end["output"]["handle"] != h


def test_search_memory_excludes_injected_and_says_so(owner):
    uid, org = owner

    async def seed():
        async with async_session_maker() as db:
            a = await memory_service.create(db, organization_id=org, user_id=uid, text="\"my region\" = EMEA",
                                            section="vocabulary", tags=["emea"], aliases=["my region"], source="agent")
            b = await memory_service.create(db, organization_id=org, user_id=uid, text="\"my region deck\" = EMEA pack",
                                            section="vocabulary", tags=["emea"], source="agent")
            return str(a.entry.id), a.entry.handle, b.entry.handle
    aid, ah, bh = _run(seed())
    end = _ok(_run(_call(SearchMemoryTool(), {"query": "my region"}, uid, org, injected=[aid])))
    handles = [e["handle"] for e in end["observation"]["entries"]]
    assert ah not in handles and bh in handles
    assert end["observation"]["excluded_injected"] == 1


@pytest.mark.parametrize("tool,args", [
    (CreateMemoryTool(), {"text": "Prefers tables", "section": "style", "tags": ["t"]}),
    (SearchMemoryTool(), {"query": "tables"}),
])
def test_memory_tools_refuse_machine_turns_and_training(owner, tool, args):
    uid, org = owner
    assert _err(_run(_call(tool, args, uid, org, trigger_source="wait")))["type"] == "unavailable"
    assert _err(_run(_call(tool, args, uid, org, mode="training")))["type"] == "unavailable"
