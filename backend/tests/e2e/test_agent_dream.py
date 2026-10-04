"""E2E tests for the nightly agent dream (docs/design/overnight-learning.md §4).

Everything runs through the real services — the runtime, BuildService,
InstructionService, the Self-Learning policy (run_for_suggestion), the inbox —
with only the boundaries stubbed: the one consolidation model call returns a
scripted proposal, and the clock is fixed inside the org-local agent window.

Seeding: orgs, agents and members are written through the models, and AI
suggestion drafts through InstructionService/BuildService exactly as the
knowledge harness stages them. The one direct write is back-dating a build's
``created_at`` — history no API can produce.
"""
from __future__ import annotations

import asyncio
import functools
import re
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.ai.agents.dreams.agent_prompts import AgentDreamProposal, ProposedArchive, ProposedGroup
from app.dependencies import async_session_maker
from app.models.agent_automation_run import AgentAutomationRun
from app.models.build_content import BuildContent
from app.models.data_source import DataSource
from app.models.dream_run import DreamRun
from app.models.instruction import Instruction, instruction_data_source_association as ASSOC
from app.models.instruction_build import InstructionBuild
from app.models.membership import Membership
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.user import User
from app.schemas.instruction_schema import InstructionCreate
from app.services.agent_reliability_service import AgentReliabilityService
from app.services.build_service import BuildService
from app.services.dreams.agent_dream import run_agent_dream
from app.services.dreams.runtime import DreamRuntime
from app.services.instruction_service import InstructionService

# Tuesday 03:30 UTC — inside the 03:00-05:00 agent window for a UTC org.
NOW = datetime(2026, 10, 6, 3, 30)

VAT = [
    "Revenue must exclude VAT when reporting sales.",
    "When the user asks for revenue, use net amounts excluding VAT.",
    "Always report revenue net of VAT (orders.amount_net).",
]
ONE_OFF = "For the Q1 2019 audit, only use the orders_2019 table."


def _run(coro):
    return asyncio.run(coro)


# ── seeding ────────────────────────────────────────────────────────────────

async def _seed(*, settings: dict, automation: dict | None = None, members: int = 3):
    sfx = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Dream Org {sfx}")
        db.add(org)
        await db.flush()
        db.add(OrganizationSettings(organization_id=org.id, config={k: {"value": v} for k, v in settings.items()}))
        admin = User(name="Admin", email=f"admin-{sfx}@example.com", hashed_password="x",
                     is_active=True, is_superuser=False, is_verified=True)
        db.add(admin)
        await db.flush()
        db.add(Membership(user_id=admin.id, organization_id=org.id, role="admin"))
        users = []
        for i in range(members):
            u = User(name=f"Member {i}", email=f"m{i}-{sfx}@example.com", hashed_password="x",
                     is_active=True, is_superuser=False, is_verified=True)
            db.add(u)
            await db.flush()
            db.add(Membership(user_id=u.id, organization_id=org.id, role="member"))
            users.append(str(u.id))
        ds = DataSource(name=f"Sales-{sfx}", organization_id=org.id, is_active=True,
                        owner_user_id=admin.id, automation_settings=automation)
        other = DataSource(name=f"Other-{sfx}", organization_id=org.id, is_active=True, owner_user_id=admin.id)
        db.add_all([ds, other])
        await db.commit()
        return {"org": str(org.id), "admin": str(admin.id), "users": users, "ds": str(ds.id), "other_ds": str(other.id)}


async def _stage(org_id, admin_id, author_id, ds_ids, text, *, days_ago: float, source="ai",
                 live=False, ai_source="completion", created_days_ago: float | None = None):
    """Stage an instruction the way the knowledge harness does (a submitted AI
    suggestion build), or — with live=True — publish it to main."""
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        admin = await db.get(User, admin_id)
        bs = BuildService()
        build = await bs.create_build(db, org_id, source=source, user_id=author_id)
        schema = await InstructionService().create_instruction(
            db, InstructionCreate(text=text, title=text[:60], status="published" if live else "draft",
                                  category="general", data_source_ids=list(ds_ids),
                                  source_type="ai" if source == "ai" else "user",
                                  ai_source=ai_source if source == "ai" else None),
            current_user=admin, organization=org, force_global=True, build=build, auto_finalize=False,
            version_status_override="published",
        )
        if live:
            await bs.submit_build(db, str(build.id), user_id=admin_id)
            await bs.approve_build(db, str(build.id), approved_by_user_id=admin_id)
            await bs.promote_build(db, str(build.id), user_id=admin_id, trigger_reliability=False)
        else:
            await bs.submit_build(db, str(build.id), user_id=author_id)
        b = await db.get(InstructionBuild, str(build.id))
        # Direct write: back-date history (no API produces an old suggestion).
        b.created_at = NOW - timedelta(days=days_ago)
        if created_days_ago is not None:
            inst = await db.get(Instruction, str(schema.id))
            inst.created_at = NOW - timedelta(days=created_days_ago)
        await db.commit()
        return str(build.id), str(schema.id)


