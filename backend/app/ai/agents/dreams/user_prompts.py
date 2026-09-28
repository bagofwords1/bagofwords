"""The user dream's one structured call: reflect on one user's day.

The model proposes two things only, both through existing systems:

- memory operations (MemoryService refuses rules, secrets and edits to
  entries the user wrote),
- at most two follow-ups, which become ordinary planned check-ins (the
  check-in limits, working window, opt-out and ownership rules apply).

Every reference must be a key we supplied (r1, m7) — never a free-form id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.ai.agents.checkins._llm import extract_json

SYSTEM = """You reflect overnight on one person's work with an AI data analyst. You write nothing to them directly: you keep what the agent remembers about them up to date, and you may plan a follow-up the agent will run later.

You receive their SESSIONS (conversation reports r1, r2, ... with a summary and their own messages), their MEMORY (facts the agent keeps about them, handles like m7), UPCOMING dated memory, their recent FOLLOW-UPS (check-ins the agent ran and how they landed), and the SCHEDULED TASKS they already have. Today's date and timezone are given.

Default to doing nothing. Everything you output must be grounded in a session or memory entry.

1. MEMORY — facts about THIS person that matter in future sessions: their role, projects, deadlines, meetings, time off (dates only), what they follow, their own shorthand. Prefer facts stated explicitly or repeated across sessions. Resolve relative dates to absolute ISO dates. NEVER store rules for how to answer or compute, business definitions, data values or query results, anything about other people, secrets, or health/personal details. Use "update" when an entry says the same thing or changed, "forget" when an entry is clearly over or wrong. Only touch entries marked (agent) or (dream) — never (you).
2. FOLLOW-UPS — at most 2, only for a concrete reason: an upcoming event a specific report prepares for (run shortly before it, during working hours), or data they are waiting on that should land by a date. Only for reports marked owner. Skip if a scheduled task already covers it, or if their recent follow-ups show they ignore this kind. The note is read cold later by the agent, so make it self-contained: WHAT to check, HOW (metric / breakdown), and WHICH RESULT would be worth telling them about.

Reply with JSON only:
{
  "memory": [
    {"op": "create", "text": "...", "tags": ["..."], "event_start": null|"YYYY-MM-DD", "event_end": null|"YYYY-MM-DD", "source": "r2"},
    {"op": "update", "handle": "m7", "text": "...", "tags": ["..."], "source": "r1"},
    {"op": "forget", "handle": "m4", "why": "..."}
  ],
  "follow_ups": [{"report": "r3", "due_local": "YYYY-MM-DDTHH:MM", "note": "...", "why": "..."}],
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
class FollowUpOp:
    report: str
    due_local: str
    note: str
    why: str = ""


@dataclass
class UserDreamProposal:
    memory: List[MemoryOp] = field(default_factory=list)
    follow_ups: List[FollowUpOp] = field(default_factory=list)
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
    for f in obj.get("follow_ups") or []:
        if isinstance(f, dict) and f.get("report") and f.get("due_local") and (f.get("note") or "").strip():
            p.follow_ups.append(FollowUpOp(
                report=str(f["report"]), due_local=str(f["due_local"]), note=str(f["note"]).strip()[:2000],
                why=_s(f.get("why"), 400),
            ))
    return p


def _section(L: List[str], title: str, rows: List[str]) -> None:
    L.append(title)
    L.extend(rows or ["(none)"])
    L.append("")


def build_prompt(*, user_name: str, today_local: str, tz_name: str, sessions: List[dict],
                 memory: List[dict], upcoming: List[dict], followups: List[dict],
                 scheduled: List[dict]) -> str:
    L: List[str] = [f"PERSON: {_s(user_name, 80) or 'the user'}", f"TODAY: {today_local} ({tz_name})", ""]
    rows: List[str] = []
    for s in sessions:
        owner = " (owner)" if s.get("owner") else ""
        rows.append(f"- {s['key']}{owner}: \"{_s(s['title'], 120)}\" — agents: {_s(s.get('agents'), 120) or 'none'} — last active {s.get('last_active')}")
        if s.get("summary"):
            rows.append(f"  summary: {_s(s['summary'], 1200)}")
        for m in s.get("messages") or []:
            rows.append(f"  • {m['when']}: {_s(m['text'], 400)}")
    _section(L, "SESSIONS:", rows)
    _section(L, "MEMORY:", [
        f"- {m['handle']} ({m['who']}){' [' + m['event'] + ']' if m.get('event') else ''}: {_s(m['text'], 280)}"
        + (f" #{' #'.join(m['tags'])}" if m.get("tags") else "")
        for m in memory
    ])
    _section(L, "UPCOMING:", [f"- {u['when']}: {_s(u['text'], 200)} ({u['handle']})" for u in upcoming])
    _section(L, "FOLLOW-UPS (recent):", [f"- {f['when']} on \"{_s(f['report'], 80)}\": {f['outcome']}" for f in followups])
    _section(L, "SCHEDULED TASKS they already have:", [f"- \"{_s(s['title'], 120)}\" ({s['cron']})" for s in scheduled])
    return "\n".join(L).rstrip()


async def run_reflection(model, prompt: str, *, run_id: str) -> Optional[UserDreamProposal]:
    from app.ai.agents.checkins._llm import call_small_model

    raw = await call_small_model(model, system=SYSTEM, prompt=prompt, usage_scope="dream_user",
                                 usage_scope_ref_id=run_id)
    return parse(raw)


def proposal_to_json(p: UserDreamProposal) -> Dict[str, Any]:
    return {
        "memory": [m.__dict__ for m in p.memory],
        "follow_ups": [f.__dict__ for f in p.follow_ups],
        "summary": p.summary,
    }
