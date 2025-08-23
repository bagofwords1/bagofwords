from typing import Optional
from pydantic import BaseModel


class ReadFileInput(BaseModel):
    path: str


class ReadFileOutput(BaseModel):
    content: str
    lines: Optional[int] = None