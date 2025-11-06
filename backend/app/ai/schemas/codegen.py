from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class CodeGenContext(BaseModel):
    user_prompt: str
    interpreted_prompt: Optional[str] = None

    # Curated/filtered strings (already rendered for prompt inclusion)
    schemas_excerpt: str
    # New: rendered descriptions of connected data sources/clients (LLM-ready text)
    data_sources_context: str = ""
    instructions_context: str = ""
    mentions_context: str = "<mentions>No mentions for this turn</mentions>"
    entities_context: str = ""
    messages_context: str = ""
    resources_context: str = ""
    files_context: str = ""
    platform: Optional[str] = None
    history_summary: str = ""

    # Observations/history
    past_observations: List[Dict[str, Any]] = []
    last_observation: Optional[Dict[str, Any]] = None

    # Optional extras to guide generation (machine-usable)
    filtered_entities: List[Dict[str, Any]] = []
    successful_queries: List[str] = []


class CodeGenRequest(BaseModel):
    context: CodeGenContext
    retries: int = 2

