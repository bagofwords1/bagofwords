from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ReadArtifactActivityInput(BaseModel):
    """What happened in this conversation's apps while the user (or your
    browser preview) used them."""

    artifact_id: Optional[str] = Field(
        default=None,
        description="Limit to one artifact (the artifact_id from create_artifact results). Omit for all apps in this conversation.",
    )
    only_errors: bool = Field(default=False, description="Only failed requests, interrupted streams, errors and console errors.")
    kinds: Optional[List[Literal["request", "stream", "filter", "error", "console", "click"]]] = Field(
        default=None, description="Only these kinds of events.")
    source: Optional[Literal["editor", "preview"]] = Field(
        default=None, description="editor = the user using the app; preview = your own browser preview. Omit for both.")
    since_minutes: Optional[int] = Field(default=None, ge=1, le=10080, description="Only the last N minutes.")
    event_id: Optional[str] = Field(default=None, description="Return one event's full detail (an id from a previous result, or the 8-char prefix shown in <app_activity>).")
    limit: int = Field(default=40, ge=1, le=100)


class ReadArtifactActivityOutput(BaseModel):
    events: List[Dict[str, Any]] = []
    event: Optional[Dict[str, Any]] = None
    failures: int = 0
    retention: Optional[str] = None
    error: Optional[str] = None
