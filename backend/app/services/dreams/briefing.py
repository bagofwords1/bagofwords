"""The session-start briefing: "Since you were here".

Pull only — it never notifies. Items (current user only):

- check-in results (sent / ran quietly) since the user last saw the briefing,
- open threads the user dream noted since the briefing was last seen
  (auto-resolved when the user returns to that report, or a check-in on it
  notifies them),
- upcoming memory events in the next days — only when something is prepared
  for them (a planned check-in or an open thread), and only when the event or
  its preparation is new since the briefing was last seen,
- at most one habit offer.

Every item carries a "why" so it reads as helpful rather than creepy, and can
be marked not useful.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import lazyload

from app.models.agent_checkin import AgentCheckin
from app.models.habit_offer import OFFER_ACCEPTED, OFFER_DECLINED, OFFER_OFFERED, HabitOffer
from app.models.user_open_thread import THREAD_DISMISSED, THREAD_OPEN, THREAD_RESOLVED, UserOpenThread
from app.services.dreams import common as C

MAX_CHECKIN_ITEMS = 3
MAX_THREAD_ITEMS = 3
MAX_EVENT_ITEMS = 2
DEFAULT_LOOKBACK_DAYS = 3


def _first_sentence(text: Optional[str], limit: int = 160) -> str:
    t = " ".join((text or "").split())
    for sep in (". ", "; "):
        if sep in t:
            t = t.split(sep, 1)[0] + "."
            break
    return t if len(t) <= limit else t[: limit - 1] + "…"


async def _membership(db, org_id: str, user_id: str):
    from app.models.membership import Membership

    return (
        await db.execute(
            select(Membership).where(Membership.organization_id == org_id, Membership.user_id == user_id)
        )
    ).scalar_one_or_none()


async def resolve_threads(db, org_id: str, user_id: str) -> int:
    """Close open threads the user already picked back up."""
    from app.models.completion import Completion

    threads = (
        await db.execute(
            select(UserOpenThread).where(
                UserOpenThread.organization_id == org_id, UserOpenThread.user_id == user_id,
                UserOpenThread.status == THREAD_OPEN, UserOpenThread.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    n = 0
    for t in threads:
        returned = (
            await db.execute(
                select(Completion.id).where(
                    Completion.report_id == t.report_id, Completion.user_id == user_id,
                    Completion.role == "user", Completion.trigger_source.is_(None),
                    Completion.webhook_id.is_(None), Completion.created_at > t.created_at,
                ).limit(1)
            )
        ).first()
        notified = None
        if returned is None:
            notified = (
                await db.execute(
                    select(AgentCheckin.id).where(
                        AgentCheckin.report_id == t.report_id, AgentCheckin.user_id == user_id,
                        AgentCheckin.status == "sent", AgentCheckin.sent_at > t.created_at,
                    ).limit(1)
                )
            ).first()
        if returned is not None or notified is not None:
            t.status = THREAD_RESOLVED
            db.add(t)
            n += 1
    if n:
        await db.commit()
    return n


async def get_briefing(db, *, organization_id: str, user_id: str, now: Optional[datetime] = None,
                       report_id: Optional[str] = None) -> Dict[str, Any]:
    from app.models.memory_entry import MemoryEntry
    from app.models.report import Report

    now = now or C.utcnow()
    org_id, user_id = str(organization_id), str(user_id)
    m = await _membership(db, org_id, user_id)
    since = (m.briefing_seen_at if m and m.briefing_seen_at else now - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    await resolve_threads(db, org_id, user_id)

    titles: Dict[str, str] = {}

    async def _title(rid: str) -> str:
        if rid not in titles:
            rep = (
                await db.execute(select(Report).options(lazyload("*")).where(Report.id == rid))
            ).scalar_one_or_none()
            # Only the user's own, live reports (items are only ever created
            # for those; this also covers a report archived or re-owned since).
            visible = (rep is not None and rep.deleted_at is None and str(rep.user_id) == user_id
                       and getattr(rep, "status", None) != "archived")
            titles[rid] = ((rep.title or "Untitled") if visible else "")
        return titles[rid]

    items: List[Dict[str, Any]] = []

    # Check-in results since last seen.
    q = select(AgentCheckin).where(
        AgentCheckin.organization_id == org_id, AgentCheckin.user_id == user_id,
        AgentCheckin.status.in_(("sent", "ran_quiet")), AgentCheckin.updated_at > since,
        AgentCheckin.briefing_feedback.is_(None),
    )
    if report_id:
        q = q.where(AgentCheckin.report_id == str(report_id))
    for ci in (await db.execute(q.order_by(AgentCheckin.updated_at.desc()).limit(MAX_CHECKIN_ITEMS))).scalars().all():
        title = await _title(str(ci.report_id))
        if not title:
            continue
        items.append({
            "kind": "checkin", "id": str(ci.id), "report_id": str(ci.report_id), "report_title": title,
            "status": ci.status,
            "text": ci.notify_subject if ci.status == "sent" and ci.notify_subject else None,
            "why": _first_sentence(ci.plan_reason or ci.note),
            "when": (ci.sent_at or ci.updated_at).isoformat() if (ci.sent_at or ci.updated_at) else None,
            "origin": ci.origin or "turn",
        })

    # Open threads.
    # "Got it" means seen: threads and events show once, until a later night
    # notes them again (the user dream replaces its threads each night).
    tq = select(UserOpenThread).where(
        UserOpenThread.organization_id == org_id, UserOpenThread.user_id == user_id,
        UserOpenThread.status == THREAD_OPEN, UserOpenThread.deleted_at.is_(None),
        UserOpenThread.created_at > since,
    )
    if report_id:
        tq = tq.where(UserOpenThread.report_id == str(report_id))
    threads = (await db.execute(tq.order_by(UserOpenThread.created_at.desc()).limit(MAX_THREAD_ITEMS))).scalars().all()
    for t in threads:
        title = await _title(str(t.report_id))
        if not title:
            continue
        items.append({
            "kind": "thread", "id": str(t.id), "report_id": str(t.report_id), "report_title": title,
            "text": t.text, "why": t.unblocked_by, "when": t.created_at.isoformat() if t.created_at else None,
        })

    # Upcoming events — only when something is prepared for them.
    planned = (
        await db.execute(
            select(AgentCheckin).where(
                AgentCheckin.organization_id == org_id, AgentCheckin.user_id == user_id,
                AgentCheckin.status == "planned",
            )
        )
    ).scalars().all()
    if (planned or threads) and not report_id:
        horizon = now + timedelta(days=C.UPCOMING_EVENT_DAYS)
        events = (
            await db.execute(
                select(MemoryEntry).where(
                    MemoryEntry.organization_id == org_id, MemoryEntry.user_id == user_id,
                    MemoryEntry.status == "active", MemoryEntry.event_start.isnot(None),
                    MemoryEntry.event_start >= now - timedelta(hours=12), MemoryEntry.event_start <= horizon,
                ).order_by(MemoryEntry.event_start.asc()).limit(MAX_EVENT_ITEMS)
            )
        ).scalars().all()
        for e in events:
            prepared = next((c for c in planned if c.due_at and c.due_at <= e.event_start), None)
            prepared_title = await _title(str(prepared.report_id)) if prepared else ""
            if prepared and not prepared_title:
                prepared = None
            fresh = (e.created_at and e.created_at > since) or (prepared and prepared.created_at and prepared.created_at > since)
            if not fresh:
                continue
            items.append({
                "kind": "event", "id": str(e.id), "handle": e.handle, "text": e.text,
                "when": e.event_start.isoformat(),
                "prepared": ({"checkin_id": str(prepared.id), "report_id": str(prepared.report_id),
                              "report_title": prepared_title,
                              "due_at": prepared.due_at.isoformat()} if prepared else None),
            })

    # One habit offer.
    if not report_id:
        offer = (
            await db.execute(
                select(HabitOffer).where(
                    HabitOffer.organization_id == org_id, HabitOffer.user_id == user_id,
                    HabitOffer.status == OFFER_OFFERED, HabitOffer.deleted_at.is_(None),
                ).order_by(HabitOffer.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        offer_title = await _title(str(offer.report_id)) if offer is not None else ""
        if offer is not None and offer_title:
            items.append({
                "kind": "habit", "id": str(offer.id), "report_id": str(offer.report_id),
                "report_title": offer_title, "text": offer.intent_text,
                "cadence": offer.cadence, "time": offer.suggested_time,
            })

    return {"items": items, "since": since.isoformat() if since else None}


async def mark_seen(db, *, organization_id: str, user_id: str, now: Optional[datetime] = None) -> None:
    m = await _membership(db, str(organization_id), str(user_id))
    if m is not None:
        m.briefing_seen_at = now or C.utcnow()
        db.add(m)
        await db.commit()


async def record_feedback(db, *, organization_id: str, user_id: str, kind: str, item_id: str,
                          useful: bool) -> bool:
    """'Not useful' dismisses the item; the verdict is stored where later
    reflections can read it."""
    org_id, user_id = str(organization_id), str(user_id)
    verdict = "useful" if useful else "not_useful"
    if kind == "checkin":
        ci = (
            await db.execute(
                select(AgentCheckin).where(
                    AgentCheckin.id == str(item_id), AgentCheckin.organization_id == org_id,
                    AgentCheckin.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if ci is None:
            return False
        ci.briefing_feedback = verdict
        db.add(ci)
    elif kind == "thread":
        t = (
            await db.execute(
                select(UserOpenThread).where(
                    UserOpenThread.id == str(item_id), UserOpenThread.organization_id == org_id,
                    UserOpenThread.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if t is None:
            return False
        if not useful:
            t.status = THREAD_DISMISSED
            db.add(t)
    elif kind == "habit":
        o = await _offer(db, org_id, user_id, item_id)
        if o is None:
            return False
        if not useful and o.status == OFFER_OFFERED:
            o.status = OFFER_DECLINED
            o.decided_at = C.utcnow()
            db.add(o)
    elif kind == "event":
        from app.models.memory_entry import MemoryEntry

        e = (
            await db.execute(
                select(MemoryEntry.id).where(
                    MemoryEntry.id == str(item_id), MemoryEntry.organization_id == org_id,
                    MemoryEntry.user_id == user_id,
                )
            )
        ).first()
        if e is None:
            return False
        # Events are the user's own memory; dismissing only hides it here.
    else:
        return False
    await db.commit()
    return True


async def _offer(db, org_id: str, user_id: str, offer_id: str) -> Optional[HabitOffer]:
    return (
        await db.execute(
            select(HabitOffer).where(
                HabitOffer.id == str(offer_id), HabitOffer.organization_id == org_id,
                HabitOffer.user_id == user_id, HabitOffer.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


def cron_for(cadence: str, time_hhmm: str) -> str:
    hh, mm = (time_hhmm or "09:00").split(":")
    if cadence.startswith("weekly:"):
        dow = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}[cadence.split(":", 1)[1]]
        return f"{int(mm)} {int(hh)} * * {dow}"
    return f"{int(mm)} {int(hh)} * * *"


async def accept_habit(db, *, organization, user, offer_id: str):
    """Create a normal scheduled prompt in the offer's report."""
    from app.schemas.scheduled_prompt_schema import ScheduledPromptCreate
    from app.services.scheduled_prompt_service import ScheduledPromptService

    o = await _offer(db, str(organization.id), str(user.id), offer_id)
    if o is None:
        return None, "not_found"
    if o.status != OFFER_OFFERED:
        return o, "not_pending"
    sp = await ScheduledPromptService().create_scheduled_prompt(
        db, str(o.report_id),
        ScheduledPromptCreate(
            prompt={"content": o.intent_text},
            title=o.intent_text[:120],
            cron_schedule=cron_for(o.cadence, o.suggested_time),
            is_active=True,
        ),
        user, organization,
    )
    o.status = OFFER_ACCEPTED
    o.decided_at = C.utcnow()
    o.scheduled_prompt_id = str(sp.id)
    db.add(o)
    await db.commit()
    return o, None


