"""Org prompt catalog: browse/top, CRUD, run-now, subscribe, assign.

Visibility and authoring/assigning are scoped per-agent (data_source) via the
RBAC resolver. Subscriptions reuse the existing ScheduledPrompt engine — each
subscription is one ScheduledPrompt that runs AS its target user and (optionally)
delivers to a channel.
"""
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.models.prompt import Prompt
from app.models.scheduled_prompt import ScheduledPrompt
from app.models.data_source import DataSource
from app.models.user import User
from app.models.organization import Organization
from app.models.membership import Membership
from app.models.group import Group
from app.models.group_membership import GroupMembership
from app.schemas.prompt_catalog_schema import (
    PromptCatalogCreate, PromptCatalogUpdate, SubscribeRequest, AssignRequest,
)
from app.core.permission_resolver import resolve_permissions, ResolvedPermissions, FULL_ADMIN

logger = logging.getLogger(__name__)


class PromptCatalogService:

    # ───────────────────────── access helpers ─────────────────────────

    @staticmethod
    def _can_access_all_agents(resolved: ResolvedPermissions, ds_ids: List[str]) -> bool:
        """Visible/runnable only if the user can access ALL of the prompt's agents."""
        if not ds_ids:
            return True  # agent-scoped prompt with no agents = org-visible
        return all(resolved.has_resource_membership('data_source', ds) for ds in ds_ids)

    @staticmethod
    def _holds_on_all_agents(resolved: ResolvedPermissions, ds_ids: List[str], perm: str) -> bool:
        if FULL_ADMIN in resolved.org_permissions:
            return True
        if not ds_ids:
            return False  # no agents → only full admin may manage/assign
        return all(resolved.has_resource_permission('data_source', ds, perm) for ds in ds_ids)

    def _is_visible(self, resolved: ResolvedPermissions, prompt: Prompt, user_id: str, ds_ids: List[str]) -> bool:
        if FULL_ADMIN in resolved.org_permissions:
            return True
        if prompt.scope == 'private':
            return str(prompt.user_id) == str(user_id)
        # scope == 'agent'
        return self._can_access_all_agents(resolved, ds_ids)

    @staticmethod
    def _ds_ids(prompt: Prompt) -> List[str]:
        return [str(ds.id) for ds in (prompt.data_sources or [])]

    # ───────────────────────── catalog reads ─────────────────────────

    async def list_prompts(
        self, db: AsyncSession, current_user: User, organization: Organization,
        sort: str = 'recent', category: Optional[str] = None, starters_only: bool = False,
    ) -> dict:
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))

        q = (
            select(Prompt)
            .filter(Prompt.organization_id == organization.id)
            .filter(Prompt.deleted_at == None)
        )
        if category:
            q = q.filter(Prompt.category == category)
        if starters_only:
            q = q.filter(Prompt.is_starter == True)
        result = await db.execute(q)
        prompts = list(result.scalars().unique().all())

        # subscriber counts (active subscriptions per prompt)
        counts = await self._subscriber_counts(db, [p.id for p in prompts])

        visible = []
        for p in prompts:
            ds_ids = self._ds_ids(p)
            if not self._is_visible(resolved, p, str(current_user.id), ds_ids):
                continue
            visible.append(self._to_response(p, ds_ids, resolved, counts.get(p.id, 0)))

        if sort == 'top':
            visible.sort(key=lambda r: r['subscriber_count'], reverse=True)
        else:
            visible.sort(key=lambda r: r['created_at'] or datetime.min, reverse=True)

        return {"prompts": visible, "meta": {"total": len(visible)}}

    async def _subscriber_counts(self, db: AsyncSession, prompt_ids: List[str]) -> dict:
        if not prompt_ids:
            return {}
        rows = await db.execute(
            select(ScheduledPrompt.prompt_id, func.count())
            .filter(ScheduledPrompt.prompt_id.in_(prompt_ids))
            .filter(ScheduledPrompt.is_active == True)
            .filter(ScheduledPrompt.deleted_at == None)
            .group_by(ScheduledPrompt.prompt_id)
        )
        return {pid: cnt for pid, cnt in rows.all()}

    def _to_response(self, p: Prompt, ds_ids: List[str], resolved: ResolvedPermissions, sub_count: int) -> dict:
        return {
            "id": p.id, "title": p.title, "text": p.text, "mode": p.mode,
            "model_id": p.model_id, "mentions": p.mentions, "scope": p.scope,
            "is_starter": p.is_starter, "status": p.status,
            "default_cron": p.default_cron, "default_channel": p.default_channel,
            "category": p.category, "tags": p.tags, "data_source_ids": ds_ids,
            "user_id": p.user_id, "created_at": p.created_at,
            "subscriber_count": sub_count,
            "can_assign": self._holds_on_all_agents(resolved, ds_ids, 'assign_prompts'),
            "can_manage": self._holds_on_all_agents(resolved, ds_ids, 'manage'),
        }

    async def get_prompt_or_404(self, db: AsyncSession, prompt_id: str, organization: Organization) -> Prompt:
        result = await db.execute(
            select(Prompt)
            .filter(Prompt.id == prompt_id)
            .filter(Prompt.organization_id == organization.id)
            .filter(Prompt.deleted_at == None)
        )
        p = result.scalar_one_or_none()
        if not p:
            raise HTTPException(status_code=404, detail="Prompt not found")
        return p

    async def get_prompt_response(self, db, prompt_id, current_user, organization) -> dict:
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        ds_ids = self._ds_ids(p)
        if not self._is_visible(resolved, p, str(current_user.id), ds_ids):
            raise HTTPException(status_code=403, detail="Access denied to this prompt")
        counts = await self._subscriber_counts(db, [p.id])
        return self._to_response(p, ds_ids, resolved, counts.get(p.id, 0))

    # ───────────────────────── catalog writes ─────────────────────────

    async def _load_data_sources(self, db, ds_ids: List[str], organization: Organization) -> List[DataSource]:
        if not ds_ids:
            return []
        rows = await db.execute(
            select(DataSource)
            .filter(DataSource.id.in_(ds_ids))
            .filter(DataSource.organization_id == organization.id)
            .filter(DataSource.deleted_at == None)
        )
        found = list(rows.scalars().unique().all())
        if len(found) != len(set(ds_ids)):
            raise HTTPException(status_code=404, detail="One or more data sources not found")
        return found

    async def create_prompt(self, db, data: PromptCatalogCreate, current_user, organization) -> Prompt:
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        # Authoring an agent-scoped prompt requires `manage` on all its agents.
        if data.scope == 'agent' and not self._holds_on_all_agents(resolved, data.data_source_ids, 'manage'):
            raise HTTPException(status_code=403, detail="manage permission required on all agents")
        data_sources = await self._load_data_sources(db, data.data_source_ids, organization)
        p = Prompt(
            title=data.title, text=data.text, mode=data.mode, model_id=data.model_id,
            mentions=data.mentions, scope=data.scope, is_starter=data.is_starter,
            status=data.status, default_cron=data.default_cron, default_channel=data.default_channel,
            category=data.category, tags=data.tags,
            user_id=current_user.id, organization_id=organization.id,
        )
        p.data_sources = data_sources
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p

    async def update_prompt(self, db, prompt_id, data: PromptCatalogUpdate, current_user, organization) -> Prompt:
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        ds_ids = self._ds_ids(p)
        if not self._holds_on_all_agents(resolved, ds_ids, 'manage') and str(p.user_id) != str(current_user.id):
            raise HTTPException(status_code=403, detail="manage permission required")
        fields = data.dict(exclude_unset=True)
        new_ds_ids = fields.pop('data_source_ids', None)
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
        if not self._holds_on_all_agents(resolved, self._ds_ids(p), 'manage') and str(p.user_id) != str(current_user.id):
            raise HTTPException(status_code=403, detail="manage permission required")
        p.deleted_at = datetime.utcnow()
        await db.commit()

    # ───────────────────────── run / subscribe / assign ─────────────────────────

    def _build_prompt_json(self, p: Prompt) -> dict:
        return {
            "content": p.text or "",
            "mode": p.mode or 'chat',
            "model_id": p.model_id,
            "mentions": p.mentions,
        }

    async def _create_anchor_report(self, db, p: Prompt, owner: User, organization: Organization):
        """Create a report owned by `owner`, seeded with the prompt's agents."""
        from app.services.report_service import ReportService
        from app.schemas.report_schema import ReportCreate
        report_service = ReportService()
        title = p.title or (p.text[:60] if p.text else "Prompt")
        report = await report_service.create_report(
            db,
            ReportCreate(title=title, data_sources=self._ds_ids(p)),
            current_user=owner,
            organization=organization,
        )
        # report_service may run in its own transaction; set mode explicitly
        from app.models.report import Report
        rep = await db.get(Report, report.id)
        if rep is not None and p.mode:
            rep.mode = p.mode
            await db.commit()
        return report.id

    async def run_now(self, db, prompt_id, current_user, organization) -> dict:
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        ds_ids = self._ds_ids(p)
        if not self._is_visible(resolved, p, str(current_user.id), ds_ids):
            raise HTTPException(status_code=403, detail="Access denied to this prompt")
        if not self._can_access_all_agents(resolved, ds_ids):
            raise HTTPException(status_code=403, detail="Access required on all of the prompt's agents")

        report_id = await self._create_anchor_report(db, p, current_user, organization)

        from app.services.completion_service import CompletionService
        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
        completion_service = CompletionService()
        prompt_schema = PromptSchema(**self._build_prompt_json(p))
        completion_id = None
        try:
            resp = await completion_service.create_completion(
                db=db, report_id=report_id,
                completion_data=CompletionCreate(prompt=prompt_schema),
                current_user=current_user, organization=organization, background=False,
            )
            for c in (getattr(resp, 'completions', None) or []):
                completion_id = c.id
        except Exception as e:
            logger.error(f"run_now failed for prompt {prompt_id}: {e}")
        return {"report_id": report_id, "completion_id": completion_id}

    async def subscribe(self, db, prompt_id, data: SubscribeRequest, current_user, organization) -> ScheduledPrompt:
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        ds_ids = self._ds_ids(p)
        if not self._is_visible(resolved, p, str(current_user.id), ds_ids):
            raise HTTPException(status_code=403, detail="Access denied to this prompt")
        if not self._can_access_all_agents(resolved, ds_ids):
            raise HTTPException(status_code=403, detail="Access required on all of the prompt's agents")
        return await self._create_subscription(
            db, p, target_user=current_user, organization=organization,
            cron=data.cron_schedule, channel=data.channel, run_mode=data.run_mode,
            created_by=current_user,
        )

    async def assign(self, db, prompt_id, data: AssignRequest, current_user, organization) -> dict:
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        ds_ids = self._ds_ids(p)
        # Assigning to others requires assign_prompts on ALL of the prompt's agents.
        if not self._holds_on_all_agents(resolved, ds_ids, 'assign_prompts'):
            raise HTTPException(status_code=403, detail="assign_prompts permission required on all agents")

        target_user_ids = await self._expand_principal(db, data, organization)
        created, skipped, ids = 0, 0, []
        for uid in target_user_ids:
            target = await db.get(User, uid)
            if not target:
                skipped += 1
                continue
            t_resolved = await resolve_permissions(db, str(uid), str(organization.id))
            if not self._can_access_all_agents(t_resolved, ds_ids):
                skipped += 1
                continue
            sp = await self._create_subscription(
                db, p, target_user=target, organization=organization,
                cron=data.cron_schedule, channel=data.channel, run_mode=data.run_mode,
                created_by=current_user,
            )
            created += 1
            ids.append(sp.id)
        return {"created": created, "skipped": skipped, "scheduled_prompt_ids": ids}

    async def _expand_principal(self, db, data: AssignRequest, organization: Organization) -> List[str]:
        if data.principal_type == 'user':
            if not data.principal_id:
                raise HTTPException(status_code=400, detail="principal_id required for user")
            return [data.principal_id]
        if data.principal_type == 'group':
            if not data.principal_id:
                raise HTTPException(status_code=400, detail="principal_id required for group")
            rows = await db.execute(
                select(GroupMembership.user_id)
                .join(Group, Group.id == GroupMembership.group_id)
                .filter(GroupMembership.group_id == data.principal_id)
                .filter(Group.organization_id == organization.id)
                .filter(GroupMembership.user_id != None)
            )
            return [r[0] for r in rows.all()]
        if data.principal_type == 'org':
            rows = await db.execute(
                select(Membership.user_id)
                .filter(Membership.organization_id == organization.id)
                .filter(Membership.user_id != None)
                .filter(Membership.deleted_at == None)
            )
            return list({r[0] for r in rows.all()})
        raise HTTPException(status_code=400, detail=f"Unknown principal_type {data.principal_type}")

    async def _create_subscription(self, db, p: Prompt, target_user: User, organization: Organization,
                                   cron: str, channel: Optional[str], run_mode: str, created_by: User) -> ScheduledPrompt:
        from app.services.scheduled_prompt_service import scheduled_prompt_service, _parse_cron_expression
        if _parse_cron_expression(cron) is None:
            raise HTTPException(status_code=400, detail="Invalid cron schedule")
        report_id = await self._create_anchor_report(db, p, target_user, organization)
        sp = ScheduledPrompt(
            report_id=report_id,
            user_id=target_user.id,
            prompt=self._build_prompt_json(p),
            cron_schedule=cron,
            is_active=True,
            prompt_id=p.id,
            channel=channel,
            run_mode=run_mode if run_mode in ('append', 'new_report') else 'append',
            created_by=created_by.id,
        )
        db.add(sp)
        await db.commit()
        await db.refresh(sp)
        try:
            scheduled_prompt_service._register_job(sp)
        except Exception as e:
            logger.error(f"Failed to register job for subscription {sp.id}: {e}")
        return sp


prompt_catalog_service = PromptCatalogService()