def _keys_in(prompt: str, *texts: str):
    """The d-keys the prompt assigned to drafts with these texts."""
    out = []
    for t in texts:
        m = re.search(r"- (d\d+): " + re.escape(t[:40]), prompt)
        assert m, f"draft not in prompt: {t}"
        out.append(m.group(1))
    return out


def _live_key(prompt: str, text: str) -> str:
    m = re.search(r"- (L\d+): [^\n]*" + re.escape(text[:30]), prompt)
    assert m, f"live instruction not in prompt: {text}"
    return m.group(1)


class _Consolidate:
    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    async def __call__(self, model, prompt, *, run_id):
        self.calls += 1
        return self.fn(prompt)


def _runtime(consolidate):
    return DreamRuntime(agent_dream=functools.partial(run_agent_dream, consolidate=consolidate, model=object()))


def _dream(rt, s, **kw):
    return _run(rt.run_unit("agent", s["org"], s["ds"], now=NOW, **kw))


async def _q(stmt):
    async with async_session_maker() as db:
        return (await db.execute(stmt)).scalars().all()


def _builds(org_id):
    return _run(_q(select(InstructionBuild).where(InstructionBuild.organization_id == org_id)
                   .order_by(InstructionBuild.build_number)))


def _nightly(org_id):
    return [b for b in _builds(org_id) if (b.title or "").startswith("Nightly ·")]


def _changed_instr(build_id):
    return _run(_q(select(BuildContent.instruction_id).where(
        BuildContent.build_id == build_id, BuildContent.is_change.is_(True))))


def _group_vat(prompt):
    return AgentDreamProposal(groups=[ProposedGroup(draft_ids=_keys_in(prompt, *VAT), title="Revenue is net of VAT",
                                                    text="Revenue means net revenue excluding VAT (orders.amount_net).")])


# ── consolidation + gates ───────────────────────────────────────────────────

@pytest.mark.e2e
def test_repeated_correction_becomes_one_scoped_build_and_backlog_merges():
    s = _run(_seed(settings={"enable_agent_dreaming": True}))
    u1, u2, u3 = s["users"]
    b1, _ = _run(_stage(s["org"], s["admin"], u1, [s["ds"]], VAT[0], days_ago=3))
    b2, _ = _run(_stage(s["org"], s["admin"], u2, [s["ds"]], VAT[1], days_ago=2))
    b3, _ = _run(_stage(s["org"], s["admin"], u3, [s["ds"]], VAT[2], days_ago=2))
    b4, _ = _run(_stage(s["org"], s["admin"], u1, [s["ds"]], ONE_OFF, days_ago=1))

    def propose(prompt):
        p = _group_vat(prompt)
        p.groups.append(ProposedGroup(draft_ids=_keys_in(prompt, ONE_OFF), title="Q1 2019", text=ONE_OFF))
        return p

    cons = _Consolidate(propose)
    run = _dream(_runtime(cons), s, ignore_window=True)

    assert run.status == "done" and cons.calls == 1
    nightly = _nightly(s["org"])
    assert len(nightly) == 1 and nightly[0].status == "pending_approval" and nightly[0].source == "ai"
    changed = _changed_instr(str(nightly[0].id))
    assert len(changed) == 1
    inst = _run(_q(select(Instruction).where(Instruction.id == changed[0])))[0]
    assert inst.ai_source == "nightly_dream" and "net" in inst.text.lower()
    scoped = _run(_q(select(ASSOC.c.data_source_id).where(ASSOC.c.instruction_id == changed[0])))
    assert scoped == [s["ds"]]
    assert "3 users" in (nightly[0].description or "")

    by_id = {str(b.id): b for b in _builds(s["org"])}
    for bid in (b1, b2, b3):
        assert by_id[bid].status == "rejected" and by_id[bid].rejection_reason.startswith("[merged]")
        assert not by_id[bid].rejected_hunks  # no reviewer verdict recorded
    assert by_id[b4].status == "pending_approval"  # the one-off is held, not merged
    assert [h["drafts"] for h in run.outputs["held"]] == [["d4"]] or len(run.outputs["held"]) == 1


