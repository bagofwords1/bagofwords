from __future__ import annotations

from datetime import datetime
import json
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

    def _ordered_steps(self) -> list[OnboardingStepKey]:
        return [
            OnboardingStepKey.organization_created,
            OnboardingStepKey.llm_configured,
            OnboardingStepKey.data_source_created,
            OnboardingStepKey.schema_selected,
            OnboardingStepKey.instructions_added,
        ]

    def _compute_current_step(self, cfg: OnboardingConfig) -> OnboardingStepKey | None:
        for step in self._ordered_steps():
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
                settings.config["onboarding"] = json.loads(cfg.json())
                flag_modified(settings, "config")
                db.add(settings)
                await db.commit()
                await db.refresh(settings)
        elif mutated:
            settings.config["onboarding"] = json.loads(cfg.json())
            flag_modified(settings, "config")
            db.add(settings)
            await db.commit()
            await db.refresh(settings)

        # Safety net (scoped): If LLM configured AND DS created, complete onboarding
        # as long as we're not actively in schema/context steps.
        llm_done = (cfg.steps.get(OnboardingStepKey.llm_configured, OnboardingStepStatus()).status == OnboardingStatus.done)
        ds_done = (cfg.steps.get(OnboardingStepKey.data_source_created, OnboardingStepStatus()).status == OnboardingStatus.done)
        if llm_done and ds_done and not cfg.completed:
            # Only auto-complete if current_step is not schema/context, or flow was dismissed
            if cfg.dismissed or cfg.current_step in (
                None,
                OnboardingStepKey.organization_created,
                OnboardingStepKey.llm_configured,
                OnboardingStepKey.data_source_created,
            ):
                cfg.completed = True
                cfg.current_step = None
                settings.config["onboarding"] = json.loads(cfg.json())
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

        # Advance step statuses based on provided current_step/completed
        now = datetime.utcnow()
        if cfg.completed:
            # Mark all steps done if completed was requested
            for step in self._ordered_steps():
                cfg.steps[step] = OnboardingStepStatus(status=OnboardingStatus.done, ts=now)
            cfg.current_step = None
        elif cfg.current_step is not None:
            # Interpret current_step as: user advanced to this step; mark all prior steps as done
            try:
                order = self._ordered_steps()
                idx = order.index(cfg.current_step)
                for s in order[:idx]:
                    # Preserve existing ts if already set
                    prev = cfg.steps.get(s)
                    ts = getattr(prev, 'ts', None) if isinstance(prev, OnboardingStepStatus) else (prev or {}).get('ts')
                    cfg.steps[s] = OnboardingStepStatus(status=OnboardingStatus.done, ts=ts or now)
            except ValueError:
                # Unknown step; ignore gracefully
                pass

        # If all steps are done, mark completed
        if not cfg.completed:
            if self._compute_current_step(cfg) is None:
                cfg.completed = True

        settings.config["onboarding"] = json.loads(cfg.json())
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


