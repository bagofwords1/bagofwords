"""Data Tunnels settings tab — list the data sources edge agents advertise.

Read-only view over what the advertisement handler persisted (A10/D2). Gated on
manage_settings, like the other admin settings tabs.
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permission_resolver import resolve_permissions, FULL_ADMIN
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.schemas.data_tunnel_schema import AdvertisedConnection, DataEdgeAgentSchema
from app.services.tunnel_registration_service import TunnelRegistrationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data-tunnels", tags=["data-tunnels"])
service = TunnelRegistrationService()


@router.get("/agents", response_model=List[DataEdgeAgentSchema])
async def list_data_edge_agents(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """List the edge agents in this org and the data sources they advertise."""
    resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
    if FULL_ADMIN not in resolved.org_permissions and not resolved.has_org_permission("manage_settings"):
        raise HTTPException(status_code=403, detail="manage_settings permission required")

    agents = await service.list_agents(db, organization)

    out: List[DataEdgeAgentSchema] = []
    for agent in agents:
        ad = agent.last_advertisement or {}
        connections = [
            AdvertisedConnection(
                name=c.get("name"),
                type=c.get("type"),
                label=c.get("label"),
                status=c.get("status"),
                reason=c.get("reason"),
            )
            for c in (ad.get("connections") or [])
        ]
        out.append(
            DataEdgeAgentSchema(
                id=agent.id,
                edge_agent_id=agent.edge_agent_id,
                label=agent.label,
                status=agent.status,
                client_version=agent.client_version,
                last_advertised_at=(
                    agent.last_advertised_at.isoformat() if agent.last_advertised_at else None
                ),
                connections=connections,
            )
        )
    return out