@pytest.mark.e2e
def test_two_users_are_below_the_gate_so_nothing_changes():
    s = _run(_seed(settings={"enable_agent_dreaming": True}))
    u1, u2, _ = s["users"]
    b1, _ = _run(_stage(s["org"], s["admin"], u1, [s["ds"]], VAT[0], days_ago=3))
    b2, _ = _run(_stage(s["org"], s["admin"], u2, [s["ds"]], VAT[1], days_ago=1))
    # Only two authors can't clear the gate, and there's nothing else to do:
    # no model call at all.
    cons = _Consolidate(lambda p: AgentDreamProposal())
    run = _dream(_runtime(cons), s, ignore_window=True)
    assert run.status == "done" and cons.calls == 0
    assert _nightly(s["org"]) == []
    assert {b.status for b in _builds(s["org"]) if str(b.id) in (b1, b2)} == {"pending_approval"}


@pytest.mark.e2e
def test_a_group_the_model_proposes_below_the_gate_is_held():
    s = _run(_seed(settings={"enable_agent_dreaming": True}, members=4))
    u1, u2, u3, u4 = s["users"]
    for u, t in ((u1, VAT[0]), (u2, VAT[1]), (u3, VAT[2])):
        _run(_stage(s["org"], s["admin"], u, [s["ds"]], t, days_ago=1))  # all on ONE day
    # An unrelated draft on another day makes a promotion *possible* overall,
    # so the model is asked — but the VAT group it proposes spans one day.
    _run(_stage(s["org"], s["admin"], u4, [s["ds"]], ONE_OFF, days_ago=2))
    cons = _Consolidate(_group_vat)
    run = _dream(_runtime(cons), s, ignore_window=True)
    assert cons.calls == 1 and _nightly(s["org"]) == []
    assert run.outputs["held"][0]["users"] == 3 and run.outputs["held"][0]["days"] == 1


@pytest.mark.e2e
def test_expired_drafts_still_count_as_evidence():
    s = _run(_seed(settings={"enable_agent_dreaming": True, "ai_suggestion_expiry_days": 10}))
    u1, u2, u3 = s["users"]
    old, _ = _run(_stage(s["org"], s["admin"], u3, [s["ds"]], VAT[2], days_ago=20))
    # First night: the old suggestion expires (nothing else to consolidate).
    _dream(_runtime(_Consolidate(lambda p: AgentDreamProposal())), s, ignore_window=True, force=True)
    assert {str(b.id): b for b in _builds(s["org"])}[old].rejection_reason.startswith("[expired]")
    # Later: two more users say the same thing on two days → with the expired
    # one that is 3 users, so the rule is promoted.
    _run(_stage(s["org"], s["admin"], u1, [s["ds"]], VAT[0], days_ago=2))
    _run(_stage(s["org"], s["admin"], u2, [s["ds"]], VAT[1], days_ago=1))
    cons = _Consolidate(_group_vat)
    run = _dream(_runtime(cons), s, ignore_window=True, force=True)
    assert len(_nightly(s["org"])) == 1, run.outputs
    assert run.outputs["created"][0]["users"] == 3


# ── expiry ─────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("expiry,expired", [(10, True), (30, False), (0, False)])
def test_stale_ai_suggestions_expire_per_org_setting_and_human_ones_never(expiry, expired):
    s = _run(_seed(settings={"enable_agent_dreaming": True, "ai_suggestion_expiry_days": expiry}))
    u1 = s["users"][0]
    ai_build, _ = _run(_stage(s["org"], s["admin"], u1, [s["ds"]], ONE_OFF, days_ago=12))
    human_build, _ = _run(_stage(s["org"], s["admin"], u1, [s["ds"]], "Human rule about refunds.",
                                 days_ago=90, source="user"))
    run = _dream(_runtime(_Consolidate(lambda p: AgentDreamProposal())), s, ignore_window=True)
    by_id = {str(b.id): b for b in _builds(s["org"])}
    assert (by_id[ai_build].status == "rejected") is expired
    if expired:
        assert by_id[ai_build].rejection_reason.startswith("[expired]")
        assert run.outputs["expired_builds"][0]["build_id"] == ai_build
    assert by_id[human_build].status == "pending_approval"


