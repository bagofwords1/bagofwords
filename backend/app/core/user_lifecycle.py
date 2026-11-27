"""Account activation follows organization membership.

Removing someone from the members list should make the removal real — they
should not keep a working login. Hard-deleting the ``users`` row is the wrong
way to get there: ``reports.user_id`` is a non-nullable FK with no ``ondelete``
(``app/models/report.py``), and queries, files, completions, completion
feedback and the audit trail all point at that same row. A delete would either
fail on the constraint or take the organization's content and its history with
it, and "Alice left the team" must not mean "Alice's dashboards are gone".

So a user who loses their last membership is *deactivated* instead. Setting
``is_active=False`` closes every authentication path at once, because all three
re-assert it per request rather than trusting an issued credential:

- the JWT dependency (``fapi.current_user(active=True)`` in ``app/core/auth.py``)
  re-loads the user, so outstanding tokens stop working immediately;
- OAuth access tokens go through ``_active_subject``, which filters on
  ``User.is_active``;
- personal API keys are rejected in ``ApiKeyService.get_user_by_api_key``.

Meanwhile every row referencing the user stays intact, and granting them a
membership again flips the flag back — the removal is reversible, which a
delete never is. SCIM already deprovisions exactly this way (its ``DELETE``
handler sets ``is_active=False`` and leaves the content alone), so this applies
one lifecycle to the members list rather than inventing a second one.

Deactivation is scoped to ``allow_multiple_organizations`` being off — the
self-hosted single-organization shape, where "not in this org" and "has no
account here" are the same statement. When multiple organizations are enabled,
holding zero memberships is an ordinary in-between state (an invite not yet
accepted, a user moving between orgs) and the account stays active.

Neither helper commits: the caller owns the transaction, so the activation flip
lands atomically with the membership change that caused it.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.membership import Membership
from app.models.user import User
from app.settings.config import settings
from app.settings.logging_config import get_logger

logger = get_logger(__name__)


async def _load_user(db: AsyncSession, user_id: str) -> User | None:
    return (
        await db.execute(select(User).where(User.id == str(user_id)))
    ).scalar_one_or_none()


async def deactivate_user_if_orphaned(db: AsyncSession, user_id: str) -> bool:
    """Close the login of a user whose last organization membership is gone.

    Call this from any path that drops a membership, *before* committing, with
    the membership row already deleted or flushed as deleted so the remaining
    count reflects the change. Returns True when the account was deactivated.

    No-ops (and returns False) when multiple organizations are allowed, when the
    user still belongs somewhere, or for the account kinds whose activation is
    owned elsewhere: service accounts (``ServiceAccount.disabled_at`` is their
    kill switch, and their backing user row is inactive by construction) and
    superusers (break-glass access must not be revocable from the members list).
    """
    if settings.bow_config.features.allow_multiple_organizations:
        return False

    user = await _load_user(db, user_id)
    if user is None or not user.is_active:
        return False
    if user.is_service_account or is_instance_admin_account(user):
        return False

    remaining = (
        await db.execute(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.user_id == str(user_id),
                Membership.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    if remaining:
        return False

    user.is_active = False
    logger.info(
        "Deactivated user %s: last organization membership removed. Their "
        "content and history are untouched; re-inviting them restores access.",
        user_id,
    )
    return True


def is_instance_admin_account(user) -> bool:
    """Whether the account is an instance administrator, by its flag or by its
    address being named in the configured list.

    Such an account is never an organization's to manage: an organization does
    not place it in itself, rewrite it, or switch it off or on.
    """
    from app.core.instance_admin import is_reserved_instance_admin_email

    if user is None:
        return False
    return bool(getattr(user, "is_superuser", False)) or is_reserved_instance_admin_email(
        getattr(user, "email", None) or ""
    )


async def may_attach_existing_account(db: AsyncSession, user, organization_id: str) -> bool:
    """Whether an organization may place an existing account in itself
    directly, without the person accepting an invite.

    Never for an instance administrator. Never when the deployment hosts
    multiple organizations: there an existing account, even one with no
    organization yet, is not this organization's to claim. On a
    single-organization deployment an account with no other organization is
    attached directly, as always (re-adding a removed member).
    """
    if is_instance_admin_account(user):
        return False
    if settings.bow_config.features.allow_multiple_organizations:
        return False
    return not await has_live_membership_elsewhere(db, str(user.id), organization_id)


async def has_live_membership_elsewhere(
    db: AsyncSession, user_id: str, organization_id: str | None
) -> bool:
    """Whether the user holds a live membership outside ``organization_id``.

    The account row (email, name, ``is_active``) is shared by every
    organization a person belongs to, so one organization may only change it
    while it is the person's sole organization. With ``organization_id=None``
    every live membership counts.
    """
    stmt = select(Membership.id).where(
        Membership.user_id == str(user_id),
        Membership.deleted_at.is_(None),
    )
    if organization_id is not None:
        stmt = stmt.where(Membership.organization_id != str(organization_id))
    # Deliberately looks across organizations.
    return (
        await db.execute(stmt.limit(1).execution_options(org_scope="system"))
    ).first() is not None


async def reactivate_user_for_membership(
    db: AsyncSession, user_id: str, organization_id: str | None = None
) -> bool:
    """Restore the login of a deactivated user who is regaining a membership.

    The counterpart to :func:`deactivate_user_if_orphaned`, so re-inviting
    someone is all it takes to bring them back. Call it from any path that
    grants a membership to an already-registered user, before committing.
    Returns True when the account was reactivated.

    ``organization_id`` is the organization granting the membership. Pass it
    whenever the membership row may already be written, so it is not mistaken
    for a membership held elsewhere.

    Only the orphan case is undone: a user who still belongs to another
    organization and is inactive was deactivated on purpose (that
    organization's directory deprovisioned them), and one organization must not
    override another's decision about the shared account.

    Deliberately *not* gated on ``allow_multiple_organizations``: a user whose
    only organization is this one needs to be able to sign in. Service accounts
    are exempt — ``is_active=False`` is their normal state, not a deactivation
    to undo.
    """
    user = await _load_user(db, user_id)
    if user is None or user.is_active or user.is_service_account:
        return False
    if is_instance_admin_account(user):
        return False
    if await has_live_membership_elsewhere(db, user_id, organization_id):
        return False

    user.is_active = True
    logger.info("Reactivated user %s: granted an organization membership.", user_id)
    return True


async def revoke_member_org_access(db: AsyncSession, organization_id: str, user_id: str) -> None:
    """Drop what a registered member held in ``organization_id`` when their
    membership ends there.

    Role assignments, group memberships and resource grants are deleted so a
    later re-invite grants exactly the invited role and nothing left over.
    Personal API keys created in the organization are revoked, and the
    member's webhooks and triggers there are switched off so nothing keeps
    running as them. Other organizations are untouched.

    Call from the paths that end a membership for good (members list, SCIM),
    before committing. Does not commit. A directory sync that retires a
    membership (soft delete) does not call this: the retired membership
    already grants nothing, and everything is as it was if the directory
    brings the person back.
    """
    from datetime import datetime

    from sqlalchemy import delete, update

    from app.core.permission_resolver import invalidate_rbac_memo
    from app.models.api_key import ApiKey
    from app.models.group import Group
    from app.models.group_membership import GroupMembership
    from app.models.resource_grant import ResourceGrant
    from app.models.role_assignment import RoleAssignment
    from app.models.webhook import Webhook

    org_id, uid = str(organization_id), str(user_id)

    # Their cached copies of custom tables stop being servable in the same
    # transaction; the daily cleanup deletes the files (it removes any copy
    # whose user is no longer a member).
    from app.models.custom_table_user_copy import CustomTableUserCopy
    await db.execute(
        update(CustomTableUserCopy).where(
            CustomTableUserCopy.organization_id == org_id,
            CustomTableUserCopy.user_id == uid,
        ).values(invalidated_at=datetime.utcnow()).execution_options(synchronize_session=False)
    )

    await db.execute(
        delete(RoleAssignment).where(
            RoleAssignment.organization_id == org_id,
            RoleAssignment.principal_type == "user",
            RoleAssignment.principal_id == uid,
        ).execution_options(synchronize_session=False)
    )
    await db.execute(
        delete(GroupMembership).where(
            GroupMembership.user_id == uid,
            GroupMembership.group_id.in_(
                select(Group.id).where(Group.organization_id == org_id)
            ),
        ).execution_options(synchronize_session=False)
    )
    await db.execute(
        delete(ResourceGrant).where(
            ResourceGrant.organization_id == org_id,
            ResourceGrant.principal_type == "user",
            ResourceGrant.principal_id == uid,
        ).execution_options(synchronize_session=False)
    )
    await db.execute(
        update(ApiKey).where(
            ApiKey.organization_id == org_id,
            ApiKey.user_id == uid,
            ApiKey.service_account_id.is_(None),
            ApiKey.deleted_at.is_(None),
        ).values(deleted_at=datetime.utcnow()).execution_options(synchronize_session=False)
    )
    await db.execute(
        update(Webhook).where(
            Webhook.organization_id == org_id,
            Webhook.user_id == uid,
            Webhook.is_active.is_(True),
        ).values(is_active=False).execution_options(synchronize_session=False)
    )
    invalidate_rbac_memo(db, uid, org_id)
