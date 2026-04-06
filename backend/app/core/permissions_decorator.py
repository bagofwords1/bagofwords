import logging as _logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
from functools import wraps
from inspect import signature
from app.models.membership import Membership, ROLES_PERMISSIONS
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
    @requires_permission("create_instructions", resource_scoped=True)  # Defers to check_resource_permissions in route
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
                # resource_scoped: defer to check_resource_permissions() in the route body
                if resource_scoped:
                    return await func(*args, **kwargs)
                await _audit_access_denied(db, user, organization, permission, func.__name__)
                raise HTTPException(status_code=403, detail="Permission denied")

            return await func(*args, **kwargs)
        return wrapper
    return decorator


def requires_data_source_access(permission, allow_public=False, membership_required=False):
    """
    Data source specific permission decorator that checks:
    1. User has sufficient role-based permission in the organization
    2. User belongs to the organization 
    3. Data source belongs to organization
    4. If allow_public=True, allows access to public data sources
    5. If membership_required=True, requires explicit membership for non-public data sources
    
    Usage:
    @requires_data_source_access("view_data_source", allow_public=True)  # Can view public or member data sources
    @requires_data_source_access("update_data_source", membership_required=True)  # Must be member to edit
    @requires_data_source_access("delete_data_source")  # Admin permission required
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
            organization = all_args.get('organization')
            db = all_args.get('db')
            data_source_id = all_args.get('data_source_id')

            if not all([user, organization, db]):
                raise HTTPException(status_code=400, detail="Missing required parameters")

            if not user.is_verified and settings.bow_config.features.verify_emails:
                raise HTTPException(status_code=403, detail="User is not verified")

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

            # Check role-based permission via RBAC resolver
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            if not resolved.has_org_permission(permission):
                await _audit_access_denied(db, user, organization, permission, func.__name__)
                raise HTTPException(status_code=403, detail="Permission denied")

            # If data_source_id is provided, check data source specific access
            if data_source_id:
                from app.models.data_source import DataSource

                stmt = select(DataSource).where(
                    DataSource.id == data_source_id,
                    DataSource.organization_id == organization.id
                )
                result = await db.execute(stmt)
                data_source = result.scalar_one_or_none()

                if not data_source:
                    raise HTTPException(status_code=404, detail="Data source not found")

                # Check data source access rules
                has_access = False

                # full_admin_access or manage-level org permissions bypass resource checks
                is_admin = (
                    FULL_ADMIN in resolved.org_permissions
                    or resolved.has_org_permission("update_data_source")
                )

                # If data source is public and allow_public flag is set
                if allow_public and data_source.is_public:
                    has_access = True

                # If user is an org admin, they can access all data sources
                elif is_admin:
                    has_access = True

                # If data source is private, check resource grants
                elif allow_public and not data_source.is_public:
                    has_access = resolved.has_resource_membership("data_source", str(data_source_id))

                # If membership is explicitly required, check resource grants
                elif membership_required:
                    has_access = resolved.has_resource_membership("data_source", str(data_source_id))

                # No access control flags - allow based on role permission only (already checked above)
                else:
                    has_access = True

                if not has_access:
                    await _audit_access_denied(db, user, organization, permission, func.__name__)
                    raise HTTPException(status_code=403, detail="Access denied to this data source")

            return await func(*args, **kwargs)
        return wrapper
    return decorator



def requires_resource_permission(resource_type: str, permission: str):
    """
    Decorator for resource-level permission checks (data sources, connections).

    License-aware logic:
    - Enterprise (custom_roles feature): checks granular permission from resource_grants
    - Non-enterprise: binary membership check; org role determines read vs write capability

    Usage:
    @requires_resource_permission("data_source", "query")
    @requires_resource_permission("connection", "manage")
    """
    # Map resource_type to the route parameter name
    _param_map = {
        "data_source": "data_source_id",
        "connection": "connection_id",
        "report": "report_id",
    }

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

            # Check org membership
            stmt = select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == organization.id
            )
            result = await db.execute(stmt)
            membership = result.scalar_one_or_none()
            if not membership:
                await _audit_access_denied(db, user, organization, permission, func.__name__)
                raise HTTPException(status_code=403, detail="User is not a member of this organization")

            # Resolve permissions
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))

            # full_admin_access bypasses everything
            if FULL_ADMIN in resolved.org_permissions:
                return await func(*args, **kwargs)

            # Get resource_id from route params
            param_name = _param_map.get(resource_type)
            resource_id = all_args.get(param_name) if param_name else None

            if not resource_id:
                # No resource_id in route — fall back to org-level check
                return await func(*args, **kwargs)

            # Check if enterprise (granular resource permissions)
            from app.ee.license import has_feature
            if has_feature("custom_roles"):
                # Enterprise: check granular permission from resource_grants
                if not resolved.has_resource_permission(resource_type, str(resource_id), permission):
                    # Also check if resource is public (for data sources)
                    if resource_type == "data_source":
                        from app.models.data_source import DataSource
                        ds_stmt = select(DataSource.is_public).where(
                            DataSource.id == resource_id,
                            DataSource.organization_id == organization.id,
                        )
                        ds_result = await db.execute(ds_stmt)
                        is_public = ds_result.scalar_one_or_none()
                        if is_public and permission not in _WRITE_RESOURCE_PERMISSIONS:
                            return await func(*args, **kwargs)

                    await _audit_access_denied(db, user, organization, permission, func.__name__)
                    raise HTTPException(status_code=403, detail="Access denied to this resource")
            else:
                # Non-enterprise: binary membership check
                # Public data sources are accessible to any org member for read ops
                if resource_type == "data_source":
                    from app.models.data_source import DataSource
                    ds_stmt = select(DataSource.is_public).where(
                        DataSource.id == resource_id,
                        DataSource.organization_id == organization.id,
                    )
                    ds_result = await db.execute(ds_stmt)
                    is_public = ds_result.scalar_one_or_none()
                    if is_public and permission not in _WRITE_RESOURCE_PERMISSIONS:
                        return await func(*args, **kwargs)

                # Private resource: must have membership
                has_membership = resolved.has_resource_membership(resource_type, str(resource_id))
                if not has_membership:
                    await _audit_access_denied(db, user, organization, permission, func.__name__)
                    raise HTTPException(status_code=403, detail="Access denied to this resource")

                # For write permissions on non-enterprise, require admin org role
                if permission in _WRITE_RESOURCE_PERMISSIONS:
                    if not resolved.has_org_permission("update_data_source"):
                        await _audit_access_denied(db, user, organization, permission, func.__name__)
                        raise HTTPException(status_code=403, detail="Admin access required for this operation")

            return await func(*args, **kwargs)
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

    Two-tier OR logic:
    - If user has the org-level permission → allowed on ALL resources (wildcard)
    - Otherwise, checks per-resource grants for each resource ID

    Raises HTTPException 403 if the user lacks the given permission on ANY
    of the listed resources.

    License-aware:
    - Enterprise (custom_roles): checks granular resource_grant permissions
    - Non-enterprise: binary membership check; write ops require admin org role
    """
    if not resource_ids:
        return

    resolved = await resolve_permissions(db, user_id, org_id)

    # full_admin_access bypasses everything
    if FULL_ADMIN in resolved.org_permissions:
        return

    # Org-level permission grants blanket access to all resources
    if resolved.has_org_permission(permission):
        return

    from app.ee.license import has_feature
    enterprise = has_feature("custom_roles")

    for rid in resource_ids:
        if enterprise:
            if not resolved.has_resource_permission(resource_type, str(rid), permission):
                raise HTTPException(
                    status_code=403,
                    detail=f"Access denied to {resource_type} {rid} for '{permission}'",
                )
        else:
            # Non-enterprise: check resource membership + specific permission if grant exists
            if not resolved.has_resource_membership(resource_type, str(rid)):
                raise HTTPException(
                    status_code=403,
                    detail=f"Access denied to {resource_type} {rid}",
                )
            # If the grant exists but doesn't include the required permission, deny
            key = (resource_type, str(rid))
            granted = resolved.resource_permissions.get(key, set())
            if granted and permission not in granted:
                raise HTTPException(
                    status_code=403,
                    detail=f"Access denied to {resource_type} {rid} for '{permission}'",
                )