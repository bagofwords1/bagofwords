"""E2E tests for the nightly user dream (docs/design/overnight-learning.md).

The user dream writes two things only — memory (through MemoryService) and
planned check-ins (through CheckinService) — so these tests check it through
those existing surfaces: the memory API and the check-in rows.

Real app throughout — routes, CompletionService, MemoryService, CheckinService,
the dream runtime and the DB — with only the boundaries stubbed:

  * the LLM: the agent's own turn is a scripted stand-in (it just answers),
    and the dream's one reflection call returns a scripted proposal built
    from the prompt it was shown (so every reference is a key the prompt
    really contained);
  * the clock: the dream runs at a fixed ``now`` just after the seeded turns;
  * the scheduler: check-ins are planned without arming a job.
"""
from __future__ import annotations

import asyncio
import functools
import re
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.ai.agents.dreams.user_prompts import FollowUpOp, MemoryOp, UserDreamProposal
from app.dependencies import async_session_maker
from app.models.agent_checkin import AgentCheckin
from app.models.completion import Completion
from app.models.dream_run import DreamRun
from app.models.organization_settings import OrganizationSettings
from app.services.dreams.runtime import DreamRuntime
from app.services.dreams.user_dream import run_user_dream


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


class _Agent:
    """Stand-in for AgentV2 (the LLM loop): answers every human turn."""

    def __init__(self, **kw):
        self.kw = kw

    async def main_execution(self):
        system = self.kw["system_completion"]
        system.completion = {"content": "Here you go."}
        system.status = "success"
        await self.kw["db"].commit()


class _Reflect:
    """The reflection boundary: records the prompt, returns fn(prompt)."""

    def __init__(self, fn=None, before=None):
        self.fn = fn or (lambda p: UserDreamProposal())
        self.before = before
        self.prompts: list[str] = []

    async def __call__(self, model, prompt, *, run_id):
        self.prompts.append(prompt)
        if self.before:
            await self.before()
        return self.fn(prompt)


def _report_key(prompt: str, title: str) -> str:
    m = re.search(r"- (r\d+)[^\n]*\"" + re.escape(title) + "\"", prompt)
    assert m, f"report {title!r} not in prompt:\n{prompt}"
    return m.group(1)


def _handle(prompt: str, text: str) -> str:
    m = re.search(r"- (m\d+) [^\n]*" + re.escape(text[:30]), prompt)
    assert m, f"memory {text!r} not in prompt"
    return m.group(1)


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_report, update_organization_settings, monkeypatch):
    import app.ai.agents.checkins.planner as planner_mod
    import app.services.completion_service as completion_mod

    async def _decline(model, **kw):
        return '{"propose": false, "reason": "test"}'

    monkeypatch.setattr(completion_mod, "AgentV2", _Agent)
    monkeypatch.setattr(planner_mod, "call_small_model", _decline)

    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    r = test_client.post("/api/llm/providers", headers=_h(admin_token, org_id), json={
        "name": f"openai-{uuid.uuid4().hex[:6]}", "provider_type": "openai",
        "credentials": {"api_key": "sk-test-not-used"},
        "models": [{"model_id": "gpt-6-luna", "name": "GPT-6 Luna", "is_custom": False, "is_default": True}],
    })
    assert r.status_code == 200, r.text

    def add_member():
        email = f"dream_{uuid.uuid4().hex[:8]}@test.com"
        r = test_client.post(f"/api/organizations/{org_id}/members",
                             json={"organization_id": org_id, "email": email, "role": "member"},
                             headers=_h(admin_token, org_id))
        assert r.status_code == 200, r.text
        create_user(email=email, password="test123")
        token = login_user(email, "test123")
        return token, whoami(token)["id"]

    class E:
        pass

    e = E()
    e.client, e.org_id, e.admin = test_client, org_id, admin_token
    e.token, e.user_id = add_member()
    e.headers = _h(e.token, org_id)
    e.add_member = add_member

    def settings(**cfg):
        update_organization_settings({k: {"value": v} for k, v in cfg.items()}, user_token=admin_token, org_id=org_id)

    def report(title, token=None):
        return create_report(title=title, user_token=token or e.token, org_id=org_id, data_sources=[])["id"]

    def turn(report_id, prompt, token=None):
        resp = test_client.post(f"/api/reports/{report_id}/completions", headers=_h(token or e.token, org_id),
                                json={"prompt": {"content": prompt}})
        assert resp.status_code == 200, resp.text

    def dream(reflect, user_id=None, **kw):
        rt = DreamRuntime(user_dream=functools.partial(run_user_dream, reflect=reflect, model=object(),
                                                       arm_checkins=False))
        # Each dream runs "now" (after the turns it should read), shifted by
        # e.nights so a later call lands on a later org-local night.
        now = datetime.utcnow() + timedelta(days=e.nights)
        return _run(rt.run_unit("user", org_id, user_id or e.user_id, now=now, ignore_window=True, **kw))

    e.settings, e.report, e.turn, e.dream = settings, report, turn, dream
    settings(enable_user_memory=True)
    # The night window itself is covered in test_overnight_common /
    # test_agent_dream; here dreams run at the real clock.
    e.now, e.nights = datetime.utcnow(), 0
    return e


