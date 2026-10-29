"""Schema/configuration authoring only; never edits user records."""

from pydantic import Field
from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolStartEvent, ToolEndEvent
from app.schemas.artifact_resource_schema import ResourceChange
from app.services.artifact_resource_service import ArtifactResources, fail


class ManageResourceInput(ResourceChange):
    artifact_id: str = Field(description="Stable parent resource_artifact_id returned by create/read_artifact.")


class ManageArtifactResources(Tool):
    @property
    def metadata(self):
        return ToolMetadata(
            name="manage_artifact_resources",
            category="action",
            description="Create, update or delete a collection schema, file resource or approved AI operation on an existing artifact. Permissions live on the definition and reference existing groups. This does not edit records or sharing. Read the artifact first; use expected_revision for update/delete and preserve existing data. For new artifacts declare resources in create_artifact.",
            input_schema=ManageResourceInput.model_json_schema(),
            timeout_seconds=30,
            max_retries=0,
            required_permissions=["update_reports"],
            tags=["artifact", "schema", "resources"],
            is_active=True,
        )

    @property
    def input_model(self):
        return ManageResourceInput

    async def run_stream(self, tool_input, runtime_ctx):
        data = ManageResourceInput.model_validate(tool_input)
        yield ToolStartEvent(type="tool.start", payload={})
        hub = runtime_ctx.get("context_hub")
        db = hub.db if hub else runtime_ctx.get("db")
        user = hub.user if hub else runtime_ctx.get("user")
        org = hub.organization if hub else runtime_ctx.get("organization")
        report = hub.report if hub else runtime_ctx.get("report")
        try:
            # Savepoint: a rejected change rolls back only itself. A session-wide
            # rollback would expire the agent's own loaded state and crash the turn.
            async with db.begin_nested():
                service = await ArtifactResources.open(db, data.artifact_id, user, org.id, manage=True)
                if report is None or str(service.report.id) != str(report.id):
                    fail("FORBIDDEN", "Resource is outside the current report", 403)
                result = await service.configure(
                    ResourceChange.model_validate(data.model_dump(exclude={"artifact_id"}))
                )
        except Exception as exc:
            from app.errors import AppError

            message = (
                exc.message
                if isinstance(exc, AppError)
                else "Resource change could not be applied; refresh the definition and check for conflicts."
            )
            yield ToolEndEvent(
                type="tool.end", payload={"output": {"success": False, "committed": False}, "observation": {"success": False, "committed": False, "action": data.action, "resource_artifact_id": data.artifact_id, "name": data.resource or (data.definition.name if data.definition else None), "error": message, "error_code": getattr(exc, "error_code", "ARTIFACT_RESOURCE_ERROR")}}
            )
            return
        await db.commit()
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": result,
                "observation": {
                    "summary": f"{dict(create='Created', update='Updated', delete='Deleted')[data.action]} {result.get('kind', 'resource')} {result.get('name', data.resource or '')} at revision {result['revision']}; change committed.",
                    **result,
                    "next_step": "Read the current definition before further changes. Update the artifact UI if needed; sharing is unchanged.",
                },
            },
        )
