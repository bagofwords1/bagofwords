from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.schemas.prompt_schema import (
    PromptCreate, PromptUpdate, PromptResponse, PromptListResponse,
    PromptRunRequest, PromptRunResponse, PromptRunForRequest, PromptRunForResponse,
)
from app.services.prompt_service import prompt_service
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.dependencies import get_async_db, get_current_organization
from app.ee.audit.service import audit_service

router = APIRouter(tags=["prompts"])

# Write authorization: prompts have no org-level permission string, and the
# required check depends on the request BODY (scope + data_source_ids), so —
# like the instruction routes' check_resource_permissions pattern — the policy
# is invoked imperatively in the route body via prompt_service.authorize_write:
#   private → author must be able to SEE every referenced data source (or none)
#   agent   → `manage` grant on every referenced agent
#   global  → full_admin only
# The service re-runs the same policy inside create/update as a backstop for
# non-HTTP callers (AI training tools call the service directly).


@router.get("/prompts", response_model=PromptListResponse)
async def list_prompts(
    category: Optional[str] = None,
    starters_only: bool = False,
    data_source_id: Optional[str] = None,
    created_by: Optional[str] = None,
    scope: Optional[str] = None,
    search: Optional[str] = None,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await prompt_service.list_prompts(
        db, current_user, organization,
        category=category, starters_only=starters_only, data_source_id=data_source_id,
        created_by=created_by, scope=scope, search=search,
    )


@router.get("/prompts/{prompt_id}", response_model=PromptResponse)
async def get_prompt(
    prompt_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await prompt_service.get_prompt_response(db, prompt_id, current_user, organization)


@router.post("/prompts", response_model=PromptResponse)
async def create_prompt(
    data: PromptCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await prompt_service.authorize_write(
        db, current_user, organization,
        scope=data.scope, ds_ids=data.data_source_ids, endpoint='prompt.create',
    )
    p = await prompt_service.create_prompt(db, data, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="prompt.created",
            user_id=current_user.id, resource_type="prompt", resource_id=p.id,
            details={"title": p.title, "scope": p.scope}, request=request,
        )
    except Exception:
        pass
    return await prompt_service.get_prompt_response(db, p.id, current_user, organization)


@router.put("/prompts/{prompt_id}", response_model=PromptResponse)
async def update_prompt(
    prompt_id: str,
    data: PromptUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await prompt_service.authorize_update(db, prompt_id, data, current_user, organization)
    p = await prompt_service.update_prompt(db, prompt_id, data, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="prompt.updated",
            user_id=current_user.id, resource_type="prompt", resource_id=p.id,
            details={"title": p.title, "scope": p.scope,
                     "fields": list(data.dict(exclude_unset=True).keys())},
            request=request,
        )
    except Exception:
        pass
    return await prompt_service.get_prompt_response(db, p.id, current_user, organization)


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(
    prompt_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await prompt_service.delete_prompt(db, prompt_id, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="prompt.deleted",
            user_id=current_user.id, resource_type="prompt", resource_id=prompt_id,
            request=request,
        )
    except Exception:
        pass
    return {"ok": True}


@router.post("/prompts/{prompt_id}/run", response_model=PromptRunResponse)
async def run_prompt(
    prompt_id: str,
    request: Request,
    data: PromptRunRequest = Body(default=PromptRunRequest()),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Run a prompt as the caller → a new report the caller owns. Returns
    { report_id } so the client can navigate to the streaming report."""
    result = await prompt_service.run_prompt(
        db, prompt_id, current_user, organization, parameters=data.parameters,
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="prompt.run",
            user_id=current_user.id, resource_type="prompt", resource_id=prompt_id,
            details={"report_id": result.get("report_id")}, request=request,
        )
    except Exception:
        pass
    return result


@router.post("/prompts/{prompt_id}/run-for", response_model=PromptRunForResponse)
async def run_prompt_for(
    prompt_id: str,
    data: PromptRunForRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Admin run-on-behalf. Fans out to eligible targets, each getting a private
    report. Returns { ran, skipped, skipped_user_ids }."""
    return await prompt_service.run_prompt_for(
        db, prompt_id, current_user, organization,
        principal_type=data.principal_type, user_ids=data.user_ids,
        group_id=data.group_id, parameters=data.parameters,
    )
