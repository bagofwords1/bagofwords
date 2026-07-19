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
from app.models.llm_model import LLM_MODEL_DETAILS
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
        logger.info("Creating LLM provider: name=%s, type=%s, org_id=%s, user_id=%s", provider_data.name, provider_data.provider_type, organization.id, current_user.id)

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
            logger.warning("Duplicate LLM provider name: name=%s, org_id=%s", provider.name, organization.id)
            raise HTTPException(
                status_code=409,
                detail=f"A provider named '{provider.name}' already exists in this organization. Please choose a different name."
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

        # Sync new models for each provider
        for provider in providers:
            # Only auto-sync preset providers with our curated catalog.
            # Custom (non-preset) providers should respect the user's explicit selections.
            if provider.is_preset:
                await self._sync_provider_with_latest_models(db, provider, organization)

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
            
            # Only set as default if there's no existing default and this model should be default
            if desired_default_model_id and model["model_id"] == desired_default_model_id and not has_default_model:
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
            if desired_small_default_model_id and model["model_id"] == desired_small_default_model_id and not has_small_default_model:
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
        organization: Organization
    ):
        """Sync a provider with the latest models from LLM_MODEL_DETAILS"""
        available_models = [
            model for model in LLM_MODEL_DETAILS 
            if model["provider_type"] == provider.provider_type
        ]
        catalog_by_id = {model["model_id"]: model for model in available_models}

        existing_models = await db.execute(
            select(LLMModel)
            .filter(LLMModel.provider_id == provider.id)
            .filter(LLMModel.deleted_at == None)
        )
        provider_models = existing_models.unique().scalars().all()
        existing_by_id = {model.model_id: model for model in provider_models}

        provider_had_default = any(model.is_default for model in provider_models)
        provider_had_small_default = any(model.is_small_default for model in provider_models)

        for model in provider_models:
            model_data = catalog_by_id.get(model.model_id)
            if not model_data or not model.is_preset:
                continue

            self._apply_catalog_model_details(model, model_data, sync_enabled=True)
            if not model_data.get("is_default", False) or not model.is_enabled:
                model.is_default = False
            if not model_data.get("is_small_default", False) or not model.is_enabled:
                model.is_small_default = False
            db.add(model)

        # Add any missing models
        for model_data in available_models:
            if model_data["model_id"] not in existing_by_id:
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
        if desired_default and desired_default.is_enabled and (provider_had_default or not has_enabled_default):
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
        if desired_small_default and desired_small_default.is_enabled and (provider_had_small_default or not has_enabled_small_default):
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
