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


class ProvisioningCapabilities(BaseModel):
    """Whether the install wizard can provision certs, and the agent endpoint."""

    enabled: bool = False
    agent_url: Optional[str] = None


class CreateAgentRequest(BaseModel):
    """Wizard step 1 — identity of the agent to provision."""

    edge_agent_id: str
    label: Optional[str] = None


class CertificateStatus(BaseModel):
    """Wizard step 2 — cert-manager Certificate readiness."""

    ready: bool = False
    reason: Optional[str] = None
