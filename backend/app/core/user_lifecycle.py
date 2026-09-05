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
    if user.is_service_account or user.is_superuser:
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


async def reactivate_user_for_membership(db: AsyncSession, user_id: str) -> bool:
    """Restore the login of a deactivated user who is regaining a membership.

    The counterpart to :func:`deactivate_user_if_orphaned`, so re-inviting
    someone is all it takes to bring them back. Call it from any path that
    grants a membership to an already-registered user, before committing.
    Returns True when the account was reactivated.

    Deliberately *not* gated on ``allow_multiple_organizations``: whatever
    deactivated the account, a user who holds a membership needs to be able to
    sign in. Service accounts are exempt — ``is_active=False`` is their normal
    state, not a deactivation to undo.
    """
    user = await _load_user(db, user_id)
    if user is None or user.is_active or user.is_service_account:
        return False

    user.is_active = True
    logger.info("Reactivated user %s: granted an organization membership.", user_id)
    return True
