"""Single source of truth for resolving an organization's main instruction build.

Only one ``InstructionBuild`` per organization is supposed to carry
``is_main=True`` — it is the live instruction set every read path resolves
against. That invariant used to be enforced only by ``promote_build`` writing
carefully, with nothing at the database level backing it up, and every reader
independently did::

    select(InstructionBuild).where(org, is_main == True, deleted_at == None)
    ... .scalar_one_or_none()

``scalar_one_or_none()`` RAISES ``MultipleResultsFound`` the moment a second
main build exists. Two promotions racing inside one org (a user instruction
save finalizing while a training session or git sync promotes its own build)
could leave exactly that state — and from then on the org was hard-broken:
``GET /instructions`` 500s ("Failed to load instructions" in the UI), the
badge counts 500, and the agent's own instruction context fails to load.

This module makes duplicate mains survivable instead of fatal. Every reader
resolves the SAME winner — the highest ``build_number``, tie-broken by newest
``created_at`` then id — so the app stays internally consistent while the
duplicate exists, and logs a warning so the condition is visible. A partial
unique index (migration ``mainbuild01``) stops new duplicates from being
created at all; this resolver is the belt to that index's braces, and what
keeps already-corrupted orgs readable.
"""

import logging
from typing import List, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instruction_build import InstructionBuild

logger = logging.getLogger(__name__)


def main_build_select(organization_id: str, *entities):
    """SELECT over the org's live main build(s), ordered so the first row is the
    canonical winner. Pass ``entities`` to project specific columns (defaults to
    the id)."""
    return (
        select(*(entities or (InstructionBuild.id,)))
        .where(
            and_(
                InstructionBuild.organization_id == str(organization_id),
                InstructionBuild.is_main == True,  # noqa: E712
                InstructionBuild.deleted_at == None,  # noqa: E711
            )
        )
        .order_by(
            InstructionBuild.build_number.desc(),
            InstructionBuild.created_at.desc(),
            InstructionBuild.id.desc(),
        )
    )


def _warn_on_duplicates(organization_id: str, rows: List) -> None:
    # Callers fetch with limit(2), so this detects the condition without
    # counting it — len(rows) is capped and must not be reported as a total.
    if len(rows) > 1:
        logger.warning(
            "Organization %s has more than one build flagged is_main; resolving "
            "to the highest build_number. The extras should be demoted — see "
            "migration mainbuild01.",
            organization_id,
        )


async def resolve_main_build_id(
    db: AsyncSession, organization_id: str
) -> Optional[str]:
    """Id of the org's main build, or None. Never raises when several builds
    claim main — picks the canonical winner and warns."""
    rows = (
        await db.execute(main_build_select(organization_id).limit(2))
    ).scalars().all()
    _warn_on_duplicates(organization_id, rows)
    return str(rows[0]) if rows else None


async def resolve_main_build(
    db: AsyncSession, organization_id: str, *, options=()
) -> Optional[InstructionBuild]:
    """The org's main build row, or None. Same duplicate tolerance as
    :func:`resolve_main_build_id`. ``options`` are loader options applied to the
    query (e.g. ``selectinload(InstructionBuild.contents)``)."""
    query = main_build_select(organization_id, InstructionBuild).limit(2)
    if options:
        query = query.options(*options)
    rows = (await db.execute(query)).scalars().all()
    _warn_on_duplicates(organization_id, rows)
    return rows[0] if rows else None
