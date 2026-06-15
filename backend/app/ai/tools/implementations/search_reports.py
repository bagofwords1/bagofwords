"""Search Reports Tool — list/find the current user's own reports.

A read-only research tool that lets the agent discover the user's prior
reports by title before referencing or reading one with ``read_report``.

Scoping: results are ALWAYS restricted to reports owned by the calling user
(``Report.user_id == user.id``) within the current organization. Reports
owned by other users — even shared ones — are never returned.
"""

from typing import AsyncIterator, Dict, Any, Type
import logging

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.schemas.search_reports import (
    SearchReportsInput,
    SearchReportsOutput,
    SearchReportsItem,
)
from app.models.report import Report

logger = logging.getLogger(__name__)


class SearchReportsTool(Tool):
    """List or substring-search the current user's own reports."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_reports",
            description=(
                "RESEARCH: List or substring-search the current user's OWN reports "
                "by title (case-insensitive). Use this to find a previous report the "
                "user is referring to before reading it with read_report. Only the "
                "user's own reports are returned — never another user's. Returns "
                "report id, title, status, mode, and whether it has artifacts. "
                "Leave query empty to list the user's most recent reports."
            ),
            category="research",
            version="1.0.0",
            input_schema=SearchReportsInput.model_json_schema(),
            output_schema=SearchReportsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=15,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["report", "search", "read"],
            observation_policy="on_trigger",
            allowed_modes=["chat", "deep"],
            examples=[
                {
                    "input": {"query": "revenue", "limit": 10},
                    "description": "Find the user's reports whose title mentions 'revenue'",
                },
                {
                    "input": {"status": "published", "limit": 20},
                    "description": "List the user's published reports",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchReportsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchReportsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = SearchReportsInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"query": data.query, "status": data.status, "limit": data.limit},
        )

        context_hub = runtime_ctx.get("context_hub")
        db = context_hub.db if context_hub else runtime_ctx.get("db")
        organization = context_hub.organization if context_hub else runtime_ctx.get("organization")
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
            # User-scoped: only the caller's own reports in this organization.
            stmt = (
                select(Report)
                .where(Report.organization_id == str(organization.id))
                .where(Report.user_id == str(user.id))
                .where(Report.status != "archived")
                .where(Report.report_type == "regular")
            )

            if data.status != "all":
                stmt = stmt.where(Report.status == data.status)

            if data.mode:
                stmt = stmt.where(Report.mode == data.mode)

            if data.query and data.query.strip():
                like = f"%{data.query.strip()}%"
                stmt = stmt.where(Report.title.ilike(like))

            stmt = stmt.order_by(Report.created_at.desc()).limit(data.limit)
            reports = (await db.execute(stmt)).scalars().all()

            items = [
                SearchReportsItem(
                    id=str(r.id),
                    title=r.title or "Untitled",
                    slug=r.slug,
                    status=r.status,
                    mode=r.mode,
                    has_artifacts=bool(r.artifacts),
                    created_at=str(r.created_at) if r.created_at else None,
                    updated_at=str(r.updated_at) if r.updated_at else None,
                )
                for r in reports
            ]

            output = SearchReportsOutput(
                success=True,
                reports=items,
                total=len(items),
                search_query=data.query,
                message=f"Found {len(items)} report(s)",
            )

            summary = (
                f"Found {len(items)} report(s) "
                f"for query='{data.query or ''}' status='{data.status}'"
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "report_search_result",
                                "count": len(items),
                                "items": [
                                    {"id": i.id, "title": i.title, "status": i.status, "mode": i.mode}
                                    for i in items
                                ],
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"search_reports failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Search failed: {e}", "code": "SEARCH_FAILED"},
            )
