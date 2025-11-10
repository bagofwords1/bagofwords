from pydantic import BaseModel
from typing import Optional, List


class TestRunBatchCreate(BaseModel):
    case_ids: Optional[List[str]] = None
    suite_id: Optional[str] = None
    trigger_reason: Optional[str] = "manual"

