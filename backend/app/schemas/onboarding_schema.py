from __future__ import annotations

from enum import Enum
from typing import Dict, Optional
from datetime import datetime

from pydantic import BaseModel, Field


class OnboardingStepKey(str, Enum):
    organization_created = "organization_created"
    llm_configured = "llm_configured"
    data_source_created = "data_source_created"
    schema_selected = "schema_selected"
    instructions_added = "instructions_added"


class OnboardingStatus(str, Enum):
    pending = "pending"
    done = "done"
    skipped = "skipped"


class OnboardingStepStatus(BaseModel):
    status: OnboardingStatus = OnboardingStatus.pending
    ts: Optional[datetime] = None


class OnboardingConfig(BaseModel):
    version: str = "v1"
    current_step: Optional[OnboardingStepKey] = None
    completed: bool = False
    dismissed: bool = False
    steps: Dict[OnboardingStepKey, OnboardingStepStatus] = Field(default_factory=dict)


class OnboardingUpdate(BaseModel):
    # Minimal updates from client; steps are generally advanced server-side via events
    dismissed: Optional[bool] = None
    completed: Optional[bool] = None
    current_step: Optional[OnboardingStepKey] = None


class OnboardingResponse(BaseModel):
    # What the GET endpoint returns
    onboarding: OnboardingConfig


