"""E2E tests for agent check-ins (docs/feedback-loops/agent-checkins.md, Loop A).

The whole lifecycle runs through the real app — routes, services, DB, the
completion pipeline, run_machine_turn, the real notify tool, NotifyService and
the inbox — with only the boundaries stubbed:

  * the LLM: the planner/judge small-model call (``call_small_model``) returns
    scripted JSON, and the agent's own turn is a scripted stand-in for the LLM
    loop (``_ScriptedAgent``) whose "decisions" are the tool calls it makes —
    the notify tool itself runs for real;
  * the clock (``checkin_service._utcnow``) — fixed inside the working window;
  * the scheduler — a recording stub (same pattern as test_wait_tool.py).

Contracts covered: setting gate at every step, eligibility (human turns only),
planned/not_proposed/rejected rows, invisibility until the judge runs, judge
skip/run, sent vs ran_quiet, notify guardrails, limits, access loss,
idempotent fire, report archive, and the TraceModal payload.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from apscheduler.jobstores.base import JobLookupError
from sqlalchemy import select

import app.services.checkin_service as cs
from app.dependencies import async_session_maker
from app.models.agent_checkin import AgentCheckin

# Tuesday, inside Mon–Fri 09:00–18:00 (org timezone defaults to UTC).
FIXED_NOW = datetime(2026, 10, 6, 10, 0)

PROPOSE = (
    '{"propose": true, "due_in_hours": 72, "note": "Re-run churn by plan after the Oct 1 '
    'subscription refresh; notify only if total churn crossed 4% or Enterprise moved noticeably.", '
    '"reason": "User will decide on the retention campaign after the refresh."}'
)
DECLINE = '{"propose": false, "reason": "One-off lookup, fully answered."}'
JUDGE_RUN = (
    '{"decision": "run", "reason": "The refresh job ran after the conversation, so the churn '
    'question is now answerable.", "focus": "Churn by plan, Sep vs Aug; compare to 3.8%."}'
)
JUDGE_SKIP = (
    '{"decision": "skip", "reason": "The user already re-ran churn in this report yesterday '
    'and concluded; nothing the note depends on changed since.", "focus": ""}'
)
NOTIFY_OK = {"subject": "Churn crossed 4% after the Oct refresh", "body": "Total churn is 4.3% (was 3.8%). Next: review Enterprise renewals."}


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


# ── boundary stubs ──────────────────────────────────────────────────────────

class _FakeScheduler:
    def __init__(self):
        self.jobs: dict = {}

    def add_job(self, *, func, trigger, run_date, id, kwargs, replace_existing, misfire_grace_time):
        assert trigger == "date"
        self.jobs[id] = {"func": func, "run_date": run_date, "kwargs": kwargs}

    def remove_job(self, job_id):
        if job_id not in self.jobs:
            raise JobLookupError(job_id)
        del self.jobs[job_id]


class _LLMScript:
    """Replies for the small-model calls, keyed by usage scope."""

    def __init__(self):
        self.replies = {"checkin_planner": PROPOSE, "checkin_judge": JUDGE_RUN}
        self.calls: list[str] = []

    async def __call__(self, model, *, system, prompt, usage_scope, usage_scope_ref_id):
        self.calls.append(usage_scope)
        return self.replies[usage_scope]


class _ScriptedAgent:
    """Stand-in for AgentV2 — the LLM loop. For a check-in run it makes the
    scripted tool calls through the REAL notify tool; otherwise it answers."""

    notify_calls: list = []
    before_tools = None  # optional async callable(db) run before tool calls
    tool_results: list = []
    answer = "Churn for September is 3.8% overall; Enterprise 2.1%."

    def __init__(self, **kw):
        self.kw = kw

    async def main_execution(self):
        from app.ai.tools.implementations.notify import NotifyTool
        from app.models.agent_execution import AgentExecution
        from app.models.user import User

        db = self.kw["db"]
        head = self.kw["head_completion"]
        system = self.kw["system_completion"]
        report = self.kw["report"]
        org = self.kw["organization"]
        db.add(AgentExecution(
            completion_id=str(system.id), organization_id=str(org.id), user_id=str(head.user_id),
            report_id=str(report.id), status="success",
        ))
        if head.trigger_source == "checkin":
            if _ScriptedAgent.before_tools:
                await _ScriptedAgent.before_tools(db)
            user = await db.get(User, str(head.user_id))
            ctx = {"db": db, "user": user, "organization": org, "report": report,
                   "head_completion": head, "system_completion": system}
            for spec in _ScriptedAgent.notify_calls:
                events = [e async for e in NotifyTool().run_stream(spec, ctx)]
                last = events[-1]
                _ScriptedAgent.tool_results.append((last.type, last.payload))
        system.completion = {"content": _ScriptedAgent.answer}
        system.status = "success"
        await db.commit()


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_report, update_organization_settings, monkeypatch):
    import app.ai.agents.checkins.judge as judge_mod
    import app.ai.agents.checkins.planner as planner_mod
    import app.services.completion_service as completion_mod

    sched = _FakeScheduler()
    llm = _LLMScript()
    monkeypatch.setattr(cs, "scheduler", sched)
    monkeypatch.setattr(cs, "_utcnow", lambda: FIXED_NOW)
    monkeypatch.setattr(planner_mod, "call_small_model", llm)
    monkeypatch.setattr(judge_mod, "call_small_model", llm)
    monkeypatch.setattr(completion_mod, "AgentV2", _ScriptedAgent)
    _ScriptedAgent.notify_calls = []
    _ScriptedAgent.before_tools = None
    _ScriptedAgent.tool_results = []

    email = f"checkin_{uuid.uuid4().hex[:8]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email, "test123")
    me = whoami(token)
    org_id = me["organizations"][0]["id"]

    # A default model so completions can run (no network: the agent is scripted).
    r = test_client.post("/api/llm/providers", headers=_h(token, org_id), json={
        "name": f"openai-{uuid.uuid4().hex[:6]}", "provider_type": "openai",
        "credentials": {"api_key": "sk-test-not-used"},
        "models": [{"model_id": "gpt-6-luna", "name": "GPT-6 Luna", "is_custom": False, "is_default": True}],
    })
    assert r.status_code == 200, r.text
    models = test_client.get("/api/llm/models", headers=_h(token, org_id)).json()
    model_id = next(m["id"] for m in models if m["model_id"] == "gpt-6-luna")

    class E:
        pass

    e = E()
    e.client, e.token, e.org_id, e.user_id = test_client, token, org_id, me["id"]
    e.model_id, e.sched, e.llm = model_id, sched, llm
    e.headers = _h(token, org_id)

    def set_settings(**cfg):
        update_organization_settings({k: {"value": v} for k, v in cfg.items()}, user_token=token, org_id=org_id)

    def new_report(title="Churn review", token_=None, data_sources=None):
        return create_report(title=title, user_token=token_ or token, org_id=org_id, data_sources=data_sources or [])["id"]

    def human_turn(report_id, prompt="What was churn last month?", token_=None):
        resp = test_client.post(f"/api/reports/{report_id}/completions", headers=_h(token_ or token, org_id),
                                json={"prompt": {"content": prompt}})
        assert resp.status_code == 200, resp.text
        return _latest_pair(report_id)

    def plan(report_id, pair, user_id=None):
        head_id, system_id = pair
        return _run(cs.checkin_service.dispatch_after_turn(
            organization_id=org_id, user_id=user_id or e.user_id, report_id=report_id,
            head_completion_id=head_id, system_completion_id=system_id,
            small_model_id=model_id, now=FIXED_NOW, wait_for_finalize=False,
        ))

    def fire(checkin_id):
        _run(cs.run_checkin_wake(checkin_id))

    def timeline(report_id):
        resp = test_client.get(f"/api/reports/{report_id}/completions", headers=e.headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["completions"]

    def notifications(source=None, token_=None):
        params = {"source": source} if source else {}
        return test_client.get("/api/notifications", headers=_h(token_ or token, org_id), params=params).json()["items"]

    def trace(report_id, token_=None):
        return test_client.get(f"/api/console/reports/{report_id}/conversation", headers=_h(token_ or token, org_id))

    e.set_settings, e.new_report, e.human_turn, e.plan, e.fire = set_settings, new_report, human_turn, plan, fire
    e.timeline, e.notifications, e.trace = timeline, notifications, trace
    return e


def _latest_pair(report_id):
    from app.models.completion import Completion

    async def _q():
        async with async_session_maker() as db:
            system = (await db.execute(
                select(Completion).where(Completion.report_id == report_id, Completion.role == "system")
                .order_by(Completion.turn_index.desc()).limit(1)
            )).scalar_one()
            return str(system.parent_id), str(system.id)
    return _run(_q())


def _rows(report_id=None, org_id=None):
    async def _q():
        async with async_session_maker() as db:
            q = select(AgentCheckin).order_by(AgentCheckin.created_at.asc())
            if report_id:
                q = q.where(AgentCheckin.report_id == report_id)
            if org_id:
                q = q.where(AgentCheckin.organization_id == org_id)
            return list((await db.execute(q)).scalars().all())
    return _run(_q())


def _seed_runs(org_id, user_id, report_id, n, status="sent", judged_at=None):
    """Past check-in runs. Direct DB write: no API produces historical runs,
    and driving N full runs would couple this test to the run path it isn't about."""
    async def _w():
        async with async_session_maker() as db:
            for _ in range(n):
                db.add(AgentCheckin(
                    organization_id=org_id, user_id=user_id, report_id=report_id,
                    status=status, judge_decision="run", judge_reason="seed",
                    judged_at=judged_at or (FIXED_NOW - timedelta(days=1)),
                ))
            await db.commit()
    _run(_w())


