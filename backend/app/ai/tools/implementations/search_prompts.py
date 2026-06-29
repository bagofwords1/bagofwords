"""Search Prompts Tool - Discover existing reusable prompts (training mode).

Lists prompts visible to the caller (access-scoped by PromptService) and applies
keyword/regex union filtering in-process, mirroring search_instructions. Use
before create_prompt to avoid duplicates or to find a prompt to edit.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.search_prompts import (
    SearchPromptsInput,
    SearchPromptsOutput,
    SearchPromptsItem,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)


class SearchPromptsTool(Tool):
    """Search/list existing reusable prompts."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_prompts",
            description=(
                "RESEARCH: Search existing reusable prompts by a list of keyword OR regex "
                "queries (case-insensitive, unioned over title + text). Filter by scope, "
                "agent (data_source_id), or starters_only. Use BEFORE create_prompt to check "
                "for duplicates or to find a prompt to edit. Leave query empty to list all "
                "visible prompts."
            ),
            category="research",
            version="1.0.0",
            input_schema=SearchPromptsInput.model_json_schema(),
            output_schema=SearchPromptsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            tags=["training", "prompt", "search", "curation"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"query": ["revenue", "monthly"], "limit": 20},
                    "description": "Find prompts mentioning revenue or monthly.",
                },
                {
                    "input": {"starters_only": True},
                    "description": "List the conversation starters across visible agents.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchPromptsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchPromptsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = SearchPromptsInput(**tool_input)
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
                "scope": data.scope,
                "data_source_id": data.data_source_id,
                "starters_only": data.starters_only,
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
            from app.services.prompt_service import prompt_service

            result = await prompt_service.list_prompts(
                db=db,
                current_user=user,
                organization=organization,
                scope=data.scope,
                data_source_id=data.data_source_id,
                starters_only=data.starters_only,
            )
            candidates = result.get("prompts", []) if isinstance(result, dict) else []

            # --- Resolve queries into compiled patterns (literal + optional regex) ---
            queries = [q for q in (data.query or []) if isinstance(q, str) and q.strip()]
            special = re.compile(r"[\^\$\.\*\+\?\[\]\(\)\{\}\|]")
            patterns = []
            pattern_errors = []
            for q in queries:
                stripped = q.strip()
                try:
                    patterns.append(re.compile(re.escape(stripped), re.IGNORECASE))
                except re.error:
                    pass
                if special.search(stripped):
                    try:
                        patterns.append(re.compile(stripped, re.IGNORECASE))
                    except re.error as re_err:
                        pattern_errors.append(f"'{stripped}': {re_err}")

            if patterns:
                matched = []
                for p in candidates:
                    haystack = (
                        (p.get("text") or "") + "\n"
                        + (p.get("title") or "") + "\n"
                        + str(p.get("id") or "")
                    )
                    if any(pat.search(haystack) for pat in patterns):
                        matched.append(p)
                items = matched[: data.limit]
                total = len(matched)
            else:
                items = candidates[: data.limit]
                total = len(candidates)

            search_items = [
                SearchPromptsItem(
                    id=str(p.get("id", "")),
                    title=p.get("title"),
                    text=p.get("text", "") or "",
                    scope=p.get("scope"),
                    mode=p.get("mode"),
                    is_starter=p.get("is_starter"),
                    data_source_ids=[str(d) for d in (p.get("data_source_ids") or [])],
                    can_manage=bool(p.get("can_manage", False)),
                )
                for p in items
            ]

            msg = f"Found {len(search_items)} prompt(s) (total matching: {total})"
            if pattern_errors:
                msg = f"{msg}. Invalid regex(es) skipped: {'; '.join(pattern_errors)}"

            output = SearchPromptsOutput(
                success=True, prompts=search_items, total=total, message=msg
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": (
                            f"Found {len(search_items)} prompt(s) "
                            f"(query={queries}, scope={data.scope or 'any'})"
                        ),
                        "artifacts": [
                            {
                                "type": "prompt_search_result",
                                "count": len(search_items),
                                "total": total,
                                "items": [
                                    {"id": i.id, "title": i.title, "scope": i.scope, "is_starter": i.is_starter}
                                    for i in search_items
                                ],
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"search_prompts failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Search failed: {e}", "code": "SEARCH_FAILED"},
            )
