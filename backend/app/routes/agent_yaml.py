"""HTTP routes for declarative Agent YAML apply / export.

Single endpoint per operation. ``apply`` is idempotent on
``(organization_id, manifest.name)``: it creates the resource if absent,
reconciles it otherwise, and returns an ``ApplyResult`` with structured
errors instead of raising HTTP exceptions on validation failures.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.schemas.agent_manifest_schema import ApplyResult, ApplyStatus, ApplyError, ApplyErrorCode
from app.services.agent_yaml_service import AgentYamlService
from app.ee.audit.service import audit_service


router = APIRouter(prefix="/agents", tags=["agents"])
service = AgentYamlService()


@router.post("/apply", response_model=ApplyResult)
async def apply_agent(
    request: Request,
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    user: User = Depends(current_user),
) -> ApplyResult:
    body = await request.body()
    if not body:
        return ApplyResult(
            status=ApplyStatus.ERROR,
            errors=[
                ApplyError(
                    loc=[],
                    code=ApplyErrorCode.YAML_PARSE_ERROR,
                    message="Empty YAML body.",
                )
            ],
        )
    try:
        yaml_text = body.decode("utf-8")
    except UnicodeDecodeError:
        return ApplyResult(
            status=ApplyStatus.ERROR,
            errors=[
                ApplyError(
                    loc=[],
                    code=ApplyErrorCode.YAML_PARSE_ERROR,
                    message="YAML body must be UTF-8.",
                )
            ],
        )
    result = await service.apply(
        db, organization, user, yaml_text, dry_run=dry_run
    )
    if not dry_run and getattr(result, "status", None) not in (ApplyStatus.ERROR, ApplyStatus.DRY_RUN):
        try:
            await audit_service.log(
                db=db, organization_id=organization.id, action="agent.applied",
                user_id=user.id, resource_type="agent",
                resource_id=str(getattr(result, "id", None) or ""),
                details={"name": getattr(result, "name", None),
                         "status": str(getattr(result, "status", None))},
                request=request,
            )
        except Exception:
            pass
    return result


@router.get("/{name}.yaml", response_class=PlainTextResponse)
async def get_agent_yaml(
    name: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    user: User = Depends(current_user),
) -> str:
    return await service.export(db, organization, user, name)


@router.get("", response_model=List[Dict[str, Any]])
async def list_agent_manifests(
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    user: User = Depends(current_user),
) -> List[Dict[str, Any]]:
    return await service.list_agents(db, organization, user)
