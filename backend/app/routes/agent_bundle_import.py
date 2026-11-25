"""Import an agent from an export bundle (the zip "Export agent" downloads).

Two calls on the same file, both multipart (``file`` + ``options`` JSON):

- ``POST /agents/import/preview`` — read-only: what the bundle contains, which
  connection each bundle connection maps to here, how many of its tables
  exist there, and which members will be left out.
- ``POST /agents/import`` — creates a NEW agent from the bundle.

Gated like ``POST /data_sources`` on existing connections: org-level
``create_data_source`` or per-connection ``create_data_sources``; the service
then requires ``create_data_sources`` on every mapped connection.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.ee.audit.service import audit_service
from app.errors import AppError, ErrorCode
from app.models.organization import Organization
from app.models.user import User
from app.schemas.agent_bundle_import_schema import (
    AgentBundleImportOptions,
    AgentBundleImportResult,
    AgentBundlePreview,
)
from app.services.agent_bundle_import_service import (
    MAX_BUNDLE_BYTES,
    AgentBundleImportService,
)

router = APIRouter(prefix="/agents", tags=["agents"])
service = AgentBundleImportService()


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read(MAX_BUNDLE_BYTES + 1)
    if len(data) > MAX_BUNDLE_BYTES:
        raise AppError.bad_request(ErrorCode.AGENT_IMPORT_INVALID_BUNDLE, "The bundle is too large.")
    return data


def _parse_options(raw: str) -> AgentBundleImportOptions:
    try:
        return AgentBundleImportOptions.model_validate(json.loads(raw or "{}"))
    except (ValueError, ValidationError) as e:
        raise AppError.bad_request(ErrorCode.VALIDATION, f"Invalid import options: {e}")


@router.post("/import/preview", response_model=AgentBundlePreview)
@requires_permission(["create_data_source", "create_data_sources"], resource_scoped=True)
async def preview_agent_import(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> AgentBundlePreview:
    return await service.preview(
        db, organization, current_user, await _read_upload(file), _parse_options(options)
    )


@router.post("/import", response_model=AgentBundleImportResult)
@requires_permission(["create_data_source", "create_data_sources"], resource_scoped=True)
async def import_agent(
    request: Request,
    file: UploadFile = File(...),
    options: str = Form("{}"),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> AgentBundleImportResult:
    result = await service.import_bundle(
        db, organization, current_user, await _read_upload(file), _parse_options(options)
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="agent.imported",
            user_id=current_user.id, resource_type="data_source",
            resource_id=result.data_source_id,
            details={
                "name": result.name,
                "instructions": result.instructions_created,
                "evals": result.evals_created,
                "queries": result.queries_created,
            },
            request=request,
        )
    except Exception:
        pass
    return result