def _planned(env, report_id=None):
    rid = report_id or env.new_report()
    pair = env.human_turn(rid)
    env.set_settings(enable_agent_checkins=True)
    row = env.plan(rid, pair)
    assert row is not None and row.status == "planned", row
    return rid, row


# ── setting gate + eligibility ──────────────────────────────────────────────

@pytest.mark.e2e
def test_setting_off_turn_creates_no_rows_and_no_llm_calls(env):
    rid = env.new_report()
    pair = env.human_turn(rid)
    assert env.plan(rid, pair) is None
    assert _rows(rid) == []
    assert env.llm.calls == []
    assert env.sched.jobs == {}


@pytest.mark.e2e
def test_planner_proposes_one_planned_row_one_job_timeline_unchanged(env):
    rid = env.new_report()
    pair = env.human_turn(rid)
    env.set_settings(enable_agent_checkins=True)
    before = [c["id"] for c in env.timeline(rid)]

    row = env.plan(rid, pair)

    rows = _rows(rid)
    assert [r.status for r in rows] == ["planned"]
    assert rows[0].note and rows[0].plan_reason and rows[0].source_completion_id == pair[1]
    assert list(env.sched.jobs) == [f"checkin:{row.id}"]
    assert env.sched.jobs[f"checkin:{row.id}"]["kwargs"] == {"checkin_id": row.id}
    # due = clamp(now+72h) shifted into the working window
    assert FIXED_NOW + timedelta(hours=2) <= rows[0].due_at <= FIXED_NOW + timedelta(days=14, hours=2)
    assert env.llm.calls == ["checkin_planner"]
    assert [c["id"] for c in env.timeline(rid)] == before


