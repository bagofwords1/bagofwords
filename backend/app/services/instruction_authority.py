"""Who may change an instruction — one copy, shared by the REST routes and MCP tools.

The rules (unchanged from the REST routes they were lifted from):

- Attached to agents → ``manage_instructions`` on EVERY one of them. Managing
  one agent an instruction is shared with is not authority over the others.
- Attached to NO agent (global) → org-level ``manage_instructions``. A
  scope-less rule reaches every agent in the org, so a per-agent grant confers
  no authority over it.
- A new scope needs the same authority as the instruction itself: an agent
  manager can't promote a rule to global, or widen it to an agent they don't
  manage.
- Deleting: the author may always retract their own unpublished suggestion,
  and their own global; otherwise the edit rule applies.

Each check raises ``HTTPException(403)`` on refusal.
"""

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions_decorator import check_resource_permissions, require_org_permission
from app.models.organization import Organization
from app.models.user import User


def _is_author(existing, user: User) -> bool:
    owner_id = getattr(existing, "user_id", None)
    return owner_id is not None and str(owner_id) == str(user.id)


def _attached_ids(existing) -> list[str]:
    return [str(ds.id) for ds in (getattr(existing, "data_sources", None) or [])]


async def require_instruction_authority(
    db: AsyncSession, user: User, organization: Organization, existing,
    *, allow_owner: bool = False,
) -> None:
    """Authority to mutate or review ``existing``.

    ``allow_owner`` keeps the edit-path carve-out: the author of a scope-less
    instruction may still edit it (the service then restricts them to content
    fields — governance on a scope-less rule stays org-level). Review actions
    don't pass it: authoring a rule is not authority to approve changes to it.
    """
    ds_ids = _attached_ids(existing)
    if ds_ids:
        await check_resource_permissions(
            db, str(user.id), str(organization.id),
            "data_source", ds_ids, "manage_instructions",
        )
        return
    if allow_owner and _is_author(existing, user):
        return
    await require_org_permission(db, str(user.id), str(organization.id), "manage_instructions")


async def require_scope_authority(
    db: AsyncSession, user: User, organization: Organization,
    data_source_ids: Iterable[str] | None,
) -> None:
    """Authority to place an instruction on ``data_source_ids``.

    ``None`` means "scope not being set" and passes; an empty list means
    global and needs org-level authority. Keep the distinction: an empty list
    is falsy, and treating it as "unchanged" is how a per-agent manager could
    once promote their own rule to global.
    """
    if data_source_ids is None:
        return
    ds_ids = [str(i) for i in data_source_ids]
    if ds_ids:
        await check_resource_permissions(
            db, str(user.id), str(organization.id),
            "data_source", ds_ids, "manage_instructions",
        )
    else:
        await require_org_permission(db, str(user.id), str(organization.id), "manage_instructions")


async def require_delete_authority(
    db: AsyncSession, user: User, organization: Organization, existing,
) -> None:
    """Authority to delete ``existing``.

    The author may always retract their own unpublished suggestion (suggesting
    is open, so retracting must be too) and their own global (de-escalating;
    avoids zombie globals an ex-admin can't clean up). Everything else needs
    the same authority as an edit.
    """
    own = _is_author(existing, user)
    if own and (getattr(existing, "status", None) != "published" or not _attached_ids(existing)):
        return
    await require_instruction_authority(db, user, organization, existing)
