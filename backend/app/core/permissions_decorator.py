from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
from functools import wraps
from inspect import signature
from app.models.membership import Membership, ROLES_PERMISSIONS
from app.settings.config import settings


def requires_permission(permission, model=None):
    """
    Enhanced decorator that checks:
    1. User has sufficient role-based permission
    2. User belongs to the organization
    3. If model is provided, checks if object belongs to organization
    
    Usage:
    @requires_permission("read:project", model=Project)  # For object-level checks
    @requires_permission("create:project")  # For general permission checks
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
                raise HTTPException(status_code=403, detail="User is not a member of this organization")

            # Check role-based permission
            if permission not in ROLES_PERMISSIONS.get(membership.role, set()):
                raise HTTPException(status_code=403, detail="Permission denied")

            # If model is provided and object_id exists and is not None and is a valid UUID-like string, verify object belongs to organization
            if model and object_id is not None:
                stmt = select(model).where(
                    model.id == object_id,
                    model.organization_id == organization.id
                )
                result = await db.execute(stmt)
                obj = result.scalar_one_or_none()
                
                if not obj:
                    raise HTTPException(status_code=404, detail="Object not found or access denied")

            return await func(*args, **kwargs)
        return wrapper
    return decorator