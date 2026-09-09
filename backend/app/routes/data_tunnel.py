"""Data Tunnels settings tab — list edge agents and provision new ones.

The read path lists what the advertisement handler persisted (A10/D2). The
provisioning endpoints back the install wizard: pre-register an agent, issue its
mTLS certificate via cert-manager, add its NATS account user, and hand back a
downloadable bundle. All gated on manage_settings, like the other admin tabs.
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permission_resolver import resolve_permissions, FULL_ADMIN
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.schemas.data_tunnel_schema import (
    AdvertisedConnection,
    CertificateStatus,
    CreateAgentRequest,
    DataEdgeAgentSchema,
    ProvisioningCapabilities,
)
from app.services import tunnel_provisioning_service as prov
from app.services.tunnel_registration_service import (
    AgentExistsError,
    TunnelRegistrationService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data-tunnels", tags=["data-tunnels"])
service = TunnelRegistrationService()


async def _require_manage_settings(db, user: User, organization: Organization) -> None:
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    if FULL_ADMIN not in resolved.org_permissions and not resolved.has_org_permission("manage_settings"):
        raise HTTPException(status_code=403, detail="manage_settings permission required")


def _to_schema(agent) -> DataEdgeAgentSchema:
    ad = agent.last_advertisement or {}
    connections = [
        AdvertisedConnection(
            name=c.get("name"), type=c.get("type"), label=c.get("label"),
            status=c.get("status"), reason=c.get("reason"),
        )
        for c in (ad.get("connections") or [])
    ]
    return DataEdgeAgentSchema(
        id=agent.id,
        edge_agent_id=agent.edge_agent_id,
        label=agent.label,
        status=agent.status,
        client_version=agent.client_version,
        last_advertised_at=(agent.last_advertised_at.isoformat() if agent.last_advertised_at else None),
        connections=connections,
    )


@router.get("/provisioning", response_model=ProvisioningCapabilities)
async def get_provisioning_capabilities(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Whether the wizard can issue certs here, and the agent-facing endpoint."""
    await _require_manage_settings(db, current_user, organization)
    from app.settings.config import settings
    return ProvisioningCapabilities(
        enabled=await prov.is_available(),
        agent_url=settings.bow_config.data_tunnel.agent_url,
    )


@router.get("/agents", response_model=List[DataEdgeAgentSchema])
async def list_data_edge_agents(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """List the edge agents in this org and the data sources they advertise."""
    await _require_manage_settings(db, current_user, organization)
    agents = await service.list_agents(db, organization)
    return [_to_schema(a) for a in agents]


@router.post("/agents", response_model=DataEdgeAgentSchema, status_code=201)
async def create_data_edge_agent(
    body: CreateAgentRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Wizard step 1: pre-register an agent and provision its mTLS identity
    (cert-manager Certificate + nats-accounts user)."""
    await _require_manage_settings(db, current_user, organization)

    if not await prov.is_available():
        raise HTTPException(
            status_code=503,
            detail="Agent provisioning is not available (data_tunnel.agent_url or the cluster is not configured).",
        )
    try:
        prov.validate_edge_agent_id(body.edge_agent_id)
    except prov.ProvisioningError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        agent = await service.create_agent(db, organization, body.edge_agent_id, body.label)
    except AgentExistsError:
        raise HTTPException(status_code=409, detail=f"edge agent '{body.edge_agent_id}' already exists")

    try:
        await prov.create_certificate(organization.id, body.edge_agent_id)
        await prov.ensure_account_user(organization.id, body.edge_agent_id)
    except prov.ProvisioningError as e:
        # Roll back the pre-registration so the wizard can be retried cleanly.
        await service.delete_agent(db, organization, agent)
        logger.exception("tunnel.provisioning.failed", extra={"edge_agent_id": body.edge_agent_id})
        raise HTTPException(status_code=502, detail=f"Provisioning failed: {e}")

    return _to_schema(agent)


@router.get("/agents/{agent_id}/certificate", response_model=CertificateStatus)
async def get_certificate_status(
    agent_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Wizard step 2: poll the Certificate's readiness."""
    await _require_manage_settings(db, current_user, organization)
    agent = await service.get_agent(db, organization, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="edge agent not found")
    try:
        ready, reason = await prov.certificate_ready(organization.id, agent.edge_agent_id)
    except prov.ProvisioningUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except prov.ProvisioningError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return CertificateStatus(ready=ready, reason=reason)


@router.get("/agents/{agent_id}/bundle")
async def download_agent_bundle(
    agent_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Wizard step 3: download config.yaml + certs as a zip."""
    await _require_manage_settings(db, current_user, organization)
    agent = await service.get_agent(db, organization, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="edge agent not found")
    try:
        data = await prov.build_bundle(organization.id, agent.edge_agent_id, agent.label)
    except prov.ProvisioningUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except prov.ProvisioningError as e:
        # Most common: cert not issued yet.
        raise HTTPException(status_code=409, detail=str(e))
    filename = f"bow-edge-agent-{agent.edge_agent_id}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_data_edge_agent(
    agent_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Deprovision an agent: remove its NATS user + certificate, then its row.

    Cluster cleanup is best-effort — the row is always removed so the UI's
    Remove action can't get stuck if the cluster is briefly unreachable."""
    await _require_manage_settings(db, current_user, organization)
    agent = await service.get_agent(db, organization, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="edge agent not found")

    edge_agent_id = agent.edge_agent_id
    try:
        await prov.remove_account_user(organization.id, edge_agent_id)
        await prov.delete_certificate(organization.id, edge_agent_id)
    except prov.ProvisioningError:
        logger.warning(
            "tunnel.provisioning.cleanup_failed",
            extra={"edge_agent_id": edge_agent_id}, exc_info=True,
        )

    await service.delete_agent(db, organization, agent)
    return Response(status_code=204)
