import logging as _logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
from functools import wraps
from inspect import signature
from app.models.membership import Membership
from app.models.instruction import Instruction
from app.settings.config import settings
from app.core.permission_resolver import resolve_permissions, FULL_ADMIN

_perm_logger = _logging.getLogger(__name__)


async def _audit_access_denied(db, user, organization, permission: str, endpoint: str) -> None:
    """Fire-and-forget audit log for permission denials."""
    try:
        from app.ee.audit.service import audit_service
        await audit_service.log(
            db=db,
            organization_id=str(organization.id) if organization else None,
            action="access.denied",
            user_id=str(user.id) if user else None,
            resource_type="permission",
            details={"permission": permission, "endpoint": endpoint},
        )
    except Exception:
        _perm_logger.debug("_audit_access_denied failed", exc_info=True)


def requires_permission(permission, model=None, owner_only=False, allow_public=False, resource_scoped=False):
    """
    Enhanced decorator that checks:
    1. User has sufficient role-based permission
    2. User belongs to the organization
    3. If model is provided, checks if object belongs to organization
    4. If owner_only=True, checks if user is the owner of the object
    5. If allow_public=True, allows access to published reports even for non-owners
    6. If resource_scoped=True, skips denial when user lacks org-level permission —
       the route body must call check_resource_permissions() to enforce per-resource access

    Usage:
    @requires_permission("delete_reports", model=Report, owner_only=True)  # Only owner can delete
    @requires_permission("view_reports", model=Report, owner_only=True, allow_public=True)  # Owner or public
    @requires_permission("manage_instructions", resource_scoped=True)  # Defers to check_resource_permissions in route
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract arguments
            sig = signature(func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()
            all_args = bound_args.arguments

            user = all_args.get('current_user')

            if not user.is_verified and settings.bow_config.features.verify_emails:
                raise HTTPException(status_code=403, detail="User is not verified")

            organization = all_args.get('organization')
            db = all_args.get('db')
            report_id = all_args.get('report_id')  # For routes with object_id parameter
            completion_id = all_args.get('completion_id')  # For routes with object_id parameter
            data_source_id = all_args.get('data_source_id')  # For routes with object_id parameter
            widget_id = all_args.get('widget_id')  # For routes with object_id parameter
            memory_id = all_args.get('memory_id')  # For routes with object_id parameter
            instruction_id = all_args.get('instruction_id') 

            object_id = report_id or completion_id or data_source_id or widget_id or memory_id or instruction_id
        

            if not all([user, organization, db]):
                raise HTTPException(status_code=400, detail="Missing required parameters")

            # Check user membership and role in organization
            stmt = select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == organization.id
            )
            result = await db.execute(stmt)
            membership = result.scalar_one_or_none()

            if not membership:
                await _audit_access_denied(db, user, organization, permission, func.__name__)
                raise HTTPException(status_code=403, detail="User is not a member of this organization")

            # If model is provided and object_id exists and is not None and is a valid UUID-like string, verify object belongs to organization
            obj = None
            if model and object_id is not None:
                stmt = select(model).where(
                    model.id == object_id,
                    model.organization_id == organization.id
                )
                result = await db.execute(stmt)
                obj = result.scalar_one_or_none()
                
                if not obj:
                    raise HTTPException(status_code=404, detail="Object not found or access denied")
                
                # Check ownership if required
                if owner_only:
                    # Check if object has user_id field (for ownership)
                    if hasattr(obj, 'user_id'):
                        is_owner = obj.user_id == user.id
                        
                        # If allow_public is True and it's a Report with published status, allow access
                        if allow_public and hasattr(obj, 'status') and obj.status == 'published':
                            pass  # Allow access to published reports
                        elif not is_owner:
                            await _audit_access_denied(db, user, organization, permission, func.__name__)
                            raise HTTPException(status_code=403, detail="Only the owner can perform this action")
                    else:
                        raise HTTPException(status_code=500, detail="Object does not support ownership checks")

            # Check role-based permission via RBAC resolver
            # `permission` may be a single string or a list/tuple (ANY-of semantics)
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            if isinstance(permission, (list, tuple, set)):
                has_role_permission = any(resolved.has_org_permission(p) for p in permission)
            else:
                has_role_permission = resolved.has_org_permission(permission)
            if not has_role_permission:
                # Special owner allowance: Instruction owner may modify/delete when not published
                if isinstance(obj, Instruction):
                    is_owner = obj and getattr(obj, 'user_id', None) == user.id
                    not_approved = obj and getattr(obj, 'global_status', None) != 'approved'
                    is_ai_orphan = (getattr(obj, 'user_id', None) is None) and (getattr(obj, 'ai_source', None) is not None)
                    if not_approved and (is_owner or is_ai_orphan):
                        # allow without role permission
                        return await func(*args, **kwargs)
                # resource_scoped: the user lacks org-level permission, but may
                # hold a per-resource grant. Verify they have the permission on
                # at least one resource before deferring the specific-resource
                # check to the route body. This blocks users who have zero
                # grants from creating resources with empty data_source_ids.
                if resource_scoped:
                    perm_to_check = permission if isinstance(permission, str) else list(permission)[0]
                    has_any_resource = any(
                        perm_to_check in perms
                        for perms in resolved.resource_permissions.values()
                    )
                    if has_any_resource:
                        return await func(*args, **kwargs)
                await _audit_access_denied(db, user, organization, permission, func.__name__)
                raise HTTPException(status_code=403, detail="Permission denied")

            return await func(*args, **kwargs)
        return wrapper
    return decorator



# Map resource_type to the route parameter name holding the resource ID.
# Extend when new resource types land (post-MVP: connection, report).
_RESOURCE_PARAM_MAP = {
    "data_source": "data_source_id",
}


def requires_resource_permission(resource_type: str, permission: str):
    """
    Decorator for resource-level permission checks.

    MVP logic (two-tier OR, mirrors check_resource_permissions):
    1. full_admin_access wildcard → allow
    2. Org-level `permission` held → allow (blanket)
    3. Per-resource grant for (resource_type, resource_id, permission) → allow
    4. Otherwise → 403

    Resource ID is pulled from the route path parameter named in _RESOURCE_PARAM_MAP.
    If the resource_type has no mapping, raises at decoration time.

    Usage:
        @requires_resource_permission("data_source", "view")
        @requires_resource_permission("data_source", "manage")
    """
    if resource_type not in _RESOURCE_PARAM_MAP:
        raise ValueError(
            f"requires_resource_permission: unknown resource_type {resource_type!r}. "
            f"Add it to _RESOURCE_PARAM_MAP."
        )

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            sig = signature(func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()
            all_args = bound_args.arguments

            user = all_args.get('current_user')
            organization = all_args.get('organization')
            db = all_args.get('db')

            if not all([user, organization, db]):
                raise HTTPException(status_code=400, detail="Missing required parameters")

            if not user.is_verified and settings.bow_config.features.verify_emails:
                raise HTTPException(status_code=403, detail="User is not verified")

            # Org membership check
            stmt = select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == organization.id,
            )
            result = await db.execute(stmt)
            membership = result.scalar_one_or_none()
            if not membership:
                await _audit_access_denied(db, user, organization, permission, func.__name__)
                raise HTTPException(status_code=403, detail="User is not a member of this organization")

            resolved = await resolve_permissions(db, str(user.id), str(organization.id))

            # Tier 1: full_admin_access wildcard
            if FULL_ADMIN in resolved.org_permissions:
                return await func(*args, **kwargs)

            # Tier 2: org-level permission grants blanket access
            if resolved.has_org_permission(permission):
                return await func(*args, **kwargs)

            # Tier 3: per-resource grant
            param_name = _RESOURCE_PARAM_MAP[resource_type]
            resource_id = all_args.get(param_name)
            if not resource_id:
                await _audit_access_denied(db, user, organization, permission, func.__name__)
                raise HTTPException(
                    status_code=400,
                    detail=f"Missing {param_name} in route for resource permission check",
                )

            if resolved.has_resource_permission(resource_type, str(resource_id), permission):
                return await func(*args, **kwargs)

            # Tier 4: public data sources are readable by any org member
            if resource_type == "data_source" and permission in ("view", "view_schema"):
                from app.models.data_source import DataSource
                ds_row = await db.execute(
                    select(DataSource).where(DataSource.id == str(resource_id))
                )
                ds_obj = ds_row.scalar_one_or_none()
                if ds_obj is not None and getattr(ds_obj, "is_public", False) and str(ds_obj.organization_id) == str(organization.id):
                    return await func(*args, **kwargs)

            await _audit_access_denied(db, user, organization, permission, func.__name__)
            raise HTTPException(status_code=403, detail="Access denied to this resource")

        return wrapper
    return decorator


async def check_resource_permissions(
    db: AsyncSession,
    user_id: str,
    org_id: str,
    resource_type: str,
    resource_ids: list[str],
    permission: str,
) -> None:
    """
    Imperative resource-permission check for cases where resource IDs come
    from the request body rather than route params.

    Three-tier OR logic (mirrors @requires_resource_permission):
    1. full_admin_access wildcard → allow
    2. Org-level `permission` held → allow on ALL resources (blanket)
    3. Per-resource grant must include `permission` for every resource_id

    Admin bypasses (e.g. `manage_instructions` ⇒ all `manage_instructions`)
    are handled inside `resolved.has_resource_permission` via the
    ORG_PERM_IMPLIES_RESOURCE map in permission_resolver.

    Raises HTTPException 403 if the user lacks the permission on ANY of
    the listed resources. Fails the whole batch on the first miss.
    """
    if not resource_ids:
        return

    resolved = await resolve_permissions(db, user_id, org_id)

    # Tier 1: full_admin_access wildcard
    if FULL_ADMIN in resolved.org_permissions:
        return

    # Tier 2: org-level permission grants blanket access
    if resolved.has_org_permission(permission):
        return

    # Tier 3: per-resource grant check (admin implications handled in resolver)
    for rid in resource_ids:
        if not resolved.has_resource_permission(resource_type, str(rid), permission):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied to {resource_type} {rid} for '{permission}'",
            )