@pytest.mark.e2e
def test_planner_declines_records_not_proposed_without_job(env):
    rid = env.new_report()
    pair = env.human_turn(rid)
    env.set_settings(enable_agent_checkins=True)
    env.llm.replies["checkin_planner"] = DECLINE
    before = [c["id"] for c in env.timeline(rid)]

    env.plan(rid, pair)

    rows = _rows(rid)
    assert [r.status for r in rows] == ["not_proposed"]
    assert rows[0].plan_reason
    assert env.sched.jobs == {}
    assert [c["id"] for c in env.timeline(rid)] == before


@pytest.mark.e2e
@pytest.mark.parametrize("raw", ["not json", '{"propose": true, "due_in_hours": 24}'])
def test_invalid_planner_output_records_nothing(env, raw):
    rid = env.new_report()
    pair = env.human_turn(rid)
    env.set_settings(enable_agent_checkins=True)
    env.llm.replies["checkin_planner"] = raw
    assert env.plan(rid, pair) is None
    assert _rows(rid) == [] and env.sched.jobs == {}


@pytest.mark.e2e
@pytest.mark.parametrize("source", ["wait", "checkin", "eval_run"])
def test_machine_turns_never_plan(env, source):
    from app.services.machine_turn import run_machine_turn
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    rid = env.new_report()
    env.human_turn(rid)
    env.set_settings(enable_agent_checkins=True)

    async def _machine():
        async with async_session_maker() as db:
            await run_machine_turn(
                db, report=await db.get(Report, rid), user=await db.get(User, env.user_id),
                organization=await db.get(Organization, env.org_id), summary="machine",
                trigger_source=source, message_type=f"{source}_event", instruction="resume",
            )
    _run(_machine())
    pair = _latest_pair(rid)
    assert env.plan(rid, pair) is None
    assert _rows(rid) == []
    assert env.llm.calls == []


