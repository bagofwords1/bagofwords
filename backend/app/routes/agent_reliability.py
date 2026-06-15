"""Agent reliability automation API.

Per-agent settings (the autonomy dials), the reliability status, and the
automation-run audit log. Read endpoints gate on ``view``; writes and manual
triggers gate on ``manage`` of the data source — the same permission that
governs other agent configuration.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_resource_permission
from app.dependencies import get_async_db, get_current_organization
from app.models.agent_automation_run import AgentAutomationRun
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.user import User
from app.schemas.agent_automation_schema import AgentAutomationPolicy
from app.services.agent_reliability_service import AgentReliabilityService

router = APIRouter(tags=["agent_reliability"])
rel_service = AgentReliabilityService()


class AutomationSettingsResponse(BaseModel):
    data_source_id: str
    reliability_status: str
    publish_status: str
    # The (possibly partial) per-agent override actually stored on the agent.
    override: Dict[str, Any]
    # The org-wide default policy this agent inherits.
    org_defaults: Dict[str, Any]
    # The resolved/effective policy the orchestrator uses.
    effective: Dict[str, Any]


class AutomationRunSchema(BaseModel):
    id: str
    trigger: str
    status: str
    iterations: int
    build_id: Optional[str] = None
    test_run_ids: List[str] = []
    detail: Dict[str, Any] = {}
    requested_by_user_id: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    @classmethod
    def from_model(cls, r: AgentAutomationRun) -> "AutomationRunSchema":
        return cls(
            id=str(r.id),
            trigger=r.trigger,
            status=r.status,
            iterations=r.iterations or 0,
            build_id=str(r.build_id) if r.build_id else None,
            test_run_ids=r.test_run_ids_json or [],
            detail=r.detail_json or {},
            requested_by_user_id=str(r.requested_by_user_id) if r.requested_by_user_id else None,
            started_at=r.started_at.isoformat() if r.started_at else None,
            finished_at=r.finished_at.isoformat() if r.finished_at else None,
        )


async def _get_agent(db: AsyncSession, organization: Organization, data_source_id: str) -> DataSource:
    ds = await db.get(DataSource, str(data_source_id))
    if ds is None or str(ds.organization_id) != str(organization.id) or ds.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Agent (data source) not found")
    return ds


async def _build_settings_response(
    db: AsyncSession, organization: Organization, ds: DataSource
) -> "AutomationSettingsResponse":
    effective = await rel_service.resolve_policy(db, organization, ds)
    try:
        settings = await organization.get_settings(db)
        org_defaults = settings.get_config("agent_automation_defaults") or {}
        if hasattr(org_defaults, "value"):
            org_defaults = getattr(org_defaults, "value", {}) or {}
    except Exception:
        org_defaults = {}
    return AutomationSettingsResponse(
        data_source_id=str(ds.id),
        reliability_status=ds.reliability_status or "ok",
        publish_status=ds.publish_status or "published",
        override=ds.automation_settings or {},
        org_defaults=org_defaults if isinstance(org_defaults, dict) else {},
        effective=effective.model_dump(),
    )


@router.get("/data_sources/{data_source_id}/automation", response_model=AutomationSettingsResponse)
@requires_resource_permission('data_source', 'view')
async def get_automation_settings(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await _get_agent(db, organization, data_source_id)
    return await _build_settings_response(db, organization, ds)


@router.patch("/data_sources/{data_source_id}/automation", response_model=AutomationSettingsResponse)
@requires_resource_permission('data_source', 'manage')
async def update_automation_settings(
    data_source_id: str,
    payload: Dict[str, Any],
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Replace the per-agent automation override. Validates the merged result
    against the policy schema so a bad value is rejected up front rather than
    silently ignored by the orchestrator."""
    ds = await _get_agent(db, organization, data_source_id)

    # Validate: the override merged over defaults must form a valid policy.
    current = dict(ds.automation_settings or {})
    current.update({k: v for k, v in (payload or {}).items() if v is not None})
    # Drop keys explicitly set to null (means "inherit org default").
    for k, v in (payload or {}).items():
        if v is None:
            current.pop(k, None)
    try:
        AgentAutomationPolicy(**{**AgentAutomationPolicy().model_dump(), **current})
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid automation settings: {e}")

    ds.automation_settings = current or None
    db.add(ds)
    await db.commit()
    await db.refresh(ds)
    return await _build_settings_response(db, organization, ds)


@router.post("/data_sources/{data_source_id}/automation/run", response_model=AutomationRunSchema)
@requires_resource_permission('data_source', 'manage')
async def trigger_automation_run(
    data_source_id: str,
    wait: bool = Query(False, description="Run the loop inline and return the completed run instead of scheduling it in the background."),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Manually kick the reliability loop for this agent.

    Default: schedule in the background and return a placeholder so the UI can
    poll ``/automation/runs``. ``wait=true``: run inline and return the
    completed AgentAutomationRun (used by tests and callers that want the
    result synchronously)."""
    from app.models.agent_automation_run import TRIGGER_MANUAL
    ds = await _get_agent(db, organization, data_source_id)
    if wait:
        run = await rel_service.run_automation(
            db, organization, ds, TRIGGER_MANUAL,
            user=current_user, changed_hint="manual run",
        )
        return AutomationRunSchema.from_model(run)
    rel_service.schedule(
        organization_id=str(organization.id),
        data_source_id=str(ds.id),
        trigger=TRIGGER_MANUAL,
        changed_hint="manual run",
        user_id=str(current_user.id),
    )
    return AutomationRunSchema(
        id="pending", trigger=TRIGGER_MANUAL, status="running", iterations=0,
        detail={"reason": "scheduled"},
    )


@router.get("/data_sources/{data_source_id}/automation/runs", response_model=List[AutomationRunSchema])
@requires_resource_permission('data_source', 'view')
async def list_automation_runs(
    data_source_id: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await _get_agent(db, organization, data_source_id)
    rows = (await db.execute(
        select(AgentAutomationRun)
        .where(AgentAutomationRun.data_source_id == str(ds.id))
        .order_by(AgentAutomationRun.created_at.desc())
        .limit(limit)
    )).scalars().all()
    return [AutomationRunSchema.from_model(r) for r in rows]
