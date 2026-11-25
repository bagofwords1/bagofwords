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
from app.utils.safe_regex import compile_bounded
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


class _GlobPattern:
    """Unanchored `*`/`?` glob, matched per line in linear time.

    fnmatch.translate("*a*b*") compiles to a regex whose failed search rescans
    from every start position — quadratic on a long haystack, and the joined
    table list of a wide warehouse is tens of KB. Here the literal pieces
    between `*`s are found in order with one forward scan.
    """

    def __init__(self, glob: str):
        self._pieces = [
            re.compile("".join("." if ch == "?" else re.escape(ch) for ch in part), re.IGNORECASE)
            for part in glob.split("*") if part
        ]

    def _search_line(self, line: str) -> bool:
        pos = 0
        for piece in self._pieces:
            m = piece.search(line, pos)
            if not m:
                return False
            pos = m.end()
        return True

    def search(self, haystack: str) -> bool:
        return any(self._search_line(line) for line in haystack.split("\n"))


def compile_query_patterns(queries) -> list:
    """Query terms → patterns, forgiving like describe_tables/get_connection.

    Case-insensitive substring, `*`/`?` globs, naive singular/plural folding of
    the query term ("albums" must find table "Album"), plus raw regex for terms
    that carry regex metacharacters. Every pattern exposes `.search(text)` and
    runs in bounded time on any haystack.
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
            patterns.append(_GlobPattern(s))
        if _SPECIAL.search(s):
            # Model-written regex: time-bounded so backtracking can't block the loop.
            rx = compile_bounded(s, re.IGNORECASE)
            if rx is not None:
                patterns.append(rx)
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


def matches_directory_path(path: str, pattern: str) -> bool:
    """Match slash-delimited segments. A ** segment consumes zero or more."""
    from fnmatch import fnmatchcase
    segments = path.casefold().split("/")
    positions = {0}
    for part in pattern.strip("/").casefold().split("/"):
        if not positions:
            return False
        if part == "**":
            positions = set(range(min(positions), len(segments) + 1))
        else:
            positions = {i + 1 for i in positions if i < len(segments) and fnmatchcase(segments[i], part)}
    return len(segments) in positions


class SearchAgentsTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_agents",
            description=(
                "RESEARCH: Find agents (data sources) and load their schema on demand. "
                "The report may show only a thin <available_agents> roster when many "
                "agents are attached — call this to pull the right one(s) in. Matches "
                "For agent folders/directories use directory=true, folder_id, or path_glob (Marketing/**); "
                "these browse organization folders, including empty folders, within report scope. Matches "
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
            allowed_modes=["chat", "training"],
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

            browsing = data.directory or data.folder_id is not None or data.path_glob is not None
            if browsing:
                result = await self._browse_directory(data, db, organization, user, report, mode)
                yield ToolEndEvent(type="tool.end", payload={
                    "output": result.model_dump(),
                    "observation": {"summary": result.model_dump_json(exclude_none=True), "artifacts": []},
                })
                return

            # Same-query dedupe: an identical search this run already returned
            # (and its schemas stay loaded in context) — don't re-fetch.
            _seen: dict = runtime_ctx.setdefault("_search_agents_seen", {})
            _qkey = "|".join(sorted(q.strip().lower() for q in (data.query or []) if isinstance(q, str)))
            if data.same_folder_as:
                _qkey = f"folder:{data.same_folder_as}:{_qkey}"
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
            # Connectors are not searched for: the connected ones are already in
            # the <connectors> block with their tools.
            from app.services.data_source_kind import is_connector
            candidates = [c for c in candidates if not is_connector(c)]
            from app.services.folder_service import visible_agent_paths
            paths = await visible_agent_paths(db, organization, user, candidates)
            if data.same_folder_as:
                home = next((a.folder_id for a in candidates if str(a.id) == data.same_folder_as
                             and str(a.id) in paths), None)
                candidates = [a for a in candidates if home and a.folder_id == home]
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

            one_liners = await load_agent_one_liners(db, candidates, organization, user)

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
                        one_liners.get(sid, ""), paths.get(sid, ""),
                        getattr(ds, "description", "") or "",
                        getattr(ds, "context", "") or "",
                        sid,
                    ])
                    # One name per line: a glob must match within a single table
                    # name, not span "orders" in one and "detail" in the next.
                    weak_hay = "\n".join(table_names.get(sid, []))
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
            folder_no_match = no_match_fallback and bool(getattr(report, "agent_folder_id", None))
            if no_match_fallback and not folder_no_match:
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
                    folder_path=paths.get(str(ds.id)),
                    description=one_liners.get(str(ds.id), "") or None,
                    status=getattr(ds, "publish_status", None),
                    reliability_status=getattr(ds, "reliability_status", None),
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
            detail = await self._render_full(db, organization, report, user, full_ds,
                                             context_hub=runtime_ctx.get("context_hub"))

            # Run working set: these agents' schemas stay rendered in context
            # for the rest of the run (no persistence — see agent_v2).
            _loaded = runtime_ctx.get("loaded_agent_ids")
            if isinstance(_loaded, set):
                _loaded.update(str(ds.id) for ds in full_ds)

            if folder_no_match:
                head = ("No matching agent in this folder. If a published instruction names an agent, "
                        "search for its exact name. If that search also fails, ask for clarification.")
            elif no_match_fallback:
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
                + (", CHAT agent — use search_chat_messages/read_chat_thread/list_chat_messages, not file tools" if surface_by_id.get(it.id) == "chat" else "")
                + (", matched on table names only" if strength.get(it.id) == "weak" else "")
                + (f", folder: {it.folder_path}" if it.folder_path else "")
                + (", focused" if it.focused else "")
                + ("" if it.attached else ", not attached to this report yet")
                + (", SIGN-IN REQUIRED — the user has not connected this agent; do not set_report_agents it — if the request needs it, call request_agent_signin with its id" if it.needs_signin else "")
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

    async def _browse_directory(self, data, db, organization, user, report, mode):
        from fastapi import HTTPException
        from app.ai.tools.implementations.agent_focus_common import resolve_candidate_agents
        from app.services.data_source_kind import is_connector
        from app.services.folder_service import folder_path, load_report_folders, queryable_folder_tree

        if report is None or user is None:
            return SearchAgentsOutput(success=True, total=0, message="No directory in this report scope.")
        tree = await queryable_folder_tree(db, organization, user)
        try:
            candidates, scope, _ = await resolve_candidate_agents(db, organization, user, report, mode)
        except HTTPException as exc:
            if exc.status_code != 409 or exc.detail != "No available agents in this folder":
                raise
            candidates, scope = [], "folder"
        # A saved manual selection may outlive a grant or publishing change.
        # Re-check current eligibility before disclosing names or descriptions.
        if scope in {"attached", "managed"}:
            from app.ai.tools.implementations.agent_focus_common import accessible_agents
            eligible = {str(agent.id) for agent in await accessible_agents(db, organization, user)}
            candidates = [agent for agent in candidates if str(agent.id) in eligible]
        candidates = [agent for agent in candidates if not is_connector(agent)]
        selected = set(await load_report_folders(db, report)) if report else set()
        allowed = set()
        if scope == "accessible" and report is not None:
            allowed.update(tree)
        else:
            # Pinned agents reveal only their visible home paths. Explicitly
            # selected folders additionally expose their queryable descendants.
            for agent in candidates:
                node = getattr(agent, "folder_id", None)
                while node in tree and node not in allowed:
                    allowed.add(node)
                    node = tree[node][0]
            children = {}
            for fid, (parent, _) in tree.items():
                children.setdefault(parent, []).append(fid)
            pending = list(selected.intersection(tree))
            while pending:
                node = pending.pop()
                allowed.add(node)
                pending.extend(children.get(node, []))

        paths = {fid: folder_path(tree, fid, "/") for fid in allowed}
        if data.path_glob is not None:
            matched = {fid for fid, path in paths.items() if matches_directory_path(path, data.path_glob)}
            if data.folder_id is not None:
                matched &= {data.folder_id}
            folders = homes = matched
        elif data.folder_id is not None:
            homes = {data.folder_id} & allowed
            folders = {fid for fid in allowed if tree[fid][0] in homes}
        else:
            folders = {fid for fid in allowed if tree[fid][0] not in allowed}
            homes = {None}

        entries = [("folder", fid, paths[fid]) for fid in sorted(folders, key=lambda fid: (paths[fid].casefold(), fid))]
        agents = {str(a.id): a for a in candidates if getattr(a, "folder_id", None) in homes}
        entries.extend(("agent", aid, a.name) for aid, a in sorted(agents.items(), key=lambda pair: (pair[1].name.casefold(), pair[0])))
        result = SearchAgentsOutput(success=True, total=len(entries),
            message="Agent directory entries. Use offset to continue if more entries remain.")
        for kind, ident, name in entries[data.offset:data.offset + data.limit]:
            if kind == "folder":
                result.folders.append({"id": ident, "path": name})
            else:
                agent = agents[ident]
                result.agents.append(SearchAgentsItem(id=ident, name=name, folder_path=paths.get(agent.folder_id),
                    description=getattr(agent, "description", None)))
        return result

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

    async def _render_full(self, db, organization, report, user, data_sources: List[Any],
                           context_hub: Any = None) -> str:
        from app.ai.tools.implementations.agent_focus_common import render_agents_full
        return await render_agents_full(db, organization, report, user, data_sources, context_hub=context_hub)