# ── settings-off hook ───────────────────────────────────────────────────────

@pytest.mark.e2e
def test_turning_setting_off_cancels_pending_and_removes_jobs(env):
    rid, row = _planned(env)
    assert f"checkin:{row.id}" in env.sched.jobs

    env.set_settings(enable_agent_checkins=False)

    assert env.sched.jobs == {}
    [r] = _rows(rid)
    assert (r.status, r.status_reason) == ("cancelled", "disabled")

    # A later (stale) fire does nothing.
    before = [c["id"] for c in env.timeline(rid)]
    env.fire(row.id)
    assert _rows(rid)[0].status == "cancelled"
    assert [c["id"] for c in env.timeline(rid)] == before
    assert env.llm.calls == ["checkin_planner"]  # no judge call


@pytest.mark.e2e
def test_fire_with_setting_off_cancels_without_judge(env):
    """The job fires after the setting was turned off by a path that didn't
    run the hook (e.g. a direct config edit): the fire itself refuses."""
    rid, row = _planned(env)

    async def _flip_off():
        from app.models.organization_settings import OrganizationSettings
        from sqlalchemy.orm.attributes import flag_modified
        async with async_session_maker() as db:
            s = (await db.execute(select(OrganizationSettings).where(OrganizationSettings.organization_id == env.org_id))).scalar_one()
            cfg = dict(s.config)
            cfg["enable_agent_checkins"] = {**cfg["enable_agent_checkins"], "value": False, "state": "disabled"}
            s.config = cfg
            flag_modified(s, "config")
            await db.commit()
    _run(_flip_off())

    env.fire(row.id)
    [r] = _rows(rid)
    assert (r.status, r.status_reason) == ("cancelled", "disabled")
    assert "checkin_judge" not in env.llm.calls


# ── judge ───────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_judge_skip_leaves_report_untouched(env):
    rid, row = _planned(env)
    env.llm.replies["checkin_judge"] = JUDGE_SKIP
    before = [c["id"] for c in env.timeline(rid)]

    env.fire(row.id)

    [r] = _rows(rid)
    assert (r.status, r.status_reason, r.judge_decision) == ("skipped", "judge_skip", "skip")
    assert r.judge_reason
    assert [c["id"] for c in env.timeline(rid)] == before
    assert env.notifications(source="checkin") == []


@pytest.mark.e2e
def test_judge_invalid_output_counts_as_skip(env):
    rid, row = _planned(env)
    env.llm.replies["checkin_judge"] = '{"decision": "run"}'  # no reason
    env.fire(row.id)
    [r] = _rows(rid)
    assert (r.status, r.status_reason, r.judge_decision) == ("skipped", "invalid_judge_output", "skip")


