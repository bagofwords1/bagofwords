"""Agent roster + focus selection for the planner context.

"Agent" is the user-facing name for a ``DataSource``. When a report is attached
to many agents, dumping every agent's full schema into the planner blows the
context budget. Instead we render a thin **roster** of ALL attached agents (name
+ one-liner + item count + status) and full schema only for a **focused** subset.

Focus is resolved in this order:
  1. an explicit ``report.focused_data_source_ids`` (set via set_report_agents /
     the prompt-box focus selector),
  2. else, when the roster exceeds ``threshold``, an auto-seed of the top
     ``seed_n`` agents by this user's recent usage,
  3. else (few agents) no roster — render everything, exactly as before.

The roster ALWAYS lists every attached agent, even outside the focus, so the
model never under-counts the agents it is connected to; only the heavy per-agent
schema is deferred (the model can pull another agent in with search_agents).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as _xml_escape

from sqlalchemy import func, select


# Default gate: at or below this many attached agents, behave exactly as before
# (render all, no roster). Above it, switch to roster + focused schema.
DEFAULT_INDEX_THRESHOLD = int(os.environ.get("BOW_AGENT_INDEX_THRESHOLD", "4"))
# How many agents to auto-seed into the focus when there's no explicit choice.
DEFAULT_FOCUS_SEED = int(os.environ.get("BOW_AGENT_FOCUS_SEED", "3"))


@dataclass
class RosterAgent:
    id: str
    name: str
    one_liner: str
    item_count: int
    item_kind: str  # "tables" | "tools" | "files" | "items"
    status: str     # "published" | "draft" | "disabled"


def _snippet(text: Optional[str], max_len: int = 160) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "…"


async def load_agent_one_liners(db, data_sources: List[Any]) -> Dict[str, str]:
    """Per-agent one-liner: ``description`` -> primary-instruction snippet ->
    ``context`` -> "". The primary-instruction fallback is batch-loaded in a
    single query so this stays O(1) round-trips regardless of agent count.
    """
    out: Dict[str, str] = {}
    need_primary: Dict[str, str] = {}  # instruction_id -> data_source_id
    for ds in data_sources:
        sid = str(ds.id)
        desc = (getattr(ds, "description", None) or "").strip()
        if desc:
            out[sid] = _snippet(desc)
            continue
        pinst = getattr(ds, "primary_instruction_id", None)
        if pinst:
            need_primary[str(pinst)] = sid
            continue
        out[sid] = _snippet(getattr(ds, "context", None))

    if need_primary:
        from app.models.instruction import Instruction
        rows = (
            await db.execute(
                select(Instruction.id, Instruction.text, Instruction.title).where(
                    Instruction.id.in_(list(need_primary.keys()))
                )
            )
        ).all()
        for iid, text, title in rows:
            sid = need_primary.get(str(iid))
            if sid:
                out[sid] = _snippet((text or title or "").strip())
        # Any agent whose primary instruction row was missing -> empty one-liner.
        for sid in need_primary.values():
            out.setdefault(sid, "")
    return out


async def rank_agents_for_user(
    db, org_id: str, user_id: Optional[str], ds_ids: List[str]
) -> Dict[str, float]:
    """Per-user usage score per agent: how many of THIS user's reports are
    attached to the agent, weighted by recency (30-day half-life). Higher =
    more/again-recently used by this user. Empty when we have no user."""
    if not ds_ids or not user_id:
        return {}
    from app.models.report import Report
    from app.models.report_data_source_association import (
        report_data_source_association as assoc,
    )

    rows = (
        await db.execute(
            select(
                assoc.c.data_source_id,
                func.count(Report.id),
                func.max(Report.last_activity_at),
            )
            .select_from(assoc.join(Report, assoc.c.report_id == Report.id))
            .where(
                Report.organization_id == str(org_id),
                Report.user_id == str(user_id),
                assoc.c.data_source_id.in_([str(x) for x in ds_ids]),
            )
            .group_by(assoc.c.data_source_id)
        )
    ).all()

    now = datetime.utcnow()
    scores: Dict[str, float] = {}
    for dsid, cnt, last in rows:
        recency = 1.0
        if last is not None:
            try:
                days = max(0.0, (now - last).total_seconds() / 86400.0)
                recency = 0.5 ** (days / 30.0)
            except Exception:
                recency = 1.0
        scores[str(dsid)] = float(cnt or 0) * (0.5 + recency)
    return scores


def _seed_focus(
    data_sources: List[Any], usage: Dict[str, float], seed_n: int
) -> List[str]:
    """Pick the top ``seed_n`` agents: usage desc, then published-first, then
    the roster's own order (stable)."""
    indexed = list(enumerate(data_sources))

    def sort_key(item):
        i, ds = item
        sid = str(ds.id)
        published = 1 if (getattr(ds, "publish_status", "published") == "published") else 0
        return (usage.get(sid, 0.0), published, -i)

    ranked = sorted(indexed, key=sort_key, reverse=True)
    return [str(ds.id) for _, ds in ranked[: max(1, seed_n)]]


