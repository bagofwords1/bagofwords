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
                "RESEARCH: Search existing organization instructions by a list of "
                "keyword OR regex queries (case-insensitive, unioned). Each `query` "
                "entry can be a plain keyword/phrase (matched as a literal substring) "
                "or a regex pattern (auto-detected by regex metacharacters, e.g. "
                "`revenue\\s*>\\s*\\$?\\d+`, `.*cancel.*`). Use BEFORE create_instruction "
                "to check for duplicates or to find an existing instruction to edit. "
                "Cast a wide net: pass 3-6 queries in ONE call covering different "
                "angles of the topic. Returns full instruction text so no separate "
                "read step is needed."
            ),
            category="research",
            version="1.0.0",
            input_schema=SearchInstructionsInput.model_json_schema(),
            output_schema=SearchInstructionsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            tags=["instruction", "search", "knowledge"],
            allowed_modes=["training", "knowledge"],
            examples=[
                {
                    "input": {"query": ["revenue"], "limit": 10},
                    "description": "Simple single-keyword lookup",
                },
                {
                    "input": {
                        "query": ["album revenue", "invoiceline", "sales", "black-elephant", "revenue threshold"],
                        "limit": 20,
                    },
                    "description": (
                        "Thorough multi-angle search — preferred when checking whether "
                        "a clarified term is already captured"
                    ),
                },
                {
                    "input": {"query": [r"revenue\s*>\s*\$?\d+", r"\b(album|track)_revenue\b"], "limit": 20},
                    "description": "Regex queries (auto-detected by metacharacters)",
                },
                {
                    "input": {
                        "query": ["cancelled order", "refund", ".*cancel.*"],
                        "category": "code_gen",
                    },
                    "description": "Mixed literal + regex queries scoped to code-gen instructions",
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
                "query": data.query,
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

            # --- Resolve each query into a compiled pattern ---
            # Mirrors describe_tables: literal substrings are escaped and wrapped
            # as case-insensitive regexes; entries with regex metacharacters are
            # also compiled as raw patterns (best-effort — invalid regex is
            # silently skipped and the literal form still matches).
            queries: list[str] = [q for q in (data.query or []) if isinstance(q, str) and q.strip()]

            special = re.compile(r"[\^\$\.\*\+\?\[\]\(\)\{\}\|]")
            compiled_patterns: list[re.Pattern] = []
            pattern_errors: list[str] = []
            for q in queries:
                stripped = q.strip()
                # Always include a case-insensitive literal substring match.
                try:
                    compiled_patterns.append(re.compile(re.escape(stripped), re.IGNORECASE))
                except re.error:
                    pass
                # Also compile as raw regex if it looks like one.
                if special.search(stripped):
                    try:
                        compiled_patterns.append(re.compile(stripped, re.IGNORECASE))
                    except re.error as re_err:
                        pattern_errors.append(f"'{stripped}': {re_err}")

            # --- Fetch candidate window ---
            # We intentionally pull a broad window once (no DB-level LIKE) and
            # apply all patterns in-process. This matches the describe_tables
            # approach and keeps the hot path simple: one SQL round-trip regardless
            # of how many queries the agent passes.
            window = max(data.limit * 5, 100) if compiled_patterns else data.limit

            result = await service.get_instructions(
                db=db,
                organization=organization,
                current_user=user,
                skip=0,
                limit=window,
                status="published",
                categories=categories,
                data_source_ids=data.data_source_ids,
                search=None,
                include_global=True,
            )

            candidates = result.get("items", []) if isinstance(result, dict) else []
            candidate_total = result.get("total", len(candidates)) if isinstance(result, dict) else len(candidates)

            # Also include instructions from the current draft build so the
            # harness can see instructions it created earlier in this session.
            training_build_id = runtime_ctx.get("training_build_id")
            if training_build_id:
                draft_result = await service.get_instructions(
                    db=db,
                    organization=organization,
                    current_user=user,
                    skip=0,
                    limit=window,
                    status="published",
                    categories=categories,
                    data_source_ids=data.data_source_ids,
                    search=None,
                    include_global=True,
                    build_id=training_build_id,
                )
                draft_items = draft_result.get("items", []) if isinstance(draft_result, dict) else []
                if draft_items:
                    seen_ids = {getattr(c, "id", None) for c in candidates}
                    for item in draft_items:
                        if getattr(item, "id", None) not in seen_ids:
                            candidates.append(item)
                    candidate_total += len(draft_items)

            # --- Apply patterns (union) ---
            if compiled_patterns:
                matched: list = []
                for it in candidates:
                    haystack = (
                        (getattr(it, "text", "") or "") + "\n" +
                        (getattr(it, "title", "") or "")
                    )
                    if any(p.search(haystack) for p in compiled_patterns):
                        matched.append(it)
                items = matched[: data.limit]
                total = len(matched)
            else:
                items = candidates[: data.limit]
                total = candidate_total

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
            if pattern_errors:
                msg = f"{msg}. Invalid regex(es) skipped: {'; '.join(pattern_errors)}"

            output = SearchInstructionsOutput(
                success=True,
                instructions=search_items,
                total=total,
                message=msg,
            )

            summary = (
                f"Found {len(search_items)} instruction(s) matching "
                f"query={queries} category='{data.category or ''}'"
            )

            output_dict = output.model_dump()
            output_dict["related_instructions"] = [
                {
                    "id": i.id,
                    "title": i.title,
                    "category": i.category,
                    "load_mode": i.load_mode,
                    "source_type": "search",
                }
                for i in search_items
            ]

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output_dict,
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
