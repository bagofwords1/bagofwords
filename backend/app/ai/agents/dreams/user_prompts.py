"""The user dream's one structured call: reflect on one user's day.

The model proposes memory operations, open threads, at most two follow-ups
and at most one habit offer. Code validates everything: memory goes through
MemoryService (which refuses rules, secrets and edits to entries the user
wrote), follow-ups through the check-in limits, habit offers only for
recurring asks that code detected, and every reference must be a key we
supplied (r1, m7, h1) — never a free-form id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.ai.agents.checkins._llm import extract_json

SYSTEM = """You reflect overnight on one person's work with an AI data analyst, so tomorrow they find things ready and their context remembered. You write nothing to them directly.

You receive their SESSIONS (conversation reports r1, r2, ... with a summary and their own messages), their MEMORY (facts the agent keeps about them, handles like m7), UPCOMING events, their recent FOLLOW-UPS (check-ins the agent ran for them and whether they opened them), and RECURRING asks detected in their history (h1, h2, ...). Today's date and timezone are given.

Default to doing nothing. Everything you output must be grounded in a session or memory entry.

1. MEMORY — facts about THIS person that matter in future sessions: their role, projects, deadlines, meetings, time off (dates only), what they follow, their own shorthand. Prefer facts that repeat across sessions or are stated explicitly. Resolve relative dates to absolute ISO dates. NEVER store rules for how to answer or compute, business definitions, data values or query results, anything about other people, secrets, or health/personal details. Use "update" when an entry says the same thing or changed, "forget" when an entry is clearly over or wrong. Only touch entries marked (agent) or (dream) — never (you).
2. OPEN THREADS — up to 5 things they left unfinished, each tied to a report, with what would unblock it. Skip anything finished.
3. FOLLOW-UPS — at most 2, only for a concrete reason: an upcoming event a specific report prepares for (prepare shortly before, during working hours), or data they are waiting on that should land by a date. The note must say what to check and what result would be worth telling them about. Do not plan the kind of follow-up they ignore. Only for reports they own (marked owner).
4. HABIT — at most one offer, only for a RECURRING ask (h1...), phrased as an offer to have it ready. Skip if one of their scheduled tasks already covers it.

