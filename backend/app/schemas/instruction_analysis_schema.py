from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime


class InstructionAnalysisLimits(BaseModel):
    prompts: int = 5
    instructions: int = 5
    resources: int = 5


class InstructionAnalysisRequest(BaseModel):
    text: str = Field(..., description="Instruction text to analyze")
    data_source_ids: Optional[List[str]] = None
    include: Optional[List[Literal["impact", "related_instructions", "resources"]]] = None
    created_since_days: int = 90
    max_prompts_scan: int = 2000
    limits: InstructionAnalysisLimits = Field(default_factory=InstructionAnalysisLimits)


class PromptSample(BaseModel):
    content: str
    created_at: Optional[datetime] = None


class ImpactEstimation(BaseModel):
    score: float
    prompts: List[PromptSample] = []


class RelatedInstructionItem(BaseModel):
    id: str
    text: str
    status: str
    createdByName: Optional[str] = None


class RelatedInstructions(BaseModel):
    count: int
    items: List[RelatedInstructionItem]


class RelatedResourceItem(BaseModel):
    id: str
    name: str
    resource_type: str
    path: Optional[str] = None


class RelatedResources(BaseModel):
    count: int
    items: List[RelatedResourceItem]


class InstructionAnalysisResponse(BaseModel):
    impact: Optional[ImpactEstimation] = None
    related_instructions: Optional[RelatedInstructions] = None
    resources: Optional[RelatedResources] = None
    meta: Dict[str, Any] = {}


