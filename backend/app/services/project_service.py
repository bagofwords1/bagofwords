"""Projects: private-by-default shared folders that group reports.

Access rules (enforced here, not in route decorators — `project_id` is not a
decorator-mapped param):
- owner  → full control (view/manage/delete)
- ResourceGrant(resource_type='project') with 'view' → see project + its reports
- ... with 'manage' → edit project metadata / members (implies view)
- access == 'org'   → every org member can view
- full_admin_access → everything

A report is IN a project via reports.project_id. Deleting a project archives
its reports (the reversible soft state single-report delete uses — hidden from
all lists, data retained) and stops their scheduled prompts.
"""
from logging import getLogger

from fastapi import HTTPException
from sqlalchemy import select, func, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, project_data_source_association, project_file_association
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
        if data.instructions is not None:
            project.instructions = data.instructions or None
        await db.commit()
        await db.refresh(project)
        return await self._to_schema(db, project, current_user)

    async def delete_project(
        self, db: AsyncSession, project_id: str, current_user: User, organization: Organization
    ) -> ProjectSchema:
        """Soft-delete the project and archive everything in it.

        Deleting the folder behaves like deleting its reports: they are
        archived (the same soft state as single-report delete — hidden from
        every list, data retained) and their scheduled prompts are stopped
        and unscheduled. project_id is kept on the archived rows for lineage.
        Only the owner (or a full admin) can delete.
        """
        from datetime import datetime
        project = await self._get_project_or_404(db, project_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        if str(project.user_id) != str(current_user.id) and FULL_ADMIN not in resolved.org_permissions:
            raise HTTPException(status_code=403, detail="Only the project owner can delete it")

        schema = await self._to_schema(db, project, current_user)

        # Archive contained reports + stop their scheduled prompts (mirrors
        # ReportService.archive_report / bulk_archive semantics exactly).
        contained = (await db.execute(
            select(Report).where(
                Report.project_id == str(project.id),
                Report.report_type == "regular",
                Report.status != "archived",
                Report.deleted_at.is_(None),
            )
        )).scalars().all()
        archived_ids = []
        for r in contained:
            r.status = "archived"
            archived_ids.append(str(r.id))
        if archived_ids:
            from app.services.report_service import ReportService
            await ReportService()._delete_scheduled_prompts_for_reports(db, archived_ids)
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

    async def _agent_names_not_visible_to(
        self, db: AsyncSession, user: User, organization: Organization, data_sources: list,
    ) -> list[str]:
        """Names of the given agents this user cannot SEE (public / DS
        membership / grant / org governance). Visibility — not credentials —
        is the sharing boundary: a user_required agent without personal creds
        yet is still resolvable, the member just connects later."""
        if not data_sources:
            return []
        from app.services.data_source_service import DataSourceService
        visible = await DataSourceService().filter_user_visible_data_sources(
            db, list(data_sources), user, organization
        )
        visible_ids = {str(ds.id) for ds in visible}
        return [ds.name for ds in data_sources if str(ds.id) not in visible_ids]

    async def _get_default_data_sources(self, db: AsyncSession, project_id: str) -> list:
        from app.models.data_source import DataSource
        rows = await db.execute(
            select(DataSource)
            .join(project_data_source_association,
                  project_data_source_association.c.data_source_id == DataSource.id)
            .where(project_data_source_association.c.project_id == str(project_id),
                   DataSource.deleted_at.is_(None))
        )
        return list(rows.scalars().all())

    async def _member_users(self, db: AsyncSession, project: Project) -> list[User]:
        """All users with a live grant on the project (excludes the owner)."""
        rows = await db.execute(
            select(User)
            .join(ResourceGrant, ResourceGrant.principal_id == User.id)
            .where(
                ResourceGrant.resource_type == RESOURCE_TYPE,
                ResourceGrant.resource_id == str(project.id),
                ResourceGrant.principal_type == "user",
                ResourceGrant.deleted_at.is_(None),
            )
        )
        return list(rows.scalars().all())

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

        # Invariant: every member can resolve every project default agent.
        # An invitee who can't see one of the agents would inherit reports
        # and context referencing an agent they can never use — block here.
        blocking = await self._agent_names_not_visible_to(
            db, target, organization, await self._get_default_data_sources(db, project.id)
        )
        if blocking:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot add this member: they don't have access to project agent(s): {', '.join(blocking)}",
            )

        # Load ANY existing row for this key, including soft-deleted ones:
        # remove_member soft-deletes, and the (type, id, principal) unique
        # constraint still holds the key — re-adding a previously removed
        # member must resurrect that row, not insert a duplicate.
        existing_row = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == RESOURCE_TYPE,
                ResourceGrant.resource_id == str(project.id),
                ResourceGrant.principal_type == "user",
                ResourceGrant.principal_id == str(payload.user_id),
            )
        )
        grant = existing_row.scalars().first()
        if grant:
            grant.permissions = list(payload.permissions)
            grant.deleted_at = None
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

    # ── Defaults (agents / files copied onto new reports) ───────────────────

    async def get_default_data_source_ids(self, db: AsyncSession, project_id: str) -> list[str]:
        rows = await db.execute(
            select(project_data_source_association.c.data_source_id).where(
                project_data_source_association.c.project_id == str(project_id)
            )
        )
        return [str(r[0]) for r in rows.all()]

    async def get_default_file_ids(self, db: AsyncSession, project_id: str) -> list[str]:
        rows = await db.execute(
            select(project_file_association.c.file_id).where(
                project_file_association.c.project_id == str(project_id)
            )
        )
        return [str(r[0]) for r in rows.all()]

    async def list_selectable_data_sources(
        self, db: AsyncSession, project_id: str,
        current_user: User, organization: Organization,
    ) -> list:
        """Agents offerable as project defaults: active, visible to the
        CALLER and to EVERY current member — the UI picker draws from this so
        an unresolvable agent is never even shown (the server-side guard in
        set_default_data_sources still backstops stale UIs)."""
        from sqlalchemy.orm import selectinload, lazyload
        from app.models.data_source import DataSource
        from app.services.data_source_service import DataSourceService
        from app.schemas.project_schema import ProjectAgentMini, _connector_key_for_ds

        project = await self._get_project_or_404(db, project_id, organization)
        if not await self.user_can_manage_project(db, current_user, project):
            raise HTTPException(status_code=403, detail="You need manage access on this project")

        rows = await db.execute(
            select(DataSource)
            .options(
                lazyload("*"),
                selectinload(DataSource.connections).options(lazyload("*")),
            )
            .where(
                DataSource.organization_id == str(organization.id),
                DataSource.is_active == True,  # noqa: E712
                DataSource.deleted_at.is_(None),
            )
        )
        svc = DataSourceService()
        candidates = await svc.filter_user_visible_data_sources(
            db, list(rows.scalars().all()), current_user, organization
        )
        for member in await self._member_users(db, project):
            if not candidates:
                break
            visible = await svc.filter_user_visible_data_sources(
                db, candidates, member, organization
            )
            visible_ids = {str(ds.id) for ds in visible}
            candidates = [ds for ds in candidates if str(ds.id) in visible_ids]

        out = []
        for ds in candidates:
            conn = (ds.connections or [None])[0]
            out.append(ProjectAgentMini(
                id=str(ds.id),
                name=ds.name,
                type=getattr(conn, "type", None),
                connector_key=_connector_key_for_ds(conn),
                icon=getattr(ds, "icon", None),
            ))
        return out

    async def set_default_data_sources(
        self, db: AsyncSession, project_id: str, data_source_ids: list[str],
        current_user: User, organization: Organization,
    ):
        from app.models.data_source import DataSource
        from app.services.data_source_service import DataSourceService
        project = await self._get_project_or_404(db, project_id, organization)
        if not await self.user_can_manage_project(db, current_user, project):
            raise HTTPException(status_code=403, detail="You need manage access on this project")

        data_sources = []
        if data_source_ids:
            rows = await db.execute(
                select(DataSource).where(
                    DataSource.id.in_([str(x) for x in data_source_ids]),
                    DataSource.organization_id == str(organization.id),
                    DataSource.deleted_at.is_(None),
                )
            )
            found = rows.scalars().all()
            if len(found) != len(set(str(x) for x in data_source_ids)):
                raise HTTPException(status_code=404, detail="Data source not found")
            # Only agents the setter can actually see may become defaults —
            # a default is copied onto members' reports, so this mirrors the
            # trusted-snapshot gate in report creation.
            data_sources = await DataSourceService().filter_user_visible_data_sources(
                db, list(found), current_user, organization
            )
            if len(data_sources) != len(found):
                raise HTTPException(status_code=403, detail="You don't have access to one of these agents")
            # Invariant (mirror of the invite-side check): every EXISTING
            # member must be able to resolve every default agent. The UI
            # only offers all-member-visible agents, but a stale picker must
            # not slip an unresolvable agent past the server.
            for member in await self._member_users(db, project):
                blocking = await self._agent_names_not_visible_to(
                    db, member, organization, data_sources
                )
                if blocking:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Member {member.name or member.email} doesn't have access to "
                            f"agent(s): {', '.join(blocking)}"
                        ),
                    )

        await db.execute(
            project_data_source_association.delete().where(
                project_data_source_association.c.project_id == str(project.id)
            )
        )
        for ds in data_sources:
            await db.execute(project_data_source_association.insert().values(
                project_id=str(project.id), data_source_id=str(ds.id),
            ))
        await db.commit()
        return await self._to_schema(db, project, current_user)

    async def set_default_files(
        self, db: AsyncSession, project_id: str, file_ids: list[str],
        current_user: User, organization: Organization,
    ):
        from app.models.file import File
        project = await self._get_project_or_404(db, project_id, organization)
        if not await self.user_can_manage_project(db, current_user, project):
            raise HTTPException(status_code=403, detail="You need manage access on this project")

        files = []
        if file_ids:
            rows = await db.execute(
                select(File).where(
                    File.id.in_([str(x) for x in file_ids]),
                    File.organization_id == str(organization.id),
                    File.deleted_at.is_(None),
                )
            )
            files = rows.scalars().all()
            if len(files) != len(set(str(x) for x in file_ids)):
                raise HTTPException(status_code=404, detail="File not found")

        await db.execute(
            project_file_association.delete().where(
                project_file_association.c.project_id == str(project.id)
            )
        )
        for f in files:
            await db.execute(project_file_association.insert().values(
                project_id=str(project.id), file_id=str(f.id),
            ))
        await db.commit()
        return await self._to_schema(db, project, current_user)

    async def get_project_files_for_report(self, db: AsyncSession, report) -> list:
        """Files inherited LIVE from the report's project (empty when the
        report is outside any project). Files are context material, not access
        scope, so unlike default agents they are resolved at read time —
        adding/removing a file on the project applies to every report in it
        immediately, and moving a report in/out changes its file space."""
        project_id = getattr(report, "project_id", None)
        if not project_id:
            return []
        from app.models.file import File
        rows = await db.execute(
            select(File)
            .join(project_file_association, project_file_association.c.file_id == File.id)
            .where(
                project_file_association.c.project_id == str(project_id),
                File.deleted_at.is_(None),
            )
        )
        return list(rows.scalars().all())

    async def list_automations(
        self, db: AsyncSession, project_id: str, current_user: User, organization: Organization
    ) -> list[dict]:
        """Automations of the project, derived through reports.project_id:
        scheduled tasks (ScheduledPrompt) and dashboard refreshes
        (report.cron_schedule). Standalone triggers join here once they carry
        a project binding."""
        project = await self.get_project_for_view(db, project_id, current_user, organization)
        from app.models.scheduled_prompt import ScheduledPrompt
        items: list[dict] = []
        sp_rows = await db.execute(
            select(ScheduledPrompt, Report.title, Report.id)
            .join(Report, Report.id == ScheduledPrompt.report_id)
            .where(
                Report.project_id == str(project.id),
                Report.status != "archived",
                Report.deleted_at.is_(None),
                ScheduledPrompt.deleted_at.is_(None),
            )
        )
        for sp, report_title, report_id in sp_rows.all():
            prompt = sp.prompt or {}
            items.append({
                "id": str(sp.id),
                "kind": "task",
                "report_id": str(report_id),
                "report_title": report_title or "untitled",
                "label": (prompt.get("content") or "")[:120] or (report_title or "untitled"),
                "cron_schedule": sp.cron_schedule,
                "is_active": bool(sp.is_active),
            })
        rf_rows = await db.execute(
            select(Report.id, Report.title, Report.cron_schedule)
            .where(
                Report.project_id == str(project.id),
                Report.cron_schedule.isnot(None),
                Report.status != "archived",
                Report.deleted_at.is_(None),
            )
        )
        for report_id, report_title, cron in rf_rows.all():
            items.append({
                "id": f"refresh-{report_id}",
                "kind": "refresh",
                "report_id": str(report_id),
                "report_title": report_title or "untitled",
                "label": report_title or "untitled",
                "cron_schedule": cron,
                "is_active": True,
            })
        return items

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
        # Dashboard count mirrors the project page's dashboards section
        # (reports with any artifact — same subquery as get_reports'
        # has_artifacts filter); automations = scheduled tasks + refreshes.
        from app.models.artifact import Artifact
        from app.models.scheduled_prompt import ScheduledPrompt
        dc = await db.execute(
            select(func.count(Report.id)).where(
                Report.project_id == str(project.id),
                Report.status != "archived",
                Report.deleted_at.is_(None),
                Report.id.in_(
                    select(Artifact.report_id).where(Artifact.report_id.isnot(None))
                ),
            )
        )
        ac_tasks = await db.execute(
            select(func.count(ScheduledPrompt.id))
            .join(Report, Report.id == ScheduledPrompt.report_id)
            .where(
                Report.project_id == str(project.id),
                Report.status != "archived",
                Report.deleted_at.is_(None),
                ScheduledPrompt.deleted_at.is_(None),
            )
        )
        ac_refresh = await db.execute(
            select(func.count(Report.id)).where(
                Report.project_id == str(project.id),
                Report.cron_schedule.isnot(None),
                Report.status != "archived",
                Report.deleted_at.is_(None),
            )
        )
        schema = ProjectSchema.model_validate(project)
        schema.user = UserSchema.from_orm(project.user) if project.user else None
        schema.report_count = rc.scalar() or 0
        schema.member_count = mc.scalar() or 0
        schema.dashboard_count = dc.scalar() or 0
        schema.automation_count = (ac_tasks.scalar() or 0) + (ac_refresh.scalar() or 0)
        schema.is_owner = str(project.user_id) == str(current_user.id)
        schema.can_manage = schema.is_owner or await self.user_can_manage_project(db, current_user, project)

        # Defaults (agents/files) for the settings tab.
        from sqlalchemy.orm import selectinload, lazyload
        from app.models.data_source import DataSource
        from app.models.file import File
        from app.schemas.project_schema import ProjectAgentMini, ProjectFileMini, _connector_key_for_ds
        ds_rows = await db.execute(
            select(DataSource)
            .options(
                lazyload("*"),
                selectinload(DataSource.connections).options(lazyload("*")),
            )
            .join(project_data_source_association,
                  project_data_source_association.c.data_source_id == DataSource.id)
            .where(project_data_source_association.c.project_id == str(project.id),
                   DataSource.deleted_at.is_(None))
        )
        schema.data_sources = []
        for ds in ds_rows.scalars().all():
            conn = (ds.connections or [None])[0]
            schema.data_sources.append(ProjectAgentMini(
                id=str(ds.id),
                name=ds.name,
                type=getattr(conn, "type", None),
                connector_key=_connector_key_for_ds(conn),
                icon=getattr(ds, "icon", None),
            ))
        f_rows = await db.execute(
            select(File.id, File.filename)
            .join(project_file_association,
                  project_file_association.c.file_id == File.id)
            .where(project_file_association.c.project_id == str(project.id),
                   File.deleted_at.is_(None))
        )
        schema.files = [ProjectFileMini(id=str(r[0]), filename=r[1]) for r in f_rows.all()]
        return schema


project_service = ProjectService()