# ── archive / edits ─────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_only_unused_ai_instructions_can_be_archived():
    s = _run(_seed(settings={"enable_agent_dreaming": True}))
    _, ai_live = _run(_stage(s["org"], s["admin"], s["admin"], [s["ds"]], "Old AI rule about fiscal weeks.",
                             days_ago=100, live=True, created_days_ago=100))
    _, human_live = _run(_stage(s["org"], s["admin"], s["admin"], [s["ds"]], "Human rule about returns.",
                                days_ago=100, live=True, source="user", created_days_ago=100))

    def propose(prompt):
        ai_key = _live_key(prompt, "Old AI rule about fiscal weeks.")
        return AgentDreamProposal(archive=[ProposedArchive(live_id=ai_key, why="obsolete")])

    run = _dream(_runtime(_Consolidate(propose)), s, ignore_window=True)
    nightly = _nightly(s["org"])
    assert len(nightly) == 1 and nightly[0].removed_count == 1
    contents = set(_run(_q(select(BuildContent.instruction_id).where(BuildContent.build_id == str(nightly[0].id)))))
    assert ai_live not in contents and human_live in contents
    assert [a["instruction_id"] for a in run.outputs["archived"]] == [ai_live]


# ── Self-Learning hand-off + notification ───────────────────────────────────

@pytest.mark.e2e
def test_auto_approve_policy_promotes_the_nightly_build_with_nightly_trigger():
    s = _run(_seed(settings={"enable_agent_dreaming": True}, automation={"mode": "auto_approve"}))
    for u, t, d in zip(s["users"], VAT, (3, 2, 1)):
        _run(_stage(s["org"], s["admin"], u, [s["ds"]], t, days_ago=d))
    run = _dream(_runtime(_Consolidate(_group_vat)), s, ignore_window=True)
    nightly = _nightly(s["org"])
    assert len(nightly) == 1 and nightly[0].is_main, run.outputs
    runs = _run(_q(select(AgentAutomationRun).where(AgentAutomationRun.data_source_id == s["ds"])))
    assert [r.trigger for r in runs] == ["nightly"]
    # Only this agent was affected (the other agent in the org is untouched).
    assert all(str(r.data_source_id) == s["ds"] for r in runs)


@pytest.mark.e2e
def test_managers_get_one_notification_per_night_only_when_something_changed():
    s = _run(_seed(settings={"enable_agent_dreaming": True}))
    for u, t, d in zip(s["users"], VAT, (3, 2, 1)):
        _run(_stage(s["org"], s["admin"], u, [s["ds"]], t, days_ago=d))
    _dream(_runtime(_Consolidate(_group_vat)), s, ignore_window=True)
    notes = _run(_q(select(Notification).where(Notification.organization_id == s["org"],
                                               Notification.type == "nightly_learning")))
    assert len(notes) == 1 and notes[0].user_id == s["admin"]
    assert "learned 1 thing" in notes[0].title
    assert notes[0].link == f"/agents/{s['ds']}"  # where the agent's pending suggestions are

    s2 = _run(_seed(settings={"enable_agent_dreaming": True}))
    _dream(_runtime(_Consolidate(lambda p: AgentDreamProposal())), s2, ignore_window=True)
    assert _run(_q(select(Notification).where(Notification.organization_id == s2["org"]))) == []


# ── runtime gates ───────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_nothing_new_skips_without_a_model_call():
    s = _run(_seed(settings={"enable_agent_dreaming": True}))
    cons = _Consolidate(lambda p: AgentDreamProposal())
    run = _dream(_runtime(cons), s, ignore_window=True)
    assert run.status == "skipped" and run.status_reason == "nothing_new" and cons.calls == 0