@pytest.mark.e2e
def test_judge_run_with_notify_is_sent_with_one_inbox_row(env):
    rid, row = _planned(env)
    _ScriptedAgent.notify_calls = [dict(NOTIFY_OK)]
    before = [c["id"] for c in env.timeline(rid)]

    env.fire(row.id)

    [r] = _rows(rid)
    assert r.status == "sent" and r.notified is True
    assert r.notify_subject == NOTIFY_OK["subject"] and r.sent_at is not None
    assert r.judge_reason and r.judge_focus
    after = env.timeline(rid)
    new = [c for c in after if c["id"] not in before]
    assert [c["role"] for c in new] == ["external", "system"]  # strip + reply; trigger hidden
    strip, reply = new
    assert strip["trigger_source"] == "checkin"
    assert strip["prompt"]["meta"]["checkin_id"] == r.id
    assert strip["prompt"]["meta"]["outcome"] == "sent"
    assert reply["id"] == r.run_completion_id
    items = env.notifications(source="checkin")
    assert len(items) == 1
    assert items[0]["title"] == NOTIFY_OK["subject"] and items[0]["link"] == f"/reports/{rid}"


@pytest.mark.e2e
def test_judge_run_without_notify_is_ran_quiet(env):
    rid, row = _planned(env)
    env.fire(row.id)
    [r] = _rows(rid)
    assert r.status == "ran_quiet" and r.notified is False and r.run_completion_id
    assert env.notifications(source="checkin") == []
    strip = next(c for c in env.timeline(rid) if c["role"] == "external")
    assert strip["prompt"]["meta"]["outcome"] == "ran_quiet"
    assert strip["prompt"]["meta"]["run_completion_id"] == r.run_completion_id


# ── notify guardrails during a check-in run ─────────────────────────────────

@pytest.mark.e2e
def test_notify_rejects_other_recipients(env):
    rid, row = _planned(env)
    _ScriptedAgent.notify_calls = [{**NOTIFY_OK, "recipients": ["someone.else@test.com"]}]
    env.fire(row.id)
    [(etype, payload)] = _ScriptedAgent.tool_results
    assert etype == "tool.error" and payload["code"] == "CHECKIN_SELF_ONLY"
    assert _rows(rid)[0].status == "ran_quiet"
    assert env.notifications(source="checkin") == []


@pytest.mark.e2e
def test_notify_second_call_rejected(env):
    rid, row = _planned(env)
    _ScriptedAgent.notify_calls = [dict(NOTIFY_OK), {**NOTIFY_OK, "subject": "again"}]
    env.fire(row.id)
    types = [t for t, _ in _ScriptedAgent.tool_results]
    assert types == ["tool.end", "tool.error"]
    assert _ScriptedAgent.tool_results[1][1]["code"] == "CHECKIN_ALREADY_NOTIFIED"
    assert len(env.notifications(source="checkin")) == 1
    assert _rows(rid)[0].status == "sent"


@pytest.mark.e2e
def test_notify_refused_when_setting_flipped_mid_run(env):
    rid, row = _planned(env)

    async def _flip(db):
        from app.models.organization_settings import OrganizationSettings
        from sqlalchemy.orm.attributes import flag_modified
        s = (await db.execute(select(OrganizationSettings).where(OrganizationSettings.organization_id == env.org_id))).scalar_one()
        cfg = dict(s.config)
        cfg["enable_agent_checkins"] = {**cfg["enable_agent_checkins"], "value": False, "state": "disabled"}
        s.config = cfg
        flag_modified(s, "config")
        await db.commit()

    _ScriptedAgent.before_tools = _flip
    _ScriptedAgent.notify_calls = [dict(NOTIFY_OK)]
    env.fire(row.id)
    [(etype, payload)] = _ScriptedAgent.tool_results
    assert etype == "tool.error" and payload["code"] == "CHECKIN_DISABLED"
    assert env.notifications(source="checkin") == []


