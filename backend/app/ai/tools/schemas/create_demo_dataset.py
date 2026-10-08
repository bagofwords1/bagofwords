from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.demo_dataset_schema import DemoDatasetSpec


class CreateDemoDatasetInput(DemoDatasetSpec):
    """Design a realistic mock SQLite dataset for any domain and, after the
    user approves it on a confirmation card, generate it, create a connection
    and create the suggested agents the user kept ticked.

    The spec is declarative only — no code, no paths. Rows are generated per
    table by the org's small default model in the code sandbox and validated
    (keys, foreign keys, types, nulls, dates) before the file is written.
    """


class CreatedDemoAgent(BaseModel):
    data_source_id: str
    name: str
    requested_name: Optional[str] = Field(None, description="The suggested name (a taken name gets a ' (2)' suffix).")
    icon: Optional[str] = None
    active_tables: List[str] = Field(default_factory=list)
    instructions_created: int = 0


class DemoTableStat(BaseModel):
    name: str
    rows: int
    attempts: int = 1


class CreateDemoDatasetOutput(BaseModel):
    success: bool
    status: Literal["created", "rejected", "failed", "invalid_spec", "limit_reached",
                    "permission_denied", "disabled", "timed_out", "no_model"]
    message: Optional[str] = None
    feedback: Optional[str] = Field(None, description="User's feedback when they rejected the proposal.")
    errors: List[str] = Field(default_factory=list, description="Spec problems to fix before calling again.")
    connection_id: Optional[str] = None
    connection_name: Optional[str] = None
    tables: List[DemoTableStat] = Field(default_factory=list)
    total_rows: int = 0
    file_size_bytes: int = 0
    seconds: float = 0.0
    model: Optional[str] = None
    agents: List[CreatedDemoAgent] = Field(default_factory=list)
    skipped_agents: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    failed_code: Optional[str] = Field(None, description="Last generator attempt for the table that failed (diagnosis).")
