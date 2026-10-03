"""MCP schema/publication authoring over the same artifact policy services."""

import os
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from app.ai.tools.mcp.base import MCPTool
from app.models.artifact_resource import ArtifactPublication
from app.schemas.artifact_resource_schema import ResourceChange, ResourceDefinition
from app.services.artifact_resource_service import ArtifactResources, fail
from app.services.artifact_publication import publish


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    artifact_id: str = Field(description="Stable resource_artifact_id, not a UI version ID.")
    action: Literal["read", "create", "update", "delete"] = "read"
    resource: str | None = None
    definition: ResourceDefinition | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)


class PublicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    artifact_id: str = Field(description="Stable resource_artifact_id.")
    version_id: str
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=100)


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
        publication = await db.scalar(
            select(ArtifactPublication).where(ArtifactPublication.artifact_id == data.artifact_id)
        )
        return {
            "success": True,
            "resource_artifact_id": data.artifact_id,
            **result,
            "resources": await service.definitions(),
            "publication": {
                "version_id": publication.version_id if publication else None,
                "revision": publication.revision if publication else 0,
            },
        }


class PublishArtifactMCPTool(MCPTool):
    name = "publish_artifact"
    description = "Select a completed artifact UI version for existing shared-site routes. Read resource definitions first for the publication revision. Does not change sharing or data permissions."

    @property
    def is_available(self):
        return True

    @property
    def input_schema(self):
        return PublicationInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        data = PublicationInput.model_validate(args)
        service = await scoped_service(db, user, organization, data, manage=True)
        if os.environ.get("BOW_ARTIFACT_RESOURCES_READ_ONLY") == "true":
            fail("UNAVAILABLE", "Artifact writes are temporarily disabled", 503)
        try:
            result = await publish(service, data.version_id, data.expected_revision, data.idempotency_key)
            await db.commit()
            return {"success": True, **result}
        except BaseException:
            await db.rollback()
            raise
