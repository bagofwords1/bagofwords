"""Prompt model service: access-aware list/get + CRUD.

Visibility is scoped to the caller's agent access (active data sources only):
  - global  → every org member
  - private → owner only
  - agent   → users with access to ALL of the prompt's ACTIVE agents

Write policy (authorize_write — one source of truth for create AND update):
  - private → any member; every referenced data source must be VISIBLE to
              the author (public or explicit membership/grant), or none
  - agent   → `manage` on every referenced agent (the same grant that gates
              editing the agent itself)
  - global  → full_admin only
The routes invoke authorize_write explicitly (route-layer enforcement, with
access.denied audit logging); create/update re-run it as a backstop because
the AI training tools call this service directly, bypassing the routes.
No UI, scheduling, or delivery here — just the model.
"""
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.models.prompt import Prompt
from app.models.prompt_run import PromptRun
from app.models.data_source import DataSource
from app.models.user import User
from app.models.organization import Organization
from app.schemas.prompt_schema import PromptCreate, PromptUpdate
from app.core.permission_resolver import resolve_permissions, ResolvedPermissions, FULL_ADMIN

# Mirrors the frontend usePromptFill.substitute placeholder regex.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")

logger = logging.getLogger(__name__)


class PromptService:

    # ── access helpers ──

    @staticmethod
    def _ds_ids(prompt: Prompt) -> List[str]:
        return [str(ds.id) for ds in (prompt.data_sources or [])]

    @staticmethod
    def _active_data_sources(prompt: Prompt) -> List[DataSource]:
        """Agents still usable by this prompt: connected/healthy (``is_active``),
        not soft-deleted, and not intentionally turned off
        (``publish_status == 'disabled'``). Hard-deleted agents never appear
        here — the relationship stops yielding a DataSource once its row is gone.
        """
        return [
            ds for ds in (prompt.data_sources or [])
            if getattr(ds, 'is_active', False)
            and getattr(ds, 'deleted_at', None) is None
            and getattr(ds, 'publish_status', 'published') != 'disabled'
        ]

    @staticmethod
    def _can_access_all(data_sources: List[DataSource], member_ds_ids: set) -> bool:
        """Principal can use every agent: the data source is public OR they hold
        an EXPLICIT membership/grant on it (``member_ds_ids``).

        This mirrors the default /agents list (data_source_service.
        get_data_sources): the membership filter applies to admins too. Unlike
        ResolvedPermissions.has_resource_membership, a full_admin does NOT
        implicitly satisfy this — the member set is built from
        get_member_data_source_ids, which deliberately does not short-circuit on
        full_admin_access. Admins keep their manage/capability bypass on the
        write and can_manage paths; only agent-prompt READ visibility is scoped
        here so it matches what the admin sees on /agents."""
        return all(
            getattr(ds, 'is_public', False) or str(ds.id) in member_ds_ids
            for ds in data_sources
        )

    @staticmethod
    async def _member_ds_ids(db: AsyncSession, user_id: str, organization: Organization) -> set:
        """Explicit-membership data-source ids for a principal, matching the
        default /agents list. Does NOT short-circuit on full_admin — that is the
        whole point of scoping agent-prompt visibility to what the admin has
        actually joined."""
        from app.core.permission_resolver import get_member_data_source_ids
        return {
            str(ds_id)
            for ds_id in await get_member_data_source_ids(db, str(user_id), str(organization.id))
        }

    @staticmethod
    def _can_manage_all(resolved: ResolvedPermissions, ds_ids: List[str]) -> bool:
        if FULL_ADMIN in resolved.org_permissions:
            return True
        if not ds_ids:
            return False
        return all(resolved.has_resource_permission('data_source', ds, 'manage') for ds in ds_ids)

    def _is_visible(self, p: Prompt, user_id: str, active_dss: List[DataSource], member_ds_ids: set) -> bool:
        """Whether ``user_id`` can resolve this prompt — the single source of
        truth for both the prompt list/get and run-for target eligibility.

        Note there is intentionally no full_admin short-circuit: agent-scoped
        prompts are visible only to explicit members of ALL their active agents
        (public agents count), so an admin sees the same agent prompts they see
        agents for on /agents — no more. global/private and the write/manage
        paths keep the admin bypass elsewhere."""
        if p.scope == 'global':
            return True
        # The owner always sees their own prompt, whatever its scope — this is
        # never a leak, and it keeps an author (incl. an admin who created an
        # agent prompt via the manage bypass without joining the agent) from
        # being locked out of the thing they just wrote.
        if str(p.user_id) == str(user_id):
            return True
        if p.scope == 'private':
            return False
        # agent: visible to members of ALL active agents; hidden once none remain.
        if not active_dss:
            return False
        return self._can_access_all(active_dss, member_ds_ids)

    def _to_response(self, p: Prompt, resolved: ResolvedPermissions, user_id: str) -> dict:
        ds_ids = self._ds_ids(p)
        can_manage = (
            FULL_ADMIN in resolved.org_permissions
            or str(p.user_id) == str(user_id)
            or (p.scope == 'agent' and self._can_manage_all(resolved, ds_ids))
        )
        return {
            "id": p.id, "title": p.title, "text": p.text, "mode": p.mode,
            "model_id": p.model_id, "mentions": p.mentions, "parameters": p.parameters,
            "scope": p.scope, "is_starter": p.is_starter, "data_source_ids": ds_ids,
            "user_id": p.user_id, "created_at": p.created_at, "can_manage": can_manage,
        }

    # ── reads ──

    async def list_prompts(
        self, db: AsyncSession, current_user: User, organization: Organization,
        category: Optional[str] = None, starters_only: bool = False,
        data_source_id: Optional[str] = None,
        created_by: Optional[str] = None, scope: Optional[str] = None,
        search: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        member_ds_ids = await self._member_ds_ids(db, current_user.id, organization)
        q = (
            select(Prompt)
            .filter(Prompt.organization_id == organization.id)
            .filter(Prompt.deleted_at == None)
        )
        if starters_only:
            q = q.filter(Prompt.is_starter == True)
        if data_source_id:
            q = q.join(Prompt.data_sources).filter(DataSource.id == data_source_id)
        if created_by:
            q = q.filter(Prompt.user_id == created_by)
        if scope:
            q = q.filter(Prompt.scope == scope)
        if search:
            term = f"%{search.strip().lower()}%"
            q = q.filter(or_(
                func.lower(func.coalesce(Prompt.title, '')).like(term),
                func.lower(func.coalesce(Prompt.text, '')).like(term),
            ))
        rows = await db.execute(q)
        prompts = list(rows.scalars().unique().all())

        visible = []
        for p in prompts:
            active_dss = self._active_data_sources(p)
            if not self._is_visible(p, str(current_user.id), active_dss, member_ds_ids):
                continue
            # Hide agent-scoped prompts whose agents are ALL gone/unusable
            # (inactive, disabled, or deleted). They can't be run and would
            # otherwise surface a bare agent id in the UI. Global/private
            # prompts are never gated on agents.
            if p.scope == 'agent' and not active_dss:
                continue
            visible.append(self._to_response(p, resolved, str(current_user.id)))
        visible.sort(key=lambda r: r["created_at"] or datetime.min, reverse=True)
        # Visibility is resolved in Python, so the limit must apply here
        # rather than in SQL. meta.total stays the pre-limit visible count.
        total = len(visible)
        if limit is not None:
            visible = visible[:limit]
        return {"prompts": visible, "meta": {"total": total}}

    async def get_prompt_or_404(self, db, prompt_id, organization) -> Prompt:
        rows = await db.execute(
            select(Prompt)
            .filter(Prompt.id == prompt_id)
            .filter(Prompt.organization_id == organization.id)
            .filter(Prompt.deleted_at == None)
        )
        p = rows.scalar_one_or_none()
        if not p:
            raise HTTPException(status_code=404, detail="Prompt not found")
        return p

    async def get_prompt_response(self, db, prompt_id, current_user, organization) -> dict:
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        member_ds_ids = await self._member_ds_ids(db, current_user.id, organization)
        if not self._is_visible(p, str(current_user.id), self._active_data_sources(p), member_ds_ids):
            raise HTTPException(status_code=403, detail="Access denied to this prompt")
        return self._to_response(p, resolved, str(current_user.id))

    # ── writes ──

    PROMPT_SCOPES = ('private', 'agent', 'global')

    @staticmethod
    async def _audit_denied(db, current_user, organization, detail: str, endpoint: str) -> None:
        """Fire-and-forget access.denied audit entry, mirroring the
        permissions_decorator helper so service-side denials are visible in
        the audit log like decorator-based ones."""
        try:
            from app.ee.audit.service import audit_service
            await audit_service.log(
                db=db, organization_id=str(organization.id), action="access.denied",
                user_id=str(current_user.id), resource_type="prompt",
                details={"detail": detail, "endpoint": endpoint},
            )
        except Exception:
            logger.debug("prompt access.denied audit failed", exc_info=True)

    async def authorize_write(
        self, db: AsyncSession, current_user: User, organization: Organization,
        *, scope: str, ds_ids: List[str], endpoint: str = 'prompt.write',
    ) -> List[DataSource]:
        """Enforce the prompt write policy for a (scope, data_source_ids) pair.

        private → any member, but every data source must be visible to the
                  author (same filter report-create uses, so public data
                  sources count); empty list is fine.
        agent   → at least one agent and `manage` on all of them.
        global  → full_admin only.

        Returns the loaded (org-scoped, active) DataSource rows so callers can
        attach them without re-querying. Raises 404 for unknown/inactive ids,
        400 for a malformed scope/agent-list, 403 on policy denials (audited).
        """
        if scope not in self.PROMPT_SCOPES:
            raise HTTPException(status_code=400, detail=f"invalid scope '{scope}'")
        ds_ids = [str(i) for i in (ds_ids or [])]
        data_sources = await self._load_data_sources(db, ds_ids, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))

        if scope == 'global':
            if FULL_ADMIN not in resolved.org_permissions:
                detail = "admin required to create a global prompt"
                await self._audit_denied(db, current_user, organization, detail, endpoint)
                raise HTTPException(status_code=403, detail=detail)
        elif scope == 'agent':
            if not ds_ids:
                raise HTTPException(status_code=400, detail="an agent prompt must reference at least one agent")
            if not self._can_manage_all(resolved, ds_ids):
                detail = "manage permission required on all agents"
                await self._audit_denied(db, current_user, organization, detail, endpoint)
                raise HTTPException(status_code=403, detail=detail)
        else:  # private
            if data_sources:
                from app.services.data_source_service import DataSourceService
                visible = await DataSourceService().filter_user_visible_data_sources(
                    db, data_sources, current_user, organization
                )
                visible_ids = {str(ds.id) for ds in visible}
                if any(str(ds.id) not in visible_ids for ds in data_sources):
                    detail = "access denied to one or more data sources"
                    await self._audit_denied(db, current_user, organization, detail, endpoint)
                    raise HTTPException(status_code=403, detail=detail)
        return data_sources

    async def _load_data_sources(self, db, ds_ids: List[str], organization: Organization) -> List[DataSource]:
        if not ds_ids:
            return []
        rows = await db.execute(
            select(DataSource)
            .filter(DataSource.id.in_(ds_ids))
            .filter(DataSource.organization_id == organization.id)
            .filter(DataSource.is_active == True)
            .filter(DataSource.deleted_at == None)
        )
        found = list(rows.scalars().unique().all())
        if len(found) != len(set(ds_ids)):
            raise HTTPException(status_code=404, detail="One or more data sources not found or inactive")
        return found

    @staticmethod
    def _params_to_json(parameters) -> Optional[list]:
        if not parameters:
            return None
        return [p.dict() if hasattr(p, 'dict') else dict(p) for p in parameters]

    async def create_prompt(self, db, data: PromptCreate, current_user, organization) -> Prompt:
        data_sources = await self.authorize_write(
            db, current_user, organization,
            scope=data.scope, ds_ids=data.data_source_ids, endpoint='prompt.create',
        )
        p = Prompt(
            title=data.title, text=data.text, mode=data.mode, model_id=data.model_id,
            mentions=data.mentions, parameters=self._params_to_json(data.parameters),
            scope=data.scope, is_starter=data.is_starter,
            user_id=current_user.id, organization_id=organization.id,
        )
        p.data_sources = data_sources
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p

    async def authorize_update(self, db, prompt_id, data: PromptUpdate, current_user, organization) -> None:
        """Pure pre-flight authorization for update_prompt (no mutation).

        1. Editability: owner, full_admin, or `manage` on all the prompt's agents.
        2. Re-run authorize_write against the POST-merge (scope, data_sources)
           whenever either changes — an owner must not promote private→agent/
           global or swap in data sources their own role couldn't have used at
           create time. Edits that leave scope+agents untouched (title, text,
           params) stay owner-editable even if the owner since lost `manage`.
        """
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        can_edit = (
            FULL_ADMIN in resolved.org_permissions
            or str(p.user_id) == str(current_user.id)
            or (p.scope == 'agent' and self._can_manage_all(resolved, self._ds_ids(p)))
        )
        if not can_edit:
            detail = "manage permission required"
            await self._audit_denied(db, current_user, organization, detail, 'prompt.update')
            raise HTTPException(status_code=403, detail=detail)

        fields = data.dict(exclude_unset=True)
        new_ds_ids = fields.get('data_source_ids')
        effective_scope = fields.get('scope', p.scope)
        current_ds_ids = self._ds_ids(p)
        effective_ds_ids = [str(i) for i in new_ds_ids] if new_ds_ids is not None else current_ds_ids
        scope_changed = effective_scope != p.scope
        ds_changed = new_ds_ids is not None and set(effective_ds_ids) != set(current_ds_ids)
        if scope_changed or ds_changed:
            await self.authorize_write(
                db, current_user, organization,
                scope=effective_scope, ds_ids=effective_ds_ids, endpoint='prompt.update',
            )

    async def update_prompt(self, db, prompt_id, data: PromptUpdate, current_user, organization) -> Prompt:
        await self.authorize_update(db, prompt_id, data, current_user, organization)
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        fields = data.dict(exclude_unset=True)
        new_ds_ids = fields.pop('data_source_ids', None)
        if 'parameters' in fields:
            fields['parameters'] = self._params_to_json(data.parameters)
        for k, v in fields.items():
            setattr(p, k, v)
        if new_ds_ids is not None:
            p.data_sources = await self._load_data_sources(db, new_ds_ids, organization)
        await db.commit()
        await db.refresh(p)
        return p

    async def delete_prompt(self, db, prompt_id, current_user, organization) -> None:
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        can_edit = (
            FULL_ADMIN in resolved.org_permissions
            or str(p.user_id) == str(current_user.id)
            or (p.scope == 'agent' and self._can_manage_all(resolved, self._ds_ids(p)))
        )
        if not can_edit:
            raise HTTPException(status_code=403, detail="manage permission required")
        p.deleted_at = datetime.utcnow()
        await db.commit()

    # ── run-time parameter substitution ──

    @staticmethod
    def substitute(text: str, values: Optional[Dict[str, Any]]) -> str:
        """Replace every `{{name}}` placeholder in `text` with the matching value.

        Mirrors the frontend usePromptFill.substitute semantics:
          - a date_range value {start, end} → "<start> to <end>"
          - missing values (None / absent) collapse the placeholder to ''
        """
        if not text:
            return ''
        values = values or {}

        def _replace(m: 're.Match') -> str:
            name = m.group(1).strip()
            v = values.get(name)
            if v is None:
                return ''
            if isinstance(v, dict):
                start = v.get('start') or ''
                end = v.get('end') or ''
                if not start and not end:
                    return ''
                return f"{start} to {end}"
            return str(v)

        return _PLACEHOLDER_RE.sub(_replace, text)

    # ── run (self) ──

    async def run_prompt(
        self, db: AsyncSession, prompt_id: str, current_user: User,
        organization: Organization, parameters: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Run a prompt as the caller: create a new report owned by the caller,
        seed it with the prompt's data sources + mode, kick off the first
        completion (substituted text), and record a prompt_runs row.

        Authz: the caller must be able to RESOLVE (see) the prompt — enforced
        by get_prompt_response which raises 403 otherwise.
        """
        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
        from app.schemas.report_schema import ReportCreate
        from app.services.report_service import ReportService
        from app.services.completion_service import CompletionService

        # 403 if not visible to the caller.
        await self.get_prompt_response(db, prompt_id, current_user, organization)
        prompt = await self.get_prompt_or_404(db, prompt_id, organization)

        text = self.substitute(prompt.text, parameters)

        report = await self._create_report_for(
            db, prompt, current_user, organization, ReportService(), ReportCreate
        )

        await CompletionService().create_completion(
            db,
            report.id,
            CompletionCreate(prompt=PromptSchema(
                content=text,
                mentions=prompt.mentions,
                mode=prompt.mode,
                model_id=prompt.model_id,
            )),
            current_user=current_user,
            organization=organization,
            background=True,
        )

        await self._record_run(
            db, prompt_id=prompt.id, user_id=current_user.id,
            actor_id=current_user.id, report_id=report.id, parameters=parameters,
        )
        return {"report_id": str(report.id)}

    async def _create_report_for(self, db, prompt, owner, organization, report_service, ReportCreate):
        """Create a report owned by `owner`, seeded with the prompt's data sources
        and mode. New reports are owner-private (artifact_visibility='none') by
        default — see Step-0 privacy invariant — which we rely on for run-for."""
        report = await report_service.create_report(
            db,
            ReportCreate(
                title=(prompt.title or (prompt.text or '')[:60] or 'Prompt run'),
                data_sources=self._ds_ids(prompt),
            ),
            owner,
            organization,
        )
        # ReportCreate has no `mode`; set it from the prompt on the ORM row.
        if getattr(prompt, 'mode', None):
            from app.models.report import Report
            row = (await db.execute(select(Report).filter(Report.id == report.id))).scalar_one_or_none()
            if row is not None:
                row.mode = prompt.mode
                await db.commit()
        return report

    async def _record_run(self, db, *, prompt_id, user_id, actor_id, report_id, parameters):
        run = PromptRun(
            prompt_id=str(prompt_id), user_id=str(user_id), actor_id=str(actor_id),
            report_id=str(report_id) if report_id else None,
            parameters=parameters or None,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run

    # ── run for (admin: run-on-behalf) ──

    async def run_for_targets(
        self, db: AsyncSession, prompt_id: str, current_user: User,
        organization: Organization,
    ) -> dict:
        """Eligible run-for targets for this prompt, for the run-for picker.

        A member is eligible when they could RESOLVE the prompt themselves —
        the exact per-target check run_prompt_for applies — so the modal only
        offers targets that would actually run instead of silently skipping.
        Groups are returned with an eligible_count so empty ones can be hidden.

        Authz mirrors run_prompt_for: full_admin or `manage` on all agents.
        """
        from app.models.membership import Membership
        from app.models.group import Group
        from app.models.group_membership import GroupMembership

        prompt = await self.get_prompt_or_404(db, prompt_id, organization)
        actor_resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        if not self._can_manage_all(actor_resolved, self._ds_ids(prompt)):
            raise HTTPException(status_code=403, detail="manage permission required on the prompt's agents")

        active_dss = self._active_data_sources(prompt)

        member_rows = await db.execute(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .filter(Membership.organization_id == organization.id)
        )
        members = list(member_rows.scalars().unique().all())

        eligible_ids: set = set()
        users = []
        for u in members:
            t_member_ds_ids = await self._member_ds_ids(db, u.id, organization)
            if not self._is_visible(prompt, str(u.id), active_dss, t_member_ds_ids):
                continue
            eligible_ids.add(str(u.id))
            users.append({"id": str(u.id), "name": u.name, "email": u.email})

        group_rows = await db.execute(
            select(Group)
            .filter(Group.organization_id == organization.id)
            .filter(Group.deleted_at == None)
        )
        gm_rows = await db.execute(
            select(GroupMembership.group_id, GroupMembership.user_id)
            .join(Group, Group.id == GroupMembership.group_id)
            .filter(Group.organization_id == organization.id)
            .filter(GroupMembership.user_id != None)
            .filter(GroupMembership.deleted_at == None)
        )
        by_group: Dict[str, List[str]] = {}
        for gid, uid in gm_rows.all():
            by_group.setdefault(str(gid), []).append(str(uid))

        groups = []
        for g in group_rows.scalars().all():
            member_ids = by_group.get(str(g.id), [])
            groups.append({
                "id": str(g.id), "name": g.name,
                "member_count": len(member_ids),
                "eligible_count": sum(1 for uid in member_ids if uid in eligible_ids),
            })

        return {"users": users, "groups": groups}

    async def run_prompt_for(
        self, db: AsyncSession, prompt_id: str, current_user: User,
        organization: Organization, principal_type: str,
        user_ids: Optional[List[str]] = None, group_id: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Admin run-on-behalf. For each eligible target user, create a report
        owned by + private to that target, run as that target, record a
        prompt_runs row (actor=admin), and notify the target.

        Authz: full_admin OR `manage` on ALL the prompt's agents.
        Eligibility: each target must be able to RESOLVE the prompt; the rest
        are reported as skipped.
        """
        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
        from app.schemas.report_schema import ReportCreate
        from app.services.report_service import ReportService
        from app.services.completion_service import CompletionService
        from app.services.inbox_service import inbox_service
        from app.models.notification import SOURCE_SCHEDULE
        from app.ee.audit.service import audit_service

        prompt = await self.get_prompt_or_404(db, prompt_id, organization)

        actor_resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        if not self._can_manage_all(actor_resolved, self._ds_ids(prompt)):
            raise HTTPException(status_code=403, detail="manage permission required on the prompt's agents")

        target_ids = await self._expand_principal(
            db, organization, principal_type, user_ids, group_id
        )

        report_service = ReportService()
        completion_service = CompletionService()
        active_dss = self._active_data_sources(prompt)

        ran: List[str] = []
        skipped: List[str] = []
        actor_name = getattr(current_user, 'name', None) or getattr(current_user, 'email', None) or 'An admin'
        prompt_title = prompt.title or (prompt.text or '')[:60] or 'a prompt'
        text = self.substitute(prompt.text, parameters)

        for tid in target_ids:
            target = await db.get(User, tid)
            if target is None:
                skipped.append(str(tid))
                continue
            t_member_ds_ids = await self._member_ds_ids(db, tid, organization)
            if not self._is_visible(prompt, str(tid), active_dss, t_member_ds_ids):
                skipped.append(str(tid))
                continue

            report = await self._create_report_for(
                db, prompt, target, organization, report_service, ReportCreate
            )

            await completion_service.create_completion(
                db,
                report.id,
                CompletionCreate(prompt=PromptSchema(
                    content=text,
                    mentions=prompt.mentions,
                    mode=prompt.mode,
                    model_id=prompt.model_id,
                )),
                current_user=target,
                organization=organization,
                background=True,
            )

            await self._record_run(
                db, prompt_id=prompt.id, user_id=tid,
                actor_id=current_user.id, report_id=report.id, parameters=parameters,
            )

            # Transparency: tell the target an admin ran a prompt for them.
            try:
                await inbox_service.notify_users(
                    db,
                    organization_id=str(organization.id),
                    user_ids=[str(tid)],
                    source=SOURCE_SCHEDULE,
                    type="prompt_run_for",
                    title=f"{actor_name} ran '{prompt_title}' for you",
                    body=f"{actor_name} ran the prompt '{prompt_title}' for you. Open the report to see the results.",
                    actor_user_id=str(current_user.id),
                    link=f"/reports/{report.id}",
                    subject={"kind": "report", "report_id": str(report.id), "prompt_id": str(prompt.id)},
                    group_key=f"prompt_run_for:{report.id}",
                )
            except Exception:
                logger.exception("run-for: inbox notification failed for user=%s", tid)

            ran.append(str(tid))

        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="prompt.run_for",
                user_id=str(current_user.id),
                resource_type="prompt",
                resource_id=str(prompt.id),
                details={
                    "principal_type": principal_type,
                    "group_id": str(group_id) if group_id else None,
                    "ran": ran,
                    "skipped": skipped,
                },
            )
        except Exception:
            logger.exception("run-for: audit log failed for prompt=%s", prompt.id)

        return {"ran": len(ran), "skipped": len(skipped), "skipped_user_ids": skipped}

    async def _expand_principal(
        self, db, organization, principal_type, user_ids, group_id,
    ) -> List[str]:
        """Resolve a principal spec into a de-duplicated list of org user ids."""
        from app.models.membership import Membership

        if principal_type == 'users':
            ids = [str(u) for u in (user_ids or [])]
        elif principal_type == 'group':
            if not group_id:
                raise HTTPException(status_code=400, detail="group_id is required for principal_type 'group'")
            from app.models.group_membership import GroupMembership
            rows = await db.execute(
                select(GroupMembership.user_id)
                .filter(GroupMembership.group_id == group_id)
                .filter(GroupMembership.user_id != None)
                .filter(GroupMembership.deleted_at == None)
            )
            ids = [str(r[0]) for r in rows.all() if r[0]]
        else:
            raise HTTPException(status_code=400, detail="principal_type must be 'users' or 'group'")

        if not ids:
            return []

        # Restrict to current org members and de-dup.
        member_rows = await db.execute(
            select(Membership.user_id)
            .filter(Membership.organization_id == organization.id)
            .filter(Membership.user_id.in_(ids))
        )
        org_member_ids = {str(r[0]) for r in member_rows.all()}
        seen = set()
        out = []
        for i in ids:
            if i in org_member_ids and i not in seen:
                seen.add(i)
                out.append(i)
        return out

    # ── conversation-starter absorption (helper for the next phase) ──

    async def materialize_starters_for_data_source(self, db, data_source: DataSource) -> int:
        """Turn a data source's `conversation_starters` strings into agent-scoped
        starter Prompts. Idempotent. Returns count created."""
        starters = getattr(data_source, 'conversation_starters', None) or []
        if not isinstance(starters, list) or not starters:
            return 0
        existing_rows = await db.execute(
            select(Prompt.text).join(Prompt.data_sources)
            .filter(DataSource.id == data_source.id)
            .filter(Prompt.is_starter == True)
            .filter(Prompt.deleted_at == None)
        )
        existing = {t for (t,) in existing_rows.all()}
        created = 0
        for raw in starters:
            text = raw if isinstance(raw, str) else (raw.get('value') if isinstance(raw, dict) else None)
            if not text or text in existing:
                continue
            p = Prompt(
                title=text[:60], text=text, scope='agent', is_starter=True, mode='chat',
                organization_id=data_source.organization_id,
                user_id=getattr(data_source, 'owner_user_id', None),
            )
            p.data_sources = [data_source]
            db.add(p)
            existing.add(text)
            created += 1
        if created:
            await db.commit()
        return created


prompt_service = PromptService()
