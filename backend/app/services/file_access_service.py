"""Who may see a file.

`manage_files` is a baseline permission every org member holds, so it only
says "this person may work with files at all" — it says nothing about WHICH
files. Without this module every member could list, download and re-attach any
file in the organization, including uploads sitting in other users' private
reports.

A non-admin may see a file when any of these holds:

1. they uploaded it;
2. it is in the library of an agent (data source) they can access;
3. it is a default file of a project they can view;
4. it is attached to a report whose conversation they can view (owner,
   project collaborator, or the conversation is shared with them);
5. it is embedded in an artifact of a report whose conversation OR artifact
   they can view — a dashboard shared internally shows its embedded images,
   but not every upload that sits in the chat behind it.

Full admins see every file in the org.

The org-wide listing (`visible_files_clause`) covers 1-3 only: it backs the file
picker, and a file reached through someone else's report is read through that
report (`GET /reports/{id}/files`), not surfaced in the picker.
"""
from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from app.core.permission_resolver import (
    FULL_ADMIN,
    get_accessible_data_source_ids,
    resolve_permissions,
)
from app.models.data_source import DataSource
from app.models.data_source_file_association import data_source_file_association
from app.models.file import File, report_file_association
from app.models.organization import Organization
from app.models.project import project_file_association
from app.models.report import Report
from app.models.user import User


async def is_full_admin(db: AsyncSession, user: User, organization: Organization) -> bool:
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    return FULL_ADMIN in resolved.org_permissions


async def _accessible_data_source_ids(
    db: AsyncSession, user: User, organization: Organization
) -> set[str]:
    """Agents the user can open: explicit grants/memberships plus public ones."""
    _, granted = await get_accessible_data_source_ids(db, str(user.id), str(organization.id))
    public = await db.execute(
        select(DataSource.id).where(
            DataSource.organization_id == str(organization.id),
            DataSource.is_public.is_(True),
        )
    )
    return {str(x) for x in granted} | {str(r[0]) for r in public.all()}


async def _visible_project_ids(
    db: AsyncSession, user: User, organization: Organization
) -> list[str]:
    from app.services.project_service import project_service
    return await project_service.get_visible_project_ids(db, user, organization)


async def visible_files_clause(
    db: AsyncSession, user: User, organization: Organization
):
    """WHERE clause for the files a user may list (rules 1-3), or None for a
    full admin, who may list every file in the org."""
    if await is_full_admin(db, user, organization):
        return None

    conditions = [File.user_id == str(user.id)]

    ds_ids = await _accessible_data_source_ids(db, user, organization)
    if ds_ids:
        conditions.append(File.id.in_(
            select(data_source_file_association.c.file_id).where(
                data_source_file_association.c.data_source_id.in_(ds_ids)
            )
        ))

    project_ids = await _visible_project_ids(db, user, organization)
    if project_ids:
        conditions.append(File.id.in_(
            select(project_file_association.c.file_id).where(
                project_file_association.c.project_id.in_(project_ids)
            )
        ))

    return or_(*conditions)


CONVERSATION = ("conversation_visibility",)
CONVERSATION_OR_ARTIFACT = ("conversation_visibility", "artifact_visibility")


async def user_can_view_report(
    db: AsyncSession, user: User, organization: Organization, report: Report,
    surfaces: tuple[str, ...] = CONVERSATION,
) -> bool:
    """Read access to a report for file purposes: its owner, a full admin, or
    anyone the visibility of one of `surfaces` (or the report's project)
    admits. Same rules as the shared-report routes (_check_visibility)."""
    if str(report.organization_id) != str(organization.id):
        return False
    if str(report.user_id) == str(user.id):
        return True
    if await is_full_admin(db, user, organization):
        return True
    from app.services.report_service import ReportService
    checker = ReportService()
    for field in surfaces:
        try:
            await checker._check_visibility(db, report, field, user)
            return True
        except HTTPException:
            continue
    return False


def user_owns_report(user: User, report: Report | None) -> bool:
    """Write access to a report's files: owner only, matching every other
    report mutation (completions, report updates)."""
    return report is not None and str(report.user_id) == str(user.id)


