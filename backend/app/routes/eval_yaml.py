"""HTTP routes for declarative Eval YAML apply / export.

Externally surfaced under ``/evals/*``. Internally these still map onto
``TestSuite`` / ``SuiteYaml`` (rename is product-side only). Wraps
``TestSuiteService.import_yaml`` with the same ``ApplyResult`` envelope
used by agent YAML so callers get a uniform response shape.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permission_resolver import FULL_ADMIN, resolve_permissions
from app.dependencies import get_async_db, get_current_organization
from app.models.eval import TestSuite
from app.models.organization import Organization
from app.models.user import User
from app.schemas.agent_manifest_schema import (
    ApplyError,
    ApplyErrorCode,
    ApplyResult,
    ApplyStatus,
)
from app.services.test_suite_service import TestSuiteService


router = APIRouter(prefix="/evals", tags=["evals"])
suite_service = TestSuiteService()


async def _require_manage_evals(
    db: AsyncSession, organization: Organization, user: User
) -> ApplyResult | None:
    """Return an error ApplyResult if the user can't manage evals, else None."""
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    if (
        FULL_ADMIN in resolved.org_permissions
        or resolved.has_org_permission("manage_evals")
    ):
        return None
    return ApplyResult(
        status=ApplyStatus.ERROR,
        errors=[
            ApplyError(
                loc=[],
                code=ApplyErrorCode.PERMISSION_DENIED,
                message="You do not have permission to manage evals.",
            )
        ],
    )


@router.post("/apply", response_model=ApplyResult)
async def apply_eval(
    request: Request,
    dry_run: bool = Query(False),
    strategy: str = Query("upsert", pattern="^(upsert|replace)$"),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> ApplyResult:
    perm_error = await _require_manage_evals(db, organization, current_user)
    if perm_error is not None:
        return perm_error
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

    if dry_run:
        # No dry-run on suite import yet; surface as a 200 with a note.
        return ApplyResult(
            status=ApplyStatus.DRY_RUN,
            warnings=[],
            diff={"note": "dry_run is not yet implemented for evals"},
        )

    # Determine create vs update by suite name presence in the YAML.
    # We do this before calling the service so we can return the proper
    # status code on the result envelope.
    import yaml as _yaml

    try:
        raw = _yaml.safe_load(yaml_text) or {}
    except _yaml.YAMLError as e:
        return ApplyResult(
            status=ApplyStatus.ERROR,
            errors=[
                ApplyError(
                    loc=[],
                    code=ApplyErrorCode.YAML_PARSE_ERROR,
                    message=str(e),
                )
            ],
        )
    suite_name = raw.get("name") if isinstance(raw, dict) else None
    existed_before = False
    if suite_name:
        q = await db.execute(
            select(TestSuite).where(
                TestSuite.organization_id == str(organization.id),
                TestSuite.name == suite_name,
            )
        )
        existed_before = q.scalar_one_or_none() is not None

    try:
        result = await suite_service.import_yaml(
            db, str(organization.id), current_user, yaml_text, strategy=strategy,
        )
    except HTTPException as e:
        return ApplyResult(
            status=ApplyStatus.ERROR,
            errors=[
                ApplyError(
                    loc=[],
                    code=ApplyErrorCode.SCHEMA_INVALID,
                    message=str(e.detail),
                )
            ],
        )

    return ApplyResult(
        status=ApplyStatus.UPDATED if existed_before else ApplyStatus.CREATED,
        id=result.get("suite_id"),
        name=result.get("suite_name"),
        diff={
            "cases_by_name": result.get("cases_by_name", {}),
            "removed_case_names": result.get("removed_case_names", []),
        },
    )


@router.get("/{name}.yaml", response_class=PlainTextResponse)
async def export_eval_yaml(
    name: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> str:
    # Look up suite by name within org → delegate to existing export_yaml
    q = await db.execute(
        select(TestSuite).where(
            TestSuite.organization_id == str(organization.id),
            TestSuite.name == name,
        )
    )
    suite = q.scalar_one_or_none()
    if suite is None:
        raise HTTPException(status_code=404, detail=f"Eval '{name}' not found")
    return await suite_service.export_yaml(
        db, str(organization.id), current_user, str(suite.id)
    )


@router.get("", response_model=List[Dict[str, Any]])
async def list_evals(
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
) -> List[Dict[str, Any]]:
    suites = await suite_service.list_suites(
        db, str(organization.id), current_user, page=1, limit=1000
    )
    return [
        {"id": str(s.id), "name": s.name, "description": s.description}
        for s in suites
    ]
