"""Agent roster + focus selection for the planner context.

"Agent" is the user-facing name for a ``DataSource``. When a report is attached
to many agents, dumping every agent's full schema into the planner blows the
context budget. Instead we render a thin **roster** of ALL attached agents (name
+ one-liner + item count + status) and full schema only for a **focused** subset.

Focus is resolved in this order:
  1. an explicit ``report.focused_data_source_ids`` (set via set_report_agents /
     the prompt-box focus selector),
  2. else, when the roster exceeds ``threshold``, NO schema is pre-loaded —
     the roster alone renders and the model must pick agents explicitly
     (search_agents → set_report_agents),
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


def decide_focus_mode(
    roster_ids,
    explicit,
    n: int,
    *,
    threshold: int = DEFAULT_INDEX_THRESHOLD,
) -> Tuple[List[str], str]:
    """Single source of truth for the roster gate.

    Given the set of attached agent ids, the caller's explicit
    ``report.focused_data_source_ids``, and the attached-agent count ``n``,
    decide how much to pre-load. Returns ``(focus_ids, mode)``:

      - ``([], "all")``       few agents, no explicit focus → render everything
                              (behavior identical to before the roster feature).
      - ``(explicit, "focus")`` explicit report focus honored.
      - ``([], "pick")``      many agents, nothing picked → roster only; the
                              model must choose (search_agents/set_report_agents).

    Used by both the schema roster (``build_focus_and_roster``) and the standing
    <instructions> scope so the two never disagree about which agents are "in
    play" for a turn.
    """
    roster = set(roster_ids or ())
    explicit = [str(x) for x in (explicit or []) if str(x) in roster]
    if not explicit and n <= threshold:
        return [], "all"
    if explicit:
        return explicit, "focus"
    return [], "pick"


# Connection types whose tools are the EMAIL family (search_email/read_email/
# list_emails) — file tools reject them, and the model can't tell from the
# item count alone (mailboxes render as file scopes). Surfacing this in the
# roster prevents the observed search_files-on-Gmail probe failures.
EMAIL_CONNECTION_TYPES = {"gmail_mail", "outlook_mail"}


def agent_tool_surface(ds) -> str:
    """Coarse tool family for an agent: "email", "browser", "" (default —
    tables/tools, already conveyed by item_kind). Mixed agents report the
    most restrictive special surface (email) first."""
    try:
        types = {getattr(c, "type", "") or "" for c in (getattr(ds, "connections", None) or [])}
    except Exception:
        return ""
    if types & EMAIL_CONNECTION_TYPES:
        return "email"
    if "browser" in types:
        return "browser"
    return ""


@dataclass
class RosterAgent:
    id: str
    name: str
    one_liner: str
    item_count: int
    item_kind: str  # "tables" | "tools" | "files" | "items"
    status: str     # "published" | "draft" | "disabled"
    surface: str = ""  # "email" when the agent's tools are the email family


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


# Names-only tail cap: beyond this many tail agents, only the count is shown
# (the model reaches them via search_agents). Keeps the roster bounded at any
# org size while preserving the "model never under-counts" guarantee.
MORE_AGENTS_NAME_CAP = int(os.environ.get("BOW_AGENT_MORE_NAMES_CAP", "50"))


def render_agent_roster_xml(
    agents: List[RosterAgent],
    focus_ids: List[str],
    usage: Optional[Dict[str, float]] = None,
    top_k: int = 10,
    loaded_ids: Optional[List[str]] = None,
) -> str:
    """Thin roster block, bounded for large orgs.

    Full one-line entries for the top ``top_k`` agents by the asker's usage
    (focused agents always get a full line regardless of rank); the rest are
    listed by NAME ONLY inside a ``<more_agents>`` tail (capped), so the model
    always knows the true agent count without paying per-agent tokens.
    """
    focus = set(focus_ids or [])
    loaded = set(loaded_ids or []) - focus
    usage = usage or {}

    # Rank: focused/loaded first, then usage desc, then original order (stable).
    indexed = list(enumerate(agents))
    ranked = sorted(
        indexed,
        key=lambda item: (item[1].id in focus or item[1].id in loaded, usage.get(item[1].id, 0.0), -item[0]),
        reverse=True,
    )
    head = [a for _, a in ranked[: max(1, top_k)]]
    # Preserve original roster order within the head for readability.
    head_ids = {a.id for a in head}
    head = [a for a in agents if a.id in head_ids]
    tail = [a for a in agents if a.id not in head_ids]

    lines = [f'<available_agents count="{len(agents)}" focused="{len(focus)}" loaded="{len(loaded)}">']
    if focus or loaded:
        lines.append(
            "  Agents (data sources) available to this report. Agents marked "
            "focused=\"true\" or loaded=\"true\" are expanded as full <agent> schema "
            "blocks below — use those schemas directly for data work. To load another "
            "agent, call search_agents. Focus follows the agents you actually use; "
            "set_report_agents is only for explicitly changing or clearing the "
            "selection. Match tools to each agent's kind: surface=\"email\" agents "
            "take the email tools (search_email/read_email/list_emails), NOT file "
            "tools; surface=\"browser\" agents take the browser tools "
            "(browser_navigate/snapshot/extract/act/vision) and only reach their "
            "allowed URLs; files take search_files/read_file; tables take "
            "describe_tables/create_data."
        )
    else:
        lines.append(
            "  Agents (data sources) available to this report. NO agent schema is "
            "loaded yet — pick first: ONE search_agents call with 2-5 multi-angle "
            "terms returns the matching agents WITH their full schema. Then work "
            "directly on the chosen agents — in-connection discovery (file listing/"
            "search, data queries) comes AFTER picking, not alongside it, and focus "
            "follows the agents you actually use automatically (set_report_agents is "
            "only for explicitly changing or clearing the selection). Do not guess "
            "table or column names from this list. Once a result this run has shown "
            "an agent's schema, USE it — proceed directly to data work with the "
            "table/column names from that result; do NOT search again for the same "
            "thing. Match tools to each agent's kind: surface=\"email\" agents take "
            "the email tools (search_email/read_email/list_emails), NOT file tools; "
            "surface=\"browser\" agents take the browser tools "
            "(browser_navigate/snapshot/extract/act/vision) and only reach their "
            "allowed URLs; files take search_files/read_file; tables take "
            "describe_tables/create_data."
        )
    for a in head:
        marks = ' focused="true"' if a.id in focus else (' loaded="true"' if a.id in loaded else "")
        surface = f' surface="{a.surface}"' if getattr(a, "surface", "") else ""
        body = _xml_escape(a.one_liner) if a.one_liner else ""
        lines.append(
            f'  <agent id="{a.id}" name="{_xml_escape(a.name)}" '
            f'{a.item_kind}="{a.item_count}" status="{a.status}"{surface}{marks}>{body}</agent>'
        )
    if tail:
        named = tail[:MORE_AGENTS_NAME_CAP]
        names = ", ".join(_xml_escape(a.name) for a in named)
        overflow = len(tail) - len(named)
        suffix = f" (+{overflow} more)" if overflow > 0 else ""
        lines.append(
            f'  <more_agents count="{len(tail)}">{names}{suffix} — find any of these '
            "with search_agents.</more_agents>"
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
    top_k: int = 10,
    loaded_ids: Optional[List[str]] = None,
) -> Tuple[Optional[List[str]], Optional[str], str]:
    """Resolve focus + build the roster block.

    Returns ``(focus_ids, roster_xml, mode)`` where:
      - ``mode == "all"``  -> focus_ids/roster None: render every agent (few
        agents attached; behavior identical to before this feature).
      - ``mode == "focus"`` -> explicit report focus honored.
      - ``mode == "pick"``  -> many agents, no pick yet: roster only, focus
        empty — the model must choose via search_agents/set_report_agents.
    """
    roster_ids = {str(ds.id) for ds in (data_sources or [])}
    n = len(data_sources or [])

    focus_ids, mode = decide_focus_mode(
        roster_ids, report_focused_ids, n, threshold=threshold
    )
    if mode == "all":
        return None, None, "all"

    # Many agents, nothing picked yet ("pick"): render the roster ONLY — no
    # schema is pre-loaded; the model must pick (search_agents →
    # set_report_agents) before data work. usage informs its ranking, not the
    # choice. One grouped query ranks the roster's top-K lines (and search
    # results).
    usage = await rank_agents_for_user(
        db, str(organization.id), str(user.id) if user else None, list(roster_ids)
    )

    count_map, kind_map = _counts_from_sections(schema_sections)
    one_liners = await load_agent_one_liners(db, data_sources)
    agents: List[RosterAgent] = []
    for ds in data_sources:
        sid = str(ds.id)
        surface = agent_tool_surface(ds)
        count = count_map.get(sid, 0)
        kind = kind_map.get(sid, "tables")
        # A browser agent has no tables/tools sections, so _counts_from_sections
        # reports it as "0 tables" — misleading. Show it as its five browser
        # tools instead, so the roster doesn't read as an empty agent.
        if surface == "browser" and not count:
            count, kind = 5, "tools"
        agents.append(
            RosterAgent(
                id=sid,
                name=getattr(ds, "name", "") or "",
                one_liner=one_liners.get(sid, ""),
                item_count=count,
                item_kind=kind,
                status=getattr(ds, "publish_status", "published") or "published",
                surface=surface,
            )
        )
    return focus_ids, render_agent_roster_xml(agents, focus_ids, usage=usage, top_k=top_k, loaded_ids=loaded_ids), mode


def render_manual_awareness_xml(
    selected_names: List[str],
    extras: List[Tuple[str, bool]],
    name_cap: int = MORE_AGENTS_NAME_CAP,
) -> Optional[str]:
    """Awareness block for MANUAL selections below the roster threshold.

    The user picked specific agent(s); their full schemas render as usual. This
    one-liner tells the model the org has OTHER accessible agents (names only,
    "(sign-in required)" when the user must Connect first), so a question the
    selection can't answer becomes a proposal instead of "I don't have that
    data". Expanding still goes through set_report_agents' user approval.
    """
    if not extras:
        return None
    total = len(selected_names) + len(extras)
    named = extras[:name_cap]
    names = ", ".join(
        _xml_escape(n) + (" (sign-in required)" if needs else "")
        for n, needs in named
    )
    overflow = len(extras) - len(named)
    suffix = f" (+{overflow} more)" if overflow > 0 else ""
    return (
        f'<available_agents count="{total}" selected="{len(selected_names)}">\n'
        "  The user selected specific agents for this report — their full <agent> "
        "schemas are below; work with those by default. The organization has other "
        "agents you may PROPOSE when the ask clearly needs data the selection lacks: "
        "use search_agents to find one (set_report_agents will ask the user before "
        "adding it; agents marked sign-in required need the user to Connect them "
        "from the agent selector first).\n"
        f'  <more_agents count="{len(extras)}">{names}{suffix}</more_agents>\n'
        "</available_agents>"
    )
