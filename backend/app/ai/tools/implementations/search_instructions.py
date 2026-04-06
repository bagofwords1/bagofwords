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
                "RESEARCH: Search existing organization instructions by keyword, "
                "category, or data source. Use BEFORE create_instruction to check "
                "for duplicates, or to find an existing instruction to edit instead. "
                "Returns full instruction text so no separate read step is needed."
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
                    "description": "Find existing instructions about revenue calculation",
                },
                {
                    "input": {"category": "code_gen", "limit": 20},
                    "description": "List all code-generation instructions",
                },
                {
                    "input": {"search": "status", "category": "general"},
                    "description": "Search general instructions about status fields",
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
            from app.services.instruction_service import InstructionService

            service = InstructionService()
            categories = [data.category] if data.category else None

            # Normalize search to short keywords: strip, lowercase, keep first 3 tokens.
            # The agent occasionally passes full sentences — collapse to a compact keyword
            # query so the downstream substring match behaves like a keyword index.
            normalized_search = data.search
            if normalized_search:
                tokens = [t for t in normalized_search.strip().lower().split() if len(t) >= 2]
                if len(tokens) > 3:
                    tokens = tokens[:3]
                normalized_search = " ".join(tokens) if tokens else None

            result = await service.get_instructions(
                db=db,
                organization=organization,
                current_user=user,
                skip=0,
                limit=data.limit,
                status="published",
                categories=categories,
                data_source_ids=data.data_source_ids,
                search=normalized_search,
                include_global=True,
            )

            items = result.get("items", []) if isinstance(result, dict) else []
            total = result.get("total", len(items)) if isinstance(result, dict) else len(items)

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

            output = SearchInstructionsOutput(
                success=True,
                instructions=search_items,
                total=total,
                message=f"Found {len(search_items)} instruction(s) (total matching: {total})",
            )

            summary = (
                f"Found {len(search_items)} instruction(s) matching "
                f"search='{data.search or ''}' category='{data.category or ''}'"
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
