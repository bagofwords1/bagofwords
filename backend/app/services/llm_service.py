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
from app.schemas.llm_schema import AnthropicCredentials, OpenAICredentials, GoogleCredentials
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
            .distinct()
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

        credentials = update_data.get('credentials')
        del update_data['credentials']

        await self._update_models(db, organization, provider, current_user, models)
        
        if credentials and credentials['api_key']:
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
        for model in models:
            db_model = LLMModel(**model)
            db_model.organization_id = organization.id
            db_model.provider = provider
            db_model.is_enabled = True
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

        provider.encrypt_credentials(api_key, api_secret)

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
        models: list[dict]
    ):
        for model in models:
            db_model = await db.execute(
                select(LLMModel).filter(LLMModel.id == model.id)
            )
            db_model = db_model.scalar_one_or_none()

            if not db_model:
                raise HTTPException(status_code=404, detail="Model not found")

            if db_model.is_enabled != model.is_enabled:
                db_model.is_enabled = model.is_enabled
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
        
