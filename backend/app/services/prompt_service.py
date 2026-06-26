"""Prompt model service: access-aware list/get + CRUD.

Visibility is scoped to the caller's agent access (active data sources only):
  - global  → every org member
  - private → owner only
  - agent   → users with access to ALL of the prompt's ACTIVE agents
Authoring an agent prompt needs `manage` on its agents; global prompts are
admin-only. No UI, scheduling, or delivery here — just the model.
"""
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.models.prompt import Prompt
from app.models.data_source import DataSource
from app.models.user import User
from app.models.organization import Organization
from app.schemas.prompt_schema import PromptCreate, PromptUpdate
from app.core.permission_resolver import resolve_permissions, ResolvedPermissions, FULL_ADMIN

logger = logging.getLogger(__name__)


class PromptService:

    # ── access helpers ──

    @staticmethod
    def _ds_ids(prompt: Prompt) -> List[str]:
        return [str(ds.id) for ds in (prompt.data_sources or [])]

    @staticmethod
    def _active_ds_ids(prompt: Prompt) -> List[str]:
        return [
            str(ds.id) for ds in (prompt.data_sources or [])
            if getattr(ds, 'is_active', False) and getattr(ds, 'deleted_at', None) is None
        ]

    @staticmethod
    def _can_access_all(resolved: ResolvedPermissions, ds_ids: List[str]) -> bool:
        return all(resolved.has_resource_membership('data_source', ds) for ds in ds_ids)

    @staticmethod
    def _can_manage_all(resolved: ResolvedPermissions, ds_ids: List[str]) -> bool:
        if FULL_ADMIN in resolved.org_permissions:
            return True
        if not ds_ids:
            return False
        return all(resolved.has_resource_permission('data_source', ds, 'manage') for ds in ds_ids)

    def _is_visible(self, resolved: ResolvedPermissions, p: Prompt, user_id: str, active_ds_ids: List[str]) -> bool:
        if FULL_ADMIN in resolved.org_permissions:
            return True
        if p.scope == 'global':
            return True
        if p.scope == 'private':
            return str(p.user_id) == str(user_id)
        # agent
        if not active_ds_ids:
            return str(p.user_id) == str(user_id)
        return self._can_access_all(resolved, active_ds_ids)

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
    ) -> dict:
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        q = (
            select(Prompt)
            .filter(Prompt.organization_id == organization.id)
            .filter(Prompt.deleted_at == None)
        )
        if starters_only:
            q = q.filter(Prompt.is_starter == True)
        if data_source_id:
            q = q.join(Prompt.data_sources).filter(DataSource.id == data_source_id)
        rows = await db.execute(q)
        prompts = list(rows.scalars().unique().all())

        visible = [
            self._to_response(p, resolved, str(current_user.id))
            for p in prompts
            if self._is_visible(resolved, p, str(current_user.id), self._active_ds_ids(p))
        ]
        visible.sort(key=lambda r: r["created_at"] or datetime.min, reverse=True)
        return {"prompts": visible, "meta": {"total": len(visible)}}

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
        if not self._is_visible(resolved, p, str(current_user.id), self._active_ds_ids(p)):
            raise HTTPException(status_code=403, detail="Access denied to this prompt")
        return self._to_response(p, resolved, str(current_user.id))

    # ── writes ──

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
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        if data.scope == 'global':
            if FULL_ADMIN not in resolved.org_permissions:
                raise HTTPException(status_code=403, detail="admin required to create a global prompt")
        elif data.scope == 'agent':
            if not data.data_source_ids:
                raise HTTPException(status_code=400, detail="an agent prompt must reference at least one agent")
            if not self._can_manage_all(resolved, data.data_source_ids):
                raise HTTPException(status_code=403, detail="manage permission required on all agents")
        data_sources = await self._load_data_sources(db, data.data_source_ids, organization)
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

    async def update_prompt(self, db, prompt_id, data: PromptUpdate, current_user, organization) -> Prompt:
        p = await self.get_prompt_or_404(db, prompt_id, organization)
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        is_admin = FULL_ADMIN in resolved.org_permissions
        can_edit = (
            is_admin
            or str(p.user_id) == str(current_user.id)
            or (p.scope == 'agent' and self._can_manage_all(resolved, self._ds_ids(p)))
        )
        if not can_edit:
            raise HTTPException(status_code=403, detail="manage permission required")
        fields = data.dict(exclude_unset=True)
        if fields.get('scope') == 'global' and not is_admin:
            raise HTTPException(status_code=403, detail="admin required to make a prompt global")
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
