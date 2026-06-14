"""Read Report Tool — read a single report owned by the current user.

Returns the report's metadata, attached data sources, artifact summary, and
(optionally) the conversation. Use ``search_reports`` first to obtain a
``report_id``.

Scoping: the report MUST be owned by the calling user
(``Report.user_id == user.id``) within the current organization. Any other
report — including ones merely shared with the user — returns a not-found
error. For deep artifact/query content the agent should use ``read_artifact``
/ ``read_query`` within that report's own session.
"""

from typing import AsyncIterator, Dict, Any, Type, Optional
import logging

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.schemas.read_report import (
    ReadReportInput,
    ReadReportOutput,
    ReadReportMessage,
    ReadReportArtifact,
)
from app.models.report import Report

logger = logging.getLogger(__name__)

# Cap conversation size so a long report doesn't blow up the observation.
_MAX_MESSAGES = 40
_MAX_CONTENT_CHARS = 2000


def _extract_content(value: Any) -> str:
    """Pull human-readable text out of a completion's prompt/completion JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("content") or value.get("text") or ""
    return str(value)


class ReadReportTool(Tool):
    """Read a single report (metadata, conversation, artifacts) owned by the user."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_report",
            description=(
                "RESEARCH: Read one of the current user's OWN reports by id — its "
                "metadata, attached data sources, artifact summary, and conversation "
                "(prompts and AI answers). Use search_reports first to find the "
                "report_id. Only the user's own reports can be read; any other report "
                "returns not found. Use this to recall what was asked or built in a "
                "previous report before continuing related work."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadReportInput.model_json_schema(),
            output_schema=ReadReportOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=20,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["report", "read"],
            observation_policy="on_trigger",
            allowed_modes=["chat", "deep"],
            examples=[
                {
                    "input": {"report_id": "<report-uuid>"},
                    "description": "Read a prior report's conversation and artifacts",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadReportInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadReportOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = ReadReportInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(type="tool.start", payload={"report_id": data.report_id})

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
            stmt = (
                select(Report)
                .where(Report.id == str(data.report_id))
                .where(Report.organization_id == str(organization.id))
                .where(Report.user_id == str(user.id))
                .options(
                    selectinload(Report.artifacts),
                    selectinload(Report.data_sources),
                    selectinload(Report.completions),
                )
            )
            report: Optional[Report] = (await db.execute(stmt)).scalars().first()
        except Exception as e:
            logger.exception(f"read_report query failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Read failed: {e}", "code": "READ_FAILED"},
            )
            return

        if not report:
            # Indistinguishable from "not yours" — by design, no leak.
            output = ReadReportOutput(
                success=False,
                report_id=str(data.report_id),
                error="not_found",
                message=f"No report owned by you with id {data.report_id}",
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": f"Report not found or not owned by you: {data.report_id}",
                        "error": {"type": "not_found", "message": output.message},
                    },
                },
            )
            return

        # Artifacts summary (most recent first)
        artifacts = sorted(
            report.artifacts or [],
            key=lambda a: a.created_at.timestamp() if a.created_at else 0,
            reverse=True,
        )
        artifact_items = [
            ReadReportArtifact(
                id=str(a.id),
                title=a.title,
                mode=a.mode,
                version=a.version,
            )
            for a in artifacts
        ]

        data_source_names = [ds.name for ds in (report.data_sources or []) if ds.name]

        # Conversation (oldest first), capped.
        conversation = []
        if data.include_conversation:
            completions = sorted(
                report.completions or [],
                key=lambda c: (c.turn_index or 0, c.created_at.timestamp() if c.created_at else 0),
            )
            for c in completions:
                # Hide internal webhook trigger rows.
                if getattr(c, "webhook_id", None) and c.role == "user":
                    continue
                raw = c.prompt if c.role == "user" else c.completion
                content = _extract_content(raw)
                if not content or not str(content).strip():
                    continue
                content = str(content)[:_MAX_CONTENT_CHARS]
                conversation.append(
                    ReadReportMessage(
                        role=c.role,
                        content=content,
                        created_at=str(c.created_at) if c.created_at else None,
                    )
                )
            if len(conversation) > _MAX_MESSAGES:
                # Keep the most recent messages.
                conversation = conversation[-_MAX_MESSAGES:]

        output = ReadReportOutput(
            success=True,
            report_id=str(report.id),
            title=report.title,
            slug=report.slug,
            status=report.status,
            mode=report.mode,
            created_at=str(report.created_at) if report.created_at else None,
            updated_at=str(report.updated_at) if report.updated_at else None,
            data_sources=data_source_names,
            artifacts=artifact_items,
            conversation=conversation,
            message=(
                f"Read report '{report.title or 'Untitled'}' "
                f"({len(artifact_items)} artifact(s), {len(conversation)} message(s))"
            ),
        )

        summary = (
            f"Read report '{report.title or 'Untitled'}' ({report.mode}) — "
            f"{len(artifact_items)} artifact(s), {len(conversation)} message(s)"
        )
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": {
                    "summary": summary,
                    "report_id": str(report.id),
                    "title": report.title,
                    "status": report.status,
                    "mode": report.mode,
                    "data_sources": data_source_names,
                    "artifacts": [a.model_dump() for a in artifact_items],
                    "conversation": [m.model_dump() for m in conversation],
                },
            },
        )
