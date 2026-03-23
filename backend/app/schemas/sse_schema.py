from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime


class SSEEvent(BaseModel):
    """Single flexible schema for all SSE events."""
    event: str  # Event type as string (e.g., "tool.started", "decision.partial")
    data: Dict[str, Any] = Field(default_factory=dict)  # Flexible data payload
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    completion_id: Optional[str] = None
    agent_execution_id: Optional[str] = None
    seq: Optional[int] = None

    class Config:
        # Allow extra fields for future extensibility
        extra = "allow"


def format_sse_event(event: SSEEvent, event_id: Optional[str] = None) -> str:
    """Format Pydantic event as SSE string."""
    lines = []

    if event_id:
        lines.append(f"id: {event_id}")

    lines.append(f"event: {event.event}")
    lines.append(f"data: {event.model_dump_json()}")
    lines.append("")  # Empty line to end event

    result = "\n".join(lines) + "\n"

    # Add padding to force immediate flush through uvicorn worker IPC buffers
    # Multi-worker uvicorn uses Gunicorn with IPC buffering between workers and master.
    # Chunks < 4KB often get buffered, causing SSE events to arrive in bulk.
    # SSE spec allows comments (lines starting with ':') which clients ignore.
    # Padding ensures each event exceeds the typical 4KB pipe buffer threshold.
    min_size = 4096  # 4KB - typical Linux pipe buffer size
    if len(result) < min_size:
        padding_needed = min_size - len(result)
        result += f": {' ' * padding_needed}\n"

    return result