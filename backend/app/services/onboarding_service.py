from __future__ import annotations

from datetime import datetime
from typing import Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.organization import Organization
from app.models.user import User
from app.models.organization_settings import OrganizationSettings
from app.schemas.onboarding_schema import (
    OnboardingConfig,
    OnboardingResponse,
    OnboardingUpdate,
    OnboardingStepKey,
    OnboardingStepStatus,
    OnboardingStatus,
)
from app.services.organization_settings_service import OrganizationSettingsService


class OnboardingService:
    def __init__(self):
        self.org_settings_service = OrganizationSettingsService()

    async def _ensure_onboarding_initialized(self, settings: OrganizationSettings) -> bool:
        """Ensure onboarding structure exists in settings.config. Returns True if mutated."""
        mutated = False
        if settings.config is None:
            settings.config = {}
            mutated = True

        onboarding_dict = settings.config.get("onboarding")
        if onboarding_dict is None or not isinstance(onboarding_dict, dict):
            # Initialize default steps
            steps: Dict[str, Dict] = {}
            for key in OnboardingStepKey:
                steps[key.value] = OnboardingStepStatus().dict()

            settings.config["onboarding"] = OnboardingConfig(
                version="v1",
                current_step=OnboardingStepKey.llm_configured,
                completed=False,
                dismissed=False,
                steps=steps,  # type: ignore[arg-type]
            ).dict()
            mutated = True
        return mutated

    def _coerce_to_config(self, raw: Dict) -> OnboardingConfig:
        # Pydantic will coerce enum keys/values
        return OnboardingConfig(**raw)

    def _compute_current_step(self, cfg: OnboardingConfig) -> OnboardingStepKey | None:
        order = [
            OnboardingStepKey.organization_created,
            OnboardingStepKey.llm_configured,
            OnboardingStepKey.data_source_created,
            OnboardingStepKey.schema_selected,
            OnboardingStepKey.instructions_added,
        ]
        for step in order:
            status = cfg.steps.get(step, OnboardingStepStatus()).status if isinstance(cfg.steps, dict) else None
            if status != OnboardingStatus.done:
                return step
        return None

    async def get_onboarding(self, db: AsyncSession, organization: Organization, current_user: User) -> OnboardingResponse:
        settings = await organization.get_settings(db)
        mutated = await self._ensure_onboarding_initialized(settings)

        raw = settings.config.get("onboarding", {})
        cfg = self._coerce_to_config(raw)

        # Ensure current_step reflects step statuses unless completed/dismissed
        if not cfg.completed and not cfg.dismissed:
            computed = self._compute_current_step(cfg)
            if cfg.current_step != computed:
                cfg.current_step = computed
                settings.config["onboarding"] = cfg.dict()
                flag_modified(settings, "config")
                db.add(settings)
                await db.commit()
                await db.refresh(settings)
        elif mutated:
            settings.config["onboarding"] = cfg.dict()
            flag_modified(settings, "config")
            db.add(settings)
            await db.commit()
            await db.refresh(settings)

        return OnboardingResponse(onboarding=cfg)

    async def update_onboarding(self, db: AsyncSession, organization: Organization, current_user: User, payload: OnboardingUpdate) -> OnboardingResponse:
        settings = await organization.get_settings(db)
        await self._ensure_onboarding_initialized(settings)

        cfg = self._coerce_to_config(settings.config.get("onboarding", {}))

        if payload.dismissed is not None:
            cfg.dismissed = payload.dismissed
        if payload.completed is not None:
            cfg.completed = payload.completed
        if payload.current_step is not None:
            cfg.current_step = payload.current_step

        # If all steps are done, mark completed
        if not cfg.completed:
            if self._compute_current_step(cfg) is None:
                cfg.completed = True

        settings.config["onboarding"] = cfg.dict()
        flag_modified(settings, "config")
        db.add(settings)
        await db.commit()
        await db.refresh(settings)

        return OnboardingResponse(onboarding=cfg)

    async def mark_step_done(self, db: AsyncSession, organization: Organization, step: OnboardingStepKey) -> None:
        """Utility to be called by other services when a step completes."""
        settings = await organization.get_settings(db)
        await self._ensure_onboarding_initialized(settings)

        cfg = self._coerce_to_config(settings.config.get("onboarding", {}))
        # Update step
        cfg.steps[step] = OnboardingStepStatus(status=OnboardingStatus.done, ts=datetime.utcnow())
        # Recompute current step and completed
        cfg.current_step = self._compute_current_step(cfg)
        if cfg.current_step is None:
            cfg.completed = True

        settings.config["onboarding"] = cfg.dict()
        flag_modified(settings, "config")
        db.add(settings)
        await db.commit()
        await db.refresh(settings)


