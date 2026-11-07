"""MCP clients see the instructions in scope and open them on demand.

Contract:
- get_context lists every instruction in the report's scope (global + the
  report's agents), and nothing outside it. 'always' instructions come with
  their full text up to a budget; past it they are listed as required entries
  with no text. Smart instructions and skills are listed without text. Nothing
  in scope is silently left out: a capped list says so and gives the total.
- read_instruction reads several ids in one call within a size budget; ids it
  could not fit are returned as not read. With report_id it only opens
  instructions in that report's scope, and records the read for the report.
- The MCP data tools take the instruction ids the client read, and their coder
  gets those instructions in full (scoped to the report like everything else).
"""

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from app.ai.tools.mcp import get_context as get_context_module

_SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(test_client, enable_mcp, create_api_key, create_user, login_user, whoami,
          create_data_source, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    enable_mcp(user_token=token, org_id=org_id)
    key = create_api_key(user_token=token, org_id=org_id)["key"]
    ds = {
        name: create_data_source(
            name=f"{name}_{uuid.uuid4().hex[:4]}", type="sqlite", config={"database": str(_SQLITE_DB)},
            credentials={}, user_token=token, org_id=org_id,
        )
        for name in ("a", "b")
    }
    report = create_report(title="catalog", user_token=token, org_id=org_id, data_sources=[ds["a"]["id"]])

    def instruction(text, *, load_mode="always", agents=(), kind=None, title=None):
        body = {"text": text, "status": "published", "load_mode": load_mode,
                "data_source_ids": [ds[a]["id"] for a in agents]}
        if kind:
            body["kind"] = kind
        if title:
            body["title"] = title
        path = "/api/instructions" if agents else "/api/instructions/global"
        r = test_client.post(path, json=body, headers=_auth(token, org_id))
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def call(name, arguments):
        r = test_client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                               "params": {"name": name, "arguments": arguments}},
                             headers={"X-API-Key": key})
        assert r.status_code == 200, r.text
        text = r.json()["result"]["content"][0]["text"]
        return json.loads(text), len(text)

    def set_smart(on):
        r = test_client.put("/api/organization/settings", headers=_auth(token, org_id),
                            json={"config": {"smart_instructions_on_demand": {"value": on}}})
        assert r.status_code == 200, r.text

    return {"token": token, "org_id": org_id, "user_id": me["id"], "key": key, "ds": ds,
            "report_id": report["id"], "instruction": instruction, "call": call, "set_smart": set_smart,
            "http": test_client}


def _context(world):
    out, size = world["call"]("get_context", {"report_id": world["report_id"]})
    return out, size


def _by_id(out):
    return {i["id"]: i for i in out["instructions"]}


@pytest.mark.e2e
def test_get_context_lists_exactly_the_instructions_in_report_scope(world):
    tag = uuid.uuid4().hex[:6]
    mk = world["instruction"]
    in_scope = {
        mk(f"[{tag}] global always: fiscal year starts in July."): ("always", True),
        mk(f"[{tag}] agent a always: amounts are in cents.", agents=["a"]): ("always", True),
        mk(f"[{tag}] agent a smart: churn means 90 days idle.", load_mode="intelligent", agents=["a"],
           title="Churn definition"): ("intelligent", False),
        mk(f"[{tag}] global smart: revenue is net of refunds.", load_mode="intelligent",
           title="Revenue rule"): ("intelligent", False),
    }
    out_of_scope = {
        mk(f"[{tag}] agent b always", agents=["b"]),
        mk(f"[{tag}] agent b smart", load_mode="intelligent", agents=["b"], title="B only"),
    }

    out, _ = _context(world)
    listed = _by_id(out)

    assert set(in_scope) <= set(listed)
    assert not (out_of_scope & set(listed))
    for inst_id, (load_mode, inline) in in_scope.items():
        entry = listed[inst_id]
        assert entry["load_mode"] == load_mode
        if inline:
            assert entry["required"] is True
            assert entry["text"] and f"[{tag}]" in entry["text"]
        else:
            assert entry["required"] is False
            assert entry.get("text") is None
            assert entry["title"]
            assert entry["tokens"] and entry["tokens"] > 0
    assert out["instructions_total"] == len(out["instructions"])
    assert out["instructions_truncated"] is False
    assert "read_instruction" in (out.get("instructions_hint") or "")