def _checkins(user_id):
    async def _q():
        async with async_session_maker() as db:
            return list((await db.execute(select(AgentCheckin).where(AgentCheckin.user_id == user_id))).scalars().all())
    return _run(_q())


def _memory(env, token=None):
    r = env.client.get("/api/users/me/memory", headers=_h(token or env.token, env.org_id))
    assert r.status_code == 200, r.text
    return r.json()["entries"]


# ── inputs ──────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_reflection_reads_only_the_users_own_human_turns(env):
    mine = env.report("Churn deep dive")
    env.turn(mine, "Churn by plan for Q3 please")
    machine = env.report("Nightly KPIs")
    env.turn(machine, "MACHINE-ONLY scheduled refresh prompt")

    async def _as_scheduled():
        # Direct write: mark the turn as a scheduled run (the scheduler path
        # isn't what this test is about).
        async with async_session_maker() as db:
            await db.execute(update(Completion).where(Completion.report_id == machine, Completion.role == "user")
                             .values(trigger_source="scheduled"))
            await db.commit()
    _run(_as_scheduled())

    other_token, _ = env.add_member()
    theirs = env.report("Their private report", token=other_token)
    env.turn(theirs, "OTHER-USER question about payroll", token=other_token)

    reflect = _Reflect()
    run = env.dream(reflect)
    assert run.status == "done", (run.status, run.status_reason)
    prompt = reflect.prompts[0]
    assert "Churn by plan for Q3" in prompt
    assert "MACHINE-ONLY" not in prompt and "OTHER-USER" not in prompt


@pytest.mark.e2e
def test_nothing_new_skips_without_a_model_call(env):
    reflect = _Reflect()
    run = env.dream(reflect)
    assert run.status == "skipped" and run.status_reason == "nothing_new"
    assert reflect.prompts == []


def _turn_in_someone_elses_report(env, other_token):
    """A turn by our user in a report another member owns. Direct write:
    setting up a shared conversation isn't what these tests are about."""
    theirs = env.report("Their report", token=other_token)

    async def _w():
        async with async_session_maker() as db:
            db.add(Completion(prompt={"content": "Look at their churn"}, completion={"content": ""}, role="user",
                              status="success", model="m", report_id=theirs, user_id=env.user_id,
                              turn_index=0, message_type="table", sigkill=None))
            await db.commit()
    _run(_w())
    return theirs


def _tomorrow_10(env):
    return (env.now + timedelta(days=1)).strftime("%Y-%m-%dT10:00")


