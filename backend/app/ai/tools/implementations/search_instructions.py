"""Search Instructions Tool - Discover existing instructions before creating/editing.

Used primarily by the knowledge harness phase to check for duplicate or related
instructions before creating new ones. Also available in training mode.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.search_instructions import (
    SearchInstructionsInput,
    SearchInstructionsOutput,
    SearchInstructionsItem,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)


class SearchInstructionsTool(Tool):
    """Search/list existing instructions to find duplicates or related entries
    before creating or editing instructions.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_instructions",
            description=(
                "RESEARCH: Search existing organization instructions by keyword(s), "
                "regex, category, or data source. Supports three search inputs — "
                "`search` (single keyword), `keywords` (OR list of keywords), and "
                "`regex` (case-insensitive pattern) — all unioned into one result set. "
                "Use BEFORE create_instruction to check for duplicates, or to find an "
                "existing instruction to edit instead. Cast a wide net: run multiple "
                "keywords in one call rather than one keyword per call. Returns full "
                "instruction text so no separate read step is needed."
            ),
            category="research",
            version="1.0.0",
            input_schema=SearchInstructionsInput.model_json_schema(),
            output_schema=SearchInstructionsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=["view_instructions"],
            tags=["instruction", "search", "knowledge"],
            allowed_modes=["training", "knowledge"],
            examples=[
                {
                    "input": {"search": "revenue", "limit": 10},
                    "description": "Simple single-keyword lookup",
                },
                {
                    "input": {
                        "keywords": ["album revenue", "invoiceline", "sales", "black-elephant"],
                        "limit": 20,
                    },
                    "description": (
                        "Thorough search across multiple angles of the same topic — "
                        "preferred when exploring whether a clarified term is already captured"
                    ),
                },
                {
                    "input": {"regex": r"revenue\s*>\s*\$?\d+", "limit": 20},
                    "description": "Regex search for existing revenue-threshold rules",
                },
                {
                    "input": {
                        "keywords": ["cancelled order", "refund"],
                        "category": "code_gen",
                    },
                    "description": "Multi-keyword search scoped to code-gen instructions",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchInstructionsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchInstructionsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = SearchInstructionsInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "search": data.search,
                "keywords": data.keywords,
                "regex": data.regex,
                "category": data.category,
                "limit": data.limit,
            },
        )

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")

        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization, user)",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        try:
            import re
            from app.services.instruction_service import InstructionService

            service = InstructionService()
            categories = [data.category] if data.category else None

            # --- Normalize keyword inputs ---
            # Collapse `search` to 1-3 short tokens to keep the DB substring match tight.
            def _normalize_keyword(kw: str) -> str | None:
                toks = [t for t in kw.strip().lower().split() if len(t) >= 2]
                if not toks:
                    return None
                return " ".join(toks[:3])

            normalized_search = _normalize_keyword(data.search) if data.search else None

            keyword_list: list[str] = []
            if data.keywords:
                for kw in data.keywords:
                    n = _normalize_keyword(kw) if kw else None
                    if n and n not in keyword_list:
                        keyword_list.append(n)

            # Compile the regex once; fall back to a message on error rather than
            # throwing the whole tool call away.
            compiled_regex = None
            regex_error = None
            if data.regex:
                try:
                    compiled_regex = re.compile(data.regex, re.IGNORECASE)
                except re.error as re_err:
                    regex_error = f"Invalid regex '{data.regex}': {re_err}"

            # --- Fetch candidates ---
            # Strategy:
            #  * If a single `search` keyword is provided, push it to the DB.
            #  * If `keywords` is provided, run one DB query per keyword and union
            #    the results in-memory (limit*N upper bound, deduped by id).
            #  * If only `regex` is provided (or in addition), fetch a broader
            #    candidate set unfiltered and apply the regex in-process.
            #  * All three filters combine as an OR union — an instruction is
            #    returned if it matches ANY of them.
            seen_ids: set[str] = set()
            merged_items: list = []
            union_total = 0

            async def _fetch(search_arg: str | None, per_query_limit: int):
                return await service.get_instructions(
                    db=db,
                    organization=organization,
                    current_user=user,
                    skip=0,
                    limit=per_query_limit,
                    status="published",
                    categories=categories,
                    data_source_ids=data.data_source_ids,
                    search=search_arg,
                    include_global=True,
                )

            def _merge(result):
                nonlocal union_total
                items = result.get("items", []) if isinstance(result, dict) else []
                total = result.get("total", len(items)) if isinstance(result, dict) else len(items)
                union_total = max(union_total, total)
                for it in items:
                    iid = str(getattr(it, "id", ""))
                    if iid and iid not in seen_ids:
                        seen_ids.add(iid)
                        merged_items.append(it)

            any_keyword_filter = bool(normalized_search or keyword_list)

            # 1. Keyword-driven DB fetches (each pushes substring match to SQL).
            if normalized_search:
                _merge(await _fetch(normalized_search, data.limit))
            for kw in keyword_list:
                _merge(await _fetch(kw, data.limit))

            # 2. Regex path: fetch a broader unfiltered window and keep rows
            #    whose text/title match the pattern. Unioned with keyword hits.
            regex_matched_ids: set[str] = set()
            if compiled_regex is not None:
                broad = await _fetch(None, max(data.limit * 3, 50))
                broad_items = broad.get("items", []) if isinstance(broad, dict) else []
                broad_total = broad.get("total", len(broad_items)) if isinstance(broad, dict) else len(broad_items)
                union_total = max(union_total, broad_total)
                for it in broad_items:
                    haystack = (
                        (getattr(it, "text", "") or "") + "\n" +
                        (getattr(it, "title", "") or "")
                    )
                    if compiled_regex.search(haystack):
                        iid = str(getattr(it, "id", ""))
                        regex_matched_ids.add(iid)
                        if iid and iid not in seen_ids:
                            seen_ids.add(iid)
                            merged_items.append(it)

            # 3. No filters at all → plain listing.
            if not any_keyword_filter and compiled_regex is None:
                _merge(await _fetch(None, data.limit))

            # Truncate after union + dedup.
            items = merged_items[: data.limit]
            total = union_total if union_total else len(items)

            search_items = []
            for it in items:
                search_items.append(
                    SearchInstructionsItem(
                        id=str(getattr(it, "id", "")),
                        title=getattr(it, "title", None),
                        text=getattr(it, "text", "") or "",
                        category=getattr(it, "category", None),
                        load_mode=getattr(it, "load_mode", None),
                        status=getattr(it, "status", None),
                    )
                )

            msg = f"Found {len(search_items)} instruction(s) (total matching: {total})"
            if regex_error:
                msg = f"{msg}. Note: {regex_error} — regex filter skipped."

            output = SearchInstructionsOutput(
                success=True,
                instructions=search_items,
                total=total,
                message=msg,
            )

            summary = (
                f"Found {len(search_items)} instruction(s) matching "
                f"search='{data.search or ''}' keywords={keyword_list} "
                f"regex='{data.regex or ''}' category='{data.category or ''}'"
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "instruction_search_result",
                                "count": len(search_items),
                                "total": total,
                                "items": [
                                    {
                                        "id": i.id,
                                        "title": i.title,
                                        "category": i.category,
                                    }
                                    for i in search_items
                                ],
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"search_instructions failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Search failed: {e}",
                    "code": "SEARCH_FAILED",
                },
            )