@pytest.mark.e2e
def test_send_email_refused_during_checkin_run(env):
    """A check-in reaches the user through notify only; send_email would skip
    the self-only / one-call / setting / source guardrails and the outcome."""
    from app.ai.tools.implementations.send_email import SendEmailTool
    from app.models.completion import Completion
    from app.models.user import User

    rid, row = _planned(env)
    env.fire(row.id)  # produce a real check-in head completion

    async def _try():
        async with async_session_maker() as db:
            head = (await db.execute(select(Completion).where(
                Completion.report_id == rid, Completion.role == "user",
                Completion.trigger_source == "checkin"))).scalar_one()
            user = await db.get(User, env.user_id)
            ctx = {"db": db, "user": user, "head_completion": head}
            return [e async for e in SendEmailTool().run_stream({"subject": "s", "body": "b"}, ctx)]
    events = _run(_try())
    assert events[-1].type == "tool.error"
    assert events[-1].payload["code"] == "CHECKIN_USE_NOTIFY"


# ── limits ──────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("cap", [1, 2, 3])
def test_weekly_cap_boundary_at_plan_time(env, cap):
    env.set_settings(enable_agent_checkins=True, checkins_max_per_user_per_week=cap)
    seed_report = env.new_report("seed")
    _seed_runs(env.org_id, env.user_id, seed_report, cap - 1)

    rid = env.new_report()
    row = env.plan(rid, env.human_turn(rid))
    assert row.status == "planned"  # exactly at the boundary: allowed

    # cap reached: next plan (another report, and after the pending one ran) is rejected
    _seed_runs(env.org_id, env.user_id, seed_report, 1)
    env.set_settings(enable_agent_checkins=False)  # clears the pending one
    env.set_settings(enable_agent_checkins=True, checkins_max_per_user_per_week=cap)
    rid2 = env.new_report("second")
    row2 = env.plan(rid2, env.human_turn(rid2))
    assert (row2.status, row2.status_reason) == ("rejected", "weekly_cap")
    assert f"checkin:{row2.id}" not in env.sched.jobs


@pytest.mark.e2e
def test_runs_older_than_a_week_do_not_count(env):
    env.set_settings(enable_agent_checkins=True, checkins_max_per_user_per_week=1)
    seed_report = env.new_report("seed")
    _seed_runs(env.org_id, env.user_id, seed_report, 3, judged_at=FIXED_NOW - timedelta(days=8))
    rid = env.new_report()
    assert env.plan(rid, env.human_turn(rid)).status == "planned"


@pytest.mark.e2e
def test_pending_per_user_and_per_report(env):
    rid, _ = _planned(env)
    # same report
    row = env.plan(rid, env.human_turn(rid))
    assert (row.status, row.status_reason) == ("rejected", "pending_exists_user")
    # the pre-check never paid for a planner call
    assert env.llm.calls == ["checkin_planner"]


@pytest.mark.e2e
@pytest.mark.parametrize("cap", [1, 3])
def test_org_daily_cap_cancels_at_fire_time(env, cap):
    rid, row = _planned(env)
    env.set_settings(checkins_max_runs_per_org_per_day=cap)
    other = env.new_report("other")
    _seed_runs(env.org_id, env.user_id, other, cap - 1, judged_at=FIXED_NOW - timedelta(hours=2))
    env.set_settings(checkins_max_per_user_per_week=100)
    # below the cap: runs
    env.fire(row.id)
    assert _rows(rid)[0].status in ("ran_quiet", "sent")

    # at the cap: the next due check-in is cancelled at fire time
    rid2 = env.new_report("next")
    row2 = env.plan(rid2, env.human_turn(rid2))
    assert row2.status == "planned"
    env.fire(row2.id)
    [r2] = _rows(rid2)
    assert (r2.status, r2.status_reason) == ("cancelled", "org_daily_cap")


# ── access / report lifecycle ───────────────────────────────────────────────

