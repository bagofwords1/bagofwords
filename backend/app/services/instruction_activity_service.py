"""The instruction changelog — every change to an organization's instructions.

A build is already a commit: it has an author, a source (a person, a git sync,
or the agent itself), a timestamp, and a set of contents that differs from the
build it forked from. This service reads that history back as an activity feed,
with the net effect of each change — how many instructions it added, altered and
removed — so a change that removed hundreds is legible as a single row rather
than something you have to go looking for.

The stored `added_count` / `modified_count` / `removed_count` columns on
InstructionBuild are NOT used: nothing writes them (`update_build_stats` has no
callers), so they are 0 everywhere. The counts here are derived from
build_contents against each build's base, which is the only trustworthy source.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.build_content import BuildContent
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.instruction_build import InstructionBuild
from app.models.organization import Organization
from app.models.user import User

logger = logging.getLogger(__name__)

#: Sources a build can originate from, in the vocabulary the UI filters on.
ACTIVITY_SOURCES = ("user", "ai", "git", "rollback")

#: Cap on the per-entry change list. An entry that touched 251 instructions
#: shows the first N and a remainder count — the feed is for recognising what
#: happened, not for enumerating it.
CHANGE_SAMPLE = 25


class InstructionActivityService:

    def _delta_columns(self):
        """Scalar subqueries for a build's net effect against its base build.

        Correlated per row rather than materialised, so a page of entries costs
        a bounded number of index seeks instead of loading every build's
        contents. A build with no base (the first) counts all of its contents
        as additions.
        """
        cur = aliased(BuildContent)
        base = aliased(BuildContent)

        # Both levels must correlate explicitly to InstructionBuild. Without it
        # on the INNER select, SQLAlchemy adds instruction_builds to that
        # subquery's FROM — a cartesian product that asks "does ANY build's base
        # hold this instruction" instead of "does THIS build's base", which
        # silently reports almost every change as a no-op.
        added = (
            select(func.count())
            .select_from(cur)
            .where(and_(
                cur.build_id == InstructionBuild.id,
                cur.deleted_at.is_(None),
                ~select(base.id).where(and_(
                    base.build_id == InstructionBuild.base_build_id,
                    base.instruction_id == cur.instruction_id,
                    base.deleted_at.is_(None),
                )).correlate(InstructionBuild, cur).exists(),
            ))
            .correlate(InstructionBuild)
            .scalar_subquery()
        )

        removed = (
            select(func.count())
            .select_from(base)
            .where(and_(
                base.build_id == InstructionBuild.base_build_id,
                base.deleted_at.is_(None),
                ~select(cur.id).where(and_(
                    cur.build_id == InstructionBuild.id,
                    cur.instruction_id == base.instruction_id,
                    cur.deleted_at.is_(None),
                )).correlate(InstructionBuild, base).exists(),
            ))
            .correlate(InstructionBuild)
            .scalar_subquery()
        )

        modified = (
            select(func.count())
            .select_from(cur)
            .join(base, and_(
                base.build_id == InstructionBuild.base_build_id,
                base.instruction_id == cur.instruction_id,
                base.deleted_at.is_(None),
            ))
            .where(and_(
                cur.build_id == InstructionBuild.id,
                cur.deleted_at.is_(None),
                cur.instruction_version_id != base.instruction_version_id,
            ))
            .correlate(InstructionBuild)
            .scalar_subquery()
        )

        return added, modified, removed

    async def get_activity(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        *,
        skip: int = 0,
        limit: int = 25,
        agent_ids: Optional[List[str]] = None,
        user_ids: Optional[List[str]] = None,
        sources: Optional[List[str]] = None,
        since: Optional[datetime] = None,
        include_empty: bool = False,
    ) -> Dict[str, Any]:
        """Changelog entries, newest first.

        `include_empty` keeps builds whose net effect is zero — carry-over
        builds that snapshot the set without altering it. They are noise in a
        changelog, so they're dropped by default.
        """
        from app.core.permission_resolver import get_accessible_data_source_ids

        org_id = str(organization.id)
        conditions = [
            InstructionBuild.organization_id == org_id,
            InstructionBuild.deleted_at.is_(None),
        ]
        # Multi-select filters are OR within a facet, AND across facets — the
        # shape people expect from a filter bar.
        if user_ids:
            conditions.append(InstructionBuild.created_by_user_id.in_(user_ids))
        if sources:
            conditions.append(InstructionBuild.source.in_(sources))
        if since:
            conditions.append(InstructionBuild.created_at >= since)

        # Same visibility rule the build list applies: a non-admin never sees a
        # build that touches a data source they have no access to.
        is_admin, accessible_ds_ids = await get_accessible_data_source_ids(
            db, str(current_user.id), org_id
        )
        if not is_admin:
            forbidden = (
                select(BuildContent.build_id)
                .join(
                    instruction_data_source_association,
                    instruction_data_source_association.c.instruction_id == BuildContent.instruction_id,
                )
                .where(
                    instruction_data_source_association.c.data_source_id.notin_(accessible_ds_ids)
                    if accessible_ds_ids else True
                )
            )
            conditions.append(InstructionBuild.id.notin_(forbidden))

        if agent_ids:
            touches_agent = (
                select(BuildContent.build_id)
                .join(
                    instruction_data_source_association,
                    instruction_data_source_association.c.instruction_id == BuildContent.instruction_id,
                )
                .where(instruction_data_source_association.c.data_source_id.in_(agent_ids))
            )
            conditions.append(InstructionBuild.id.in_(touches_agent))

        added, modified, removed = self._delta_columns()

        actor = aliased(User)
        stmt = (
            select(
                InstructionBuild.id,
                InstructionBuild.build_number,
                InstructionBuild.title,
                InstructionBuild.description,
                InstructionBuild.status,
                InstructionBuild.source,
                InstructionBuild.is_main,
                InstructionBuild.created_at,
                InstructionBuild.commit_sha,
                InstructionBuild.base_build_id,
                actor.id,
                actor.name,
                actor.email,
                added.label("added"),
                modified.label("modified"),
                removed.label("removed"),
            )
            .select_from(InstructionBuild)
            .outerjoin(actor, actor.id == InstructionBuild.created_by_user_id)
            .where(and_(*conditions))
        )
        if not include_empty:
            stmt = stmt.where(or_(added > 0, modified > 0, removed > 0))

        total = (await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar() or 0

        rows = (await db.execute(
            stmt.order_by(
                InstructionBuild.created_at.desc(),
                InstructionBuild.build_number.desc(),
                InstructionBuild.id.desc(),
            ).offset(skip).limit(limit)
        )).all()

        items = [
            {
                "build_id": str(r[0]),
                "build_number": r[1],
                "title": r[2],
                "description": r[3],
                "status": r[4],
                "source": r[5],
                "is_live": bool(r[6]),
                "created_at": r[7],
                "commit_sha": r[8],
                "has_base": r[9] is not None,
                "actor": (
                    {"id": str(r[10]), "name": r[11], "email": r[12]} if r[10] else None
                ),
                "added": int(r[13] or 0),
                "modified": int(r[14] or 0),
                "removed": int(r[15] or 0),
            }
            for r in rows
        ]

        return {
            "items": items,
            "total": total,
            "page": (skip // limit) + 1 if limit > 0 else 1,
            "per_page": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 1,
        }

    async def get_entry_changes(
        self,
        db: AsyncSession,
        organization: Organization,
        build_id: str,
    ) -> Dict[str, Any]:
        """What one changelog entry actually touched, as instruction titles.

        Returns a bounded sample per bucket plus the true totals, so an entry
        that removed hundreds is readable without shipping hundreds of rows.
        """
        build = (await db.execute(
            select(InstructionBuild).where(and_(
                InstructionBuild.id == build_id,
                InstructionBuild.organization_id == str(organization.id),
                InstructionBuild.deleted_at.is_(None),
            ))
        )).scalar_one_or_none()
        if not build:
            return {"added": [], "modified": [], "removed": [],
                    "added_total": 0, "modified_total": 0, "removed_total": 0}

        async def contents(bid) -> Dict[str, str]:
            if not bid:
                return {}
            rows = (await db.execute(
                select(BuildContent.instruction_id, BuildContent.instruction_version_id)
                .where(and_(BuildContent.build_id == bid, BuildContent.deleted_at.is_(None)))
            )).all()
            return {str(i): str(v) for i, v in rows}

        cur = await contents(build.id)
        base = await contents(build.base_build_id)

        added_ids = [i for i in cur if i not in base]
        removed_ids = [i for i in base if i not in cur]
        modified_ids = [i for i in cur if i in base and cur[i] != base[i]]

        # One lookup for every id we will actually render.
        sample_ids = (added_ids[:CHANGE_SAMPLE] + removed_ids[:CHANGE_SAMPLE]
                      + modified_ids[:CHANGE_SAMPLE])
        labels: Dict[str, str] = {}
        if sample_ids:
            for iid, title, text in (await db.execute(
                select(Instruction.id, Instruction.title, Instruction.text)
                .where(Instruction.id.in_(sample_ids))
            )).all():
                labels[str(iid)] = (title or (text or "").split("\n")[0][:80] or "Untitled")

        def render(ids: List[str]) -> List[Dict[str, str]]:
            return [
                {"id": i, "label": labels.get(i, "(deleted instruction)")}
                for i in ids[:CHANGE_SAMPLE]
            ]

        return {
            "added": render(added_ids),
            "modified": render(modified_ids),
            "removed": render(removed_ids),
            "added_total": len(added_ids),
            "modified_total": len(modified_ids),
            "removed_total": len(removed_ids),
        }
