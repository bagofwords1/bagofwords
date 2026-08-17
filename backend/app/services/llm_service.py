from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.models.llm_provider import LLMProvider
from app.models.llm_model import LLMModel
from app.models.organization import Organization
from app.models.user import User
from app.settings.config import settings
from app.models.llm_provider import LLM_PROVIDER_DETAILS
from app.models.llm_model import LLM_MODEL_DETAILS, DEFAULT_CUSTOM_MODEL_CONTEXT_WINDOW
from app.schemas.llm_schema import AnthropicCredentials, OpenAICredentials, GoogleCredentials, LLMModelSchema, LLMProviderCreate, LLMProviderTestConnection
from app.ai.llm.llm import LLM
from app.dependencies import async_session_maker
from datetime import datetime
from app.core.telemetry import telemetry
from app.ee.audit.service import audit_service
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

class LLMService:
    def __init__(self):
        pass

    async def get_providers(
        self, 
        db: AsyncSession, 
        organization: Organization,
        current_user: User
    ):
        """Get all LLM providers for an organization"""
        result = await db.execute(
            select(LLMProvider)
            .filter(LLMProvider.organization_id == organization.id)
            .filter(LLMProvider.deleted_at == None)
            .filter(LLMProvider.is_enabled == True)
        )
        return result.unique().scalars().all()

    async def get_available_providers(
        self, 
        db: AsyncSession, 
        organization: Organization,
        current_user: User
    ):
        return LLM_PROVIDER_DETAILS
    
    async def get_available_models(
        self, 
        db: AsyncSession, 
        organization: Organization,
        current_user: User
    ):
        return LLM_MODEL_DETAILS

    async def create_provider(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        provider_data
    ):
        """Create a new custom LLM provider"""
        # Capture identifiers up front. After a failed commit + rollback the ORM
        # objects get expired, and touching their attributes would trigger a lazy
        # (sync) DB load outside the greenlet context -> MissingGreenlet.
        org_id = organization.id
        provider_name = provider_data.name
        logger.info("Creating LLM provider: name=%s, type=%s, org_id=%s, user_id=%s", provider_name, provider_data.provider_type, org_id, current_user.id)

        models = provider_data.models
        del provider_data.models
        del provider_data.config
        credentials = provider_data.credentials
        del provider_data.credentials

        provider = LLMProvider(**provider_data.dict())
        self._set_provider_credentials(provider, credentials)

        provider.organization_id = organization.id

        # Persist the provider first so duplicate name errors are caught cleanly here
        db.add(provider)
        try:
            await db.commit()
            await db.refresh(provider)
        except IntegrityError:
            await db.rollback()
            logger.warning("Duplicate LLM provider name: name=%s, org_id=%s", provider_name, org_id)
            raise HTTPException(
                status_code=409,
                detail=f"A provider named '{provider_name}' already exists in this organization. Please choose a different name."
            )

        logger.info("LLM provider created: id=%s, name=%s, type=%s, org_id=%s", provider.id, provider.name, provider.provider_type, organization.id)

        # Now create/update models for this provider (commits internally)
        await self._create_models(db, organization, provider, current_user, models)

        # Telemetry: LLM provider created
        try:
            await telemetry.capture(
                "llm_provider_created",
                {
                    "provider_type": provider.provider_type,
                    "is_preset": bool(getattr(provider, "is_preset", False)),
                    "num_models": len(models or []),
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_provider.created",
                user_id=str(current_user.id),
                resource_type="llm_provider",
                resource_id=str(provider.id),
                details={"name": provider.name, "provider_type": provider.provider_type},
            )
        except Exception:
            pass

        return provider

    async def update_provider(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        provider_id: str,
        provider_data
    ):
        """Update provider settings"""
        logger.info("Updating LLM provider: provider_id=%s, org_id=%s, user_id=%s", provider_id, organization.id, current_user.id)
        provider = await db.execute(
            select(LLMProvider).filter(
                LLMProvider.id == provider_id,
                LLMProvider.organization_id == organization.id
            )
        )
        provider = provider.unique().scalar_one_or_none()
        
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        
        if provider.is_preset:
            raise HTTPException(status_code=400, detail="Cannot update preset providers")

        update_data = provider_data.dict(exclude_unset=True)
        models = provider_data.models
        del provider_data.models

        credentials = update_data.pop('credentials', None)

        await self._update_models(db, organization, provider, current_user, models)

        # Allow updating provider additional_config (e.g., base_url) without requiring api_key
        if credentials is not None:
            self._set_provider_credentials(provider, credentials)

        db.add(provider)
        try:
            await db.commit()
            await db.refresh(provider)
        except IntegrityError:
            await db.rollback()
            logger.warning("Duplicate LLM provider name on update: name=%s, org_id=%s", update_data.get('name', provider.name), organization.id)
            raise HTTPException(
                status_code=409,
                detail=f"A provider with the name '{update_data.get('name', provider.name)}' already exists in this organization."
            )

        logger.info("LLM provider updated: id=%s, name=%s, type=%s, org_id=%s", provider.id, provider.name, provider.provider_type, organization.id)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_provider.updated",
                user_id=str(current_user.id),
                resource_type="llm_provider",
                resource_id=str(provider.id),
                details={"name": provider.name, "provider_type": provider.provider_type},
            )
        except Exception:
            pass

        return provider
    
    async def create_model(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model,
    ) -> LLMModel:
        """Create a single (typically custom) model under one of the org's
        providers.

        The route POST /llm/models has called this since it existed, but the
        method itself was never implemented — every custom-model creation
         500'd with AttributeError. The normal UI flow creates models inside
        the provider payload (_create_models), which is why nobody noticed.

        Default handling mirrors _create_models: making this model the default
        (or small default) clears the flag from whichever model held it.
        """
        from fastapi import HTTPException

        provider = (await db.execute(
            select(LLMProvider)
            .filter(LLMProvider.id == str(model.provider_id))
            .filter(LLMProvider.organization_id == organization.id)
            .filter(LLMProvider.deleted_at == None)  # noqa: E711
        )).unique().scalar_one_or_none()
        if provider is None:
            raise HTTPException(status_code=404, detail="Provider not found")

        dup = (await db.execute(
            select(LLMModel)
            .filter(LLMModel.organization_id == organization.id)
            .filter(LLMModel.provider_id == provider.id)
            .filter(LLMModel.model_id == model.model_id)
            .filter(LLMModel.deleted_at == None)  # noqa: E711
        )).unique().scalars().first()
        if dup is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Model '{model.model_id}' already exists on this provider",
            )

        if model.is_default:
            for row in (await db.execute(
                select(LLMModel)
                .filter(LLMModel.organization_id == organization.id)
                .filter(LLMModel.is_default == True)  # noqa: E712
            )).unique().scalars().all():
                row.is_default = False
        if model.is_small_default:
            for row in (await db.execute(
                select(LLMModel)
                .filter(LLMModel.organization_id == organization.id)
                .filter(LLMModel.is_small_default == True)  # noqa: E712
            )).unique().scalars().all():
                row.is_small_default = False

        row = LLMModel(
            name=model.name or model.model_id,
            model_id=model.model_id,
            provider_id=provider.id,
            organization_id=organization.id,
            is_custom=bool(getattr(model, "is_custom", True)),
            is_preset=False,
            is_enabled=True,
            is_default=bool(model.is_default),
            is_small_default=bool(model.is_small_default),
            supports_vision=bool(getattr(model, "supports_vision", False)),
            supports_vision_override=getattr(model, "supports_vision_override", None),
            supports_image_generation=bool(getattr(model, "supports_image_generation", False)),
            supports_image_generation_override=getattr(model, "supports_image_generation_override", None),
            context_window_tokens=getattr(model, "context_window_tokens", None),
            context_window_tokens_override=getattr(model, "context_window_tokens_override", None),
            max_output_tokens=getattr(model, "max_output_tokens", None),
            input_cost_per_million_tokens_usd=getattr(model, "input_cost_per_million_tokens_usd", None),
            output_cost_per_million_tokens_usd=getattr(model, "output_cost_per_million_tokens_usd", None),
            # Model-specific config (e.g. an explicit sampling temperature) —
            # update_model already accepts and merges this; creation should too.
            config=dict(getattr(model, "config", None) or {}) or None,
        )
        # Same NULL-window guard as _create_models: a custom model with no
        # explicit size gets the conservative default, never NULL.
        if row.is_custom and row.context_window_tokens is None:
            row.context_window_tokens = DEFAULT_CUSTOM_MODEL_CONTEXT_WINDOW
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    async def update_model(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        model_data,
    ) -> LLMModel:
        """Update a model's editable fields (name, config).

        Like create_model, the route PATCH /llm/models/{model_id} predates the
        implementation — it used to 500 with AttributeError. Config keys are
        merged (not replaced) so e.g. routing_hint survives an unrelated update.
        """
        model = await self._get_owned_model(db, organization, model_id)

        data = model_data.dict(exclude_unset=True)
        if data.get("name"):
            model.name = data["name"]
        if data.get("config") is not None:
            # Reassign (not mutate) so SQLAlchemy detects the JSON change.
            model.config = {**dict(model.config or {}), **data["config"]}
        db.add(model)
        await db.commit()
        await db.refresh(model)

        logger.info("LLM model updated: id=%s, model_id=%s, fields=%s, org_id=%s", model.id, model.model_id, list(data.keys()), organization.id)
        return model

    async def delete_model(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
    ):
        """Soft-delete a model.

        Like create_model, the route DELETE /llm/models/{model_id} predates the
        implementation — it used to 500 with AttributeError. Guards:
        - default / small-default models can't be deleted (something must serve
          requests; make another model the default first);
        - preset-catalog models on a PRESET provider can't be deleted, because
          _sync_provider_with_latest_models recreates any catalog model missing
          from the provider on the next models fetch. User-added providers are
          never synced, so their models (preset-catalog or custom) delete fine.
        """
        model = await self._get_owned_model(db, organization, model_id)
        if model.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Model not found")

        if model.is_default or model.is_small_default:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete the default or small default model. Make another model the default first.",
            )

        provider = (await db.execute(
            select(LLMProvider).filter(LLMProvider.id == model.provider_id)
        )).unique().scalar_one_or_none()
        if provider is not None and provider.is_preset and model.is_preset:
            raise HTTPException(
                status_code=400,
                detail="Models of a preset provider are managed by the catalog and cannot be deleted. Disable the model instead.",
            )

        model.deleted_at = datetime.now()
        model.is_enabled = False
        db.add(model)
        await db.commit()

        logger.info("LLM model deleted: id=%s, name=%s, model_id=%s, org_id=%s", model.id, model.name, model.model_id, organization.id)
        return {"message": "Model deleted successfully"}

    async def get_model_by_id(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str
    ):
        """Get a model by id.

        Enforces per-model access control: a restricted model the current user
        has no grant for is rejected with 403 (the security boundary — callers
        select models by id straight from the request).
        """
        model = await db.execute(
            select(LLMModel).filter(LLMModel.id == model_id).filter(LLMModel.organization_id == organization.id)
        )
        model = model.scalar_one_or_none()
        if model is not None and current_user is not None:
            from app.core.permission_resolver import user_can_use_model
            if not await user_can_use_model(db, current_user.id, organization.id, model):
                raise HTTPException(status_code=403, detail="You do not have access to this model")
        return model

    async def _get_owned_model(
        self,
        db: AsyncSession,
        organization: Organization,
        model_id: str,
    ) -> LLMModel:
        """Load a model scoped to the org without access enforcement (admin path)."""
        result = await db.execute(
            select(LLMModel).filter(
                LLMModel.id == model_id,
                LLMModel.organization_id == organization.id,
            )
        )
        model = result.scalar_one_or_none()
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
        return model

    async def get_model_access(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
    ):
        """Return restriction state + the principals granted `use` on a model."""
        from app.models.resource_grant import ResourceGrant
        from app.models.user import User as UserModel
        from app.models.group import Group
        from app.models.role import Role

        model = await self._get_owned_model(db, organization, model_id)

        result = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == "llm_model",
                ResourceGrant.resource_id == str(model.id),
                ResourceGrant.organization_id == str(organization.id),
                ResourceGrant.deleted_at.is_(None),
            )
        )
        grants = result.scalars().all()

        user_ids = [g.principal_id for g in grants if g.principal_type == "user"]
        group_ids = [g.principal_id for g in grants if g.principal_type == "group"]
        role_ids = [g.principal_id for g in grants if g.principal_type == "role"]

        user_names: dict = {}
        if user_ids:
            r = await db.execute(select(UserModel.id, UserModel.name, UserModel.email).where(UserModel.id.in_(user_ids)))
            for uid, name, email in r.all():
                user_names[uid] = name or email or uid
        group_names: dict = {}
        if group_ids:
            r = await db.execute(select(Group.id, Group.name).where(Group.id.in_(group_ids)))
            for gid, name in r.all():
                group_names[gid] = name
        role_names: dict = {}
        if role_ids:
            r = await db.execute(select(Role.id, Role.name).where(Role.id.in_(role_ids)))
            for rid, name in r.all():
                role_names[rid] = name

        def _name(g):
            if g.principal_type == "group":
                return group_names.get(g.principal_id)
            if g.principal_type == "role":
                return role_names.get(g.principal_id)
            return user_names.get(g.principal_id)

        return {
            "model_id": str(model.id),
            "is_restricted": bool(getattr(model, "is_restricted", False)),
            "is_default": bool(model.is_default),
            "is_small_default": bool(model.is_small_default),
            "members": [
                {
                    "grant_id": g.id,
                    "principal_type": g.principal_type,
                    "principal_id": g.principal_id,
                    "principal_name": _name(g),
                }
                for g in grants
            ],
        }

    async def set_model_restricted(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        is_restricted: bool,
    ):
        """Toggle whether a model is access-restricted."""
        model = await self._get_owned_model(db, organization, model_id)
        if is_restricted and (model.is_default or model.is_small_default):
            raise HTTPException(
                status_code=400,
                detail="Default models are available to all members and cannot be restricted.",
            )
        model.is_restricted = is_restricted
        db.add(model)
        await db.commit()

        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_model.restriction_changed",
                user_id=str(current_user.id),
                resource_type="llm_model",
                resource_id=str(model.id),
                details={"name": model.name, "is_restricted": is_restricted},
            )
        except Exception:
            pass
        return {"success": True, "is_restricted": is_restricted}

    async def add_model_access(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        principal_type: str,
        principal_id: str,
    ):
        """Grant a user/group/role `use` access to a restricted model."""
        from app.models.resource_grant import ResourceGrant

        if principal_type not in ("user", "group", "role"):
            raise HTTPException(status_code=400, detail="Invalid principal_type")

        model = await self._get_owned_model(db, organization, model_id)

        existing = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == "llm_model",
                ResourceGrant.resource_id == str(model.id),
                ResourceGrant.principal_type == principal_type,
                ResourceGrant.principal_id == principal_id,
                ResourceGrant.organization_id == str(organization.id),
                ResourceGrant.deleted_at.is_(None),
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Access grant already exists")

        grant = ResourceGrant(
            organization_id=str(organization.id),
            resource_type="llm_model",
            resource_id=str(model.id),
            principal_type=principal_type,
            principal_id=principal_id,
            permissions=["use"],
        )
        db.add(grant)
        await db.commit()
        await db.refresh(grant)

        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_model.access_granted",
                user_id=str(current_user.id),
                resource_type="llm_model",
                resource_id=str(model.id),
                details={"principal_type": principal_type, "principal_id": principal_id},
            )
        except Exception:
            pass
        return {"grant_id": grant.id, "principal_type": principal_type, "principal_id": principal_id}

    async def remove_model_access(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        grant_id: str,
    ):
        """Revoke a `use` grant from a model."""
        from app.models.resource_grant import ResourceGrant

        model = await self._get_owned_model(db, organization, model_id)
        result = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.id == grant_id,
                ResourceGrant.resource_type == "llm_model",
                ResourceGrant.resource_id == str(model.id),
                ResourceGrant.organization_id == str(organization.id),
                ResourceGrant.deleted_at.is_(None),
            )
        )
        grant = result.scalar_one_or_none()
        if not grant:
            raise HTTPException(status_code=404, detail="Access grant not found")

        await db.delete(grant)
        await db.commit()

        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_model.access_revoked",
                user_id=str(current_user.id),
                resource_type="llm_model",
                resource_id=str(model.id),
                details={"principal_type": grant.principal_type, "principal_id": grant.principal_id},
            )
        except Exception:
            pass
        return {"success": True}

    async def list_models_for_principal(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        principal_type: str,
        principal_id: str,
    ):
        """List restricted models with whether `principal` is granted use of each.

        Powers the role-centric access editor (a role's modal lists the
        restricted models it may use). Only restricted models are returned —
        open models are usable by everyone, so there is nothing to grant.
        """
        from app.models.resource_grant import ResourceGrant

        if principal_type not in ("user", "group", "role"):
            raise HTTPException(status_code=400, detail="Invalid principal_type")

        # All restricted models in the org (admin path — no access filtering).
        result = await db.execute(
            select(LLMModel)
            .join(LLMModel.provider)
            .filter(LLMProvider.organization_id == organization.id)
            .filter(LLMProvider.deleted_at == None)
            .filter(LLMModel.deleted_at == None)
            .filter(LLMModel.is_restricted == True)
        )
        models = result.unique().scalars().all()

        # Existing grants for this principal across those models.
        model_ids = [str(m.id) for m in models]
        grant_ids: dict = {}
        if model_ids:
            gr = await db.execute(
                select(ResourceGrant).where(
                    ResourceGrant.resource_type == "llm_model",
                    ResourceGrant.resource_id.in_(model_ids),
                    ResourceGrant.principal_type == principal_type,
                    ResourceGrant.principal_id == principal_id,
                    ResourceGrant.organization_id == str(organization.id),
                    ResourceGrant.deleted_at.is_(None),
                )
            )
            for g in gr.scalars().all():
                grant_ids[g.resource_id] = g.id

        return [
            {
                "model_id": str(m.id),
                "name": m.name or m.model_id,
                "provider_name": getattr(getattr(m, "provider", None), "name", "") or "",
                "granted": str(m.id) in grant_ids,
                "grant_id": grant_ids.get(str(m.id)),
            }
            for m in models
        ]

    async def delete_provider(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        provider_id: str
    ):
        """Delete a custom provider"""
        logger.info("Deleting LLM provider: provider_id=%s, org_id=%s, user_id=%s", provider_id, organization.id, current_user.id)
        provider = await db.execute(
            select(LLMProvider).filter(
                LLMProvider.id == provider_id,
                LLMProvider.organization_id == organization.id
            )
        )
        provider = provider.unique().scalar_one_or_none()
        
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
            
        if provider.is_preset:
            raise HTTPException(status_code=400, detail="Cannot delete preset providers")
        
        models = provider.models
        for model in models:
            if model.is_default or model.is_small_default:
                raise HTTPException(status_code=400, detail="Cannot delete models that are set as default or small default")

        provider.deleted_at = datetime.now()
        provider.is_enabled = False

        await self._disable_models(db, organization, provider)

        db.add(provider)
        await db.commit()

        logger.info("LLM provider deleted: id=%s, name=%s, type=%s, org_id=%s", provider.id, provider.name, provider.provider_type, organization.id)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_provider.deleted",
                user_id=str(current_user.id),
                resource_type="llm_provider",
                resource_id=str(provider.id),
                details={"name": provider.name, "provider_type": provider.provider_type},
            )
        except Exception:
            pass

        return {"message": "Provider deleted successfully"}

    async def get_models(
        self, 
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        is_enabled: bool = None
    ):
        """Get all LLM models for an organization, optionally filtered by status"""
        # First, get all active providers
        providers = await db.execute(
            select(LLMProvider)
            .filter(LLMProvider.organization_id == organization.id)
            .filter(LLMProvider.deleted_at == None)
        )
        providers = providers.unique().scalars().all()

        # Sync every provider whose provider_type has catalog entries, so a
        # deployment upgrade's new catalog models reach existing orgs on the
        # next models-list load — customer-created (bring-your-own-key)
        # providers included, not just BOW-managed preset ones. Provider types
        # with no catalog (the OpenAI-compatible "custom" type) have nothing
        # to sync from and are skipped entirely.
        catalog_provider_types = {m["provider_type"] for m in LLM_MODEL_DETAILS}
        for provider in providers:
            if provider.is_preset or provider.provider_type in catalog_provider_types:
                await self._sync_provider_with_latest_models(
                    db, provider, organization,
                    customer_managed=not provider.is_preset,
                )

        await db.commit()

        # Get all models with filters
        query = select(LLMModel).join(LLMModel.provider).filter(
            LLMProvider.organization_id == organization.id
        ).filter(
            LLMProvider.deleted_at == None
        ).filter(
            LLMModel.deleted_at == None
        ).filter(
            LLMProvider.is_enabled == True
        )
        
        if is_enabled is not None:
            query = query.filter(LLMModel.is_enabled == is_enabled)
        
        result = await db.execute(query)
        models = result.unique().scalars().all()

        # Per-model access control: hide restricted models the user can't use.
        # No-op when the feature is unlicensed (fail open) or nothing is restricted.
        from app.core.permission_resolver import llm_access_control_active, get_accessible_model_ids
        if current_user is not None and llm_access_control_active() and any(getattr(m, "is_restricted", False) for m in models):
            is_admin, granted = await get_accessible_model_ids(db, current_user.id, organization.id)
            if not is_admin:
                granted_set = set(granted)
                models = [
                    m for m in models
                    if not getattr(m, "is_restricted", False)
                    or getattr(m, "is_default", False)
                    or getattr(m, "is_small_default", False)
                    or str(m.id) in granted_set
                ]

        # Mark the caller's personal default so clients can preselect it without
        # a second request. Transient attribute (feeds LLMModelSchema.is_user_default),
        # only set when the preferred model survived the access filtering above.
        user_default_id = None
        if current_user is not None:
            user_default_id = await self.get_user_default_model_id(db, organization, current_user)
        visible_ids = {str(m.id) for m in models}
        if user_default_id not in visible_ids:
            user_default_id = None
        for m in models:
            m.is_user_default = (str(m.id) == user_default_id)

        # Prefer small default models first, then regular default, then by provider/name
        def _sort_key(m):
            try:
                provider_name = getattr(getattr(m, "provider", None), "name", "") or ""
            except Exception:
                provider_name = ""
            model_name = getattr(m, "name", None) or getattr(m, "model_id", "")
            # False > True when cast to int, so invert using not
            return (
                0 if getattr(m, "is_small_default", False) else 1,
                0 if getattr(m, "is_default", False) else 1,
                provider_name.lower(),
                str(model_name).lower(),
            )
        return sorted(models, key=_sort_key)

    async def setup_default_providers(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User
    ):
        """Setup default LLM providers from config for a new organization"""
        for llm_config in settings.default_llm:
            provider = LLMProvider(
                name=llm_config["provider"],
                provider_type=llm_config["provider"],
                api_key=llm_config["key"],
                api_secret=llm_config.get("secret"),
                organization_id=organization.id,
                is_preset=True,
                use_preset_credentials=True
            )
            db.add(provider)
            
            for model_name in llm_config.get("available_models", []):
                model = LLMModel(
                    name=model_name,
                    model_id=model_name,
                    provider=provider,
                    is_preset=True
                )
                db.add(model)
        
        await db.commit()

    async def toggle_provider(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        provider_id: str,
        enabled: bool
    ):
        """Enable/disable a provider"""
        provider = await db.execute(
            select(LLMProvider).filter(
                LLMProvider.id == provider_id,
                LLMProvider.organization_id == organization.id
            )
        )
        provider = provider.scalar_one_or_none()

        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        provider.is_enabled = enabled
        await db.commit()

        logger.info("LLM provider toggled: id=%s, name=%s, enabled=%s, org_id=%s", provider.id, provider.name, enabled, organization.id)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_provider.toggled",
                user_id=str(current_user.id),
                resource_type="llm_provider",
                resource_id=str(provider.id),
                details={"name": provider.name, "enabled": enabled},
            )
        except Exception:
            pass

        return {"success": True}

    async def toggle_model(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        enabled: bool
    ):
        """Enable/disable a model"""
        model = await db.execute(
            select(LLMModel).join(LLMProvider).filter(
                LLMModel.id == model_id,
                LLMProvider.organization_id == organization.id
            )
        )
        model = model.scalar_one_or_none()

        if not model:
            raise HTTPException(status_code=404, detail="Model not found")

        if model.is_default or model.is_small_default:
            raise HTTPException(status_code=400, detail="Cannot disable models that are set as default or small default")

        model.is_enabled = enabled
        await db.commit()

        logger.info("LLM model toggled: id=%s, name=%s, model_id=%s, enabled=%s, org_id=%s", model.id, model.name, model.model_id, enabled, organization.id)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_model.toggled",
                user_id=str(current_user.id),
                resource_type="llm_model",
                resource_id=str(model.id),
                details={"name": model.name, "model_id": model.model_id, "enabled": enabled},
            )
        except Exception:
            pass

        return {"success": True}

    async def toggle_vision(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        enabled: bool
    ):
        """Manually enable/disable image (vision) support for a model.

        Sets an explicit override so the choice survives catalog re-syncs, and
        updates the effective `supports_vision` flag that inference reads.
        """
        model = await db.execute(
            select(LLMModel).join(LLMProvider).filter(
                LLMModel.id == model_id,
                LLMProvider.organization_id == organization.id
            )
        )
        model = model.scalar_one_or_none()

        if not model:
            raise HTTPException(status_code=404, detail="Model not found")

        model.supports_vision_override = enabled
        model.supports_vision = enabled
        await db.commit()

        logger.info("LLM model vision toggled: id=%s, name=%s, model_id=%s, supports_vision=%s, org_id=%s", model.id, model.name, model.model_id, enabled, organization.id)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_model.vision_toggled",
                user_id=str(current_user.id),
                resource_type="llm_model",
                resource_id=str(model.id),
                details={"name": model.name, "model_id": model.model_id, "supports_vision": enabled},
            )
        except Exception:
            pass

        return {"success": True}

    async def toggle_image_generation(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        enabled: bool
    ):
        """Manually mark/unmark a model as an image-generation model.

        Sets an explicit override so the choice survives catalog re-syncs, and
        updates the effective `supports_image_generation` flag that gates the
        generate_image tool. An image model is never a chat default, so enabling
        it also clears any default/small-default flag on that model.
        """
        model = await db.execute(
            select(LLMModel).join(LLMProvider).filter(
                LLMModel.id == model_id,
                LLMProvider.organization_id == organization.id
            )
        )
        model = model.scalar_one_or_none()

        if not model:
            raise HTTPException(status_code=404, detail="Model not found")

        model.supports_image_generation_override = enabled
        model.supports_image_generation = enabled
        if enabled:
            # Image models can never be the org's default / small default.
            model.is_default = False
            model.is_small_default = False
        await db.commit()

        logger.info("LLM model image-generation toggled: id=%s, name=%s, model_id=%s, supports_image_generation=%s, org_id=%s", model.id, model.name, model.model_id, enabled, organization.id)

        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_model.image_generation_toggled",
                user_id=str(current_user.id),
                resource_type="llm_model",
                resource_id=str(model.id),
                details={"name": model.name, "model_id": model.model_id, "supports_image_generation": enabled},
            )
        except Exception:
            pass

        return {"success": True}

    async def set_temperature(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        temperature: float | None,
    ):
        """Set (or clear) a model's explicit sampling temperature.

        Stored on ``LLMModel.config['temperature']`` and merged so other config
        keys (e.g. routing_hint) are preserved. None clears the override — the
        client then sends no temperature parameter at all and the endpoint's own
        default applies, which is the only universally safe behavior (a growing
        set of models rejects any non-default temperature with a 400).
        """
        if temperature is not None and not (0.0 <= temperature <= 2.0):
            raise HTTPException(status_code=400, detail="Temperature must be between 0 and 2")

        model = await db.execute(
            select(LLMModel).join(LLMProvider).filter(
                LLMModel.id == model_id,
                LLMProvider.organization_id == organization.id,
            )
        )
        model = model.scalar_one_or_none()
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")

        cfg = dict(model.config or {})
        if temperature is not None:
            cfg["temperature"] = temperature
        else:
            cfg.pop("temperature", None)
        # Reassign (not mutate) so SQLAlchemy detects the JSON change.
        model.config = cfg
        await db.commit()

        logger.info(
            "LLM model temperature set: id=%s, model_id=%s, temperature=%s, org_id=%s",
            model.id, model.model_id, temperature, organization.id,
        )
        try:
            await audit_service.log(
                db=db, organization_id=str(organization.id),
                action="llm_model.temperature_set", user_id=str(current_user.id),
                resource_type="llm_model", resource_id=str(model.id),
                details={"model_id": model.model_id, "temperature": temperature},
            )
        except Exception:
            pass

        return {"success": True, "temperature": temperature}

    async def set_context_window(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        tokens: int | None
    ):
        """Manually size (or reset) a model's context window.

        A value sets an explicit override so the choice survives catalog
        re-syncs, and updates the effective `context_window_tokens` the agent's
        token budget reads. None clears the override: preset models fall back
        to the catalog size, custom models to whatever was set at creation.
        """
        if tokens is not None and tokens <= 0:
            raise HTTPException(status_code=400, detail="Context window must be a positive number of tokens")

        model = await db.execute(
            select(LLMModel).join(LLMProvider).filter(
                LLMModel.id == model_id,
                LLMProvider.organization_id == organization.id
            )
        )
        model = model.scalar_one_or_none()

        if not model:
            raise HTTPException(status_code=404, detail="Model not found")

        model.context_window_tokens_override = tokens
        if tokens is not None:
            model.context_window_tokens = tokens
        else:
            # Reset: preset models return to the catalog size; custom models keep
            # their stored value (there is no catalog to fall back to).
            catalog = next(
                (
                    m for m in LLM_MODEL_DETAILS
                    if m["model_id"] == model.model_id
                    and m["provider_type"] == model.provider.provider_type
                ),
                None
            )
            if catalog and catalog.get("context_window_tokens") is not None:
                model.context_window_tokens = catalog["context_window_tokens"]
            elif model.is_custom and model.context_window_tokens is None:
                # Clearing the override on a custom model must not reintroduce
                # a NULL window — fall back to the same default used at creation.
                model.context_window_tokens = DEFAULT_CUSTOM_MODEL_CONTEXT_WINDOW
        await db.commit()

        logger.info("LLM model context window set: id=%s, name=%s, model_id=%s, context_window_tokens=%s, override=%s, org_id=%s", model.id, model.name, model.model_id, model.context_window_tokens, tokens, organization.id)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_model.context_window_set",
                user_id=str(current_user.id),
                resource_type="llm_model",
                resource_id=str(model.id),
                details={"name": model.name, "model_id": model.model_id, "context_window_tokens": model.context_window_tokens, "override": tokens},
            )
        except Exception:
            pass

        return {"success": True, "context_window_tokens": model.context_window_tokens}

    async def set_pricing(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        input_cost: float | None,
        output_cost: float | None,
    ):
        """Set a model's per-million-token USD pricing (input/output).

        Powers the cost console and the Auto-router savings math. None leaves a
        field unchanged; a negative value is rejected. Applies to any model the
        org owns (preset or custom) — an admin often needs to correct a preset
        rate or price a self-hosted model the catalog can't.
        """
        for label, val in (("input", input_cost), ("output", output_cost)):
            if val is not None and val < 0:
                raise HTTPException(status_code=400, detail=f"{label} cost must be non-negative")

        model = await db.execute(
            select(LLMModel).join(LLMProvider).filter(
                LLMModel.id == model_id,
                LLMProvider.organization_id == organization.id,
            )
        )
        model = model.scalar_one_or_none()
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")

        if input_cost is not None:
            model.input_cost_per_million_tokens_usd = float(input_cost)
        if output_cost is not None:
            model.output_cost_per_million_tokens_usd = float(output_cost)
        await db.commit()

        logger.info(
            "LLM model pricing set: id=%s, model_id=%s, in=%s, out=%s, org_id=%s",
            model.id, model.model_id,
            model.input_cost_per_million_tokens_usd,
            model.output_cost_per_million_tokens_usd, organization.id,
        )
        try:
            await audit_service.log(
                db=db, organization_id=str(organization.id),
                action="llm_model.pricing_set", user_id=str(current_user.id),
                resource_type="llm_model", resource_id=str(model.id),
                details={
                    "model_id": model.model_id,
                    "input_cost_per_million_tokens_usd": model.input_cost_per_million_tokens_usd,
                    "output_cost_per_million_tokens_usd": model.output_cost_per_million_tokens_usd,
                },
            )
        except Exception:
            pass

        return {
            "success": True,
            "input_cost_per_million_tokens_usd": model.input_cost_per_million_tokens_usd,
            "output_cost_per_million_tokens_usd": model.output_cost_per_million_tokens_usd,
        }

    async def set_routing_hint(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_id: str,
        hint: str | None,
    ):
        """Set (or clear) a model's Auto-router guidance.

        Stored on ``LLMModel.config['routing_hint']`` and merged so other config
        keys (e.g. reasoning_effort) are preserved. A non-empty hint makes the
        model a routing target the planner can escalate to; clearing it removes
        the model from the routing set. Empty/whitespace clears.
        """
        model = await db.execute(
            select(LLMModel).join(LLMProvider).filter(
                LLMModel.id == model_id,
                LLMProvider.organization_id == organization.id,
            )
        )
        model = model.scalar_one_or_none()
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")

        cfg = dict(model.config or {})
        clean = (hint or "").strip()
        if clean:
            if len(clean) > 500:
                clean = clean[:500]
            cfg["routing_hint"] = clean
        else:
            cfg.pop("routing_hint", None)
        # Reassign (not mutate) so SQLAlchemy detects the JSON change.
        model.config = cfg
        await db.commit()

        logger.info(
            "LLM model routing hint set: id=%s, model_id=%s, has_hint=%s, org_id=%s",
            model.id, model.model_id, bool(clean), organization.id,
        )
        try:
            await audit_service.log(
                db=db, organization_id=str(organization.id),
                action="llm_model.routing_hint_set", user_id=str(current_user.id),
                resource_type="llm_model", resource_id=str(model.id),
                details={"model_id": model.model_id, "has_hint": bool(clean)},
            )
        except Exception:
            pass

        return {"success": True, "routing_hint": cfg.get("routing_hint")}

    async def get_fallback_order(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
    ):
        """The org's LLM fallback state: toggle value + ordered model summaries.

        Entries whose model has since been disabled/deleted are kept in the
        stored order (so re-enabling restores them) but flagged inactive so the
        UI can badge them.
        """
        settings = await organization.get_settings(db)
        from app.ai.llm.fallback import get_fallback_order as _read_order
        order = _read_order(settings)
        enabled = bool(getattr(settings.get_config("llm_fallback"), "value", False))

        models = []
        if order:
            result = await db.execute(
                select(LLMModel).join(LLMProvider).filter(
                    LLMModel.id.in_(order),
                    LLMModel.organization_id == organization.id,
                )
            )
            by_id = {str(m.id): m for m in result.unique().scalars().all()}
            for mid in order:
                m = by_id.get(mid)
                if not m:
                    continue
                models.append({
                    "id": str(m.id),
                    "name": m.name,
                    "model_id": m.model_id,
                    "provider_type": getattr(m.provider, "provider_type", None),
                    "provider_name": getattr(m.provider, "name", None),
                    "is_active": bool(m.is_enabled and m.deleted_at is None and m.provider and m.provider.is_enabled),
                })
        return {"enabled": enabled, "order": models}

    async def set_fallback_order(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        model_ids: list[str],
    ):
        """Persist the org's ordered LLM fallback list (settings key
        ``llm_fallback_order``). Validates every id against the org's models;
        dedupes preserving order; caps the length."""
        from app.ai.llm.fallback import MAX_FALLBACK_CHAIN

        seen: set[str] = set()
        ids: list[str] = []
        for raw in model_ids or []:
            mid = str(raw).strip()
            if mid and mid not in seen:
                seen.add(mid)
                ids.append(mid)
        if len(ids) > MAX_FALLBACK_CHAIN:
            raise HTTPException(status_code=400, detail=f"Fallback order is capped at {MAX_FALLBACK_CHAIN} models.")

        if ids:
            result = await db.execute(
                select(LLMModel.id).join(LLMProvider).filter(
                    LLMModel.id.in_(ids),
                    LLMModel.organization_id == organization.id,
                    LLMModel.deleted_at == None,  # noqa: E711
                )
            )
            known = {str(r[0]) for r in result.all()}
            missing = [i for i in ids if i not in known]
            if missing:
                raise HTTPException(status_code=400, detail=f"Unknown model id(s) in fallback order: {', '.join(missing)}")

        settings = await organization.get_settings(db)
        cfg = dict(settings.config or {})
        cfg["llm_fallback_order"] = ids
        settings.config = cfg  # reassign so SQLAlchemy detects the JSON change
        db.add(settings)
        await db.commit()

        logger.info("LLM fallback order set: org_id=%s, count=%d", organization.id, len(ids))
        try:
            await audit_service.log(
                db=db, organization_id=str(organization.id),
                action="llm.fallback_order_set", user_id=str(current_user.id),
                resource_type="organization_settings", resource_id=str(organization.id),
                details={"model_ids": ids},
            )
        except Exception:
            pass

        return await self.get_fallback_order(db, organization, current_user)

    async def _create_models(
        self,
        db: AsyncSession,
        organization: Organization,
        provider: LLMProvider,
        current_user: User,
        models: list[dict]
    ):
        # First check if org already has a default model
        existing_default = await db.execute(
            select(LLMModel)
            .filter(LLMModel.organization_id == organization.id)
            .filter(LLMModel.is_default == True)
        )
        # .first() (not scalar_one_or_none) so an org that already has more than
        # one default model doesn't blow up here — we only need to know one exists.
        has_default_model = existing_default.scalars().first() is not None
        # And whether org already has a small default model
        existing_small_default = await db.execute(
            select(LLMModel)
            .filter(LLMModel.organization_id == organization.id)
            .filter(getattr(LLMModel, "is_small_default") == True)
        )
        # Same here: tolerate multiple small-default rows instead of raising.
        has_small_default_model = existing_small_default.scalars().first() is not None

        def _catalog_details(model: dict) -> dict | None:
            return next(
                (
                    catalog_model
                    for catalog_model in LLM_MODEL_DETAILS
                    if catalog_model["model_id"] == model["model_id"]
                    and catalog_model["provider_type"] == provider.provider_type
                ),
                None
            )

        def _incoming_model_is_enabled(model: dict, model_details: dict | None) -> bool:
            if provider.is_preset and model_details and not model.get("is_custom", False):
                return model_details.get("is_enabled", True)
            return model.get("is_enabled", True)

        desired_default_model_id = None
        desired_small_default_model_id = None
        for model in models:
            model_details = _catalog_details(model)
            if not model_details or not _incoming_model_is_enabled(model, model_details):
                continue
            if (
                desired_default_model_id is None
                and not has_default_model
                and model_details.get("is_default", False)
            ):
                desired_default_model_id = model["model_id"]
            if (
                desired_small_default_model_id is None
                and not has_small_default_model
                and model_details.get("is_small_default", False)
            ):
                desired_small_default_model_id = model["model_id"]

        for model in models:
            # For preset models: remove context_window_tokens and pricing from model dict (we only use preset values)
            # For custom models: allow these fields to be set by clients
            is_preset_model = model.get("is_preset", False) or not model.get("is_custom", False)
            if is_preset_model:
                model_dict = {
                    k: v for k, v in model.items() 
                    if k not in ["context_window_tokens", "input_cost_per_million_tokens_usd", "output_cost_per_million_tokens_usd"]
                }
            else:
                model_dict = model
            db_model = LLMModel(**model_dict)
            db_model.organization_id = organization.id
            db_model.provider = provider
            db_model.is_custom = model.get("is_custom", False)
            
            # Check if this model would be default according to config
            model_details = _catalog_details(model)

            if provider.is_preset and model_details and not db_model.is_custom:
                db_model.is_enabled = model_details.get("is_enabled", True)
            else:
                db_model.is_enabled = model.get("is_enabled", True)

            # Image-generation models are NOT chat models — never (auto-)assign
            # them as the org's default or small default.
            is_image_gen = bool(
                (model_details or {}).get("supports_image_generation")
                or model.get("supports_image_generation")
            )

            # Only set as default if there's no existing default and this model should be default
            if is_image_gen:
                db_model.is_default = False
            elif desired_default_model_id and model["model_id"] == desired_default_model_id and not has_default_model:
                db_model.is_default = True
                # Only allow one default model
                has_default_model = True
            # Fallback: if org still has no default and this is an enabled model, make it the default
            # This ensures custom/Azure providers (not in LLM_MODEL_DETAILS) get a default model
            elif not desired_default_model_id and not has_default_model and db_model.is_enabled:
                db_model.is_default = True
                has_default_model = True
            else:
                db_model.is_default = False

            # Only set as small default if there's no existing small default and this model should be small default
            if is_image_gen:
                setattr(db_model, "is_small_default", False)
            elif desired_small_default_model_id and model["model_id"] == desired_small_default_model_id and not has_small_default_model:
                setattr(db_model, "is_small_default", True)
                has_small_default_model = True
            # Fallback: if org still has no small default and this is an enabled model, make it the small default
            elif not desired_small_default_model_id and not has_small_default_model and db_model.is_enabled:
                setattr(db_model, "is_small_default", True)
                has_small_default_model = True
            else:
                setattr(db_model, "is_small_default", False)
            
            # Set context_window_tokens, pricing, and supports_vision
            # For preset models: use values from LLM_MODEL_DETAILS
            # For custom models: use values from model dict if provided (already set via LLMModel(**model_dict))
            # A non-null supports_vision_override always wins over the catalog default (see _resolve_supports_vision).
            vision_override = model.get("supports_vision_override")
            db_model.supports_vision_override = vision_override
            cw_override = model.get("context_window_tokens_override")
            db_model.context_window_tokens_override = cw_override
            if model_details and not db_model.is_custom:
                if model_details.get("context_window_tokens") is not None:
                    db_model.context_window_tokens = model_details["context_window_tokens"]
                if model_details.get("max_output_tokens") is not None:
                    db_model.max_output_tokens = model_details["max_output_tokens"]
                if model_details.get("input_cost_per_million_tokens_usd") is not None:
                    db_model.input_cost_per_million_tokens_usd = model_details["input_cost_per_million_tokens_usd"]
                if model_details.get("output_cost_per_million_tokens_usd") is not None:
                    db_model.output_cost_per_million_tokens_usd = model_details["output_cost_per_million_tokens_usd"]
                db_model.supports_vision = self._resolve_supports_vision(
                    vision_override, model_details.get("supports_vision", False)
                )
                img_override = model.get("supports_image_generation_override")
                db_model.supports_image_generation_override = img_override
                db_model.supports_image_generation = self._resolve_supports_vision(
                    img_override, model_details.get("supports_image_generation", False)
                )
            elif db_model.is_custom:
                # Inherit catalog fields when model_id+provider_type match; user values take precedence.
                if model_details:
                    if not model.get("name"):
                        db_model.name = model_details["name"]
                    if db_model.context_window_tokens is None and model_details.get("context_window_tokens") is not None:
                        db_model.context_window_tokens = model_details["context_window_tokens"]
                    if db_model.max_output_tokens is None and model_details.get("max_output_tokens") is not None:
                        db_model.max_output_tokens = model_details["max_output_tokens"]
                    if db_model.input_cost_per_million_tokens_usd is None and model_details.get("input_cost_per_million_tokens_usd") is not None:
                        db_model.input_cost_per_million_tokens_usd = model_details["input_cost_per_million_tokens_usd"]
                    if db_model.output_cost_per_million_tokens_usd is None and model_details.get("output_cost_per_million_tokens_usd") is not None:
                        db_model.output_cost_per_million_tokens_usd = model_details["output_cost_per_million_tokens_usd"]
                    base_vision = bool(model.get("supports_vision")) or model_details.get("supports_vision", False)
                    db_model.supports_vision = self._resolve_supports_vision(vision_override, base_vision)
                    img_override = model.get("supports_image_generation_override")
                    db_model.supports_image_generation_override = img_override
                    base_image = bool(model.get("supports_image_generation")) or model_details.get("supports_image_generation", False)
                    db_model.supports_image_generation = self._resolve_supports_vision(img_override, base_image)
                else:
                    db_model.supports_vision = self._resolve_supports_vision(
                        vision_override, model.get("supports_vision", False)
                    )
            else:
                db_model.supports_vision = self._resolve_supports_vision(
                    vision_override, db_model.supports_vision
                )

            # A non-null context-window override always wins over catalog/user values.
            if cw_override is not None:
                db_model.context_window_tokens = int(cw_override)
            # Custom model, no catalog match, no user/admin size: assume a
            # conservative default rather than leaving the window NULL (which
            # silently degrades every window-derived budget downstream).
            if db_model.is_custom and db_model.context_window_tokens is None:
                db_model.context_window_tokens = DEFAULT_CUSTOM_MODEL_CONTEXT_WINDOW

            db.add(db_model)

        await db.commit()

    def _set_provider_credentials(
        self, 
        provider: LLMProvider,
        credentials: dict
    ):
        api_key = credentials.get("api_key") or None
        api_secret = credentials.get("api_secret") or None

        # Merge/maintain provider-specific additional_config
        # Always work on a COPY so SQLAlchemy sees a new object assignment for JSON column
        existing_additional_config = dict(provider.additional_config or {})

        # Azure: endpoint_url
        if provider.provider_type == "azure":
            # Only act on endpoint_url if the key is present in the payload
            if "endpoint_url" in credentials:
                endpoint_url = credentials.get("endpoint_url")
                if endpoint_url:
                    existing_additional_config = { **existing_additional_config, "endpoint_url": endpoint_url }
                elif credentials.get("endpoint_url") is None or credentials.get("endpoint_url") == "":
                    # Explicitly clear endpoint_url when set to empty/null
                    existing_additional_config.pop("endpoint_url", None)

        # OpenAI: base_url (optional)
        if provider.provider_type == "openai":
            base_url = credentials.get("base_url")
            if base_url:
                existing_additional_config = { **existing_additional_config, "base_url": base_url }
            elif "base_url" in credentials and (credentials.get("base_url") is None or credentials.get("base_url") == ""):
                # Explicitly clear base_url
                existing_additional_config.pop("base_url", None)

        # OpenAI / Azure: native web search opt-in (non-secret flag → additional_config)
        if provider.provider_type in ("openai", "azure"):
            if "enable_web_search" in credentials:
                existing_additional_config = {
                    **existing_additional_config,
                    "enable_web_search": bool(credentials.get("enable_web_search")),
                }

        # Azure: opt-in to the Responses API (off → Chat Completions, works in
        # every region). Gates web search.
        if provider.provider_type == "azure":
            if "use_responses_api" in credentials:
                existing_additional_config = {
                    **existing_additional_config,
                    "use_responses_api": bool(credentials.get("use_responses_api")),
                }

        # Custom (OpenAI-compatible): base_url (required), verify_ssl (optional)
        if provider.provider_type == "custom":
            base_url = credentials.get("base_url")
            if base_url:
                existing_additional_config = { **existing_additional_config, "base_url": base_url }
            # For custom providers, base_url is required - don't clear it
            if "verify_ssl" in credentials:
                raw_verify_ssl = credentials.get("verify_ssl", True)
                # Coerce string values to boolean (frontend may send "true"/"false" strings)
                if isinstance(raw_verify_ssl, str):
                    verify_ssl = raw_verify_ssl.lower() not in ("false", "0", "no", "")
                else:
                    verify_ssl = bool(raw_verify_ssl)
                existing_additional_config = { **existing_additional_config, "verify_ssl": verify_ssl }

        # Bedrock: region (required), auth_mode
        if provider.provider_type == "bedrock":
            region = credentials.get("region")
            if region:
                existing_additional_config = { **existing_additional_config, "region": region }
            # Only update auth_mode when explicitly present in the payload
            if "auth_mode" in credentials:
                raw_auth_mode = credentials.get("auth_mode")
                # Normalize to lowercase string if provided as a string
                if isinstance(raw_auth_mode, str):
                    auth_mode = raw_auth_mode.lower()
                else:
                    auth_mode = raw_auth_mode

                allowed_auth_modes = {"iam", "api_key", "access_keys"}
                if auth_mode not in allowed_auth_modes:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid auth_mode for Bedrock provider: {raw_auth_mode!r}. "
                               f"Allowed values are: {', '.join(sorted(allowed_auth_modes))}."
                    )

                existing_additional_config = { **existing_additional_config, "auth_mode": auth_mode }

            # Map AWS access keys to api_key/api_secret for encrypted storage
            if credentials.get("aws_access_key_id"):
                api_key = credentials["aws_access_key_id"]
            if credentials.get("aws_secret_access_key"):
                api_secret = credentials["aws_secret_access_key"]
        provider.additional_config = existing_additional_config if existing_additional_config else None

        # Only (re-)encrypt credentials when a new key/secret is provided
        if api_key is not None or api_secret is not None:
            # If only one of them provided, keep the other as existing if present
            try:
                existing_api_key, existing_api_secret = provider.decrypt_credentials()
            except Exception:
                existing_api_key, existing_api_secret = None, None

            effective_api_key = api_key if api_key is not None else existing_api_key
            effective_api_secret = api_secret if api_secret is not None else existing_api_secret
            provider.encrypt_credentials(effective_api_key, effective_api_secret)

    async def _disable_models(
        self, 
        db: AsyncSession,
        organization: Organization,
        provider: LLMProvider
    ):
        for model in provider.models:
            model.is_enabled = False
            db.add(model)
        await db.commit()

    async def _update_models(
        self, 
        db: AsyncSession,
        organization: Organization,
        provider: LLMProvider,
        current_user: User,
        models: list[LLMModelSchema]
    ):
        # Check if org already has default models (needed for new model creation)
        existing_default = await db.execute(
            select(LLMModel)
            .filter(LLMModel.organization_id == organization.id)
            .filter(LLMModel.is_default == True)
        )
        # .first() (not scalar_one_or_none) so an org that already has more than
        # one default model doesn't blow up here — we only need to know one exists.
        has_default_model = existing_default.scalars().first() is not None
        existing_small_default = await db.execute(
            select(LLMModel)
            .filter(LLMModel.organization_id == organization.id)
            .filter(getattr(LLMModel, "is_small_default") == True)
        )
        # Same here: tolerate multiple small-default rows instead of raising.
        has_small_default_model = existing_small_default.scalars().first() is not None

        for model in models:
            # If model has an ID, update existing model
            if model.id:
                db_model = await db.execute(
                    select(LLMModel).filter(LLMModel.id == model.id)
                )
                db_model = db_model.scalar_one_or_none()

                if not db_model:
                    raise HTTPException(status_code=404, detail="Model not found")

                # Update fields that can be changed
                if db_model.is_enabled != model.is_enabled:
                    db_model.is_enabled = model.is_enabled
                    db.add(db_model)
                
                # Optional token/pricing/vision fields
                # For preset models: sync from LLM_MODEL_DETAILS (not updatable by clients)
                # For custom models: allow clients to optionally set these values
                catalog = next(
                    (m for m in LLM_MODEL_DETAILS if m["model_id"] == db_model.model_id and m["provider_type"] == provider.provider_type),
                    None
                )
                # A non-null vision override from the client is honored for both preset and custom models.
                if getattr(model, "supports_vision_override", None) is not None:
                    db_model.supports_vision_override = model.supports_vision_override
                # Same for the image-generation override (mark/unmark as an image model).
                if getattr(model, "supports_image_generation_override", None) is not None:
                    db_model.supports_image_generation_override = model.supports_image_generation_override
                # Same for a context-window override.
                if getattr(model, "context_window_tokens_override", None) is not None:
                    db_model.context_window_tokens_override = model.context_window_tokens_override
                if db_model.is_preset:
                    if catalog:
                        if catalog.get("context_window_tokens") is not None:
                            db_model.context_window_tokens = catalog["context_window_tokens"]
                        if catalog.get("max_output_tokens") is not None:
                            db_model.max_output_tokens = catalog["max_output_tokens"]
                        if catalog.get("input_cost_per_million_tokens_usd") is not None:
                            db_model.input_cost_per_million_tokens_usd = catalog["input_cost_per_million_tokens_usd"]
                        if catalog.get("output_cost_per_million_tokens_usd") is not None:
                            db_model.output_cost_per_million_tokens_usd = catalog["output_cost_per_million_tokens_usd"]
                    catalog_vision = catalog.get("supports_vision", False) if catalog else db_model.supports_vision
                    db_model.supports_vision = self._resolve_supports_vision(
                        db_model.supports_vision_override, catalog_vision
                    )
                    catalog_image = catalog.get("supports_image_generation", False) if catalog else db_model.supports_image_generation
                    db_model.supports_image_generation = self._resolve_supports_vision(
                        db_model.supports_image_generation_override, catalog_image
                    )
                else:
                    # Custom models: user values take precedence; fall back to catalog when model_id+provider_type match.
                    if getattr(model, "context_window_tokens", None) is not None:
                        db_model.context_window_tokens = model.context_window_tokens
                    elif catalog and catalog.get("context_window_tokens") is not None:
                        db_model.context_window_tokens = catalog["context_window_tokens"]
                    if getattr(model, "max_output_tokens", None) is not None:
                        db_model.max_output_tokens = model.max_output_tokens
                    elif catalog and catalog.get("max_output_tokens") is not None:
                        db_model.max_output_tokens = catalog["max_output_tokens"]
                    if getattr(model, "input_cost_per_million_tokens_usd", None) is not None:
                        db_model.input_cost_per_million_tokens_usd = model.input_cost_per_million_tokens_usd
                    elif catalog and catalog.get("input_cost_per_million_tokens_usd") is not None:
                        db_model.input_cost_per_million_tokens_usd = catalog["input_cost_per_million_tokens_usd"]
                    if getattr(model, "output_cost_per_million_tokens_usd", None) is not None:
                        db_model.output_cost_per_million_tokens_usd = model.output_cost_per_million_tokens_usd
                    elif catalog and catalog.get("output_cost_per_million_tokens_usd") is not None:
                        db_model.output_cost_per_million_tokens_usd = catalog["output_cost_per_million_tokens_usd"]
                    base_vision = bool(getattr(model, "supports_vision", False)) or (
                        catalog.get("supports_vision", False) if catalog else False
                    )
                    db_model.supports_vision = self._resolve_supports_vision(
                        db_model.supports_vision_override, base_vision
                    )
                    base_image = bool(getattr(model, "supports_image_generation", False)) or (
                        catalog.get("supports_image_generation", False) if catalog else False
                    )
                    db_model.supports_image_generation = self._resolve_supports_vision(
                        db_model.supports_image_generation_override, base_image
                    )

                # A non-null context-window override always wins over catalog/user values
                # (this is what keeps preset models from re-syncing back to the catalog size).
                if db_model.context_window_tokens_override is not None:
                    db_model.context_window_tokens = int(db_model.context_window_tokens_override)

                db.add(db_model)
            else:
                # If model doesn't have an ID, create new model
                # For preset models: get context_window_tokens, pricing, and vision from LLM_MODEL_DETAILS
                # For custom models: allow clients to optionally set these values
                context_window_tokens = None
                max_output_tokens = None
                input_cost = None
                output_cost = None
                supports_vision = False

                catalog = next(
                    (m for m in LLM_MODEL_DETAILS if m["model_id"] == model.model_id and m["provider_type"] == provider.provider_type),
                    None
                )
                if model.is_preset:
                    if catalog:
                        if catalog.get("context_window_tokens") is not None:
                            context_window_tokens = catalog["context_window_tokens"]
                        if catalog.get("max_output_tokens") is not None:
                            max_output_tokens = catalog["max_output_tokens"]
                        if catalog.get("input_cost_per_million_tokens_usd") is not None:
                            input_cost = catalog["input_cost_per_million_tokens_usd"]
                        if catalog.get("output_cost_per_million_tokens_usd") is not None:
                            output_cost = catalog["output_cost_per_million_tokens_usd"]
                        supports_vision = catalog.get("supports_vision", False)
                else:
                    # User values take precedence; fall back to catalog when model_id+provider_type match.
                    context_window_tokens = getattr(model, "context_window_tokens", None) or (catalog.get("context_window_tokens") if catalog else None)
                    max_output_tokens = getattr(model, "max_output_tokens", None) or (catalog.get("max_output_tokens") if catalog else None)
                    input_cost = getattr(model, "input_cost_per_million_tokens_usd", None) or (catalog.get("input_cost_per_million_tokens_usd") if catalog else None)
                    output_cost = getattr(model, "output_cost_per_million_tokens_usd", None) or (catalog.get("output_cost_per_million_tokens_usd") if catalog else None)
                    supports_vision = getattr(model, "supports_vision", False) or (catalog.get("supports_vision", False) if catalog else False)

                # A non-null vision override from the client wins over the catalog default.
                supports_vision_override = getattr(model, "supports_vision_override", None)
                supports_vision = self._resolve_supports_vision(supports_vision_override, supports_vision)
                # Same for a context-window override.
                context_window_tokens_override = getattr(model, "context_window_tokens_override", None)
                context_window_tokens = self._resolve_context_window(context_window_tokens_override, context_window_tokens)

                # Set as default if org has no default and this model is enabled
                should_be_default = not has_default_model and model.is_enabled
                should_be_small_default = not has_small_default_model and model.is_enabled

                db_model = LLMModel(
                    name=model.name or (catalog["name"] if catalog else None) or model.model_id,
                    model_id=model.model_id,
                    provider=provider,
                    organization_id=organization.id,
                    is_enabled=model.is_enabled,
                    is_custom=model.is_custom,
                    is_preset=model.is_preset,
                    is_default=should_be_default,
                    is_small_default=should_be_small_default,
                    supports_vision=supports_vision,
                    supports_vision_override=supports_vision_override,
                    context_window_tokens=context_window_tokens,
                    context_window_tokens_override=context_window_tokens_override,
                    max_output_tokens=max_output_tokens,
                    input_cost_per_million_tokens_usd=input_cost,
                    output_cost_per_million_tokens_usd=output_cost,
                )
                db.add(db_model)
                
                # Update flags so subsequent models don't also become default
                if should_be_default:
                    has_default_model = True
                if should_be_small_default:
                    has_small_default_model = True

        await db.commit()
    
    async def set_default_model(
        self, 
        db: AsyncSession,
        current_user: User,
        organization: Organization,
        model_id: str,
        small: bool = False
    ):
        default_model = await db.execute(
            select(LLMModel).filter(LLMModel.id == model_id)
        )
        default_model = default_model.scalar_one_or_none()

        if not default_model:
            raise HTTPException(status_code=404, detail="Model not found")
        
        if not default_model.is_enabled:
            raise HTTPException(status_code=400, detail="Model is not enabled")

        # Image-generation models are not chat models — they can never be the
        # org's default or small default.
        if getattr(default_model, "supports_image_generation", False):
            raise HTTPException(
                status_code=400,
                detail="Image-generation models cannot be set as the default or small default model.",
            )

        org_models = await db.execute(
            select(LLMModel).filter(LLMModel.organization_id == organization.id)
        )
        org_models = org_models.unique().scalars().all()

        if small:
            for model in org_models:
                model.is_small_default = False
                db.add(model)
            default_model.is_small_default = True
        else:
            for model in org_models:
                model.is_default = False
                db.add(model)
            default_model.is_default = True

        db.add(default_model)
        await db.commit()

        logger.info("LLM default model set: id=%s, name=%s, model_id=%s, small=%s, org_id=%s", default_model.id, default_model.name, default_model.model_id, small, organization.id)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="llm_model.set_default",
                user_id=str(current_user.id),
                resource_type="llm_model",
                resource_id=str(default_model.id),
                details={"name": default_model.name, "model_id": default_model.model_id, "small": small},
            )
        except Exception:
            pass

        return {"success": True}
    
    async def get_default_model(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        is_small: bool = False
    ):
        """Get the default model for an organization. If is_small=True, prefer small default, fallback to regular, then first enabled."""
        if is_small:
            small_default = await db.execute(
                select(LLMModel)
                .filter(LLMModel.organization_id == organization.id)
                .filter(getattr(LLMModel, "is_small_default") == True)
                .filter(LLMModel.is_enabled == True)
            )
            # .first() so a duplicated small-default flag resolves to one model
            # instead of raising MultipleResultsFound on the hot completion path.
            small_default = small_default.scalars().first()
            if small_default:
                return small_default
        # Regular default
        default = await db.execute(
            select(LLMModel)
            .filter(LLMModel.organization_id == organization.id)
            .filter(LLMModel.is_default == True)
            .filter(LLMModel.is_enabled == True)
        )
        default_model = default.scalars().first()
        if default_model:
            return default_model
        
        # Fallback: return first enabled model (for custom providers without is_default set)
        first_enabled = await db.execute(
            select(LLMModel)
            .filter(LLMModel.organization_id == organization.id)
            .filter(LLMModel.is_enabled == True)
            .limit(1)
        )
        return first_enabled.scalar_one_or_none()

    async def get_user_default_model_id(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
    ) -> str | None:
        """Raw per-user default preference (membership column). May be stale —
        callers that need a usable model go through get_default_model_for_user."""
        from app.models.membership import Membership
        result = await db.execute(
            select(Membership.default_llm_model_id).where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(organization.id),
            )
        )
        row = result.first()
        return row[0] if row else None

    async def get_default_model_for_user(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
    ):
        """Effective default model for interactive flows.

        The user's per-org default wins when it is still enabled, its provider
        is alive, and the user can still use it (a model can become restricted
        after being picked). Anything stale falls back silently to the org
        default — a user preference must never break chat.
        """
        if user is not None:
            preferred_id = await self.get_user_default_model_id(db, organization, user)
            if preferred_id:
                result = await db.execute(
                    select(LLMModel)
                    .join(LLMModel.provider)
                    .filter(LLMModel.id == preferred_id)
                    .filter(LLMModel.organization_id == organization.id)
                    .filter(LLMModel.deleted_at == None)
                    .filter(LLMModel.is_enabled == True)
                    .filter(LLMProvider.deleted_at == None)
                    .filter(LLMProvider.is_enabled == True)
                )
                model = result.unique().scalar_one_or_none()
                if model is not None:
                    from app.core.permission_resolver import user_can_use_model
                    if await user_can_use_model(db, user.id, organization.id, model):
                        return model
        return await self.get_default_model(db, organization, user)

    async def set_user_default_model(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        model_id: str | None,
    ) -> str | None:
        """Set (or clear, with None) the current user's default model.

        Unlike resolution, setting is strict: the model must exist in the org,
        be enabled, and be usable by this user — restricted models require a
        grant even though org defaults would bypass that check elsewhere.
        """
        from app.models.membership import Membership

        if model_id:
            result = await db.execute(
                select(LLMModel)
                .join(LLMModel.provider)
                .filter(LLMModel.id == model_id)
                .filter(LLMModel.organization_id == organization.id)
                .filter(LLMModel.deleted_at == None)
                .filter(LLMProvider.deleted_at == None)
                .filter(LLMProvider.is_enabled == True)
            )
            model = result.unique().scalar_one_or_none()
            if model is None:
                raise HTTPException(status_code=404, detail="Model not found")
            if not model.is_enabled:
                raise HTTPException(status_code=400, detail="Model is not enabled")
            from app.core.permission_resolver import user_can_use_model
            if not await user_can_use_model(db, user.id, organization.id, model):
                raise HTTPException(status_code=403, detail="You do not have access to this model")

        result = await db.execute(
            select(Membership).where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(organization.id),
            )
        )
        membership = result.scalars().first()
        if membership is None:
            raise HTTPException(status_code=404, detail="Membership not found")

        membership.default_llm_model_id = str(model_id) if model_id else None
        db.add(membership)
        await db.commit()
        return membership.default_llm_model_id

    async def get_default_model_for_report(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        report,
    ):
        """Effective default model for a completion run in a given report.

        Precedence: the report-level override wins when it is still enabled, its
        provider is alive, and the user running the completion can use it (a
        model can be disabled/restricted after being picked, or set by a
        teammate with access this user lacks). Anything stale falls back
        silently to the per-user default, then the org default — a report
        preference must never break chat. This mirrors
        get_default_model_for_user, layered one tier above it.
        """
        report_model_id = getattr(report, "model_id", None) if report is not None else None
        if report_model_id:
            result = await db.execute(
                select(LLMModel)
                .join(LLMModel.provider)
                .filter(LLMModel.id == report_model_id)
                .filter(LLMModel.organization_id == organization.id)
                .filter(LLMModel.deleted_at == None)
                .filter(LLMModel.is_enabled == True)
                .filter(LLMProvider.deleted_at == None)
                .filter(LLMProvider.is_enabled == True)
            )
            model = result.unique().scalar_one_or_none()
            if model is not None:
                # No user (system/scheduled path) → honor the report model as-is;
                # with a user, gate on their access like the user-default path.
                if user is None:
                    return model
                from app.core.permission_resolver import user_can_use_model
                if await user_can_use_model(db, user.id, organization.id, model):
                    return model
        return await self.get_default_model_for_user(db, organization, user)

    async def validate_model_for_user(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        model_id: str,
    ) -> LLMModel:
        """Strict validation used when a user *sets* a model reference (e.g. the
        report-level override). Unlike resolution, this raises: the model must
        exist in the org, be enabled, and be usable by this user — restricted
        models require a grant even though defaults bypass that check at read
        time. Returns the validated model.
        """
        result = await db.execute(
            select(LLMModel)
            .join(LLMModel.provider)
            .filter(LLMModel.id == model_id)
            .filter(LLMModel.organization_id == organization.id)
            .filter(LLMModel.deleted_at == None)
            .filter(LLMProvider.deleted_at == None)
            .filter(LLMProvider.is_enabled == True)
        )
        model = result.unique().scalar_one_or_none()
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found")
        if not model.is_enabled:
            raise HTTPException(status_code=400, detail="Model is not enabled")
        if user is not None:
            from app.core.permission_resolver import user_can_use_model
            if not await user_can_use_model(db, user.id, organization.id, model):
                raise HTTPException(status_code=403, detail="You do not have access to this model")
        return model

    async def set_default_models_from_config(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User
    ):
        if not settings.bow_config.default_llm:
            return
        
        for llm_config in settings.bow_config.default_llm:
            api_key = llm_config.api_key
            api_secret = ""

            provider = LLMProvider(
                name=llm_config.provider_name,
                provider_type=llm_config.provider_type,
                organization_id=organization.id,
                is_preset=True,
                use_preset_credentials=True
            )
            provider.encrypt_credentials(api_key, api_secret)

            db.add(provider)
            
            # Create models for this provider
            for model_config in llm_config.models:
                # Extract model_id and name from the config
                model_id = model_config.model_id
                model_name = model_config.model_name
                is_default = model_config.is_default
                is_enabled = model_config.is_enabled
                is_small_default = model_config.is_small_default

                # Get context_window_tokens, pricing, and vision from LLM_MODEL_DETAILS if available
                model_details = next(
                    (m for m in LLM_MODEL_DETAILS if m["model_id"] == model_id),
                    None
                )
                context_window_tokens = model_details.get("context_window_tokens") if model_details else None
                max_output_tokens = model_details.get("max_output_tokens") if model_details else None
                input_cost = model_details.get("input_cost_per_million_tokens_usd") if model_details else None
                output_cost = model_details.get("output_cost_per_million_tokens_usd") if model_details else None
                supports_vision = model_details.get("supports_vision", False) if model_details else False

                model = LLMModel(
                    name=model_name,
                    model_id=model_id,
                    provider=provider,
                    organization_id=organization.id,
                    is_preset=True,
                    is_enabled=is_enabled,
                    is_default=is_default,
                    is_small_default=is_small_default,
                    supports_vision=supports_vision,
                    context_window_tokens=context_window_tokens,
                    max_output_tokens=max_output_tokens,
                    input_cost_per_million_tokens_usd=input_cost,
                    output_cost_per_million_tokens_usd=output_cost
                )
                db.add(model)

        await db.commit()
        
    @staticmethod
    def _resolve_supports_vision(override, catalog_value) -> bool:
        """Effective vision flag: a non-null admin override always wins over the catalog."""
        if override is not None:
            return bool(override)
        return bool(catalog_value)

    @staticmethod
    def _resolve_context_window(override, catalog_value):
        """Effective context window: a non-null admin override always wins over the catalog."""
        if override is not None:
            return int(override)
        return catalog_value

    @staticmethod
    def _apply_catalog_model_details(model: LLMModel, model_data: dict, *, sync_enabled: bool = False) -> None:
        model.name = model_data["name"]
        model.is_preset = model_data.get("is_preset", True)
        if sync_enabled:
            model.is_enabled = model_data.get("is_enabled", True)
        # Respect an admin's manual vision override so it survives catalog re-syncs.
        model.supports_vision = LLMService._resolve_supports_vision(
            getattr(model, "supports_vision_override", None),
            model_data.get("supports_vision", False),
        )
        # Same for the context window: an admin-set size survives catalog re-syncs.
        model.context_window_tokens = LLMService._resolve_context_window(
            getattr(model, "context_window_tokens_override", None),
            model_data.get("context_window_tokens"),
        )
        if "max_output_tokens" in model_data:
            model.max_output_tokens = model_data.get("max_output_tokens")
        model.input_cost_per_million_tokens_usd = model_data.get("input_cost_per_million_tokens_usd")
        model.output_cost_per_million_tokens_usd = model_data.get("output_cost_per_million_tokens_usd")

    async def _sync_provider_with_latest_models(
        self,
        db: AsyncSession,
        provider: LLMProvider,
        organization: Organization,
        customer_managed: bool = False,
    ):
        """Sync a provider with the latest models from LLM_MODEL_DETAILS.

        ``customer_managed`` marks a bring-your-own-key provider the admin
        created and curates. The catalog is still the source of truth for
        model *metadata* and for which models exist, but the admin's choices
        win over the catalog's opinions:
          - a model the admin disabled stays disabled (no enabled-state sync),
          - a model the admin deleted stays deleted (tombstones honored),
          - the org's default/small-default models are never re-pointed —
            except as a safety net when the org would otherwise be left with
            no enabled default at all (e.g. its default model id was retired
            from the catalog).
        New catalog models are added enabled for both kinds of provider.
        """
        available_models = [
            model for model in LLM_MODEL_DETAILS
            if model["provider_type"] == provider.provider_type
        ]
        catalog_by_id = {model["model_id"]: model for model in available_models}

        all_rows = (await db.execute(
            select(LLMModel)
            .filter(LLMModel.provider_id == provider.id)
        )).unique().scalars().all()
        provider_models = [m for m in all_rows if m.deleted_at is None]
        existing_by_id = {model.model_id: model for model in provider_models}
        # On customer-managed providers a soft-deleted row is a tombstone: the
        # admin removed that model on purpose, so the add-missing pass below
        # must not resurrect it. Preset (BOW-managed) providers keep the
        # historical recreate-anything-missing behavior.
        blocked_ids = (
            {m.model_id for m in all_rows if m.deleted_at is not None}
            if customer_managed else set()
        )

        provider_had_default = any(model.is_default for model in provider_models)
        provider_had_small_default = any(model.is_small_default for model in provider_models)

        for model in provider_models:
            model_data = catalog_by_id.get(model.model_id)
            if not model.is_preset:
                continue
            # Preset row whose id left the catalog: the provider retired the
            # model, so every call to it is a permanent 404 that no retry or
            # fallback can heal. Disable it and surrender the default flags —
            # the promotion below then moves the org onto the catalog's current
            # default instead of leaving it pinned to a dead id. Custom models
            # (skipped above) stay untouched; the row itself is kept so history
            # and usage records still resolve.
            if not model_data:
                model.is_enabled = False
                model.is_default = False
                model.is_small_default = False
                db.add(model)
                continue

            # Customer-managed: refresh catalog metadata but never sync the
            # enabled flag (the admin's on/off choices stand) and never strip
            # the admin's default flags to match catalog opinion.
            self._apply_catalog_model_details(model, model_data, sync_enabled=not customer_managed)
            if not customer_managed:
                if not model_data.get("is_default", False) or not model.is_enabled:
                    model.is_default = False
                if not model_data.get("is_small_default", False) or not model.is_enabled:
                    model.is_small_default = False
            db.add(model)

        # Add any missing models. On customer-managed providers only usable
        # (catalog-enabled) models are added — a catalog entry that ships
        # disabled would just clutter the admin's curated list with a dead row.
        # Preset providers keep the full catalog mirror.
        for model_data in available_models:
            if customer_managed and not model_data.get("is_enabled", True):
                continue
            if model_data["model_id"] not in existing_by_id and model_data["model_id"] not in blocked_ids:
                model = LLMModel(
                    name=model_data["name"],
                    model_id=model_data["model_id"],
                    is_preset=model_data["is_preset"],
                    is_enabled=model_data["is_enabled"],
                    provider=provider,
                    organization_id=organization.id,
                    is_default=False,
                    is_small_default=False,
                    supports_vision=model_data.get("supports_vision", False),
                    context_window_tokens=model_data.get("context_window_tokens"),
                    max_output_tokens=model_data.get("max_output_tokens"),
                    input_cost_per_million_tokens_usd=model_data.get("input_cost_per_million_tokens_usd"),
                    output_cost_per_million_tokens_usd=model_data.get("output_cost_per_million_tokens_usd")
                )
                db.add(model)
                provider_models.append(model)
                existing_by_id[model.model_id] = model

        await db.flush()

        org_models_result = await db.execute(
            select(LLMModel)
            .filter(LLMModel.organization_id == organization.id)
            .filter(LLMModel.deleted_at == None)
        )
        org_models = org_models_result.unique().scalars().all()

        default_data = next(
            (model for model in available_models if model.get("is_default") and model.get("is_enabled", True)),
            None,
        )
        desired_default = existing_by_id.get(default_data["model_id"]) if default_data else None
        has_enabled_default = any(model.is_default and model.is_enabled for model in org_models)
        # Customer-managed providers never have their default re-pointed to the
        # catalog's pick — promotion runs only as a safety net when the org has
        # no enabled default left (e.g. its default's model id was retired).
        default_promotion_allowed = (
            (not has_enabled_default) if customer_managed
            else (provider_had_default or not has_enabled_default)
        )
        if desired_default and desired_default.is_enabled and default_promotion_allowed:
            for model in org_models:
                model.is_default = False
                db.add(model)
            desired_default.is_default = True
            db.add(desired_default)

        small_default_data = next(
            (model for model in available_models if model.get("is_small_default") and model.get("is_enabled", True)),
            None,
        )
        desired_small_default = existing_by_id.get(small_default_data["model_id"]) if small_default_data else None
        has_enabled_small_default = any(model.is_small_default and model.is_enabled for model in org_models)
        small_default_promotion_allowed = (
            (not has_enabled_small_default) if customer_managed
            else (provider_had_small_default or not has_enabled_small_default)
        )
        if desired_small_default and desired_small_default.is_enabled and small_default_promotion_allowed:
            for model in org_models:
                model.is_small_default = False
                db.add(model)
            desired_small_default.is_small_default = True
            db.add(desired_small_default)
        
    async def test_connection(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        provider: LLMProviderTestConnection
    ):
        logger.info("Testing LLM connection: provider_type=%s, name=%s, provider_id=%s, org_id=%s, user_id=%s", provider.provider_type, provider.name, getattr(provider, 'provider_id', None), organization.id, current_user.id)

        # When testing an already-saved provider, load it so blank credential
        # fields fall back to the stored (encrypted) values.
        stored_provider = None
        if getattr(provider, 'provider_id', None):
            result = await db.execute(
                select(LLMProvider)
                .filter(LLMProvider.id == provider.provider_id)
                .filter(LLMProvider.organization_id == organization.id)
                .filter(LLMProvider.deleted_at == None)
            )
            stored_provider = result.unique().scalar_one_or_none()
            if stored_provider is None:
                raise HTTPException(status_code=404, detail="Provider not found")

        # Build an in-memory provider based on the payload (no DB writes)
        provider_obj = LLMProvider(
            name=provider.name,
            provider_type=provider.provider_type,
            organization_id=organization.id,
            is_preset=False,
            is_enabled=True,
            use_preset_credentials=False,
            additional_config=None
        )

        # Seed encrypted credentials + config from the saved provider; any
        # credentials supplied in the payload override these below.
        if stored_provider is not None:
            provider_obj.api_key = stored_provider.api_key
            provider_obj.api_secret = stored_provider.api_secret
            provider_obj.additional_config = dict(stored_provider.additional_config or {})

        # Set credentials and merge provider-specific additional_config
        self._set_provider_credentials(provider_obj, provider.credentials or {})

        # Choose a model to test from user-provided list, preferring default or custom
        selected_model = None
        if provider.models:
            # Try catalog default first among provided models
            catalog_default = next(
                (m for m in LLM_MODEL_DETAILS if m["provider_type"] == provider.provider_type and m.get("is_default")),
                None
            )
            preferred = None
            if catalog_default is not None:
                preferred = next((m for m in provider.models if m.get("model_id") == catalog_default["model_id"] and m.get("is_enabled", True)), None)

            # Prefer an explicitly default and enabled model from payload if still not found
            if not preferred:
                preferred = next((m for m in provider.models if m.get("is_default") and m.get("is_enabled", True)), None)
            # Otherwise prefer any enabled custom model
            if not preferred:
                preferred = next((m for m in provider.models if m.get("is_custom", False) and m.get("is_enabled", True)), None)
            # Otherwise prefer any enabled model
            if not preferred:
                preferred = next((m for m in provider.models if m.get("is_enabled", True)), None)

            if preferred:
                selected_model = LLMModel(
                    name=preferred.get("name") or preferred.get("model_id"),
                    model_id=preferred["model_id"],
                    provider=provider_obj,
                    organization_id=organization.id,
                    is_enabled=True,
                    is_custom=preferred.get("is_custom", False),
                    is_preset=preferred.get("is_preset", False),
                    is_default=False
                )

        # Fallback to default/first enabled model for the provider type
        if selected_model is None:
            default_model_data = next(
                (m for m in LLM_MODEL_DETAILS if m["provider_type"] == provider.provider_type and m.get("is_default")),
                None
            )
            if default_model_data is None:
                default_model_data = next(
                    (m for m in LLM_MODEL_DETAILS if m["provider_type"] == provider.provider_type and m.get("is_enabled")),
                    None
                )
            if default_model_data is None:
                raise HTTPException(status_code=400, detail="No available models for the specified provider type")

            selected_model = LLMModel(
                name=default_model_data["name"],
                model_id=default_model_data["model_id"],
                provider=provider_obj,
                organization_id=organization.id,
                is_enabled=True,
                is_custom=False,
                is_preset=bool(default_model_data.get("is_preset", False)),
                is_default=False
            )

        # Run a lightweight connection test against the LLM client
        logger.info("Testing LLM connection with model: model_id=%s, provider_type=%s", selected_model.model_id, provider.provider_type)
        llm = LLM(selected_model, usage_session_maker=async_session_maker)
        result = await llm.test_connection()
        if result.get("success"):
            logger.info("LLM connection test passed: provider_type=%s, model_id=%s, org_id=%s", provider.provider_type, selected_model.model_id, organization.id)
        else:
            logger.error("LLM connection test failed: provider_type=%s, model_id=%s, org_id=%s, message=%s", provider.provider_type, selected_model.model_id, organization.id, result.get("message"))
        return result