@pytest.mark.e2e
def test_member_losing_data_source_access_is_cancelled_access_lost(env, create_user, login_user, whoami, create_data_source):
    c = env.client
    member_email = f"checkin_member_{uuid.uuid4().hex[:6]}@test.com"
    c.post(f"/api/organizations/{env.org_id}/members", headers=env.headers,
           json={"organization_id": env.org_id, "email": member_email, "role": "member"})
    create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")
    member_id = whoami(member_token)["id"]

    import tempfile, sqlite3, os
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE subs (id INTEGER, plan TEXT, churned INTEGER)")
    conn.commit()
    conn.close()
    ds = create_data_source(name=f"subs-{uuid.uuid4().hex[:4]}", type="sqlite", config={"database": path},
                            credentials={}, user_token=env.token, org_id=env.org_id)
    r = c.post(f"/api/data_sources/{ds['id']}/members", headers=env.headers,
               json={"principal_type": "user", "principal_id": member_id})
    assert r.status_code == 200, r.text

    rid = env.new_report("member churn", token_=member_token, data_sources=[ds["id"]])
    pair = env.human_turn(rid, token_=member_token)
    env.set_settings(enable_agent_checkins=True)
    row = env.plan(rid, pair, user_id=member_id)
    assert row.status == "planned"

    r = c.delete(f"/api/data_sources/{ds['id']}/members/{member_id}", headers=env.headers)
    assert r.status_code == 204, r.text

    env.fire(row.id)
    [r] = _rows(rid)
    assert (r.status, r.status_reason) == ("cancelled", "access_lost")
    assert "checkin_judge" not in env.llm.calls
    os.unlink(path)


@pytest.mark.e2e
def test_archiving_report_cancels_pending(env):
    rid, row = _planned(env)
    r = env.client.delete(f"/api/reports/{rid}", headers=env.headers)
    assert r.status_code == 200, r.text
    [r] = _rows(rid)
    assert (r.status, r.status_reason) == ("cancelled", "report_deleted")
    assert env.sched.jobs == {}


@pytest.mark.e2e
def test_idempotent_fire_runs_turn_once(env):
    rid, row = _planned(env)
    env.fire(row.id)
    env.fire(row.id)
    strips = [c for c in env.timeline(rid) if c["role"] == "external"]
    assert len(strips) == 1
    assert env.llm.calls.count("checkin_judge") == 1


@pytest.mark.e2e
def test_redelivered_fire_outside_claim_window_runs_once(env):
    """A second delivery that wins its own run-claim (another replica, a later
    claim bucket) must still find nothing to do: only a 'planned' row runs."""
    rid, row = _planned(env)

    async def _fire_direct():
        async with async_session_maker() as db:
            return await cs.checkin_service.fire(db, row.id, now=FIXED_NOW)
    first = _run(_fire_direct())
    second = _run(_fire_direct())
    assert first in ("ran_quiet", "sent") and second is None
    assert len([c for c in env.timeline(rid) if c["role"] == "external"]) == 1


@pytest.mark.e2e
def test_live_run_in_report_rearms(env):
    from app.models.completion import Completion

    rid, row = _planned(env)

    async def _mark_live():
        async with async_session_maker() as db:
            c = (await db.execute(select(Completion).where(Completion.report_id == rid, Completion.role == "system"))).scalars().first()
            c.status = "in_progress"
            await db.commit()
    _run(_mark_live())
    env.fire(row.id)
    [r] = _rows(rid)
    assert r.status == "planned"
    assert r.due_at == FIXED_NOW + timedelta(minutes=30)
    assert f"checkin:{r.id}" in env.sched.jobs
    assert "checkin_judge" not in env.llm.calls


# ── TraceModal payload ──────────────────────────────────────────────────────

@pytest.mark.e2e
def test_trace_lists_every_checkin_with_status_note_and_judge_reason(env):
    # 1) planned → sent
    rid, row = _planned(env)
    _ScriptedAgent.notify_calls = [dict(NOTIFY_OK)]
    env.fire(row.id)
    # 2) a second turn the planner declines
    env.llm.replies["checkin_planner"] = DECLINE
    env.plan(rid, env.human_turn(rid, "thanks"))

    resp = env.trace(rid)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    by_status = {c["status"]: c for c in data["checkins"]}
    assert set(by_status) == {"sent", "not_proposed"}
    sent = by_status["sent"]
    assert sent["note"] and sent["judge_reason"] and sent["judge_decision"] == "run"
    assert sent["notify_subject"] == NOTIFY_OK["subject"]
    assert by_status["not_proposed"]["plan_reason"]
    # the run turn is marked as a machine turn and linked to its row
    run_turns = [t for t in data["turns"] if t.get("checkin_id")]
    assert len(run_turns) == 1
    assert run_turns[0]["trigger_source"] == "checkin"
    assert run_turns[0]["completion_id"] == sent["run_completion_id"]
    # the planning turn anchors the card
    assert any(t["completion_id"] == sent["source_completion_id"] for t in data["turns"])


