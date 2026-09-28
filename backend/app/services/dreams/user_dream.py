"""The nightly user dream: reflect on one user's day.

Gather (code) → one small-model call → validate and apply (code):

- memory: create / update / forget through MemoryService (source='dream';
  entries the user wrote are never touched; rules, secrets and personal
  details are refused there),
- follow-ups: at most 2 check-ins through ``CheckinService.plan_from_dream``
  (the ordinary check-in limits, working window, opt-out and ownership rules
  apply).

That is all it writes: no new user-facing objects. Results show up where
memory and check-ins already do.

Inputs are human-initiated turns only: machine turns (check-ins, waits,
scheduled runs, webhooks, evals) are never read, so a dream never learns from
its own output.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import lazyload, selectinload

from app.services.dreams import common as C
from app.services.dreams.runtime import DreamResult

logger = logging.getLogger(__name__)

MAX_SESSIONS = 8
MAX_MESSAGES_PER_SESSION = 10
MAX_MEMORY_IN_PROMPT = 80
FIRST_RUN_LOOKBACK_HOURS = 36


def _text(prompt_json: Any, limit: int = 400) -> str:
    if isinstance(prompt_json, dict):
        t = str(prompt_json.get("content") or "")
    else:
        t = str(prompt_json or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def _local(dt: Optional[datetime], tz: str) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))


def _fmt(dt: Optional[datetime], tz: str) -> str:
    l = _local(dt, tz)
    return l.strftime("%a %Y-%m-%d %H:%M") if l else "unknown"


@dataclass
class UserGathered:
    sessions: List[dict] = field(default_factory=list)
    memory: List[dict] = field(default_factory=list)
    upcoming: List[dict] = field(default_factory=list)
    followups: List[dict] = field(default_factory=list)
    scheduled: List[dict] = field(default_factory=list)
    report_keys: Dict[str, str] = field(default_factory=dict)  # key -> report_id
    report_owner: Dict[str, bool] = field(default_factory=dict)
    memory_by_handle: Dict[str, Any] = field(default_factory=dict)
    first_message_by_report: Dict[str, str] = field(default_factory=dict)


async def gather(db, organization_id: str, user_id: str, *, since: datetime, now: datetime, tz: str) -> UserGathered:
    from app.models.agent_checkin import AgentCheckin
    from app.models.completion import Completion
    from app.models.report import Report
    from app.models.report_context_state import ReportContextState
    from app.models.scheduled_prompt import ScheduledPrompt
    from app.services.context_compaction_service import render_summary_for_prompt
    from app.services.memory_service import MemoryService

    g = UserGathered()
    human = [
        Completion.user_id == str(user_id),
        Completion.role == "user",
        Completion.trigger_source.is_(None),
        Completion.webhook_id.is_(None),
        Completion.scheduled_prompt_id.is_(None),
    ]

    # Sessions with new human activity.
    rows = (
        await db.execute(
            select(Completion.report_id, Completion.prompt, Completion.created_at)
            .join(Report, Report.id == Completion.report_id)
            .where(Report.organization_id == str(organization_id), Report.deleted_at.is_(None),
                   Completion.created_at > since, *human)
            .order_by(Completion.created_at.desc())
        )
    ).all()
    by_report: Dict[str, List[tuple]] = defaultdict(list)
    for rid, prompt, created in rows:
        by_report[str(rid)].append((prompt, created))
    ordered = sorted(by_report.items(), key=lambda kv: kv[1][0][1], reverse=True)[:MAX_SESSIONS]

    def _key_for(report_id: str) -> str:
        for k, v in g.report_keys.items():
            if v == report_id:
                return k
        k = f"r{len(g.report_keys) + 1}"
        g.report_keys[k] = report_id
        return k

    for report_id, msgs in ordered:
        report = (
            await db.execute(
                select(Report).options(selectinload(Report.data_sources)).where(Report.id == report_id)
            )
        ).scalar_one_or_none()
        if report is None:
            continue
        key = _key_for(report_id)
        state = (
            await db.execute(select(ReportContextState).where(ReportContextState.report_id == report_id))
        ).scalar_one_or_none()
        summary = ""
        if state is not None and state.summary_json:
            try:
                summary = render_summary_for_prompt(state.summary_json)
            except Exception:
                summary = ""
        owner = str(report.user_id) == str(user_id)
        g.report_owner[key] = owner
        messages = [
            {"when": _fmt(created, tz), "text": _text(prompt)}
            for prompt, created in reversed(msgs[:MAX_MESSAGES_PER_SESSION])
            if _text(prompt)
        ]
        if messages:
            g.first_message_by_report[report_id] = messages[-1]["text"]
        g.sessions.append({
            "key": key, "title": report.title or "Untitled", "owner": owner,
            "agents": ", ".join(ds.name for ds in (report.data_sources or []) if ds.name),
            "last_active": _fmt(msgs[0][1], tz), "summary": summary, "messages": messages,
        })

    # Memory.
    entries = await MemoryService().active_entries(db, organization_id, user_id, now=now)
    for e in entries[:MAX_MEMORY_IN_PROMPT]:
        who = {"user": "you", "dream": "dream"}.get(e.source or "agent", "agent")
        event = None
        if e.event_start:
            event = e.event_start.strftime("%Y-%m-%d") + (f" → {e.event_end.strftime('%Y-%m-%d')}" if e.event_end else "")
        g.memory.append({"handle": e.handle, "who": who, "text": e.text, "tags": list(e.tags or []), "event": event})
        g.memory_by_handle[e.handle] = e
        if e.event_start and now <= e.event_start <= now + timedelta(days=14):
            g.upcoming.append({"when": e.event_start.strftime("%a %Y-%m-%d"), "text": e.text, "handle": e.handle})

    # Recent follow-ups and how they landed.
    cis = (
        await db.execute(
            select(AgentCheckin).where(
                AgentCheckin.organization_id == str(organization_id), AgentCheckin.user_id == str(user_id),
            ).order_by(AgentCheckin.created_at.desc()).limit(10)
        )
    ).scalars().all()
    for ci in cis:
        rep = await db.get(Report, str(ci.report_id))
        g.followups.append({
            "when": _fmt(ci.created_at, tz), "report": (rep.title if rep else "a report") or "a report",
            "outcome": _checkin_outcome(ci),
        })

    sps = (
        await db.execute(
            select(ScheduledPrompt).options(lazyload("*")).where(
                ScheduledPrompt.user_id == str(user_id), ScheduledPrompt.is_active.is_(True),
            )
        )
    ).scalars().all()
    g.scheduled = [{"title": sp.title or _text(sp.prompt, 120), "cron": sp.cron_schedule} for sp in sps]

    return g


def _checkin_outcome(ci) -> str:
    st = ci.status
    if st == "sent":
        return "notified you" + (" (opened)" if getattr(ci, "_opened", False) else "")
    return {
        "ran_quiet": "checked, nothing new",
        "skipped": "skipped by the judge",
        "planned": "planned",
        "cancelled": f"cancelled ({ci.status_reason or ''})",
        "rejected": f"not planned ({ci.status_reason or ''})",
        "not_proposed": "no follow-up proposed",
        "failed": "failed",
    }.get(st, st)


def _parse_local(value: str, tz: str) -> Optional[datetime]:
    """'YYYY-MM-DDTHH:MM' (org-local) → naive UTC."""
    try:
        v = value.strip().replace(" ", "T")
        if len(v) == 10:
            v += "T09:00"
        local = datetime.fromisoformat(v[:16])
    except Exception:
        return None
    local = local.replace(tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


async def run_user_dream(
    db,
    *,
    run_id: str,
    organization,
    org_settings,
    target_id: str,
    now: datetime,
    reflect=None,
    model=None,
    rng=None,
    arm_checkins: bool = True,
) -> DreamResult:
    from app.ai.agents.dreams.user_prompts import build_prompt, proposal_to_json, run_reflection
    from app.models.membership import Membership
    from app.models.user import User
    from app.services.checkin_service import checkin_service

    org_id = str(organization.id)
    user_id = str(target_id)
    tz = C.org_timezone(org_settings)
    membership = (
        await db.execute(
            select(Membership).where(Membership.organization_id == org_id, Membership.user_id == user_id)
        )
    ).scalar_one_or_none()
    since = (membership.user_dreamed_at if membership and membership.user_dreamed_at
             else now - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS))
    g = await gather(db, org_id, user_id, since=since, now=now, tz=tz)
    summary = {
        "sessions": len(g.sessions), "memory": len(g.memory), "upcoming": len(g.upcoming),
        "followups": len(g.followups),
    }
    if not g.sessions and not g.upcoming:
        return DreamResult(status="skipped", reason="nothing_new", inputs_summary=summary)

    user = await db.get(User, user_id)
    if model is None and reflect is None:
        from app.services.llm_service import LLMService

        try:
            model = await LLMService().get_default_model(db, organization, user, is_small=True)
        except Exception:
            model = None
        if model is None:
            return DreamResult(status="skipped", reason="no_model", inputs_summary=summary)

    prompt = build_prompt(
        user_name=(user.name if user else "") or "", today_local=C.local_now(now, tz).strftime("%a %Y-%m-%d %H:%M"),
        tz_name=tz, sessions=g.sessions, memory=g.memory, upcoming=g.upcoming, followups=g.followups,
        scheduled=g.scheduled,
    )
    await db.commit()
    proposal = await (reflect or run_reflection)(model, prompt, run_id=run_id)
    if proposal is None:
        return DreamResult(status="done", reason="invalid_output", inputs_summary=summary,
                           tool_calls=[{"name": "reflect", "result": "invalid_output"}])
    tool_calls: List[Dict[str, Any]] = [{"name": "reflect", "proposal": proposal_to_json(proposal)}]

    # A switch may have flipped during the model call: re-check before writes.
    from app.services.dreams.runtime import load_org_settings

    # End the read transaction (fresh snapshot) without expiring loaded
    # objects — expire_on_commit is off, a rollback would expire them all.
    await db.commit()
    org_settings = await load_org_settings(db, org_id)
    membership = (
        await db.execute(
            select(Membership).where(Membership.organization_id == org_id, Membership.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not C.user_dreaming_enabled(org_settings):
        return DreamResult(status="cancelled", reason="disabled", inputs_summary=summary, tool_calls=tool_calls)

    applied: Dict[str, Any] = {"memory": [], "follow_ups": [], "refused": []}
    await _apply_memory(db, org_id, user_id, g, proposal, applied, run_id=run_id, now=now)
    if C.checkins_enabled(org_settings) and not (membership and membership.checkins_opt_out):
        for f in proposal.follow_ups[: C.MAX_DREAM_CHECKINS]:
            rid = g.report_keys.get(f.report)
            if not rid or not g.report_owner.get(f.report):
                applied["refused"].append({"kind": "follow_up", "report": f.report, "reason": "not_owner_or_unknown"})
                continue
            due = _parse_local(f.due_local, tz)
            if due is None:
                applied["refused"].append({"kind": "follow_up", "report": f.report, "reason": "bad_due"})
                continue
            row = await checkin_service.plan_from_dream(
                db, organization_id=org_id, user_id=user_id, report_id=rid, note=f.note,
                plan_reason=f.why or "Planned overnight", due_at=due, dream_run_id=run_id, now=now,
                rng=rng, arm=arm_checkins,
            )
            applied["follow_ups"].append({
                "checkin_id": str(row.id), "report_id": rid, "status": row.status,
                "status_reason": row.status_reason, "due_at": row.due_at.isoformat() if row.due_at else None,
            })
    elif proposal.follow_ups:
        applied["refused"].append({"kind": "follow_up", "reason": "checkins_off_or_opted_out"})
    await db.commit()

    applied["summary"] = proposal.summary
    return DreamResult(status="done", inputs_summary=summary, tool_calls=tool_calls, outputs=applied)


async def _apply_memory(db, org_id, user_id, g: UserGathered, proposal, applied, *, run_id, now) -> None:
    from app.services.memory_rules import MemoryValidationError
    from app.services.memory_service import MemoryService

    svc = MemoryService()
    for op in proposal.memory[:8]:
        try:
            if op.op == "create":
                rid = g.report_keys.get(op.source or "")
                evidence = None
                if rid:
                    evidence = {"report_id": rid, "quote": (g.first_message_by_report.get(rid) or "")[:200],
                                "dream_run_id": run_id}
                res = await svc.create(
                    db, organization_id=org_id, user_id=user_id, text=op.text, tags=op.tags or ["overnight"],
                    event_start=op.event_start, event_end=op.event_end, source="dream", evidence=evidence, now=now,
                )
                applied["memory"].append({"op": "create", "handle": res.entry.handle, "deduped": res.deduped})
                continue
            entry = g.memory_by_handle.get((op.handle or "").lower())
            if entry is None:
                applied["refused"].append({"kind": "memory", "op": op.op, "handle": op.handle, "reason": "unknown_handle"})
                continue
            if (entry.source or "agent") == "user":
                applied["refused"].append({"kind": "memory", "op": op.op, "handle": op.handle, "reason": "user_authored"})
                continue
            if op.op == "update":
                changes = {"text": op.text, "tags": op.tags or None}
                if op.event_start:
                    changes["event_start"] = op.event_start
                if op.event_end:
                    changes["event_end"] = op.event_end
                await svc.update(db, entry, changes=changes, source="dream", now=now)
                applied["memory"].append({"op": "update", "handle": entry.handle})
            elif op.op == "forget":
                await svc.forget(db, entry)
                applied["memory"].append({"op": "forget", "handle": entry.handle})
        except MemoryValidationError as e:
            applied["refused"].append({"kind": "memory", "op": op.op, "reason": getattr(e, "code", "invalid")})
        except Exception:
            logger.exception("user dream: memory op failed")
            applied["refused"].append({"kind": "memory", "op": op.op, "reason": "error"})