def render_agent_roster_xml(agents: List[RosterAgent], focus_ids: List[str]) -> str:
    """Thin roster block: every attached agent, one line each, marking which are
    currently focused (full schema below) vs. deferred (load via search_agents)."""
    focus = set(focus_ids or [])
    lines = [f'<available_agents count="{len(agents)}" focused="{len(focus)}">']
    lines.append(
        "  Agents (data sources) attached to this report. Full schema below is shown "
        "ONLY for agents marked focused=\"true\". To load another agent's tables/tools "
        "and instructions, call search_agents; to change which agents are focused, call "
        "set_report_agents."
    )
    for a in agents:
        focused_attr = ' focused="true"' if a.id in focus else ""
        body = _xml_escape(a.one_liner) if a.one_liner else ""
        lines.append(
            f'  <agent id="{a.id}" name="{_xml_escape(a.name)}" '
            f'{a.item_kind}="{a.item_count}" status="{a.status}"{focused_attr}>{body}</agent>'
        )
    lines.append("</available_agents>")
    return "\n".join(lines)


def _counts_from_sections(schema_sections: List[Any]) -> Tuple[Dict[str, int], Dict[str, str]]:
    count_map: Dict[str, int] = {}
    kind_map: Dict[str, str] = {}
    for sec in schema_sections or []:
        try:
            sid = str(sec.info.id)
        except Exception:
            continue
        t = len(getattr(sec, "tables", []) or [])
        m = len(getattr(sec, "mcp_tools", []) or [])
        f = len(getattr(sec, "file_scopes", []) or [])
        nonzero = [(c, k) for c, k in ((t, "tables"), (m, "tools"), (f, "files")) if c]
        if len(nonzero) == 1:
            count_map[sid], kind_map[sid] = nonzero[0]
        elif len(nonzero) > 1:
            count_map[sid], kind_map[sid] = sum(c for c, _ in nonzero), "items"
        else:
            count_map[sid], kind_map[sid] = 0, "tables"
    return count_map, kind_map


async def build_focus_and_roster(
    db,
    organization: Any,
    user: Any,
    data_sources: List[Any],
    schema_sections: List[Any],
    report_focused_ids: Optional[List[str]],
    *,
    threshold: int = DEFAULT_INDEX_THRESHOLD,
    seed_n: int = DEFAULT_FOCUS_SEED,
) -> Tuple[Optional[List[str]], Optional[str], str]:
    """Resolve focus + build the roster block.

    Returns ``(focus_ids, roster_xml, mode)`` where:
      - ``mode == "all"``  -> focus_ids/roster None: render every agent (few
        agents attached; behavior identical to before this feature).
      - ``mode == "focus"`` -> explicit report focus honored.
      - ``mode == "seed"``  -> auto-seeded focus (many agents, no explicit pick).
    """
    roster_ids = {str(ds.id) for ds in (data_sources or [])}
    n = len(data_sources or [])

    explicit = [str(x) for x in (report_focused_ids or []) if str(x) in roster_ids]
    if explicit:
        focus_ids, mode = explicit, "focus"
    elif n > threshold:
        usage = await rank_agents_for_user(
            db, str(organization.id), str(user.id) if user else None, list(roster_ids)
        )
        focus_ids, mode = _seed_focus(data_sources, usage, seed_n), "seed"
    else:
        return None, None, "all"

    count_map, kind_map = _counts_from_sections(schema_sections)
    one_liners = await load_agent_one_liners(db, data_sources)
    agents: List[RosterAgent] = []
    for ds in data_sources:
        sid = str(ds.id)
        agents.append(
            RosterAgent(
                id=sid,
                name=getattr(ds, "name", "") or "",
                one_liner=one_liners.get(sid, ""),
                item_count=count_map.get(sid, 0),
                item_kind=kind_map.get(sid, "tables"),
                status=getattr(ds, "publish_status", "published") or "published",
            )
        )
    return focus_ids, render_agent_roster_xml(agents, focus_ids), mode
