"""Persisting data edge agent advertisements — design A10 / D2.

`register_advertisement` is called by the NATS advertisement handler (leader
worker only) with the org resolved from the subject. It upserts one
`DataEdgeAgent` row and, per advertised connection, creates or updates a
tunnel-mode `Connection` — rejecting on name conflict rather than upserting,
exactly as the interactive path does, and deactivating connections the agent
has stopped advertising.

`TunnelRegistrationService.list_agents` is the read path the Data Tunnels UI
uses.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.data_edge_agent import DataEdgeAgent
from app.models.organization import Organization

logger = logging.getLogger(__name__)


def _connection_config(conn: dict) -> dict:
    """Non-secret config carried by a tunneled Connection row.

    Only the advertised, non-credential fields — the whole point of a tunnel is
    that Bow never holds the login (D1).
    """
    return {
        "label": conn.get("label"),
        "query_timeout_seconds": conn.get("query_timeout_seconds"),
        "catalog_identity_available": conn.get("catalog_identity_available"),
    }


async def register_advertisement(org_id: str, payload: dict) -> None:
    """Upsert the agent and its connections from one advertisement (A10)."""
    edge_agent_id = payload.get("edge_agent_id")
    if not edge_agent_id:
        logger.warning("tunnel.register.no_edge_agent_id", extra={"org_id": org_id})
        return

    advertised = payload.get("connections", []) or []
    now = datetime.utcnow()

    async with async_session_maker() as db:
        org = (
            await db.execute(select(Organization).filter(Organization.id == org_id))
        ).scalar_one_or_none()
        if org is None:
            # Subject named an org that does not exist here. Nothing to attach to.
            logger.warning("tunnel.register.unknown_org", extra={"org_id": org_id})
            return

        # -- per-connection create/update, recording an outcome for each -------
        outcomes = []
        registered_names = []
        for conn in advertised:
            name = conn.get("name")
            if not name:
                continue
            existing = (
                await db.execute(
                    select(Connection).filter(
                        Connection.organization_id == org_id,
                        Connection.name == name,
                    )
                )
            ).scalar_one_or_none()

            if existing is None:
                db.add(
                    Connection(
                        name=name,
                        type=conn.get("type", "unknown"),
                        config=_connection_config(conn),
                        credentials=None,  # never for a tunneled connection (D1)
                        tunnel_mode=True,
                        edge_agent_id=edge_agent_id,
                        organization_id=org_id,
                        is_active=True,
                    )
                )
                outcomes.append({**_outcome(conn), "status": "registered"})
                registered_names.append(name)
            elif existing.tunnel_mode and existing.edge_agent_id == edge_agent_id:
                # Same agent re-advertising: update in place and reactivate.
                existing.type = conn.get("type", existing.type)
                existing.config = _connection_config(conn)
                existing.is_active = True
                outcomes.append({**_outcome(conn), "status": "registered"})
                registered_names.append(name)
            else:
                # Name already claimed by a direct connection or another agent.
                # Reject — do not upsert and silently merge two databases (A10).
                owner = existing.edge_agent_id if existing.tunnel_mode else "a direct connection"
                outcomes.append({
                    **_outcome(conn),
                    "status": "conflict",
                    "reason": f"name already claimed by {owner}",
                })

        # -- withdrawal: names this agent used to serve but no longer does -----
        prior = (
            await db.execute(
                select(Connection).filter(
                    Connection.organization_id == org_id,
                    Connection.tunnel_mode.is_(True),
                    Connection.edge_agent_id == edge_agent_id,
                    Connection.is_active.is_(True),
                )
            )
        ).scalars().all()
        for row in prior:
            if row.name not in registered_names:
                # Deactivate, never delete: keeps data-source membership, RBAC,
                # and indexed schema for when the name returns (A10).
                row.is_active = False

        # -- upsert the agent row, storing the advertisement whole (D2) --------
        agent = (
            await db.execute(
                select(DataEdgeAgent).filter(
                    DataEdgeAgent.organization_id == org_id,
                    DataEdgeAgent.edge_agent_id == edge_agent_id,
                )
            )
        ).scalar_one_or_none()
        stored = {
            "edge_agent_name": payload.get("edge_agent_name"),
            "version": payload.get("version"),
            "connections": outcomes,
        }
        if agent is None:
            agent = DataEdgeAgent(
                edge_agent_id=edge_agent_id,
                organization_id=org_id,
                last_connected_at=now,
            )
            db.add(agent)
        agent.status = "online"
        agent.label = payload.get("edge_agent_name")
        agent.client_version = payload.get("version")
        agent.last_advertisement = stored
        agent.last_advertised_at = now

        await db.commit()
        logger.info(
            "tunnel.advertisement.persisted",
            extra={
                "org_id": org_id,
                "edge_agent_id": edge_agent_id,
                "registered": len(registered_names),
                "conflicts": sum(1 for o in outcomes if o["status"] == "conflict"),
            },
        )


def _outcome(conn: dict) -> dict:
    return {"name": conn.get("name"), "type": conn.get("type"), "label": conn.get("label")}


class TunnelRegistrationService:
    """Read path for the Data Tunnels UI."""

    async def list_agents(self, db, organization: Organization) -> list[DataEdgeAgent]:
        result = await db.execute(
            select(DataEdgeAgent)
            .filter(DataEdgeAgent.organization_id == organization.id)
            .order_by(DataEdgeAgent.edge_agent_id)
        )
        return list(result.scalars().all())
