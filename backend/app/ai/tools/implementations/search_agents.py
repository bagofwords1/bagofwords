"""search_agents — discover agents (data sources) and load their full schema.

RESEARCH tool. When a report is attached to many agents, the planner sees a thin
``<available_agents>`` roster (name + one-liner) instead of every agent's full
schema. search_agents lets it pull the right agent(s) IN: it matches by
name/description/primary-instruction/table names, ranks by the caller's recent
usage, and returns the matched agents' FULL tables/tools schema **and their
always-loaded instructions** in the observation — exactly what an attached agent
looks like today. Follow it with set_report_agents to keep those agents focused.
"""
from typing import Any, AsyncIterator, Dict, List, Type
import logging
import re

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.search_agents import (
    SearchAgentsInput,
    SearchAgentsItem,
    SearchAgentsOutput,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)

# How many matched agents get their FULL schema rendered into the observation.
# The rest are listed as one-liners so a broad query can't dump everything.
_FULL_RENDER_CAP = 3
_SPECIAL = re.compile(r"[\^\$\.\*\+\?\[\]\(\)\{\}\|]")


def compile_query_patterns(queries) -> list:
    """Query terms → regex patterns, forgiving like describe_tables/get_connection.

    Case-insensitive substring, `*`/`?` globs, naive singular/plural folding of
    the query term ("albums" must find table "Album"), plus raw regex for terms
    that carry regex metacharacters.
    """
    patterns: list = []
    for q in queries or []:
        if not isinstance(q, str) or not q.strip():
            continue
        s = q.strip()
        variants = {s}
        low = s.lower()
        if low.endswith("ies") and len(low) > 4:
            variants.add(s[:-3] + "y")   # countries -> country
        if low.endswith("es") and len(low) > 3:
            variants.add(s[:-2])          # invoices -> invoic(e substring)
        if low.endswith("s") and len(low) > 2:
            variants.add(s[:-1])          # albums -> album
        for v in variants:
            try:
                # Short terms ("PO", "AWS") substring-match everywhere
                # ("portal", "flaws") — anchor them on word boundaries.
                esc = re.escape(v)
                if len(v) <= 3:
                    esc = rf"\b{esc}\b"
                patterns.append(re.compile(esc, re.IGNORECASE))
            except re.error:
                pass
        if "*" in s or "?" in s:
            import fnmatch
            try:
                # fnmatch anchors the whole string; wrap so the glob may match
                # anywhere in the haystack line.
                patterns.append(re.compile(fnmatch.translate(f"*{s}*"), re.IGNORECASE))
            except re.error:
                pass
        if _SPECIAL.search(s):
            try:
                patterns.append(re.compile(s, re.IGNORECASE))
            except re.error:
                pass
    return patterns


def match_quality(patterns, strong_haystack: str, weak_haystack: str):
    """(strength, hits) for one agent against the compiled patterns.

    strength: "strong" when any pattern hits the descriptive fields (name,
    description, one-liner, context), "weak" when only table/tool names hit,
    None when nothing matches. hits = how many patterns matched anywhere —
    multi-term coverage ranks a 3-term match above a 1-term match, so a music
    database matching only "invoices" sorts below a procurement source
    matching "purchase", "order" AND "invoices".
    """
    strong = any(p.search(strong_haystack) for p in patterns)
    hits = sum(1 for p in patterns if p.search(strong_haystack) or p.search(weak_haystack))
    if strong:
        return "strong", hits
    if hits:
        return "weak", hits
    return None, 0


class SearchAgentsTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_agents",
            description=(
                "RESEARCH: Find agents (data sources) and load their schema on demand. "
                "The report may show only a thin <available_agents> roster when many "
                "agents are attached — call this to pull the right one(s) in. Matches "
                "your `query` terms (keyword or regex, unioned) against each agent's "
                "name, description, primary instruction, and table/tool names, ranks by "
                "your recent usage, and returns the matched agents' FULL tables/tools "
                "schema plus their always-on instructions in the result. Omit `query` to "
                "list all candidate agents. After finding the right agent, call "
                "set_report_agents to keep it focused for the rest of the task."
            ),
            category="research",
            version="1.0.0",
            input_schema=SearchAgentsInput.model_json_schema(),
            output_schema=SearchAgentsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=25,
            idempotent=True,
            required_permissions=[],
            tags=["agent", "data_source", "search", "schema"],
            allowed_modes=["chat", "deep", "training"],
            examples=[
                {"input": {"query": ["revenue", "orders", "sales"], "limit": 5},
                 "description": "Find the agent that covers revenue/sales."},
                {"input": {"limit": 20}, "description": "List all candidate agents."},
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchAgentsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchAgentsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = SearchAgentsInput(**(tool_input or {}))
        except Exception as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return

        yield ToolStartEvent(type="tool.start", payload={"query": data.query, "limit": data.limit, "title": data.title})

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        mode = runtime_ctx.get("mode") or "chat"
        if not all([db, organization]):
            yield ToolErrorEvent(type="tool.error", payload={"error": "Missing required runtime context (db, organization)", "code": "MISSING_CONTEXT"})
            return

        try:
            from app.ai.tools.implementations.agent_focus_common import resolve_candidate_agents
            from app.ai.context.agent_roster import load_agent_one_liners, rank_agents_for_user

            # Same-query dedupe: an identical search this run already returned
            # (and its schemas stay loaded in context) — don't re-fetch.
            _seen: dict = runtime_ctx.setdefault("_search_agents_seen", {})
            _qkey = "|".join(sorted(q.strip().lower() for q in (data.query or []) if isinstance(q, str)))
            if _qkey and _qkey in _seen:
                prev = _seen[_qkey]
                msg = (
                    f"You already searched for this ({prev.get('head', 'same query')}). Those agents' "
                    "schemas are loaded in your context — proceed directly to data work "
                    "(create_data / inspect_data); do not search again."
                )
                out = SearchAgentsOutput(success=True, agents=[], total=prev.get("total", 0), message=msg)
                yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(),
                                    "observation": {"summary": msg, "artifacts": []}})
                return

            candidates, scope, attached_ids = await resolve_candidate_agents(db, organization, user, report, mode)
            if not candidates:
                out = SearchAgentsOutput(success=True, agents=[], total=0,
                                         message=f"No {scope} agents available to search in {mode} mode.")
                yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(),
                                    "observation": {"summary": out.message, "artifacts": []}})
                return

            cand_ids = [str(ds.id) for ds in candidates]
            # Table names per agent (for match recall) — one query for all candidates.
            table_names: Dict[str, List[str]] = {cid: [] for cid in cand_ids}
            try:
                from app.models.datasource_table import DataSourceTable
                from sqlalchemy import select as _select
                rows = (await db.execute(
                    _select(DataSourceTable.datasource_id, DataSourceTable.name)
                    .where(DataSourceTable.datasource_id.in_(cand_ids), DataSourceTable.is_active == True)
                )).all()
                for dsid, tname in rows:
                    table_names.setdefault(str(dsid), []).append(tname or "")
            except Exception:
                logger.debug("search_agents: table-name lookup failed", exc_info=True)

            one_liners = await load_agent_one_liners(db, candidates)

            queries = [q for q in (data.query or []) if isinstance(q, str) and q.strip()]
            patterns = compile_query_patterns(queries)

            from app.ai.tools.implementations.agent_focus_common import signin_required_ids
            needs_signin = await signin_required_ids(db, candidates, user)
            usage = await rank_agents_for_user(db, str(organization.id), str(user.id) if user else None, cand_ids)
            focus_ids = set(str(x) for x in (getattr(report, "focused_data_source_ids", None) or [])) if report else set()

            matched: List[Any] = []
            strength: Dict[str, str] = {}
            hits: Dict[str, int] = {}
            for ds in candidates:
                sid = str(ds.id)
                if patterns:
                    strong_hay = "\n".join([
                        getattr(ds, "name", "") or "",
                        one_liners.get(sid, ""),
                        getattr(ds, "description", "") or "",
                        getattr(ds, "context", "") or "",
                        sid,
                    ])
                    weak_hay = " ".join(table_names.get(sid, []))
                    q, n = match_quality(patterns, strong_hay, weak_hay)
                    if q is None:
                        continue
                    strength[sid] = q
                    hits[sid] = n
                matched.append(ds)

            # Zero matches must not dead-end the run: fall back to the
            # usage-ranked candidates so the model can pick and proceed
            # instead of reporting "no agents matched" and then guessing.
            no_match_fallback = bool(patterns) and not matched
            if no_match_fallback:
                matched = list(candidates)

            # Rank: match strength first (descriptive-field hits beat
            # table-name-only), then term coverage, then the caller's usage.
            matched.sort(
                key=lambda ds: (
                    strength.get(str(ds.id)) == "strong",
                    hits.get(str(ds.id), 0),
                    usage.get(str(ds.id), 0.0),
                ),
                reverse=True,
            )
            total = len(matched)
            matched = matched[: data.limit]

            items = [
                SearchAgentsItem(
                    id=str(ds.id), name=getattr(ds, "name", "") or "",
                    description=one_liners.get(str(ds.id), "") or None,
                    status=getattr(ds, "publish_status", None),
                    focused=str(ds.id) in focus_ids,
                    attached=str(ds.id) in attached_ids,
                    needs_signin=str(ds.id) in needs_signin,
                    score=round(float(usage.get(str(ds.id), 0.0)), 3),
                    **self._icon_hints(ds),
                )
                for ds in matched
            ]

            # Render FULL schema + always-instructions for the top matches — this is
            # what the agent "looks like today" when attached.
            # Tiered rendering: full schema only for STRONG matches (or, with
            # no strong match / on fallback, just the top-ranked one). Weak
            # table-name-only matches stay one-liners — loading an irrelevant
            # agent's schema pollutes attention for the rest of the run.
            if no_match_fallback:
                full_ds = matched[:1]
            elif patterns:
                strong_ds = [ds for ds in matched if strength.get(str(ds.id)) == "strong"]
                full_ds = (strong_ds or matched[:1])[:_FULL_RENDER_CAP]
            else:
                full_ds = matched[:_FULL_RENDER_CAP]
            detail = await self._render_full(db, organization, report, user, full_ds)

            # Run working set: these agents' schemas stay rendered in context
            # for the rest of the run (no persistence — see agent_v2).
            _loaded = runtime_ctx.get("loaded_agent_ids")
            if isinstance(_loaded, set):
                _loaded.update(str(ds.id) for ds in full_ds)

            if no_match_fallback:
                head = (
                    f"No direct match for {queries}; showing the {total} available {scope} "
                    "agent(s) ranked by your recent usage — pick from these."
                )
            else:
                head = (
                    f"Found {total} agent(s)"
                    + (f" matching {queries}" if queries else "")
                    + f" among {scope} agents."
                )
            from app.ai.context.agent_roster import agent_tool_surface
            surface_by_id = {str(ds.id): agent_tool_surface(ds) for ds in matched}
            listing = "\n".join(
                f"- {it.name} (id={it.id}, {it.status or 'published'}"
                + (", EMAIL agent — use search_email/read_email/list_emails, not file tools" if surface_by_id.get(it.id) == "email" else "")
                + (", matched on table names only" if strength.get(it.id) == "weak" else "")
                + (", focused" if it.focused else "")
                + ("" if it.attached else ", not in the user's selection — focusing it will ask for their approval")
                + (", SIGN-IN REQUIRED — the user must Connect this agent from the agent selector before it can be used; do not set_report_agents it, tell the user instead" if it.needs_signin else "")
                + (f") — {it.description}" if it.description else ")")
                for it in items
            )
            extra = ""
            if total > len(full_ds):
                extra = (
                    f"\n\nFull schema shown for the top {len(full_ds)}. Narrow the query or call "
                    "set_report_agents to focus a specific agent."
                )
            summary = f"{head}\n{listing}{extra}\n\n{detail}".strip()

            if _qkey:
                _seen[_qkey] = {"head": head, "total": total}
            out = SearchAgentsOutput(success=True, agents=items, total=total, message=head)
            yield ToolEndEvent(type="tool.end", payload={
                "output": out.model_dump(),
                "observation": {
                    "summary": summary,
                    "artifacts": [{
                        "type": "agent_search_result",
                        "count": len(items),
                        "total": total,
                        "items": [{"id": it.id, "name": it.name, "focused": it.focused} for it in items],
                    }],
                },
            })
        except Exception as e:
            logger.exception(f"search_agents failed: {e}")
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Search failed: {e}", "code": "SEARCH_FAILED"})

    @staticmethod
    def _icon_hints(ds: Any) -> Dict[str, Any]:
        """Icon props for DataSourceIcon: per-agent icon override, connection type,
        and catalog/connector key (best-effort; connections may not be loaded)."""
        hints: Dict[str, Any] = {"icon": getattr(ds, "icon", None)}
        try:
            conns = list(getattr(ds, "connections", None) or [])
            if conns:
                c0 = conns[0]
                hints["type"] = getattr(c0, "type", None)
                cfg = getattr(c0, "config", None) or {}
                if isinstance(cfg, dict):
                    hints["connector_key"] = cfg.get("catalog_key")
        except Exception:
            pass
        return hints

    async def _render_full(self, db, organization, report, user, data_sources: List[Any]) -> str:
        from app.ai.tools.implementations.agent_focus_common import render_agents_full
        return await render_agents_full(db, organization, report, user, data_sources)