@pytest.mark.e2e
@pytest.mark.parametrize("settings,automation", [
    ({"enable_agent_dreaming": False}, None),
    ({"enable_agent_dreaming": True}, {"nightly_learning": False}),
])
def test_switches_off_cancel_before_any_work(settings, automation):
    s = _run(_seed(settings=settings, automation=automation))
    for u, t, d in zip(s["users"], VAT, (3, 2, 1)):
        _run(_stage(s["org"], s["admin"], u, [s["ds"]], t, days_ago=d))
    cons = _Consolidate(_group_vat)
    run = _dream(_runtime(cons), s, ignore_window=True)
    assert run.status == "cancelled" and run.status_reason == "disabled"
    assert cons.calls == 0 and _nightly(s["org"]) == []


@pytest.mark.e2e
def test_a_unit_runs_once_per_night_and_only_inside_the_window():
    s = _run(_seed(settings={"enable_agent_dreaming": True}))
    rt = _runtime(_Consolidate(lambda p: AgentDreamProposal()))
    # Outside the window (no ignore_window) nothing runs.
    assert _run(rt.run_unit("agent", s["org"], s["ds"], now=NOW.replace(hour=12))) is None
    first = _dream(rt, s)
    assert first is not None
    assert _dream(rt, s) is None  # same org-local night
    runs = _run(_q(select(DreamRun).where(DreamRun.data_source_id == s["ds"])))
    assert len(runs) == 1


@pytest.mark.e2e
def test_watermark_advances_on_done_and_not_on_failure():
    s = _run(_seed(settings={"enable_agent_dreaming": True}))
    for u, t, d in zip(s["users"], VAT, (3, 2, 1)):
        _run(_stage(s["org"], s["admin"], u, [s["ds"]], t, days_ago=d))

    def boom(prompt):
        raise RuntimeError("model exploded")

    run = _dream(_runtime(_Consolidate(boom)), s, ignore_window=True)
    assert run.status == "failed" and run.status_reason.startswith("error:")
    ds = _run(_q(select(DataSource).where(DataSource.id == s["ds"])))[0]
    assert ds.agent_dreamed_at is None
    # A retry the same night (the next sweep; force skips the hour claim).
    run2 = _dream(_runtime(_Consolidate(_group_vat)), s, ignore_window=True, force=True)
    assert run2.status == "done"
    ds = _run(_q(select(DataSource).where(DataSource.id == s["ds"])))[0]
    assert ds.agent_dreamed_at == NOW


@pytest.mark.e2e
def test_the_org_nightly_budget_stops_further_units():
    s = _run(_seed(settings={"enable_agent_dreaming": True}))

    async def _spent():
        async with async_session_maker() as db:
            db.add(DreamRun(organization_id=s["org"], kind="agent", data_source_id=s["other_ds"],
                            local_date="2026-10-06", status="done", tokens=2_000_000))
            await db.commit()
    _run(_spent())
    run = _dream(_runtime(_Consolidate(lambda p: AgentDreamProposal())), s, ignore_window=True)
    assert run.status == "skipped" and run.status_reason == "budget"


# ── suggestion scoping regression ───────────────────────────────────────────

@pytest.mark.e2e
def test_suggestion_with_several_instructions_on_one_agent_affects_only_that_agent():
    """Before: several instructions on ONE agent (or any global instruction
    copied unchanged from main) made a suggestion look like it affected every
    agent in the org."""
    s = _run(_seed(settings={}))
    # A global live instruction in main.
    _run(_stage(s["org"], s["admin"], s["admin"], [], "Global rule.", days_ago=5, live=True))

    async def _two_on_one_agent():
        async with async_session_maker() as db:
            org = await db.get(Organization, s["org"])
            admin = await db.get(User, s["admin"])
            bs = BuildService()
            build = await bs.create_build(db, s["org"], source="ai", user_id=s["admin"])
            for t in ("Rule one.", "Rule two.", "Rule three."):
                await InstructionService().create_instruction(
                    db, InstructionCreate(text=t, status="draft", category="general", data_source_ids=[s["ds"]]),
                    current_user=admin, organization=org, force_global=True, build=build, auto_finalize=False,
                    version_status_override="published",
                )
            agents = await AgentReliabilityService()._resolve_suggestion_agents(db, s["org"], str(build.id))
            return [str(a.id) for a in agents]

    assert _run(_two_on_one_agent()) == [s["ds"]]