@pytest.mark.e2e
def test_an_upcoming_memory_event_wakes_the_dream_without_new_turns(env):
    when = (env.now + timedelta(days=2)).date().isoformat()
    r = env.client.post("/api/users/me/memory", json={"text": "Board meeting", "date": when}, headers=env.headers)
    assert r.status_code == 200, r.text
    reflect = _Reflect()
    run = env.dream(reflect)
    assert run.status == "done" and len(reflect.prompts) == 1
    assert "Board meeting" in reflect.prompts[0].split("UPCOMING:")[1]


# ── outputs: memory + planned check-ins, nothing else ─────────────────────────

@pytest.mark.e2e
def test_a_night_updates_memory_and_plans_a_check_in(env):
    env.settings(enable_agent_checkins=True)
    board_day = (env.now + timedelta(days=2)).date().isoformat()
    rid = env.report("Board prep")
    env.turn(rid, f"I need churn by plan ready for the board meeting on {board_day}")

    def propose(prompt):
        r = _report_key(prompt, "Board prep")
        return UserDreamProposal(
            memory=[MemoryOp(op="create", text="Board meeting", tags=["board"], event_start=board_day, source=r)],
            follow_ups=[FollowUpOp(report=r, due_local=_tomorrow_10(env),
                                   note="Re-run churn by plan; tell me if it moved >0.5pt",
                                   why="Board meeting in two days")],
        )

    run = env.dream(_Reflect(propose))
    assert run.status == "done", run.outputs
    assert set(run.outputs) == {"memory", "follow_ups", "refused", "summary"}

    mem = [m for m in _memory(env) if m["text"] == "Board meeting"]
    assert len(mem) == 1 and mem[0]["source"] == "dream" and mem[0]["date"].startswith(board_day)

    rows = _checkins(env.user_id)
    assert len(rows) == 1 and rows[0].status == "planned" and rows[0].origin == "dream"
    assert rows[0].dream_run_id == run.id and rows[0].report_id == rid
    assert "churn by plan" in rows[0].note.lower()


@pytest.mark.e2e
def test_dream_never_edits_what_the_user_wrote_and_memory_rules_still_apply(env):
    r = env.client.post("/api/users/me/memory", json={"text": "Leads the EMEA expansion"}, headers=env.headers)
    assert r.status_code == 200, r.text
    rid = env.report("EMEA")
    env.turn(rid, "EMEA pipeline by country")

    def propose(prompt):
        h = _handle(prompt, "Leads the EMEA expansion")
        return UserDreamProposal(memory=[
            MemoryOp(op="update", handle=h, text="Leads APAC now"),
            MemoryOp(op="forget", handle=h),
            MemoryOp(op="forget", handle="m999"),
            MemoryOp(op="create", text="Always compute revenue net of VAT"),
            MemoryOp(op="create", text="my password: hunter22"),
        ])

    run = env.dream(_Reflect(propose))
    assert run.status == "done"
    texts = [m["text"] for m in _memory(env)]
    assert texts == ["Leads the EMEA expansion"]
    reasons = [x["reason"] for x in run.outputs["refused"] if x["kind"] == "memory"]
    assert reasons.count("user_authored") == 2 and "unknown_handle" in reasons
    assert sum(1 for x in reasons if x.startswith("memory.")) == 2


@pytest.mark.e2e
@pytest.mark.parametrize("checkins_on,opted_out", [(False, False), (True, True)])
def test_check_ins_need_checkins_on_and_the_user_not_opted_out(env, checkins_on, opted_out):
    env.settings(enable_agent_checkins=checkins_on)
    if opted_out:
        r = env.client.put("/api/users/me/checkins", json={"enabled": False}, headers=env.headers)
        assert r.status_code == 200, r.text
    rid = env.report("Pipeline")
    env.turn(rid, "Pipeline for next week")

    def propose(prompt):
        return UserDreamProposal(
            memory=[MemoryOp(op="create", text="Tracks next week's pipeline")],
            follow_ups=[FollowUpOp(report=_report_key(prompt, "Pipeline"), due_local=_tomorrow_10(env),
                                   note="Check the pipeline")],
        )

    run = env.dream(_Reflect(propose))
    assert run.status == "done"
    assert _checkins(env.user_id) == []
    assert any(x["kind"] == "follow_up" for x in run.outputs["refused"])
    # Memory is independent of the check-in switches.
    assert [m["text"] for m in _memory(env)] == ["Tracks next week's pipeline"]