@pytest.mark.e2e
def test_get_context_lists_skills_without_their_text(world):
    skill = world["instruction"]("Step 1: profile the table. Step 2: …", kind="skill",
                                 load_mode="intelligent", title="Profile a table")
    out, _ = _context(world)
    entry = _by_id(out).get(skill)
    assert entry is not None
    assert entry["kind"] == "skill" and entry.get("text") is None


@pytest.mark.e2e
@pytest.mark.parametrize("sizes", [[30_000, 30_000, 30_000, 30_000], [120_000], [5_000, 99_000, 5_000]])
def test_always_instructions_past_the_budget_are_listed_as_required(world, sizes):
    tag = uuid.uuid4().hex[:6]
    ids = []
    for i, n in enumerate(sizes):
        ids.append(world["instruction"]((f"[{tag}:{i}] " + "rule text line\n" * n)[:n]))

    out, size = _context(world)
    listed = _by_id(out)

    assert set(ids) <= set(listed), "an always instruction in scope is missing from get_context"
    inline = [listed[i] for i in ids if listed[i].get("text") is not None]
    deferred = [listed[i] for i in ids if listed[i].get("text") is None]
    assert all(e["required"] for e in inline + deferred)
    inline_cost = sum(len(json.dumps(e["text"])) for e in inline)
    assert inline_cost <= get_context_module.INLINE_INSTRUCTIONS_BUDGET
    if sum(sizes) > get_context_module.INLINE_INSTRUCTIONS_BUDGET:
        assert deferred, "over-budget always instructions must be listed, not inlined"
    for e in deferred:
        assert e["tokens"] > 0
    # Required entries are never the ones a capped list drops.
    assert out["instructions_truncated"] is False


@pytest.mark.e2e
def test_a_capped_catalog_says_so_and_keeps_required_entries(world, monkeypatch):
    monkeypatch.setattr(get_context_module, "INSTRUCTION_CATALOG_LIMIT", 3)
    tag = uuid.uuid4().hex[:6]
    required = world["instruction"](f"[{tag}] always rule")
    smart = [world["instruction"](f"[{tag}] smart {i}", load_mode="intelligent", title=f"Smart {i} {tag}")
             for i in range(6)]

    out, _ = _context(world)
    listed = _by_id(out)

    assert required in listed
    optional_listed = [i for i in smart if i in listed]
    assert len(optional_listed) <= 3
    assert out["instructions_truncated"] is True
    assert out["instructions_total"] >= 7
    assert out["instructions_total"] > len(out["instructions"])


@pytest.mark.e2e
def test_read_instruction_reads_several_ids_in_one_call(world):
    tag = uuid.uuid4().hex[:6]
    texts = {
        world["instruction"](f"[{tag}] one", load_mode="intelligent", agents=["a"], title="One"): f"[{tag}] one",
        world["instruction"](f"[{tag}] two"): f"[{tag}] two",
    }
    out, _ = world["call"]("read_instruction", {"instruction_ids": list(texts)})
    assert out["success"], out
    got = {i["id"]: i["text"] for i in out["instructions"]}
    assert got == texts
    assert not out.get("not_read_ids")


@pytest.mark.e2e
def test_a_batch_read_stays_within_its_budget_and_names_what_it_skipped(world):
    tag = uuid.uuid4().hex[:6]
    ids = [world["instruction"]((f"[{tag}:{i}] " + "x" * 39_000), load_mode="intelligent", title=f"Big {i}")
           for i in range(5)]

    out, size = world["call"]("read_instruction", {"instruction_ids": ids})

    read = [i["id"] for i in out["instructions"]]
    skipped = out.get("not_read_ids") or []
    assert read and skipped
    assert set(read) | set(skipped) == set(ids) and not (set(read) & set(skipped))
    assert size < get_context_module.INLINE_INSTRUCTIONS_BUDGET + 20_000
    assert out["message"]


@pytest.mark.e2e
def test_read_instruction_rejects_mixing_a_batch_with_a_window(world):
    inst = world["instruction"]("some rule")
    out, _ = world["call"]("read_instruction", {"instruction_ids": [inst], "grep_pattern": "rule"})
    assert out["success"] is False and out["error_message"]