Reply with JSON only:
{
  "memory": [
    {"op": "create", "text": "...", "tags": ["..."], "event_start": null|"YYYY-MM-DD", "event_end": null|"YYYY-MM-DD", "source": "r2"},
    {"op": "update", "handle": "m7", "text": "...", "tags": ["..."], "source": "r1"},
    {"op": "forget", "handle": "m4", "why": "..."}
  ],
  "open_threads": [{"report": "r1", "text": "...", "unblocked_by": "..."|null}],
  "follow_ups": [{"report": "r3", "due_local": "YYYY-MM-DDTHH:MM", "note": "...", "why": "..."}],
  "habit": null | {"recurring": "h1", "intent": "...", "cadence": "daily"|"weekly:mon".."weekly:sun", "time": "HH:MM"},
  "summary": "one line"
}"""


@dataclass
class MemoryOp:
    op: str
    text: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    handle: Optional[str] = None
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    source: Optional[str] = None
    why: str = ""


@dataclass
class ThreadOp:
    report: str
    text: str
    unblocked_by: Optional[str] = None


@dataclass
class FollowUpOp:
    report: str
    due_local: str
    note: str
    why: str = ""


@dataclass
class HabitOp:
    recurring: str
    intent: str
    cadence: str
    time: str


@dataclass
class UserDreamProposal:
    memory: List[MemoryOp] = field(default_factory=list)
    open_threads: List[ThreadOp] = field(default_factory=list)
    follow_ups: List[FollowUpOp] = field(default_factory=list)
    habit: Optional[HabitOp] = None
    summary: str = ""


def _s(v: Any, n: int) -> str:
    v = "" if v is None else " ".join(str(v).split())
    return v if len(v) <= n else v[: n - 1] + "…"


def parse(raw: Any) -> Optional[UserDreamProposal]:
    obj = extract_json(raw)
    if not isinstance(obj, dict):
        return None
    p = UserDreamProposal(summary=_s(obj.get("summary"), 300))
    for m in obj.get("memory") or []:
        if not isinstance(m, dict) or m.get("op") not in ("create", "update", "forget"):
            continue
        tags = [str(t) for t in (m.get("tags") or []) if isinstance(t, (str, int))][:4]
        p.memory.append(MemoryOp(
            op=m["op"], text=(m.get("text") or None), tags=tags,
            handle=(str(m["handle"]) if m.get("handle") else None),
            event_start=m.get("event_start") or None, event_end=m.get("event_end") or None,
            source=(str(m["source"]) if m.get("source") else None), why=_s(m.get("why"), 300),
        ))
    for t in obj.get("open_threads") or []:
        if isinstance(t, dict) and t.get("report") and (t.get("text") or "").strip():
            p.open_threads.append(ThreadOp(
                report=str(t["report"]), text=_s(t["text"], 200),
                unblocked_by=_s(t.get("unblocked_by"), 200) or None,
            ))
    for f in obj.get("follow_ups") or []:
        if isinstance(f, dict) and f.get("report") and f.get("due_local") and (f.get("note") or "").strip():
            p.follow_ups.append(FollowUpOp(
                report=str(f["report"]), due_local=str(f["due_local"]), note=str(f["note"]).strip()[:2000],
                why=_s(f.get("why"), 400),
            ))
    h = obj.get("habit")
    if isinstance(h, dict) and h.get("recurring") and h.get("intent") and h.get("cadence") and h.get("time"):
        p.habit = HabitOp(recurring=str(h["recurring"]), intent=_s(h["intent"], 200),
                          cadence=str(h["cadence"]), time=str(h["time"]))
    return p


def build_prompt(*, user_name: str, today_local: str, tz_name: str, sessions: List[dict],
                 memory: List[dict], upcoming: List[dict], followups: List[dict],
                 recurring: List[dict], scheduled: List[dict], open_threads: List[dict]) -> str:
    L: List[str] = [f"PERSON: {_s(user_name, 80) or 'the user'}", f"TODAY: {today_local} ({tz_name})", ""]
    L.append("SESSIONS:")
    for s in sessions:
        owner = " (owner)" if s.get("owner") else ""
        L.append(f"- {s['key']}{owner}: \"{_s(s['title'], 120)}\" — agents: {_s(s.get('agents'), 120) or 'none'} — last active {s.get('last_active')}")
        if s.get("summary"):
            L.append(f"  summary: {_s(s['summary'], 1200)}")
        for m in s.get("messages") or []:
            L.append(f"  • {m['when']}: {_s(m['text'], 400)}")
    if not sessions:
        L.append("(none)")
    L.append("")
    L.append("MEMORY:")
    for m in memory:
        when = f" [{m['event']}]" if m.get("event") else ""
        tags = f" #{' #'.join(m['tags'])}" if m.get("tags") else ""
        L.append(f"- {m['handle']} ({m['who']}){when}: {_s(m['text'], 280)}{tags}")
    if not memory:
        L.append("(none)")
    L.append("")
    L.append("UPCOMING:")
    for u in upcoming:
        L.append(f"- {u['when']}: {_s(u['text'], 200)} ({u['handle']})")
    if not upcoming:
        L.append("(none)")
    L.append("")
    L.append("FOLLOW-UPS (recent):")
    for f in followups:
        L.append(f"- {f['when']} on \"{_s(f['report'], 80)}\": {f['outcome']}")
    if not followups:
        L.append("(none)")
    L.append("")
    L.append("OPEN THREADS (current):")
    for t in open_threads:
        L.append(f"- {t['report_key'] or 'r?'}: {_s(t['text'], 200)}")
    if not open_threads:
        L.append("(none)")
    L.append("")
    L.append("RECURRING asks:")
    for r in recurring:
        L.append(f"- {r['key']}: \"{_s(r['sample'], 200)}\" — {r['count']} times over {r['weeks']} weeks, usually {r['weekday']} ~{r['hour']:02d}:00 (report {r['report_key'] or 'n/a'})")
    if not recurring:
        L.append("(none)")
    L.append("")
    L.append("SCHEDULED TASKS they already have:")
    for s in scheduled:
        L.append(f"- \"{_s(s['title'], 120)}\" ({s['cron']})")
    if not scheduled:
        L.append("(none)")
    return "\n".join(L)


async def run_reflection(model, prompt: str, *, run_id: str) -> Optional[UserDreamProposal]:
    from app.ai.agents.checkins._llm import call_small_model

    raw = await call_small_model(model, system=SYSTEM, prompt=prompt, usage_scope="dream_user",
                                 usage_scope_ref_id=run_id)
    return parse(raw)


def proposal_to_json(p: UserDreamProposal) -> Dict[str, Any]:
    return {
        "memory": [m.__dict__ for m in p.memory],
        "open_threads": [t.__dict__ for t in p.open_threads],
        "follow_ups": [f.__dict__ for f in p.follow_ups],
        "habit": p.habit.__dict__ if p.habit else None,
        "summary": p.summary,
    }