@pytest.mark.e2e
@pytest.mark.parametrize("scenario,expected", [
    ("skip", ("skipped", "judge_skip")),
    ("off", ("cancelled", "disabled")),
    ("archive", ("cancelled", "report_deleted")),
])
def test_trace_shows_decisions_that_left_nothing_visible(env, scenario, expected):
    rid, row = _planned(env)
    if scenario == "skip":
        env.llm.replies["checkin_judge"] = JUDGE_SKIP
        env.fire(row.id)
    elif scenario == "off":
        env.set_settings(enable_agent_checkins=False)
    else:
        env.client.delete(f"/api/reports/{rid}", headers=env.headers)
    [c] = env.trace(rid).json()["checkins"]
    assert (c["status"], c["status_reason"]) == expected
    assert c["note"]
    if scenario == "skip":
        assert c["judge_reason"]


@pytest.mark.e2e
def test_trace_not_readable_by_plain_member(env, create_user, login_user):
    rid, _ = _planned(env)
    member_email = f"checkin_viewer_{uuid.uuid4().hex[:6]}@test.com"
    env.client.post(f"/api/organizations/{env.org_id}/members", headers=env.headers,
                    json={"organization_id": env.org_id, "email": member_email, "role": "member"})
    create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")
    resp = env.trace(rid, token_=member_token)
    assert resp.status_code in (403, 404), resp.text
    assert "checkins" not in resp.text


# ── per-user opt-out (Phase 4) ──────────────────────────────────────────────

def _set_opt_in(env, enabled: bool):
    r = env.client.put("/api/users/me/checkins", headers=env.headers, json={"enabled": enabled})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
def test_my_checkins_preference_reports_org_availability(env):
    r = env.client.get("/api/users/me/checkins", headers=env.headers).json()
    assert r == {"enabled": True, "available": False}
    env.set_settings(enable_agent_checkins=True)
    assert env.client.get("/api/users/me/checkins", headers=env.headers).json()["available"] is True
    assert _set_opt_in(env, False) == {"enabled": False, "available": True}
    assert env.client.get("/api/users/me/checkins", headers=env.headers).json()["enabled"] is False


@pytest.mark.e2e
def test_opted_out_user_is_never_planned_for(env):
    env.set_settings(enable_agent_checkins=True)
    _set_opt_in(env, False)
    rid = env.new_report()
    assert env.plan(rid, env.human_turn(rid)) is None
    assert _rows(rid) == [] and env.llm.calls == [] and env.sched.jobs == {}
    # opting back in restores planning
    _set_opt_in(env, True)
    assert env.plan(rid, env.human_turn(rid, "and again")).status == "planned"


@pytest.mark.e2e
def test_opting_out_cancels_pending_checkins(env):
    rid, row = _planned(env)
    _set_opt_in(env, False)
    [r] = _rows(rid)
    assert (r.status, r.status_reason) == ("cancelled", "opted_out")
    assert env.sched.jobs == {}
    env.fire(row.id)
    assert _rows(rid)[0].status == "cancelled"
    assert "checkin_judge" not in env.llm.calls


@pytest.mark.e2e
def test_opt_out_is_respected_at_fire_time(env):
    rid, row = _planned(env)

    async def _opt_out_directly():
        # Direct write: models an opt-out whose cancel sweep didn't reach this
        # row (e.g. a race with the fire), so the fire-time check is what's tested.
        from app.models.membership import Membership
        async with async_session_maker() as db:
            m = (await db.execute(select(Membership).where(
                Membership.user_id == env.user_id, Membership.organization_id == env.org_id))).scalar_one()
            m.checkins_opt_out = True
            await db.commit()
    _run(_opt_out_directly())
    env.fire(row.id)
    [r] = _rows(rid)
    assert (r.status, r.status_reason) == ("cancelled", "opted_out")
    assert "checkin_judge" not in env.llm.calls
