from typing import List, Optional
from pydantic import BaseModel, Field


class ClarifyInput(BaseModel):
    """Input schema for clarify tool - asks clarifying questions to the user."""
    
    questions: List[str] = Field(
        ..., 
        description="List of specific clarifying questions to ask the user",
        min_length=1,
        max_length=5
    )
    context: Optional[str] = Field(
        None,
        description="Brief context about why clarification is needed"
    )


class ClarifyOutput(BaseModel):
    """Output schema for clarify tool response."""
    
    questions_asked: List[str] = Field(
        ...,
        description="The clarifying questions that were presented to the user"
    )
    status: str = Field(
        default="awaiting_response",
        description="Status of the clarification request"
    )
