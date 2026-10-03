"""MCP resource schema authoring over the same artifact policy services."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.ai.tools.mcp.base import MCPTool
from app.schemas.artifact_resource_schema import ResourceChange, ResourceDefinition
from app.services.artifact_resource_service import ArtifactResources, fail


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    artifact_id: str = Field(description="Stable resource_artifact_id, not a UI version ID.")
    action: Literal["read", "create", "update", "delete"] = "read"
    resource: str | None = None
    definition: ResourceDefinition | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)


async def scoped_service(db, user, organization, data, manage=False):
    service = await ArtifactResources.open(db, data.artifact_id, user, str(organization.id), manage=manage)
    if str(service.report.id) != data.report_id:
        fail("NOT_FOUND", "Artifact not found in this report", 404)
    return service


class ManageArtifactResourcesMCPTool(MCPTool):
    name = "manage_artifact_resources"
    description = "Read, create, update or delete artifact collection schemas, file resources and AI operation configuration. Never edits user records. Read first for revisions; mutations require a stable idempotency key. Permissions reuse existing identities/groups and do not change sharing."

    @property
    def is_available(self):
        return True

    @property
    def input_schema(self):
        return ResourceInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        data = ResourceInput.model_validate(args)
        service = await scoped_service(db, user, organization, data, manage=data.action != "read")
        result = {}
        if data.action != "read":
            change = ResourceChange.model_validate(data.model_dump(exclude={"report_id", "artifact_id"}))
            try:
                result = await service.configure(change)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "success": True,
            "resource_artifact_id": data.artifact_id,
            **result,
            **({"resources": await service.definitions()} if data.action == "read" else {}),
        }
