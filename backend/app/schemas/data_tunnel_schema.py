"""Response schemas for the Data Tunnels settings tab."""
from typing import List, Optional

from pydantic import BaseModel


class AdvertisedConnection(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    label: Optional[str] = None
    status: Optional[str] = None   # "registered" | "conflict"
    reason: Optional[str] = None   # set when status == "conflict"


class DataEdgeAgentSchema(BaseModel):
    """One edge agent and the data sources it last advertised."""

    id: str
    edge_agent_id: str
    label: Optional[str] = None
    status: Optional[str] = None
    client_version: Optional[str] = None
    last_advertised_at: Optional[str] = None
    connections: List[AdvertisedConnection] = []

    class Config:
        from_attributes = True
