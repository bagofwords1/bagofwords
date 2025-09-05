from typing import List, Optional
from pydantic import BaseModel


class InstructionDraft(BaseModel):
    text: str
    category: Optional[str] = None


class SuggestInstructionsInput(BaseModel):
    # Optional hint or seed text; tool can work with runtime context only
    hint: Optional[str] = None


class SuggestInstructionsOutput(BaseModel):
    instructions: List[InstructionDraft]

