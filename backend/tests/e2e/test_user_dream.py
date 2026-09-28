"""E2E tests for the nightly user dream and the session-start briefing
(docs/design/overnight-learning.md §5–6).

Real app throughout — routes, CompletionService, MemoryService, CheckinService,
ScheduledPromptService, the dream runtime and the DB — with only the
boundaries stubbed:

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

from app.ai.agents.dreams.user_prompts import FollowUpOp, HabitOp, MemoryOp, ThreadOp, UserDreamProposal
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

    def briefing(token=None, **params):
        r = test_client.get("/api/users/me/briefing", headers=_h(token or e.token, org_id), params=params)
        assert r.status_code == 200, r.text
        return r.json()["items"]

    e.settings, e.report, e.turn, e.dream, e.briefing = settings, report, turn, dream, briefing
    settings(enable_user_dreaming=True)
    # The night window itself is covered in test_overnight_common /
    # test_agent_dream; here dreams run at the real clock.
    e.now, e.nights = datetime.utcnow(), 0
    return e


def _backdate(report_id, prompt_text, when):
    """Direct write: move a human turn into the past (no API produces
    weeks-old history)."""
    async def _w():
        async with async_session_maker() as db:
            rows = (await db.execute(select(Completion).where(
                Completion.report_id == report_id, Completion.role == "user"))).scalars().all()
            for c in rows:
                if (c.prompt or {}).get("content") == prompt_text:
                    c.created_at = when
            await db.commit()
    _run(_w())


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


# ── outputs ─────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_night_prepares_memory_threads_and_a_follow_up_shown_in_the_briefing(env):
    env.settings(enable_user_dreaming=True, enable_agent_checkins=True)
    board_day = (env.now + timedelta(days=2)).date().isoformat()
    rid = env.report("Board prep")
    env.turn(rid, f"I need churn by plan ready for the board meeting on {board_day}")

    def propose(prompt):
        r = _report_key(prompt, "Board prep")
        due = (env.now + timedelta(days=1)).strftime("%Y-%m-%dT10:00")
        return UserDreamProposal(
            memory=[MemoryOp(op="create", text="Board meeting", tags=["board"], event_start=board_day, source=r)],
            open_threads=[ThreadOp(report=r, text="Churn by plan for the board", unblocked_by="the Monday refresh")],
            follow_ups=[FollowUpOp(report=r, due_local=due, note="Re-run churn by plan; tell me if it moved >0.5pt",
                                   why="Board meeting in two days")],
        )

    run = env.dream(_Reflect(propose))
    assert run.status == "done", run.outputs

    mem = [m for m in _memory(env) if m["text"] == "Board meeting"]
    assert len(mem) == 1 and mem[0]["source"] == "dream" and mem[0]["date"].startswith(board_day)

    rows = _checkins(env.user_id)
    assert len(rows) == 1 and rows[0].status == "planned" and rows[0].origin == "dream"
    assert rows[0].dream_run_id == run.id and rows[0].report_id == rid

    items = env.briefing()
    kinds = {i["kind"] for i in items}
    assert {"thread", "event"} <= kinds
    event = next(i for i in items if i["kind"] == "event")
    assert event["prepared"] and event["prepared"]["report_id"] == rid


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
def test_follow_ups_need_checkins_on_and_not_opted_out(env, checkins_on, opted_out):
    env.settings(enable_user_dreaming=True, enable_agent_checkins=checkins_on)
    if opted_out:
        r = env.client.put("/api/users/me/checkins", json={"enabled": False}, headers=env.headers)
        assert r.status_code == 200, r.text
    rid = env.report("Pipeline")
    env.turn(rid, "Pipeline for next week")

    def propose(prompt):
        r = _report_key(prompt, "Pipeline")
        return UserDreamProposal(follow_ups=[FollowUpOp(report=r, due_local=(env.now + timedelta(days=1)).strftime("%Y-%m-%dT10:00"),
                                                        note="Check the pipeline")])

    run = env.dream(_Reflect(propose))
    assert run.status == "done"
    assert _checkins(env.user_id) == []
    assert any(x["kind"] == "follow_up" for x in run.outputs["refused"])


@pytest.mark.e2e
def test_follow_ups_only_for_reports_the_user_owns_and_keys_it_was_shown(env):
    env.settings(enable_user_dreaming=True, enable_agent_checkins=True)
    rid = env.report("Mine")
    env.turn(rid, "Revenue by month")

    def propose(prompt):
        return UserDreamProposal(follow_ups=[FollowUpOp(report="r99", due_local="2030-01-01T10:00", note="x")])

    run = env.dream(_Reflect(propose))
    assert _checkins(env.user_id) == []
    assert run.outputs["refused"][0]["reason"] == "not_owner_or_unknown"


# ── threads ─────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_open_thread_resolves_when_the_user_returns_and_not_useful_dismisses(env):
    a, b = env.report("Churn"), env.report("Margins")
    env.turn(a, "Churn by plan")
    env.turn(b, "Gross margin by product")

    def propose(prompt):
        return UserDreamProposal(open_threads=[
            ThreadOp(report=_report_key(prompt, "Churn"), text="Churn by plan, Enterprise split"),
            ThreadOp(report=_report_key(prompt, "Margins"), text="Margins for the new SKUs"),
        ])

    env.dream(_Reflect(propose))
    threads = {i["report_id"]: i for i in env.briefing() if i["kind"] == "thread"}
    assert set(threads) == {a, b}

    env.turn(a, "Back on churn: Enterprise split please")
    remaining = [i for i in env.briefing() if i["kind"] == "thread"]
    assert [i["report_id"] for i in remaining] == [b]

    r = env.client.post(f"/api/users/me/briefing/items/thread/{remaining[0]['id']}/feedback",
                        json={"useful": False}, headers=env.headers)
    assert r.status_code == 200
    assert [i for i in env.briefing() if i["kind"] == "thread"] == []


@pytest.mark.e2e
def test_a_quiet_night_keeps_yesterdays_threads(env):
    a = env.report("Churn")
    env.turn(a, "Churn by plan")
    env.dream(_Reflect(lambda p: UserDreamProposal(open_threads=[ThreadOp(report=_report_key(p, "Churn"), text="Churn")])))
    # Next night nothing new: no reflection, threads untouched.
    env.nights += 1
    run = env.dream(_Reflect())
    assert run.status == "skipped"
    assert [i["report_id"] for i in env.briefing() if i["kind"] == "thread"] == [a]


# ── habits ──────────────────────────────────────────────────────────────────

def _weekly_asks(env, rid, text="Weekly pipeline by region"):
    for w in (3, 2, 1):
        t = f"{text} (w{w})"
        env.turn(rid, t)
        _backdate(rid, t, env.now - timedelta(weeks=w))
    env.turn(rid, text)


def _offer_habit(prompt):
    m = re.search(r"- (h\d+): ", prompt)
    assert m, "no recurring ask in prompt"
    return UserDreamProposal(habit=HabitOp(recurring=m.group(1), intent="Weekly pipeline by region",
                                           cadence="weekly:mon", time="08:30"))


@pytest.mark.e2e
def test_habit_offer_accept_creates_a_normal_scheduled_task(env):
    rid = env.report("Pipeline")
    _weekly_asks(env, rid)
    run = env.dream(_Reflect(_offer_habit))
    assert run.status == "done" and run.outputs["habit"], (run.status_reason, run.outputs)

    habit = [i for i in env.briefing() if i["kind"] == "habit"]
    assert len(habit) == 1 and habit[0]["report_id"] == rid

    r = env.client.post(f"/api/users/me/habit_offers/{habit[0]['id']}/accept", headers=env.headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted" and r.json()["scheduled_prompt_id"]
    sps = env.client.get(f"/api/reports/{rid}/scheduled-prompts", headers=env.headers).json()
    assert [s["cron_schedule"] for s in sps] == ["30 8 * * 1"]

    again = env.client.post(f"/api/users/me/habit_offers/{habit[0]['id']}/accept", headers=env.headers)
    assert again.status_code == 400 and again.json()["error_code"] == "habit_offer.not_pending"
    # Already scheduled: the next night doesn't offer it again.
    env.turn(rid, "Weekly pipeline by region again")
    env.nights += 1
    run2 = env.dream(_Reflect(_offer_habit))
    assert run2.outputs["habit"] is None
    assert {"kind": "habit", "reason": "already_scheduled"} in run2.outputs["refused"]


@pytest.mark.e2e
def test_declined_habit_is_not_offered_again(env):
    rid = env.report("Pipeline")
    _weekly_asks(env, rid)
    env.dream(_Reflect(_offer_habit))
    offer = next(i for i in env.briefing() if i["kind"] == "habit")
    r = env.client.post(f"/api/users/me/habit_offers/{offer['id']}/decline", headers=env.headers)
    assert r.status_code == 200 and r.json()["status"] == "declined"

    env.turn(rid, "Weekly pipeline by region, again")
    env.nights += 1
    run = env.dream(_Reflect(_offer_habit))
    assert run.outputs["habit"] is None
    assert {"kind": "habit", "reason": "declined_recently"} in run.outputs["refused"]



@pytest.mark.e2e
def test_habit_for_an_ask_code_never_saw_recur_is_refused(env):
    one_off = env.report("One off")
    env.turn(one_off, "Top customers in March")
    run = env.dream(_Reflect(lambda p: UserDreamProposal(habit=HabitOp(recurring="h7", intent="Top customers",
                                                                       cadence="daily", time="09:00"))))
    assert run.status == "done" and run.outputs["habit"] is None
    assert {"kind": "habit", "reason": "not_a_detected_recurring_ask"} in run.outputs["refused"]


# ── privacy / switches ──────────────────────────────────────────────────────

@pytest.mark.e2e
def test_briefing_items_and_overnight_log_are_owner_only(env):
    rid = env.report("Churn")
    env.turn(rid, "Churn by plan")
    env.dream(_Reflect(lambda p: UserDreamProposal(open_threads=[ThreadOp(report=_report_key(p, "Churn"), text="Churn")])))
    thread = next(i for i in env.briefing() if i["kind"] == "thread")

    other_token, _ = env.add_member()
    assert env.briefing(token=other_token) == []
    r = env.client.post(f"/api/users/me/briefing/items/thread/{thread['id']}/feedback", json={"useful": False},
                        headers=_h(other_token, env.org_id))
    assert r.status_code == 404 and r.json()["error_code"] == "briefing.item_not_found"
    assert [i["id"] for i in env.briefing() if i["kind"] == "thread"] == [thread["id"]]

    mine = env.client.get("/api/users/me/overnight/log", headers=env.headers).json()["runs"]
    theirs = env.client.get("/api/users/me/overnight/log", headers=_h(other_token, env.org_id)).json()["runs"]
    assert len(mine) == 1 and mine[0]["open_threads"] == 1 and theirs == []


@pytest.mark.e2e
def test_per_user_toggle_and_org_switch_gate_the_dream(env):
    rid = env.report("Churn")
    env.turn(rid, "Churn by plan")
    r = env.client.put("/api/users/me/overnight", json={"enabled": False}, headers=env.headers)
    assert r.status_code == 200 and r.json() == {"enabled": False, "available": True}
    reflect = _Reflect()
    run = env.dream(reflect)
    assert run.status == "cancelled" and reflect.prompts == []

    env.client.put("/api/users/me/overnight", json={"enabled": True}, headers=env.headers)
    env.settings(enable_user_dreaming=False)
    assert env.client.get("/api/users/me/overnight", headers=env.headers).json()["available"] is False
    run = env.dream(reflect, force=True)
    assert run.status == "cancelled" and reflect.prompts == []


@pytest.mark.e2e
def test_switching_off_during_the_reflection_writes_nothing(env):
    rid = env.report("Churn")
    env.turn(rid, "Churn by plan")

    async def flip_off():
        async with async_session_maker() as db:
            s = (await db.execute(select(OrganizationSettings).where(
                OrganizationSettings.organization_id == env.org_id))).scalar_one()
            cfg = dict(s.config or {})
            cfg["enable_user_dreaming"] = {"value": False}
            s.config = cfg
            await db.commit()

    reflect = _Reflect(lambda p: UserDreamProposal(
        memory=[MemoryOp(op="create", text="Follows churn weekly")],
        open_threads=[ThreadOp(report=_report_key(p, "Churn"), text="Churn")],
    ), before=flip_off)
    run = env.dream(reflect)
    assert run.status == "cancelled" and len(reflect.prompts) == 1
    assert _memory(env) == [] and [i for i in env.briefing() if i["kind"] == "thread"] == []


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


@pytest.mark.e2e
def test_got_it_hides_items_until_a_later_night_notes_them_again(env):
    env.settings(enable_user_dreaming=True, enable_agent_checkins=True)
    board_day = (env.now + timedelta(days=2)).date().isoformat()
    rid = env.report("Board prep")
    env.turn(rid, f"Churn for the board meeting on {board_day}")

    def propose(prompt):
        r = _report_key(prompt, "Board prep")
        return UserDreamProposal(
            memory=[MemoryOp(op="create", text="Board meeting", event_start=board_day, source=r)],
            open_threads=[ThreadOp(report=r, text="Churn by plan for the board")],
            follow_ups=[FollowUpOp(report=r, due_local=(env.now + timedelta(days=1)).strftime("%Y-%m-%dT10:00"),
                                   note="Re-run churn")],
        )

    env.dream(_Reflect(propose))
    assert {i["kind"] for i in env.briefing()} >= {"thread", "event"}

    r = env.client.post("/api/users/me/briefing/seen", headers=env.headers)
    assert r.status_code == 200
    assert env.briefing() == []  # seen: nothing comes back on the next visit

    # The next night notes the unfinished thread again → it shows once more.
    env.turn(rid, "Still need the Enterprise split")
    env.nights += 1
    env.dream(_Reflect(lambda p: UserDreamProposal(
        open_threads=[ThreadOp(report=_report_key(p, "Board prep"), text="Enterprise split")])))
    assert [i["text"] for i in env.briefing() if i["kind"] == "thread"] == ["Enterprise split"]