@pytest.mark.e2e
def test_check_ins_only_on_reports_the_user_owns_and_keys_it_was_shown(env):
    env.settings(enable_agent_checkins=True)
    mine = env.report("Mine")
    env.turn(mine, "Revenue by month")
    other_token, _ = env.add_member()
    _turn_in_someone_elses_report(env, other_token)

    def propose(prompt):
        return UserDreamProposal(follow_ups=[
            FollowUpOp(report=_report_key(prompt, "Their report"), due_local=_tomorrow_10(env), note="theirs"),
            FollowUpOp(report="r99", due_local=_tomorrow_10(env), note="unknown key"),
        ])

    run = env.dream(_Reflect(propose))
    assert _checkins(env.user_id) == []
    reasons = [x["reason"] for x in run.outputs["refused"] if x["kind"] == "follow_up"]
    assert reasons == ["not_owner_or_unknown", "not_owner_or_unknown"]


@pytest.mark.e2e
def test_at_most_two_check_ins_a_night(env):
    env.settings(enable_agent_checkins=True, checkins_max_per_user_per_week=10)
    rids = [env.report(f"Report {i}") for i in range(3)]
    for rid in rids:
        env.turn(rid, "Numbers please")

    def propose(prompt):
        return UserDreamProposal(follow_ups=[
            FollowUpOp(report=_report_key(prompt, f"Report {i}"), due_local=_tomorrow_10(env), note=f"Check {i}")
            for i in range(3)
        ])

    env.dream(_Reflect(propose))
    assert len(_checkins(env.user_id)) == 2


# ── switches / runtime ──────────────────────────────────────────────────────

@pytest.mark.e2e
def test_the_dream_needs_user_memory_on(env):
    rid = env.report("Churn")
    env.turn(rid, "Churn by plan")
    env.settings(enable_user_memory=False)
    reflect = _Reflect()
    run = env.dream(reflect)
    assert run.status == "cancelled" and reflect.prompts == []


@pytest.mark.e2e
def test_switching_off_during_the_reflection_writes_nothing(env):
    env.settings(enable_agent_checkins=True)
    rid = env.report("Churn")
    env.turn(rid, "Churn by plan")

    async def flip_off():
        async with async_session_maker() as db:
            s = (await db.execute(select(OrganizationSettings).where(
                OrganizationSettings.organization_id == env.org_id))).scalar_one()
            cfg = dict(s.config or {})
            cfg["enable_user_memory"] = {"value": False}
            s.config = cfg
            await db.commit()

    reflect = _Reflect(lambda p: UserDreamProposal(
        memory=[MemoryOp(op="create", text="Follows churn weekly")],
        follow_ups=[FollowUpOp(report=_report_key(p, "Churn"), due_local=_tomorrow_10(env), note="Re-run churn")],
    ), before=flip_off)
    run = env.dream(reflect)
    assert run.status == "cancelled" and len(reflect.prompts) == 1
    assert _memory(env) == [] and _checkins(env.user_id) == []


@pytest.mark.e2e
def test_watermark_moves_so_the_next_night_reads_only_new_turns(env):
    rid = env.report("Churn")
    env.turn(rid, "FIRST-NIGHT question")
    env.dream(_Reflect())
    env.turn(rid, "SECOND-NIGHT question")
    env.nights += 1
    reflect = _Reflect()
    run = env.dream(reflect)
    assert run.status == "done"
    assert "SECOND-NIGHT" in reflect.prompts[0] and "FIRST-NIGHT" not in reflect.prompts[0]

    async def _runs():
        async with async_session_maker() as db:
            return (await db.execute(select(DreamRun).where(DreamRun.user_id == env.user_id))).scalars().all()
    assert len(_run(_runs())) == 2
