from typing import Optional, List
from pydantic import BaseModel


class AnswerQuestionInput(BaseModel):
    question: str


class AnswerQuestionOutput(BaseModel):
    answer: str
    citations: Optional[List[str]] = None