async def _any_viewable_report(
    db: AsyncSession, user: User, organization: Organization, report_ids: Iterable[str],
    surfaces: tuple[str, ...],
) -> bool:
    ids = list({str(r) for r in report_ids if r})
    if not ids:
        return False
    reports = (await db.execute(
        select(Report).options(lazyload("*")).where(
            Report.id.in_(ids),
            Report.organization_id == str(organization.id),
        )
    )).scalars().all()
    for report in reports:
        if await user_can_view_report(db, user, organization, report, surfaces):
            return True
    return False


async def user_can_view_file(
    db: AsyncSession, user: User, organization: Organization, file: File
) -> bool:
    if file is None or str(file.organization_id) != str(organization.id):
        return False
    if str(file.user_id or "") == str(user.id):
        return True
    if await is_full_admin(db, user, organization):
        return True

    # 2. Agent library.
    ds_ids = {str(r[0]) for r in (await db.execute(
        select(data_source_file_association.c.data_source_id).where(
            data_source_file_association.c.file_id == str(file.id)
        )
    )).all()}
    if ds_ids and ds_ids & await _accessible_data_source_ids(db, user, organization):
        return True

    # 3. Project defaults.
    project_ids = {str(r[0]) for r in (await db.execute(
        select(project_file_association.c.project_id).where(
            project_file_association.c.file_id == str(file.id)
        )
    )).all()}
    if project_ids and project_ids & set(await _visible_project_ids(db, user, organization)):
        return True

    # 4. Attached to a report whose conversation the user can view.
    report_ids = [r[0] for r in (await db.execute(
        select(report_file_association.c.report_id).where(
            report_file_association.c.file_id == str(file.id)
        )
    )).all()]
    if await _any_viewable_report(db, user, organization, report_ids, CONVERSATION):
        return True

    # 5. Embedded in an artifact of a report the user can view. Artifacts
    # carry their embedded files as {"files": [{"id": ...}]} in the version
    # content; the id is a UUID, so a substring match on the serialized JSON
    # is exact enough and portable across sqlite and postgres.
    # Only reports someone besides their owner can open are scanned (plus the
    # caller's own), so a request never walks every artifact in the org.
    from app.models.artifact import ArtifactVersion
    embedded_in = [r[0] for r in (await db.execute(
        select(ArtifactVersion.report_id)
        .join(Report, Report.id == ArtifactVersion.report_id)
        .where(
            ArtifactVersion.organization_id == str(organization.id),
            or_(
                Report.user_id == str(user.id),
                Report.project_id.isnot(None),
                Report.artifact_visibility.in_(("public", "internal", "shared")),
                Report.conversation_visibility.in_(("public", "internal", "shared")),
            ),
            cast(ArtifactVersion.content, String).contains(str(file.id)),
        ).distinct()
    )).all()]
    return await _any_viewable_report(
        db, user, organization, embedded_in, CONVERSATION_OR_ARTIFACT
    )


async def get_viewable_file_or_404(
    db: AsyncSession, user: User, organization: Organization, file_id: str
) -> File:
    """Load a file the user may view. 404 (not 403) when they may not, so file
    ids belonging to other users don't leak their existence."""
    file = (await db.execute(
        select(File).where(
            File.id == str(file_id),
            File.organization_id == str(organization.id),
        )
    )).scalar_one_or_none()
    if file is None or not await user_can_view_file(db, user, organization, file):
        raise HTTPException(status_code=404, detail="File not found")
    return file


async def filter_viewable_files(
    db: AsyncSession, user: User, organization: Organization, file_ids: Iterable[str]
) -> list[File]:
    """The subset of `file_ids` the user may view, as File rows (org-scoped)."""
    ids = list({str(f) for f in file_ids if f})
    if not ids:
        return []
    files = (await db.execute(
        select(File).where(
            File.id.in_(ids),
            File.organization_id == str(organization.id),
        )
    )).scalars().all()
    return [f for f in files if await user_can_view_file(db, user, organization, f)]

