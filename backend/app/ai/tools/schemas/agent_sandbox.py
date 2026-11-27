"""Input and output of the four agent-sandbox tools."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class CreateSandboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: Optional[str] = Field(
        default=None,
        description="Name of the environment to create the sandbox from. Leave empty for the default environment.",
    )


class SandboxFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Path relative to /workspace, e.g. 'helpers/clean.py'.")
    content: str = Field(description="The file's text content.")


class RunInSandboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["python", "node", "go", "bash"] = Field(
        default="python", description="Language of `code`, and the tool `packages` are installed with.",
    )
    code: Optional[str] = Field(default=None, description="Source code to run. Written to a file and executed.")
    command: Optional[str] = Field(
        default=None,
        description="Shell command to run in /workspace instead of (or after writing) `code`, e.g. 'python analyze.py --year 2025'.",
    )
    files: List[SandboxFile] = Field(default_factory=list, description="Extra text files to write before the run.")
    packages: List[str] = Field(
        default_factory=list,
        description="Packages to install before the run with the language's own tool (pip, npm, go get). "
                    "Only works when the sandbox's network policy allows the package registries.",
    )
    input_file_ids: List[str] = Field(
        default_factory=list,
        description="Ids of files of THIS report to copy into /workspace/inputs before the run.",
    )
    timeout_seconds: Optional[int] = Field(
        default=None, ge=1, le=3600,
        description="Stop the run after this many seconds. Capped by the environment's run timeout.",
    )


class SandboxEmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SandboxArtifact(BaseModel):
    file_id: Optional[str] = None
    name: str
    size: int
    type: Optional[str] = None


class CreateSandboxOutput(BaseModel):
    success: bool
    sandbox_id: Optional[str] = None
    state: Optional[str] = None
    environment: Optional[str] = None
    network_policy: Optional[str] = None
    network: Optional[str] = None
    languages: List[str] = []
    limits: Dict[str, Any] = {}
    created: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class RunInSandboxOutput(BaseModel):
    success: bool
    run_id: Optional[str] = None
    sandbox_id: Optional[str] = None
    status: Optional[str] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    artifacts: List[SandboxArtifact] = []
    skipped_artifacts: List[Dict[str, Any]] = []
    duration_seconds: Optional[float] = None
    environment: Optional[str] = None
    network_policy: Optional[str] = None
    notes: List[str] = []
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class SandboxStatusOutput(BaseModel):
    success: bool
    exists: bool = False
    sandbox_id: Optional[str] = None
    state: Optional[str] = None
    environment: Optional[str] = None
    network_policy: Optional[str] = None
    network: Optional[str] = None
    limits: Dict[str, Any] = {}
    uptime_seconds: Optional[int] = None
    expires_at: Optional[str] = None
    recent_runs: List[Dict[str, Any]] = []
    available_environments: List[Dict[str, Any]] = []
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class DeleteSandboxOutput(BaseModel):
    success: bool
    deleted: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None