@pytest.mark.e2e
def test_read_instruction_with_report_id_only_opens_instructions_in_its_scope(world):
    mk = world["instruction"]
    in_scope = [mk("a smart", load_mode="intelligent", agents=["a"], title="A"), mk("global rule")]
    other = mk("b smart", load_mode="intelligent", agents=["b"], title="B")

    for inst in in_scope:
        out, _ = world["call"]("read_instruction", {"instruction_id": inst, "report_id": world["report_id"]})
        assert out["success"], out
    out, _ = world["call"]("read_instruction", {"instruction_id": other, "report_id": world["report_id"]})
    assert out["success"] is False
    out, _ = world["call"]("read_instruction", {"instruction_ids": [in_scope[0], other],
                                                "report_id": world["report_id"]})
    assert [i["id"] for i in out["instructions"]] == [in_scope[0]]
    assert other in (out.get("not_found_ids") or [])
    # Without a report the caller's own visibility applies, as before.
    out, _ = world["call"]("read_instruction", {"instruction_id": other})
    assert out["success"], out


@pytest.mark.e2e
def test_a_report_scoped_read_is_recorded_for_that_report(world):
    inst = world["instruction"]("a smart rule", load_mode="intelligent", agents=["a"], title="A")
    world["call"]("read_instruction", {"instruction_id": inst, "report_id": world["report_id"]})

    # No API lists usage events per report; read the table the agent's
    # prior-reads seeding reads (ContextHub._load_prior_instruction_reads).
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.instruction_usage_event import InstructionUsageEvent

    async def _events():
        async with async_session_maker() as db:
            rows = await db.execute(select(InstructionUsageEvent).where(
                InstructionUsageEvent.report_id == world["report_id"],
                InstructionUsageEvent.instruction_id == inst,
            ))
            return [r.load_reason for r in rows.scalars().all()]

    assert "on_demand" in asyncio.run(_events())


@pytest.mark.e2e
def test_data_tools_advertise_instruction_ids(world):
    r = world["http"].post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           headers={"X-API-Key": world["key"]})
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    for name in ("create_data", "inspect_data", "create_artifact", "edit_artifact"):
        assert "instruction_ids" in tools[name]["inputSchema"]["properties"], name


async def _coder_instructions(world, instruction_ids):
    """The instructions text the MCP data tools' coder receives for this report."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.dependencies import async_session_maker
    from app.models.report import Report
    from app.models.user import User
    from app.models.organization import Organization
    from app.models.data_source import DataSource
    from app.ai.tools.mcp.context import build_rich_context

    async with async_session_maker() as db:
        report = (await db.execute(
            select(Report).options(selectinload(Report.data_sources).selectinload(DataSource.connections))
            .where(Report.id == world["report_id"])
        )).scalar_one()
        user = await db.get(User, world["user_id"])
        org = await db.get(Organization, world["org_id"])
        ctx = await build_rich_context(
            db=db, user=user, organization=org, report=report,
            prompt="list the albums", use_llm_selection=False, instruction_ids=instruction_ids,
        )
        rendered = ctx.context_hub.get_view().static.instructions
        return ctx.instructions_text, (rendered.render() if rendered else "")


@pytest.mark.e2e
@pytest.mark.parametrize("smart", [False, True])
def test_data_tools_give_their_coder_the_instructions_the_client_read(world, smart):
    world["set_smart"](smart)
    tag = uuid.uuid4().hex[:6]
    picked = world["instruction"](f"PICKED-{tag}: churn means 90 days idle.", load_mode="intelligent",
                                  agents=["a"], title=f"Zebra {tag}")
    out_of_scope = world["instruction"](f"OTHER-{tag}: b only.", load_mode="intelligent",
                                        agents=["b"], title=f"Yak {tag}")

    text, coder = asyncio.run(_coder_instructions(world, []))
    assert f"PICKED-{tag}" not in coder

    text, coder = asyncio.run(_coder_instructions(world, [picked, out_of_scope]))
    assert f"PICKED-{tag}" in coder and f"PICKED-{tag}" in text
    assert f"OTHER-{tag}" not in coder