async def decline_habit(db, *, organization_id: str, user_id: str, offer_id: str):
    o = await _offer(db, str(organization_id), str(user_id), offer_id)
    if o is None:
        return None, "not_found"
    if o.status != OFFER_OFFERED:
        return o, "not_pending"
    o.status = OFFER_DECLINED
    o.decided_at = C.utcnow()
    db.add(o)
    await db.commit()
    return o, None


async def overnight_log(db, *, organization_id: str, user_id: str, days: int = 14) -> List[Dict[str, Any]]:
    """The user's own dream runs, in plain terms (owner-only)."""
    from app.models.dream_run import KIND_USER, DreamRun

    since = C.utcnow() - timedelta(days=days)
    runs = (
        await db.execute(
            select(DreamRun).where(
                DreamRun.organization_id == str(organization_id), DreamRun.user_id == str(user_id),
                DreamRun.kind == KIND_USER, DreamRun.created_at >= since,
            ).order_by(DreamRun.created_at.desc())
        )
    ).scalars().all()
    out = []
    for r in runs:
        o = r.outputs or {}
        mem = o.get("memory") or []
        out.append({
            "id": str(r.id), "date": r.local_date, "status": r.status, "reason": r.status_reason,
            "memory_created": sum(1 for x in mem if x.get("op") == "create" and not x.get("deduped")),
            "memory_updated": sum(1 for x in mem if x.get("op") == "update"),
            "memory_forgotten": sum(1 for x in mem if x.get("op") == "forget"),
            "open_threads": len(o.get("open_threads") or []),
            "follow_ups": [
                {"report_id": f.get("report_id"), "status": f.get("status"), "due_at": f.get("due_at")}
                for f in (o.get("follow_ups") or [])
            ],
            "habit": o.get("habit"),
            "summary": o.get("summary"),
        })
    return out
