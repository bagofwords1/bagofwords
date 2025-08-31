from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException

from app.models.llm_provider import LLMProvider
from app.models.llm_model import LLMModel
from app.models.organization import Organization
from app.models.user import User
from app.settings.config import settings
from app.models.llm_provider import LLM_PROVIDER_DETAILS
from app.models.llm_model import LLM_MODEL_DETAILS
from app.schemas.llm_schema import AnthropicCredentials, OpenAICredentials, GoogleCredentials, LLMModelSchema
from datetime import datetime

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

        models = provider_data.models
        del provider_data.models
        del provider_data.config
        credentials = provider_data.credentials
        del provider_data.credentials

        provider = LLMProvider(**provider_data.dict())
        self._set_provider_credentials(provider, credentials)

        provider.organization_id = organization.id

        await self._create_models(db, organization, provider, current_user, models)

        db.add(provider)
        await db.commit()
        await db.refresh(provider)

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
        await db.commit()
        await db.refresh(provider)

        return provider

    async def delete_provider(
        self, 
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        provider_id: str
    ):
        """Delete a custom provider"""
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
            if model.is_default:
                raise HTTPException(status_code=400, detail="Cannot delete default models")

        provider.deleted_at = datetime.now()
        provider.is_enabled = False

        await self._disable_models(db, organization, provider)

        db.add(provider)
        await db.commit()
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
        return result.unique().scalars().all()

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
        
        if model.is_default:
            raise HTTPException(status_code=400, detail="Cannot disable default models")
        
        model.is_enabled = enabled
        await db.commit()
        return {"success": True}
    
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
        has_default_model = existing_default.scalar_one_or_none() is not None

        for model in models:
            db_model = LLMModel(**model)
            db_model.organization_id = organization.id
            db_model.provider = provider
            db_model.is_enabled = True
            db_model.is_custom = model.get("is_custom", False)
            
            # Check if this model would be default according to config
            model_details = next(
                (m for m in LLM_MODEL_DETAILS if m["model_id"] == model["model_id"]),
                None
            )
            
            # Only set as default if there's no existing default and this model should be default
            if model_details and model_details.get("is_default", False) and not has_default_model:
                db_model.is_default = True
                # Only allow one default model
                has_default_model = True
            else:
                db_model.is_default = False
                
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
            else:
                # If model doesn't have an ID, create new model
                db_model = LLMModel(
                    name=model.name or model.model_id,
                    model_id=model.model_id,
                    provider=provider,
                    organization_id=organization.id,
                    is_enabled=model.is_enabled,
                    is_custom=model.is_custom,
                    is_preset=model.is_preset,
                    is_default=False  # New models are not default by default
                )
                db.add(db_model)

        await db.commit()
    
    async def set_default_model(
        self, 
        db: AsyncSession,
        current_user: User,
        organization: Organization,
        model_id: str
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

        for model in org_models:
            model.is_default = False
            db.add(model)

        default_model.is_default = True

        db.add(default_model)
        await db.commit()
        return {"success": True }
    
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

                model = LLMModel(
                    name=model_name,
                    model_id=model_id,
                    provider=provider,
                    organization_id=organization.id,
                    is_preset=True,
                    is_enabled=is_enabled,
                    is_default=is_default
                )
                db.add(model)

        await db.commit()
        
    async def _sync_provider_with_latest_models(
        self,
        db: AsyncSession,
        provider: LLMProvider,
        organization: Organization
    ):
        """Sync a provider with the latest models from LLM_MODEL_DETAILS"""
        # Get available models for this provider type
        available_models = [
            model for model in LLM_MODEL_DETAILS 
            if model["provider_type"] == provider.provider_type
        ]

        # Get existing model IDs for this provider
        existing_models = await db.execute(
            select(LLMModel.model_id)
            .filter(LLMModel.provider_id == provider.id)
        )
        existing_model_ids = {model[0] for model in existing_models}

        # Add any missing models
        for model_data in available_models:
            if model_data["model_id"] not in existing_model_ids:
                model = LLMModel(
                    name=model_data["name"],
                    model_id=model_data["model_id"],
                    is_preset=model_data["is_preset"],
                    is_enabled=model_data["is_enabled"],
                    provider=provider,
                    organization_id=organization.id
                )
                db.add(model)

        