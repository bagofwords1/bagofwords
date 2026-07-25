"""Projects: private-by-default shared folders that group reports.

Access rules (enforced here, not in route decorators — `project_id` is not a
decorator-mapped param):
- owner  → full control (view/manage/delete)
- ResourceGrant(resource_type='project') with 'view' → see project + its reports
- ... with 'manage' → edit project metadata / members (implies view)
- access == 'org'   → every org member can view
- full_admin_access → everything

A report is IN a project via reports.project_id. Deleting a project never
deletes reports — they return to their owners' root lists (project_id=NULL).
"""
from logging import getLogger

from fastapi import HTTPException
from sqlalchemy import select, func, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.report import Report
from app.models.resource_grant import ResourceGrant
from app.models.user import User
from app.models.organization import Organization
from app.core.permission_resolver import (
    resolve_permissions,
    principal_belongs_to_org,
    FULL_ADMIN,
)
from app.schemas.project_schema import (
    ProjectCreate,
    ProjectUpdate,
    ProjectSchema,
    ProjectListResponse,
    ProjectMemberSchema,
    ProjectMemberUpsert,
)
from app.schemas.user_schema import UserSchema

logger = getLogger(__name__)

RESOURCE_TYPE = "project"


class ProjectService:

    # ── Access helpers (also used by ReportService) ─────────────────────────

    async def get_visible_project_ids(
        self, db: AsyncSession, user: User, organization: Organization
    ) -> list[str]:
        """All project ids in the org the user can view: owned, granted
        (view/manage, directly or via group/role), or org-shared."""
        resolved = await resolve_permissions(db, str(user.id), str(organization.id))
        granted_ids = {
            rid for (rtype, rid), perms in resolved.resource_permissions.items()
            if rtype == RESOURCE_TYPE and ({"view", "manage"} & perms)
        }
        conditions = [Project.user_id == str(user.id), Project.access == "org"]
        if FULL_ADMIN in resolved.org_permissions:
            # Admins can view every project (parity with the decorator's
            # admin view bypass); their sidebar list still only shows
            # owned/granted/org projects via list_projects.
            conditions = [Project.id.isnot(None)]
        elif granted_ids:
            conditions.append(Project.id.in_(granted_ids))

        rows = await db.execute(
            select(Project.id).where(
                Project.organization_id == str(organization.id),
                Project.deleted_at.is_(None),
                self._or(*conditions),
            )
        )
        return [str(r[0]) for r in rows.all()]

    @staticmethod
    def _or(*conditions):
        from sqlalchemy import or_
        return or_(*conditions)

    async def user_can_view_project(
        self, db: AsyncSession, user: User | None, project: Project
    ) -> bool:
        if user is None or project is None or project.deleted_at is not None:
            return False
        if str(project.user_id) == str(user.id):
            return True
        # Any non-owner path requires org membership (grants are org-scoped,
        # but access='org' must not leak to removed members).
        if not await principal_belongs_to_org(db, user, str(project.organization_id)):
            return False
        if project.access == "org":
            return True
        resolved = await resolve_permissions(db, str(user.id), str(project.organization_id))
        if FULL_ADMIN in resolved.org_permissions:
            return True
        return resolved.has_resource_permission(RESOURCE_TYPE, str(project.id), "view")

    async def user_can_manage_project(
        self, db: AsyncSession, user: User, project: Project
    ) -> bool:
        if user is None or project is None:
            return False
        if str(project.user_id) == str(user.id):
            return True
        resolved = await resolve_permissions(db, str(user.id), str(project.organization_id))
        if FULL_ADMIN in resolved.org_permissions:
            return True
        return resolved.has_resource_permission(RESOURCE_TYPE, str(project.id), "manage")

    async def _get_project_or_404(
        self, db: AsyncSession, project_id: str, organization: Organization
    ) -> Project:
        result = await db.execute(
            select(Project).where(
                Project.id == str(project_id),
                Project.organization_id == str(organization.id),
                Project.deleted_at.is_(None),
            )
        )
        project = result.scalar_one_or_none()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    async def get_project_for_view(
        self, db: AsyncSession, project_id: str, current_user: User, organization: Organization
    ) -> Project:
        """Load a project the user can view, or raise. 404 (not 403) when the
        caller has no access, so private project ids don't leak existence."""
        project = await self._get_project_or_404(db, project_id, organization)
        if not await self.user_can_view_project(db, current_user, project):
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    # ── CRUD ────────────────────────────────────────────────────────────────

    async def create_project(
        self, db: AsyncSession, data: ProjectCreate, current_user: User, organization: Organization
    ) -> ProjectSchema:
        project = Project(
            name=data.name,
            description=data.description,
            color=data.color,
            access="private",
            user_id=str(current_user.id),
            organization_id=str(organization.id),
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)
        return await self._to_schema(db, project, current_user)

    async def list_projects(
        self, db: AsyncSession, current_user: User, organization: Organization
    ) -> ProjectListResponse:
        """Projects the user owns, was granted, or that are org-shared —
        with report counts, in one round trip per aggregate."""
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        granted = {
            rid: perms for (rtype, rid), perms in resolved.resource_permissions.items()
            if rtype == RESOURCE_TYPE
        }
        conditions = [Project.user_id == str(current_user.id), Project.access == "org"]
        if granted:
            conditions.append(Project.id.in_(list(granted.keys())))

        result = await db.execute(
            select(Project).where(
                Project.organization_id == str(organization.id),
                Project.deleted_at.is_(None),
                self._or(*conditions),
            ).order_by(Project.name.asc())
        )
        projects = result.scalars().all()
        project_ids = [str(p.id) for p in projects]

        report_counts: dict[str, int] = {}
        member_counts: dict[str, int] = {}
        if project_ids:
            rc = await db.execute(
                select(Report.project_id, func.count(Report.id))
                .where(
                    Report.project_id.in_(project_ids),
                    Report.status != "archived",
                    Report.deleted_at.is_(None),
                )
                .group_by(Report.project_id)
            )
            report_counts = {str(row[0]): row[1] for row in rc.all()}
            mc = await db.execute(
                select(ResourceGrant.resource_id, func.count(ResourceGrant.id))
                .where(
                    ResourceGrant.resource_type == RESOURCE_TYPE,
                    ResourceGrant.resource_id.in_(project_ids),
                    ResourceGrant.deleted_at.is_(None),
                )
                .group_by(ResourceGrant.resource_id)
            )
            member_counts = {str(row[0]): row[1] for row in mc.all()}

        is_admin = FULL_ADMIN in resolved.org_permissions
        out = []
        for p in projects:
            pid = str(p.id)
            is_owner = str(p.user_id) == str(current_user.id)
            schema = ProjectSchema.model_validate(p)
            schema.user = UserSchema.from_orm(p.user) if p.user else None
            schema.report_count = report_counts.get(pid, 0)
            schema.member_count = member_counts.get(pid, 0)
            schema.is_owner = is_owner
            schema.can_manage = is_owner or is_admin or "manage" in granted.get(pid, set())
            out.append(schema)
        return ProjectListResponse(projects=out)

    async def get_project(
        self, db: AsyncSession, project_id: str, current_user: User, organization: Organization
    ) -> ProjectSchema:
        project = await self.get_project_for_view(db, project_id, current_user, organization)
        return await self._to_schema(db, project, current_user)

    async def update_project(
        self, db: AsyncSession, project_id: str, data: ProjectUpdate,
        current_user: User, organization: Organization,
    ) -> ProjectSchema:
        project = await self._get_project_or_404(db, project_id, organization)
        if not await self.user_can_manage_project(db, current_user, project):
            raise HTTPException(status_code=403, detail="You need manage access on this project")
        if data.name is not None:
            project.name = data.name
        if data.description is not None:
            project.description = data.description
        if data.color is not None:
            project.color = data.color
        if data.access is not None:
            project.access = data.access
        await db.commit()
        await db.refresh(project)
        return await self._to_schema(db, project, current_user)

    async def delete_project(
        self, db: AsyncSession, project_id: str, current_user: User, organization: Organization
    ) -> ProjectSchema:
        """Soft-delete the project; contained reports return to their owners'
        root lists. Only the owner (or a full admin) can delete."""
        from datetime import datetime
        project = await self._get_project_or_404(db, project_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        if str(project.user_id) != str(current_user.id) and FULL_ADMIN not in resolved.org_permissions:
            raise HTTPException(status_code=403, detail="Only the project owner can delete it")

        schema = await self._to_schema(db, project, current_user)
        # Detach reports BEFORE soft-deleting so nothing points at a dead project.
        await db.execute(
            sa_update(Report)
            .where(Report.project_id == str(project.id))
            .values(project_id=None)
        )
        # Revoke grants so re-used ids can never resurrect access.
        await db.execute(
            sa_update(ResourceGrant)
            .where(
                ResourceGrant.resource_type == RESOURCE_TYPE,
                ResourceGrant.resource_id == str(project.id),
                ResourceGrant.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.utcnow())
        )
        project.deleted_at = datetime.utcnow()
        await db.commit()
        return schema

    # ── Members (sharing) ────────────────────────────────────────────────────

    async def list_members(
        self, db: AsyncSession, project_id: str, current_user: User, organization: Organization
    ) -> list[ProjectMemberSchema]:
        project = await self.get_project_for_view(db, project_id, current_user, organization)
        rows = await db.execute(
            select(ResourceGrant, User)
            .join(User, User.id == ResourceGrant.principal_id)
            .where(
                ResourceGrant.resource_type == RESOURCE_TYPE,
                ResourceGrant.resource_id == str(project.id),
                ResourceGrant.principal_type == "user",
                ResourceGrant.deleted_at.is_(None),
            )
        )
        members = [
            ProjectMemberSchema(
                user_id=str(grant.principal_id),
                user_name=user.name,
                user_email=user.email,
                permissions=list(grant.permissions or []),
            )
            for grant, user in rows.all()
        ]
        # The owner is an implicit member with manage.
        owner_row = await db.execute(select(User).where(User.id == str(project.user_id)))
        owner = owner_row.scalar_one_or_none()
        if owner:
            members.insert(0, ProjectMemberSchema(
                user_id=str(owner.id),
                user_name=owner.name,
                user_email=owner.email,
                permissions=["owner"],
            ))
        return members

    async def upsert_member(
        self, db: AsyncSession, project_id: str, payload: ProjectMemberUpsert,
        current_user: User, organization: Organization,
    ) -> list[ProjectMemberSchema]:
        project = await self._get_project_or_404(db, project_id, organization)
        if not await self.user_can_manage_project(db, current_user, project):
            raise HTTPException(status_code=403, detail="You need manage access on this project")
        if str(payload.user_id) == str(project.user_id):
            raise HTTPException(status_code=400, detail="The project owner already has full access")

        # The grantee must be a member of this organization.
        target_row = await db.execute(select(User).where(User.id == str(payload.user_id)))
        target = target_row.scalar_one_or_none()
        if not target or not await principal_belongs_to_org(db, target, str(organization.id)):
            raise HTTPException(status_code=400, detail="User is not a member of this organization")

        existing_row = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == RESOURCE_TYPE,
                ResourceGrant.resource_id == str(project.id),
                ResourceGrant.principal_type == "user",
                ResourceGrant.principal_id == str(payload.user_id),
                ResourceGrant.deleted_at.is_(None),
            )
        )
        grant = existing_row.scalar_one_or_none()
        if grant:
            grant.permissions = list(payload.permissions)
        else:
            db.add(ResourceGrant(
                organization_id=str(organization.id),
                resource_type=RESOURCE_TYPE,
                resource_id=str(project.id),
                principal_type="user",
                principal_id=str(payload.user_id),
                permissions=list(payload.permissions),
            ))
        await db.commit()
        return await self.list_members(db, project_id, current_user, organization)

    async def remove_member(
        self, db: AsyncSession, project_id: str, user_id: str,
        current_user: User, organization: Organization,
    ) -> list[ProjectMemberSchema]:
        from datetime import datetime
        project = await self._get_project_or_404(db, project_id, organization)
        # A member may remove THEMSELF (leave); anything else needs manage.
        if str(user_id) != str(current_user.id):
            if not await self.user_can_manage_project(db, current_user, project):
                raise HTTPException(status_code=403, detail="You need manage access on this project")
        if str(user_id) == str(project.user_id):
            raise HTTPException(status_code=400, detail="The project owner cannot be removed")
        await db.execute(
            sa_update(ResourceGrant)
            .where(
                ResourceGrant.resource_type == RESOURCE_TYPE,
                ResourceGrant.resource_id == str(project.id),
                ResourceGrant.principal_type == "user",
                ResourceGrant.principal_id == str(user_id),
                ResourceGrant.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.utcnow())
        )
        await db.commit()
        return await self.list_members(db, project_id, current_user, organization)

    # ── Report moves ─────────────────────────────────────────────────────────

    async def move_reports(
        self, db: AsyncSession, report_ids: list[str], project_id: str | None,
        current_user: User, organization: Organization,
    ) -> int:
        """Move owned reports into a project (or back to the root when
        project_id is None). Non-owned / cross-org ids are rejected."""
        if not report_ids:
            return 0
        project = None
        if project_id:
            project = await self._get_project_or_404(db, project_id, organization)
            if not await self.user_can_view_project(db, current_user, project):
                raise HTTPException(status_code=404, detail="Project not found")

        rows = await db.execute(
            select(Report).where(
                Report.id.in_([str(r) for r in report_ids]),
                Report.organization_id == str(organization.id),
                Report.deleted_at.is_(None),
            )
        )
        reports = rows.scalars().all()
        found_ids = {str(r.id) for r in reports}
        missing = [str(r) for r in report_ids if str(r) not in found_ids]
        if missing:
            raise HTTPException(status_code=404, detail="Report not found")
        not_owned = [r for r in reports if str(r.user_id) != str(current_user.id)]
        if not_owned:
            raise HTTPException(status_code=403, detail="Only the report owner can move it")

        for r in reports:
            r.project_id = str(project.id) if project else None
        await db.commit()
        return len(reports)

    # ── Internals ────────────────────────────────────────────────────────────

    async def _to_schema(
        self, db: AsyncSession, project: Project, current_user: User
    ) -> ProjectSchema:
        rc = await db.execute(
            select(func.count(Report.id)).where(
                Report.project_id == str(project.id),
                Report.status != "archived",
                Report.deleted_at.is_(None),
            )
        )
        mc = await db.execute(
            select(func.count(ResourceGrant.id)).where(
                ResourceGrant.resource_type == RESOURCE_TYPE,
                ResourceGrant.resource_id == str(project.id),
                ResourceGrant.deleted_at.is_(None),
            )
        )
        schema = ProjectSchema.model_validate(project)
        schema.user = UserSchema.from_orm(project.user) if project.user else None
        schema.report_count = rc.scalar() or 0
        schema.member_count = mc.scalar() or 0
        schema.is_owner = str(project.user_id) == str(current_user.id)
        schema.can_manage = schema.is_owner or await self.user_can_manage_project(db, current_user, project)
        return schema


project_service = ProjectService()
