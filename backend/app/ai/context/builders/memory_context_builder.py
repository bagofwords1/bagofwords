"""MemoryContextBuilder — the tiered <memory> block for one planner turn.

Memory is facts about the user; rules about how to answer live in
instructions. Pure code, no LLM calls. However many entries a user has, the
rendered block stays within fixed budgets:

  always   dated facts starting within 21 days or ended within the last 2
           days, then the strongest undated facts (user-confirmed first,
           then most seen, then most recent)                  (~1,100 chars)
  matched  other active entries whose text / aliases / tags overlap the
           turn's keywords, or that carry an object tag (agent:<id>,
           data_source:<id>, report:<id>) present in the turn
                                                   (~750 chars, ≤10 entries)
  index    one line summarizing what is not shown + the tags in use, so
           writers reuse existing tags                           (~200 chars)
  search_memory covers everything else.

Scoring reuses the lexical keyword matcher instructions use
(app.ai.context.keyword_match), with a unicode-aware split so non-Latin
prompts match too.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from app.ai.context.keyword_match import extract_keywords, stem
from app.services import memory_rules as R

ALWAYS_BUDGET = 1100
MATCHED_BUDGET = 750
MATCHED_MAX_ENTRIES = 10
INDEX_BUDGET = 200
EVENT_LOOKAHEAD = timedelta(days=21)
EVENT_LOOKBACK = timedelta(days=2)



@dataclass
class InjectedEntry:
    id: str
    handle: str
    dated: bool
    tier: str  # "always" | "matched"
    reason: Optional[str] = None


@dataclass
class MemoryContext:
    body: str = ""
    injected: List[InjectedEntry] = field(default_factory=list)
    index_line: str = ""
    total_entries: int = 0

    @property
    def chars(self) -> int:
        return len(self.body)

    @property
    def injected_ids(self) -> List[str]:
        return [i.id for i in self.injected]

    def trace(self) -> dict:
        """Metadata for the trace — handles/tiers, never text."""
        return {
            "injected": [
                {"id": i.id, "handle": i.handle, "dated": i.dated, "tier": i.tier} for i in self.injected
            ],
            "chars": self.chars,
            "total_entries": self.total_entries,
            "hidden": max(self.total_entries - len(self.injected), 0),
        }


def header(user_name: Optional[str]) -> str:
    who = f" about {user_name}" if user_name else " about this user"
    return (
        f"(Facts{who}: their work, projects, dates, what they follow, their own shorthand.\n"
        "No rules here — how to answer and what things mean come from <instructions>.)"
    )


def _entry_words(entry) -> Tuple[Set[str], Set[str], Set[str]]:
    text_kw = extract_keywords(entry.text or "", unicode=True)
    alias_kw: Set[str] = set()
    for a in entry.aliases or []:
        alias_kw |= extract_keywords(a, unicode=True)
    tag_kw: Set[str] = set()
    for t in entry.tags or []:
        if R.is_object_tag(t):
            continue
        tag_kw |= extract_keywords(t.replace("-", " "), unicode=True)
        tag_kw.add(t)
    return text_kw, alias_kw, tag_kw


def _hits(keywords: Set[str], words: Set[str]) -> Set[str]:
    if not keywords or not words:
        return set()
    stems = {stem(w) for w in words}
    return {k for k in keywords if k in words or stem(k) in stems}


def score_entry(entry, keywords: Set[str], object_tags: Set[str]) -> Tuple[float, Optional[str]]:
    """Relevance of one entry to the turn. Object-tag matches weigh highest,
    then aliases (the user's own words), tags, then the text."""
    obj = [t for t in (entry.tags or []) if t in object_tags]
    text_kw, alias_kw, tag_kw = _entry_words(entry)
    alias_hits = _hits(keywords, alias_kw)
    tag_hits = _hits(keywords, tag_kw)
    text_hits = _hits(keywords, text_kw)
    score = 10.0 * len(obj) + 3.0 * len(alias_hits) + 2.0 * len(tag_hits) + 1.0 * len(text_hits)
    if score <= 0:
        return 0.0, None
    if obj:
        reason = obj[0].split(":", 1)[0]
    else:
        words = sorted(alias_hits or tag_hits or text_hits)
        reason = ", ".join(f'"{w}"' for w in words[:2])
    return score, reason


def _fmt_day(dt: datetime) -> str:
    return dt.strftime("%a %Y-%m-%d")


def _relative(now: datetime, start: datetime, end: Optional[datetime]) -> str:
    today = now.date()
    s, e = start.date(), (end.date() if end else start.date())
    if s <= today <= e and s != e:
        return "now"
    delta = (s - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta > 1:
        return f"in {delta} days"
    back = (today - e).days
    if back == 0:
        return "today"
    return "yesterday" if back == 1 else f"{back} days ago"


def render_entry(entry, now: datetime, reason: Optional[str] = None) -> str:
    text = (entry.text or "").strip()
    line = f"[{entry.handle}] {text}"
    if entry.event_start:
        when = _fmt_day(entry.event_start)
        if entry.event_end and entry.event_end.date() != entry.event_start.date():
            when += f" → {entry.event_end.strftime('%m-%d')}"
        line += f" — {when} ({_relative(now, entry.event_start, entry.event_end)})"
    if entry.aliases:
        also = ", ".join(f'"{a}"' for a in entry.aliases[:3])
        line += f" (their words: {also})"
    if reason:
        line += f"   ← matched: {reason}"
    return line


def _in_event_window(entry, now: datetime) -> bool:
    if entry.event_start is None:
        return False
    end = entry.event_end or entry.event_start
    return entry.event_start <= now + EVENT_LOOKAHEAD and end >= now - EVENT_LOOKBACK


def _always_rank(entry) -> tuple:
    return (
        0 if entry.source == "user" else 1,
        -int(entry.seen_count or 0),
        -((entry.last_seen_at or datetime.min) - datetime.min).total_seconds(),
        entry.seq or 0,
    )


def build_memory_context(
    entries: Sequence,
    *,
    now: datetime,
    keywords: Iterable[str] = (),
    object_tags: Iterable[str] = (),
    user_name: Optional[str] = None,
) -> MemoryContext:
    """Render the <memory> body for ``entries`` (active, un-expired)."""
    live = [e for e in entries if getattr(e, "status", "active") == "active" and not R.is_expired(e, now)]
    ctx = MemoryContext(total_entries=len(live))
    if not live:
        return ctx
    kw = {k for k in keywords if k}
    objs = {R.normalize_tag(t) for t in object_tags if t}

    lines: List[str] = [header(user_name)]
    shown: Set[str] = set()

    # --- always tier -------------------------------------------------------
    events = sorted((e for e in live if _in_event_window(e, now)), key=lambda e: (e.event_start, e.seq or 0))
    core = sorted((e for e in live if e.event_start is None), key=_always_rank)
    used = 0
    for e in events + list(core):
        line = render_entry(e, now)
        if used + len(line) + 1 > ALWAYS_BUDGET:
            continue  # overflow: may still surface in the matched tier
        lines.append(line)
        used += len(line) + 1
        shown.add(str(e.id))
        ctx.injected.append(InjectedEntry(str(e.id), e.handle, e.event_start is not None, "always"))

    # --- matched tier ------------------------------------------------------
    candidates = []
    for e in live:
        if str(e.id) in shown:
            continue
        score, reason = score_entry(e, kw, objs)
        if score > 0:
            candidates.append((score, e, reason))
    candidates.sort(key=lambda c: (-c[0], -int(c[1].seen_count or 0), c[1].seq or 0))
    used = 0
    n = 0
    for score, e, reason in candidates:
        if n >= MATCHED_MAX_ENTRIES:
            break
        line = render_entry(e, now, reason)
        if used + len(line) + 1 > MATCHED_BUDGET:
            continue
        lines.append(line)
        used += len(line) + 1
        n += 1
        shown.add(str(e.id))
        ctx.injected.append(InjectedEntry(str(e.id), e.handle, e.event_start is not None, "matched", reason))

    # --- index line --------------------------------------------------------
    hidden = [e for e in live if str(e.id) not in shown]
    tag_counts = Counter(t for e in live for t in (e.tags or []) if not R.is_object_tag(t))
    ctx.index_line = index_line(hidden, tag_counts)
    if ctx.index_line:
        lines.append(ctx.index_line)

    ctx.body = "\n".join(lines)
    return ctx


def index_line(hidden: Sequence, tag_counts: Counter) -> str:
    """≤ INDEX_BUDGET chars: how many facts aren't shown, plus the tags in use
    (with counts) so writers reuse them."""
    head = f"Also remembered (not shown): {len(hidden)} more" if hidden else "Tags in use"
    tail = " — use search_memory." if hidden else ""
    sep = " · tags: " if hidden else ": "
    if not hidden and not tag_counts:
        return ""
    bits: List[str] = []
    for t, c in tag_counts.most_common():
        candidate = head + sep + ", ".join(bits + [f"{t} ({c})"]) + tail
        if len(candidate) > INDEX_BUDGET:
            break
        bits.append(f"{t} ({c})")
    line = head + (sep + ", ".join(bits) if bits else "") + tail
    return line[:INDEX_BUDGET]


# ---------------------------------------------------------------------------
# DB-facing wrapper used by the agent
# ---------------------------------------------------------------------------

class MemoryContextBuilder:
    def __init__(self, db, organization_id: str, user_id: str):
        self.db = db
        self.organization_id = str(organization_id)
        self.user_id = str(user_id)

    async def build(
        self,
        *,
        prompt_texts: Sequence[str] = (),
        report_title: Optional[str] = None,
        object_tags: Iterable[str] = (),
        user_name: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MemoryContext:
        from app.services.memory_service import memory_service

        now = now or datetime.utcnow()
        entries = await memory_service.active_entries(self.db, self.organization_id, self.user_id, now=now)
        keywords: Set[str] = set()
        for t in list(prompt_texts) + [report_title or ""]:
            keywords |= extract_keywords(t or "", unicode=True)
        return build_memory_context(
            entries, now=now, keywords=keywords, object_tags=object_tags, user_name=user_name
        )
