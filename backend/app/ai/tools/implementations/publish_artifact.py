"""Select a completed UI version; existing artifact sharing remains authoritative."""

import os
from pydantic import BaseModel, Field, ConfigDict
from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolStartEvent, ToolEndEvent
from app.services.artifact_resource_service import ArtifactResources, fail
from app.services.artifact_publication import publish


class PublishArtifactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    version_id: str
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=100)


class PublishArtifactTool(Tool):
    @property
    def metadata(self):
        return ToolMetadata(
            name="publish_artifact",
            category="action",
            description="Opt into explicit UI publication or select another completed version for shared viewers. Read artifact first. expected_revision is the publication revision (0 before first publication). Does not change sharing, data or resource permissions.",
            input_schema=PublishArtifactInput.model_json_schema(),
            timeout_seconds=30,
            max_retries=0,
            required_permissions=["update_reports"],
            tags=["artifact", "resources"],
            is_active=os.environ.get("BOW_ARTIFACT_RESOURCES_ENABLED") == "true",
        )

    @property
    def input_model(self):
        return PublishArtifactInput

    async def run_stream(self, tool_input, runtime_ctx):
        data = PublishArtifactInput.model_validate(tool_input)
        yield ToolStartEvent(type="tool.start", payload={})
        hub = runtime_ctx.get("context_hub")
        db = hub.db if hub else runtime_ctx.get("db")
        user = hub.user if hub else runtime_ctx.get("user")
        report = hub.report if hub else runtime_ctx.get("report")
        org = hub.organization if hub else runtime_ctx.get("organization")
        try:
            if os.environ.get("BOW_ARTIFACT_RESOURCES_ENABLED") != "true":
                fail("UNAVAILABLE", "Artifact resources are not enabled", 404)
            # Savepoint: a rejected publication must not expire the agent's state.
            async with db.begin_nested():
                service = await ArtifactResources.open(db, data.artifact_id, user, org.id, manage=True)
                if report is None or str(report.id) != str(service.report.id):
                    fail("FORBIDDEN", "Artifact is outside this report", 403)
                result = await publish(service, data.version_id, data.expected_revision, data.idempotency_key)
        except Exception as exc:
            from app.errors import AppError

            message = (
                exc.message
                if isinstance(exc, AppError)
                else "Publication failed; refresh the artifact and check for conflicts."
            )
            yield ToolEndEvent(
                type="tool.end", payload={"output": {"success": False}, "observation": {"error": message}}
            )
            return
        await db.commit()
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": result,
                "observation": {"summary": "Shared artifact UI version selected", "publication": result},
            },
        )
