"""Sub-agents — delegated agent runs with wave fan-in (docs/design/sub-agents.md).

Loop A of docs/feedback-loops/sub-agents.md: the whole mechanism on a scripted
LLM, no provider. Only the LLM boundary is stubbed (``LLM.inference_stream_v2``
for the planner, ``LLM.inference_stream`` for titles/judges); routes,
services, the agent loop, the wave tracker and persistence run real.

The script plays four roles, keyed on what the request carries:
  * parent, first planner call   → N `delegate` calls in ONE response
  * parent, after the receipts   → one line of text, turn ends
  * child (system prompt "Mode: Sub-agent") → create_note, then finish_subtask;
    a child titled "Seasonality" answers in prose only and never finishes, to
    exercise the forced-finish nudge and the synthesized partial record;
    a child titled "Slow" stalls so a test can stop it mid-run
  * fan-in turn (<sub_agent_results> in the prompt) → the synthesis text

Run: cd backend && uv run pytest tests/e2e/test_sub_agents.py -q --db=sqlite
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

import pytest

from app.dependencies import async_session_maker

HYPOTHESES = [
    ("Pricing change hypothesis", "Did the 2026-09-10 premium price change reduce paid orders?"),
    ("App release 5.2.0 hypothesis", "Did the checkout-service 5.2.0 release on 2026-09-14 raise iOS/PayFlow failures?"),
    ("APAC campaign hypothesis", "Did the APAC campaign change paid orders or only sessions?"),
    ("Seasonality hypothesis", "Is the drop inside normal weekly seasonality?"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Scripted LLM
# ─────────────────────────────────────────────────────────────────────────────

def _all_text(messages) -> str:
    out = []
    for m in messages or []:
        c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    if isinstance(b.get("text"), str):
                        out.append(b["text"])
                    cc = b.get("content")
                    if isinstance(cc, str):
                        out.append(cc)
                    elif isinstance(cc, list):
                        for x in cc:
                            if isinstance(x, dict) and isinstance(x.get("text"), str):
                                out.append(x["text"])
    return "\n".join(out)


def _tool_result_count(messages) -> int:
    n = 0
    for m in messages or []:
        c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if isinstance(c, list):
            n += sum(1 for b in c if isinstance(b, dict) and b.get("type") == "tool_result")
    if n == 0:
        # legacy (non-transcript) path: observations serialized into text
        txt = _all_text(messages)
        n = len(re.findall(r"<last_observation>", txt))
    return n


def _child_title(system: str) -> str:
    m = re.search(r'delegated goal: "([^"]+)"', system or "")
    return m.group(1) if m else ""


def make_scripted_stream(*, hypotheses=HYPOTHESES, slow_seconds: float = 0.0, calls: list | None = None):
    """Build the `LLM.inference_stream_v2` replacement."""
    from app.ai.llm.types import (
        MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
    )

    def _tool(name, args):
        tid = f"tu_{uuid.uuid4().hex[:8]}"
        return [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args)]

    def _end(reason="end_turn"):
        return [MessageStopEvent(stop_reason=reason, raw_stop_reason=reason), UsageEvent(input_tokens=120, output_tokens=30)]

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        sys_text = system or ""
        text = _all_text(messages)
        n_results = _tool_result_count(messages)
        tool_names = {getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in (tools or [])}
        if calls is not None:
            idx = text.find('"tool_name"')
            shapes = []
            for m in messages or []:
                c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
                shapes.append("str" if isinstance(c, str) else [b.get("type") for b in c if isinstance(b, dict)])
            lo = text.find("<last_observation>")
            calls.append({"system": sys_text[:120], "n_results": n_results, "tools": sorted(x for x in tool_names if x),
                          # a child's goal prompt must never reach a parent planner call
                          "child_goal_leak": "CONTEXT FROM THE PARENT" in text,
                          "tool_name_ctx": text[max(0, idx - 150): idx + 150] if idx >= 0 else None, "n_msgs": len(messages or []),
                          "shapes": shapes, "last_obs_ctx": text[max(0, lo - 200): lo + 300] if lo >= 0 else None})
        events = []
        if "Mode: Sub-agent" in sys_text:
            title = _child_title(sys_text)
            if "Slow" in title and slow_seconds:
                await asyncio.sleep(slow_seconds)
            if "Seasonality" in title:
                # Never calls finish_subtask: the loop must nudge once, then
                # the runtime synthesizes a partial record.
                events += [TextDeltaEvent(text="Weekly seasonality looks ordinary; nothing to add.")] + _end()
            elif n_results in (0, 1):
                # Twice with identical arguments: a live model re-saved its
                # plan note every step. The second call must hand back the
                # existing note (no duplicate) and the repeat guard must only
                # nudge, so the child still reaches finish_subtask.
                events += _tool(
                    "create_note",
                    {"title": f"Ledger — {title}", "content": f"- [x] checked {title}\n- numbers: 787 → 676 paid/day"},
                ) + _end("tool_use")
            else:
                m = re.search(r'"note_id"\s*:\s*"([^"]+)"', text)
                note_id = m.group(1) if m else None
                supported = "release" in title.lower()
                events += _tool(
                    "finish_subtask",
                    {
                        "outcome": "supported" if supported else "refuted",
                        "confidence": 0.9 if supported else 0.8,
                        "summary": (
                            "Checkout failures jump to 59% for iOS 5.2.0 on PayFlow from 2026-09-14; the rest stays at 3%."
                            if supported else
                            f"{title}: no effect on paid orders per day (787 before vs 785 after)."
                        ),
                        "findings": [{"claim": "Daily paid orders fell from 787 to 676.", "magnitude": "-14% per day"}],
                        "ruled_out": [] if supported else [title],
                        "evidence": ([{"kind": "note", "id": note_id, "label": "ledger"}] if note_id else []),
                        "caveats": ["window 2026-08-01..2026-09-30"],
                        "unknown": None,
                        "report_note_id": note_id,
                    },
                ) + _end("tool_use")
        elif "<sub_agent_results" in text:
            events += [TextDeltaEvent(text=(
                "Root cause: the checkout-service 5.2.0 release on 2026-09-14 broke PayFlow payments on iOS "
                "(failure rate 59% vs 3% elsewhere). Ruled out: the premium price change, the APAC campaign "
                "and seasonality. One investigation ended without a verdict and is listed as partial."
            ))] + _end()
        elif "DELEGATION (sub-agents" in sys_text and "delegate" in tool_names:
            if n_results == 0:
                for title, goal in hypotheses:
                    events += _tool("delegate", {
                        "goal": goal + " Report the delta per day and whether it can explain a 15% total drop.",
                        "context": "Paid orders = rows in orders with status='paid'. Baseline 787 paid/day before 2026-09-14.",
                        "title": title,
                    })
                events += _end("tool_use")
            else:
                events += [TextDeltaEvent(text=f"Launched {len(hypotheses)} sub-agents to test the candidate causes; I will report back when they finish.")] + _end()
        elif "delegate" not in tool_names and "DELEGATION" not in sys_text and n_results == 0 and "Launched" not in text and "<sub_agent" not in text and "Mode: Chat" in sys_text:
            # delegate hidden (setting off): answer directly
            events += [TextDeltaEvent(text="Sub-agents are off here, so I answer directly: paid orders fell 14% from 2026-09-14.")] + _end()
        else:
            events += [TextDeltaEvent(text="ok")] + _end()
        for e in events:
            yield e

    return scripted


async def _plain_text_stream(self, prompt, **kwargs):
    yield "Checkout drop investigation"


def _plain_text(self, prompt, *args, **kwargs):
    # Sync one-shot inference (judge, title, check-in planner): never hit a provider.
    return "ok"


@pytest.fixture
def live_client():
    """A TestClient whose event loop lives for the whole test.

    The plain ``test_client`` opens a fresh portal (event loop) per request, so
    background tasks a request spawns — the sub-agent launcher, every child run,
    the fan-in — are cancelled the moment the request returns. Used as a
    context manager the client keeps one loop up across requests, which is
    what the real server does.
    """
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as client:
        yield client


def _post_completion(client, report_id: str, prompt: str, headers: dict):
    r = client.post(f"/api/reports/{report_id}/completions", json={"prompt": {"content": prompt, "widget_id": None, "step_id": None, "mentions": [{}]}}, headers=headers, params={"background": False})
    assert r.status_code == 200, r.text
    return r.json()["completions"]


@pytest.fixture
def scripted_llm(monkeypatch):
    from app.ai.llm.llm import LLM
    calls: list = []

    def _install(**kw):
        monkeypatch.setattr(LLM, "inference_stream_v2", make_scripted_stream(calls=calls, **kw))
        monkeypatch.setattr(LLM, "inference_stream", _plain_text_stream)
        monkeypatch.setattr(LLM, "inference", _plain_text)
        return calls

    return _install


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _env(create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch, title="Sub-agents"):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    report = create_report(title=title, user_token=token, org_id=org_id, data_sources=[])
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    return report, headers, org_id, token


def _wait_for(pred, *, timeout_s: float = 120.0, step: float = 1.0, what: str = "condition"):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}; last={last!r}")


def _sub_agents(client, report_id, headers):
    r = client.get(f"/api/reports/{report_id}/sub-agents", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_wave_fans_out_runs_children_hidden_and_fans_in(
    live_client, create_user, login_user, whoami, create_report, create_llm_provider_and_models,
    monkeypatch, scripted_llm, set_visibility,
):
    """One planner response with N delegate calls → one wave with N children;
    children are hidden completions with parent links; each finishes through
    finish_subtask (or is synthesized); the records are written back onto the
    delegate rows; exactly one fan-in turn fires with every record, after which
    the parent synthesizes. The whole contract, end to end."""
    from sqlalchemy import select
    from app.models.agent_execution import AgentExecution
    from app.models.completion import Completion
    from app.models.sub_agent import SubAgentRun, SubAgentWave

    calls = scripted_llm()
    report, headers, org_id, token = _env(create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch)
    rid = report["id"]

    test_client = live_client
    completions = _post_completion(test_client, rid, "Paid orders dropped about 15% since mid-September. Why?", headers)
    parent_sys = next(c for c in completions if c["role"] == "system")
    assert parent_sys["status"] == "success", parent_sys

    # The parent's turn ended with the receipts; the wave runs in the background.
    data = _wait_for(lambda: (lambda d: d if d["waves"] and d["waves"][-1]["status"] in ("done", "timed_out", "stopped") else None)(_sub_agents(test_client, rid, headers)),
                     timeout_s=180, what="wave to fan in")
    wave = data["waves"][-1]
    assert wave["status"] == "done", wave
    assert wave["total"] == len(HYPOTHESES) and wave["remaining"] == 0
    assert data["background_tasks"] == []  # nothing live once the wave fanned in
    runs = {r["title"]: r for r in wave["sub_agents"]}
    assert set(runs) == {t for t, _ in HYPOTHESES}

    # Contract: three finished through finish_subtask, one synthesized.
    for title, r in runs.items():
        assert r["status"] == "finished", r
        if "Seasonality" in title:
            assert r["exit_reason"] == "no_finish_call"
            assert r["outcome"] == "partial"
        else:
            assert r["exit_reason"] == "finish_call"
            assert r["outcome"] in ("supported", "refuted")
            assert 0 <= float(r["confidence"]) <= 1
        assert r["child_completion_id"] and r["child_execution_id"]
    assert runs["App release 5.2.0 hypothesis"]["outcome"] == "supported"

    # Detail endpoint: record + envelope + the child's tool calls + its note.
    detail = test_client.get(f"/api/reports/{rid}/sub-agents/{runs['Pricing change hypothesis']['sub_agent_id']}", headers=headers).json()
    assert detail["record"]["outcome"] == "refuted"
    assert detail["envelope"]["exit_reason"] == "finish_call"
    child_calls = [c for c in calls if "Mode: Sub-agent" in c["system"]]
    # History isolation: the children's hidden completions (their goal prompt,
    # their tool calls) never ride in the parent's conversation history.
    parent_calls = [c for c in calls if "Mode: Sub-agent" not in c["system"]]
    assert parent_calls and not any(c["child_goal_leak"] for c in parent_calls), [c for c in parent_calls if c["child_goal_leak"]]
    assert [c["tool"] for c in detail["calls"]] == ["create_note", "create_note", "finish_subtask"], (detail["calls"], child_calls[:2])
    assert "already exists" in str(detail["calls"][1].get("summary") or detail["calls"][1]), detail["calls"][1]
    assert detail.get("note", {}).get("content"), detail.get("note")
    # One note per child, never a duplicate, and no record ever carries the
    # repeat guard's own text.
    from app.models.note import Note
    async def _note_titles():
        async with async_session_maker() as db:
            return [n.title for n in (await db.execute(select(Note).where(Note.report_id == rid))).scalars().all()]
    titles = _run(_note_titles())
    assert len(titles) == len(set(titles)) == len(HYPOTHESES) - 1, titles  # the never-finishing child wrote no note
    assert not any(str(r.get("summary") or "").startswith("Stopping") for r in runs.values()), runs
    assert detail["steps_used"] >= 2

    # Timeline: children hidden, parent + fan-in strip + synthesis visible.
    # The wave is `done` the moment the fan-in turn fires; its reply is a
    # separate agent turn that finishes a little later, so wait for it.
    def _timeline_with_terminal_reply():
        r = test_client.get(f"/api/reports/{rid}/completions", headers=headers)
        assert r.status_code == 200
        comps = r.json()["completions"]
        replies = [c for c in comps if c["role"] == "system" and c.get("trigger_source") == "sub_agent_wave"]
        return comps if replies and replies[0]["status"] != "in_progress" else None
    comps = _wait_for(_timeline_with_terminal_reply, timeout_s=60, what="fan-in reply to finish")
    assert all((c.get("trigger_source") or "") != "sub_agent" for c in comps), [c.get("trigger_source") for c in comps]
    # The shared conversation page (GET /c/{token}) hides the children too:
    # their system rows used to render as orphan assistant bubbles.
    vis = set_visibility(rid, "conversation", "public", user_token=token, org_id=org_id).json()
    pub = test_client.get(f"/api/c/{vis['conversation_share_token']}")
    assert pub.status_code == 200, pub.text
    pub_items = pub.json().get("completions") or pub.json().get("items") or []
    assert pub_items, pub.json()
    assert all((c.get("trigger_source") or "") != "sub_agent" for c in pub_items), [c.get("trigger_source") for c in pub_items]
    assert sum(1 for c in pub_items if c.get("role") == "system") == 2, [(c.get("role"), c.get("trigger_source")) for c in pub_items]
    strips = [c for c in comps if c["role"] == "external" and c.get("trigger_source") == "sub_agent_wave"]
    assert len(strips) == 1, [(c["role"], c.get("trigger_source")) for c in comps]
    assert strips[0]["prompt"]["meta"]["total"] == len(HYPOTHESES)
    replies = [c for c in comps if c["role"] == "system" and c.get("trigger_source") == "sub_agent_wave"]
    assert len(replies) == 1 and replies[0]["status"] == "success"
    _reply_text = " ".join(
        [((replies[0].get("completion") or {}).get("content") or "")]
        + [(b.get("content") or "") for b in (replies[0].get("completion_blocks") or [])]
    )
    assert "Root cause" in _reply_text, replies[0]

    # Write-back: the parent's delegate tool executions carry the records.
    delegate_blocks = [b for c in comps if c["id"] == parent_sys["id"] for b in (c.get("completion_blocks") or [])
                       if (b.get("tool_execution") or {}).get("tool_name") == "delegate"]
    assert len(delegate_blocks) == len(HYPOTHESES)
    for b in delegate_blocks:
        rj = b["tool_execution"]["result_json"]
        assert rj["sub_agent_id"] in {r["sub_agent_id"] for r in runs.values()}
        assert isinstance(rj.get("record"), dict) and rj["record"].get("outcome"), rj
        assert rj.get("status") == "finished"

    # Summary rows.
    summ = test_client.get(f"/api/reports/{rid}/summary", headers=headers).json()
    assert len(summ["sub_agents"]) == len(HYPOTHESES)
    assert all(s["message_id"] == parent_sys["id"] for s in summ["sub_agents"])

    # DB invariants: child execution links, exactly one wave, hidden children.
    async def _db_checks():
        async with async_session_maker() as db:
            waves = (await db.execute(select(SubAgentWave).where(SubAgentWave.report_id == rid))).scalars().all()
            assert len(waves) == 1
            rows = (await db.execute(select(SubAgentRun).where(SubAgentRun.wave_id == waves[0].id))).scalars().all()
            for row in rows:
                ae = await db.get(AgentExecution, row.child_execution_id)
                assert ae is not None and ae.parent_agent_execution_id == waves[0].parent_execution_id
                assert ae.parent_tool_execution_id == row.parent_tool_execution_id
                assert ae.status in ("success", "error")
                child = await db.get(Completion, row.child_completion_id)
                assert child.trigger_source == "sub_agent" and child.status in ("success", "error", "stopped")
                assert row.envelope_json and row.envelope_json["exit_reason"] == row.exit_reason
            return True
    assert _run(_db_checks())


@pytest.mark.e2e
def test_stop_one_child_keeps_wave_alive_and_stop_wave_skips_fan_in(
    live_client, create_user, login_user, whoami, create_report, create_llm_provider_and_models,
    monkeypatch, scripted_llm,
):
    """Stopping one child records it as interrupted and the wave still fans in
    with every record; stopping a whole wave stops its children and creates no
    fan-in turn. Both go through the same service path the deadline job uses."""
    from sqlalchemy import select
    from app.models.completion import Completion

    hyps = [("Slow pricing hypothesis", "Takes a while."), ("App release 5.2.0 hypothesis", "Fast one.")]
    scripted_llm(hypotheses=hyps, slow_seconds=25.0)
    report, headers, org_id, token = _env(create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch, "Stop one")
    rid = report["id"]
    test_client = live_client
    _post_completion(test_client, rid, "Why did paid orders drop?", headers)

    data = _wait_for(lambda: (lambda d: d if d["waves"] and any(r["status"] == "running" and "Slow" in r["title"] for r in d["waves"][-1]["sub_agents"]) else None)(_sub_agents(test_client, rid, headers)),
                     timeout_s=90, what="slow child to start")
    slow = next(r for r in data["waves"][-1]["sub_agents"] if "Slow" in r["title"])
    assert data["background_tasks"], "a running child must show as a background task"

    r = test_client.post(f"/api/reports/{rid}/sub-agents/{slow['sub_agent_id']}/stop", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "interrupted", r.text

    data = _wait_for(lambda: (lambda d: d if d["waves"][-1]["status"] == "done" else None)(_sub_agents(test_client, rid, headers)),
                     timeout_s=120, what="wave to fan in after a child stop")
    runs = {x["title"]: x for x in data["waves"][-1]["sub_agents"]}
    assert runs["Slow pricing hypothesis"]["status"] == "interrupted"
    assert runs["Slow pricing hypothesis"]["exit_reason"] == "stopped"
    assert runs["Slow pricing hypothesis"]["outcome"] == "partial"  # synthesized, never a missing record
    assert runs["App release 5.2.0 hypothesis"]["status"] == "finished"
    comps = test_client.get(f"/api/reports/{rid}/completions", headers=headers).json()["completions"]
    assert sum(1 for c in comps if c["role"] == "external" and c.get("trigger_source") == "sub_agent_wave") == 1

    # ── stop the whole wave on a second report ──
    hyps2 = [("Slow A hypothesis", "slow"), ("Slow B hypothesis", "slow")]
    scripted_llm(hypotheses=hyps2, slow_seconds=40.0)
    report2 = create_report(title="Stop wave", user_token=token, org_id=org_id, data_sources=[])
    rid2 = report2["id"]
    _post_completion(test_client, rid2, "Why did paid orders drop?", headers)
    data = _wait_for(lambda: (lambda d: d if d["waves"] and d["waves"][-1]["status"] == "running" else None)(_sub_agents(test_client, rid2, headers)),
                     timeout_s=60, what="wave to start")
    wave_id = data["waves"][-1]["id"]
    r = test_client.post(f"/api/reports/{rid2}/sub-agent-waves/{wave_id}/stop", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "stopped", r.text
    data = _sub_agents(test_client, rid2, headers)
    assert data["waves"][-1]["status"] == "stopped"
    assert all(x["status"] == "interrupted" for x in data["waves"][-1]["sub_agents"]), data["waves"][-1]["sub_agents"]
    assert data["background_tasks"] == []
    # Give any in-flight finalize a moment, then assert no fan-in ever appears.
    time.sleep(6)
    comps = test_client.get(f"/api/reports/{rid2}/completions", headers=headers).json()["completions"]
    assert not any(c.get("trigger_source") == "sub_agent_wave" for c in comps)

    async def _children_stopped():
        async with async_session_maker() as db:
            rows = (await db.execute(select(Completion).where(Completion.report_id == rid2, Completion.trigger_source == "sub_agent", Completion.role == "system"))).scalars().all()
            # Every child that had started is sigkilled. On SQLite the wave
            # runs its children one at a time (effective_concurrency), so a
            # child the stop reached while still pending has no completion
            # at all — it is interrupted above without ever running.
            return bool(rows) and all(c.sigkill is not None for c in rows)
    assert _wait_for(lambda: _run(_children_stopped()), timeout_s=30, what="children sigkilled")


@pytest.mark.e2e
def test_stop_reaches_a_child_that_is_still_starting(
    live_client, create_user, login_user, whoami, create_report, create_llm_provider_and_models,
    monkeypatch, scripted_llm,
):
    """A stop that lands while a child is being started (its run claimed, its
    completion not yet recorded on the row) still stops that child: the
    completion is sigkilled and the run ends as stopped, not finished."""
    from sqlalchemy import select

    from app.models.agent_execution import AgentExecution
    from app.models.completion import Completion
    from app.models.sub_agent import SubAgentRun
    from app.services.completion_service import CompletionService
    from app.services.sub_agent_service import sub_agent_service

    # The API cannot place a stop inside that window on demand, so stop the
    # wave from inside the child's create_completion: the run is already
    # claimed, its completion is about to be created and not yet recorded on
    # the row. The real stop_wave and create_completion both run; only the
    # timing is forced.
    real_create = CompletionService.create_completion
    stopped_from: list = []

    async def stop_then_create(self, *args, **kwargs):
        if kwargs.get("trigger_source") == "sub_agent":
            async with async_session_maker() as db:
                row = await db.get(SubAgentRun, kwargs["completion_data"].prompt.sub_agent_id)
                await sub_agent_service.stop_wave(db, row.wave_id, reason="stopped while starting")
                stopped_from.append(str(row.id))
        return await real_create(self, *args, **kwargs)

    monkeypatch.setattr(CompletionService, "create_completion", stop_then_create)
    hyps = [(f"Starting {uuid.uuid4().hex[:6]} hypothesis", "slow"), (f"Starting {uuid.uuid4().hex[:6]} hypothesis", "slow")]
    scripted_llm(hypotheses=hyps)
    report, headers, _org_id, _token = _env(create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch, "Stop while starting")
    rid = report["id"]
    _post_completion(live_client, rid, "Why did paid orders drop?", headers)

    data = _wait_for(lambda: (lambda d: d if d["waves"] and d["waves"][-1]["status"] == "stopped" else None)(_sub_agents(live_client, rid, headers)),
                     timeout_s=60, what="wave stopped while a child was starting")
    assert stopped_from, "the stop must have landed inside a child's start"
    assert all(x["status"] == "interrupted" and x["exit_reason"] == "stopped" for x in data["waves"][-1]["sub_agents"]), data["waves"][-1]["sub_agents"]
    assert data["background_tasks"] == []

    async def _children_stopped():
        async with async_session_maker() as db:
            rows = (await db.execute(select(Completion).where(
                Completion.report_id == rid, Completion.trigger_source == "sub_agent", Completion.role == "system",
            ))).scalars().all()
            # On Postgres a second child may still be finishing its start when
            # the wave reads stopped; it is signalled as soon as it does.
            return bool(rows) and all(c.sigkill is not None for c in rows)

    assert _wait_for(lambda: _run(_children_stopped()), timeout_s=30, what="every started child sigkilled")

    async def _executions_ended():
        async with async_session_maker() as db:
            runs = (await db.execute(select(SubAgentRun).where(SubAgentRun.report_id == rid))).scalars().all()
            out = []
            for r in runs:
                if not r.child_completion_id:
                    continue  # never started: interrupted while pending
                ae = await db.get(AgentExecution, r.child_execution_id) if r.child_execution_id else None
                if ae is None or ae.status not in ("success", "error", "sigkill"):
                    return None
                out.append(ae.status)
            return out or None

    statuses = _wait_for(lambda: _run(_executions_ended()), timeout_s=60, what="started children to end")
    # Stopped before (or as) it began work: the run honours the stop instead
    # of running to completion.
    assert all(s == "sigkill" for s in statuses), statuses
    comps = live_client.get(f"/api/reports/{rid}/completions", headers=headers).json()["completions"]
    assert not any(c.get("trigger_source") == "sub_agent_wave" for c in comps)


@pytest.mark.e2e
def test_setting_off_hides_delegate_and_no_wave_is_created(
    live_client, create_user, login_user, whoami, create_report, create_llm_provider_and_models,
    update_organization_settings, monkeypatch, scripted_llm,
):
    """enable_sub_agents=false: delegate is not in the catalog (the scripted
    parent sees no delegate tool and answers directly), and no wave exists."""
    calls = scripted_llm()
    report, headers, org_id, token = _env(create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch, "Setting off")
    update_organization_settings({"enable_sub_agents": {"value": False}}, user_token=token, org_id=org_id)
    rid = report["id"]
    test_client = live_client
    completions = _post_completion(test_client, rid, "Paid orders dropped about 15% since mid-September. Why?", headers)
    parent_sys = next(c for c in completions if c["role"] == "system")
    assert parent_sys["status"] == "success"
    planner_calls = [c for c in calls if "Mode: Chat" in c["system"]]
    assert planner_calls and all("delegate" not in c["tools"] for c in planner_calls)
    assert _sub_agents(test_client, rid, headers) == {"waves": [], "background_tasks": []}


@pytest.mark.e2e
def test_max_per_wave_refuses_excess_with_a_receipt(
    live_client, create_user, login_user, whoami, create_report, create_llm_provider_and_models,
    update_organization_settings, monkeypatch, scripted_llm,
):
    """sub_agent_max_per_wave caps a wave; the extra delegate call gets a refused
    receipt (never a silent drop) and the wave still fans in with the rest."""
    scripted_llm()
    report, headers, org_id, token = _env(create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch, "Cap")
    update_organization_settings({"sub_agent_max_per_wave": {"value": 2}}, user_token=token, org_id=org_id)
    rid = report["id"]
    test_client = live_client
    completions = _post_completion(test_client, rid, "Why did paid orders drop?", headers)
    parent_sys = next(c for c in completions if c["role"] == "system")
    data = _wait_for(lambda: (lambda d: d if d["waves"] and d["waves"][-1]["status"] == "done" else None)(_sub_agents(test_client, rid, headers)),
                     timeout_s=180, what="capped wave to fan in")
    assert data["waves"][-1]["total"] == 2
    comps = test_client.get(f"/api/reports/{rid}/completions", headers=headers).json()["completions"]
    delegate_results = [b["tool_execution"]["result_json"] for c in comps if c["id"] == parent_sys["id"]
                        for b in (c.get("completion_blocks") or []) if (b.get("tool_execution") or {}).get("tool_name") == "delegate"]
    assert len(delegate_results) == len(HYPOTHESES), [(c["id"] == parent_sys["id"], c["role"], [((b.get("tool_execution") or {}).get("tool_name")) for b in (c.get("completion_blocks") or [])]) for c in comps]
    refused = [r for r in delegate_results if r.get("status") == "refused"]
    assert len(refused) == len(HYPOTHESES) - 2 and all(r.get("refused") for r in refused)
