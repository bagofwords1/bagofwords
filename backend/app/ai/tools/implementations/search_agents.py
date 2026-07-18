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

            candidates, scope = await resolve_candidate_agents(db, organization, user, report, mode)
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

            # Compile query patterns (literal + regex), mirroring search_instructions.
            queries = [q for q in (data.query or []) if isinstance(q, str) and q.strip()]
            patterns: List[re.Pattern] = []
            for q in queries:
                s = q.strip()
                try:
                    patterns.append(re.compile(re.escape(s), re.IGNORECASE))
                except re.error:
                    pass
                if _SPECIAL.search(s):
                    try:
                        patterns.append(re.compile(s, re.IGNORECASE))
                    except re.error:
                        pass

            usage = await rank_agents_for_user(db, str(organization.id), str(user.id) if user else None, cand_ids)
            focus_ids = set(str(x) for x in (getattr(report, "focused_data_source_ids", None) or [])) if report else set()

            matched: List[Any] = []
            for ds in candidates:
                sid = str(ds.id)
                if patterns:
                    haystack = "\n".join([
                        getattr(ds, "name", "") or "",
                        one_liners.get(sid, ""),
                        getattr(ds, "description", "") or "",
                        getattr(ds, "context", "") or "",
                        " ".join(table_names.get(sid, [])),
                        sid,
                    ])
                    if not any(p.search(haystack) for p in patterns):
                        continue
                matched.append(ds)

            matched.sort(key=lambda ds: usage.get(str(ds.id), 0.0), reverse=True)
            total = len(matched)
            matched = matched[: data.limit]

            items = [
                SearchAgentsItem(
                    id=str(ds.id), name=getattr(ds, "name", "") or "",
                    description=one_liners.get(str(ds.id), "") or None,
                    status=getattr(ds, "publish_status", None),
                    focused=str(ds.id) in focus_ids,
                    score=round(float(usage.get(str(ds.id), 0.0)), 3),
                )
                for ds in matched
            ]

            # Render FULL schema + always-instructions for the top matches — this is
            # what the agent "looks like today" when attached.
            full_ds = matched[:_FULL_RENDER_CAP]
            detail = await self._render_full(db, organization, report, user, full_ds)

            head = (
                f"Found {total} agent(s)"
                + (f" matching {queries}" if queries else "")
                + f" among {scope} agents."
            )
            listing = "\n".join(
                f"- {it.name} (id={it.id}, {it.status or 'published'}"
                + (", focused" if it.focused else "")
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

    async def _render_full(self, db, organization, report, user, data_sources: List[Any]) -> str:
        """Full tables/tools schema + always-on instructions for the given agents,
        matching the eager per-agent render used when an agent is attached."""
        if not data_sources:
            return ""
        parts: List[str] = []
        ds_ids = [str(ds.id) for ds in data_sources]
        try:
            from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
            builder = SchemaContextBuilder(db, data_sources, organization, report, user=user)
            ctx = await builder.build(with_stats=True, data_source_ids=ds_ids)
            schema_xml = ctx.render_combined(top_k_per_ds=10, index_limit=1000)
            if schema_xml:
                parts.append(schema_xml)
        except Exception:
            logger.exception("search_agents: schema render failed")
        try:
            from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
            ib = InstructionContextBuilder(db, organization, current_user=user, data_source_ids=ds_ids)
            isec = await ib.build(query=None, data_source_ids=ds_ids)
            inst_xml = isec.render(include_catalog=False)
            if inst_xml and inst_xml.strip():
                parts.append(inst_xml)
        except Exception:
            logger.exception("search_agents: instruction render failed")
        return "\n".join(parts)
