"""Backfill for entagent01: take agent names out of saved queries' code.

This uses the LIVE classification in app.services.entity_code (templatize /
render), not a frozen copy: an upgrade classifies existing queries by the same
rules a save applies today. tests/unit/test_entity_per_agent_migration.py runs
this backfill and its downgrade against those rules, so a change to them that
would change what the migration does to existing queries fails there.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Dict, List

import sqlalchemy as sa

from dataclasses import replace

from app.services.entity_code import (
    AgentInfo, ConnInfo, MODE_BOUND, all_client_keys, render, templatize,
)


def _agents(bind, org_id: str) -> Dict[str, AgentInfo]:
    rows = bind.execute(sa.text(
        """SELECT d.id AS ds_id, d.name AS ds_name, c.id AS c_id, c.name AS c_name,
                  c.type AS c_type, c.is_active AS c_active
           FROM data_sources d
           LEFT JOIN domain_connection dc ON dc.data_source_id = d.id
           LEFT JOIN connections c ON c.id = dc.connection_id
           WHERE d.organization_id = :org
           ORDER BY d.created_at, d.id, c.created_at, c.id"""
    ), {"org": org_id}).mappings().all()
    names: Dict[str, str] = {}
    conns: Dict[str, List[ConnInfo]] = {}
    for r in rows:
        ds_id = str(r["ds_id"])
        names[ds_id] = str(r["ds_name"] or "")
        conns.setdefault(ds_id, [])
        if r["c_id"] is not None:
            active = r["c_active"]
            conns[ds_id].append(ConnInfo(
                id=str(r["c_id"]), name=str(r["c_name"] or ""), type=str(r["c_type"] or ""),
                is_active=True if active is None else bool(active),
            ))
    return {i: AgentInfo(id=i, name=names[i], connections=tuple(conns[i])) for i in names}


def backfill(bind) -> None:
    entities = bind.execute(sa.text(
        "SELECT id, organization_id, code FROM entities"
    )).mappings().all()
    agents_by_org: Dict[str, Dict[str, AgentInfo]] = {}
    for e in entities:
        org = str(e["organization_id"])
        if org not in agents_by_org:
            agents_by_org[org] = _agents(bind, org)
        org_agents = agents_by_org[org]
        attached = [
            str(r[0]) for r in bind.execute(sa.text(
                """SELECT a.data_source_id FROM entity_data_source_association a
                   JOIN data_sources d ON d.id = a.data_source_id
                   WHERE a.entity_id = :e ORDER BY d.created_at, d.id"""
            ), {"e": e["id"]}).all()
        ]
        candidates = [org_agents[i] for i in attached if i in org_agents]
        if not candidates:
            # Saved from an Auto report: no agent rows. Its code keeps the
            # agent names it names and runs on exactly those; left unclassified.
            continue
        result = templatize(
            e["code"] or "", candidates,
            existing_keys=all_client_keys(org_agents.values()),
        )
        if result.origin_id:
            origin = result.origin_id
        elif result.mode == MODE_BOUND and result.agent_ids:
            origin = next(i for i in attached if i in result.agent_ids)
        else:
            origin = attached[0] if attached else None
        bind.execute(sa.text(
            "UPDATE entities SET code = :code, code_mode = :mode, origin_data_source_id = :origin WHERE id = :id"
        ), {"code": result.code, "mode": result.mode, "origin": origin, "id": e["id"]})
        if result.repaired:
            # A key that named nothing (a deleted or renamed agent) was taken
            # from the query's agents: record what it was, so it can be traced
            # and reverted by hand.
            details = "CAST(:details AS JSON)" if bind.dialect.name == "postgresql" else ":details"
            bind.execute(sa.text(
                f"""INSERT INTO audit_logs (id, created_at, organization_id, user_id, action, resource_type, resource_id, details)
                   VALUES (:id, :at, :org, NULL, 'entity.code_repaired', 'entity', :eid, {details})"""
            ), {
                "id": str(uuid.uuid4()), "at": datetime.utcnow(), "org": org, "eid": e["id"],
                "details": json.dumps({"repaired": result.repaired, "by": "migration entagent01"}),
            })
        if origin:
            bind.execute(sa.text(
                "UPDATE entity_user_results SET data_source_id = :origin WHERE entity_id = :id AND data_source_id IS NULL"
            ), {"origin": origin, "id": e["id"]})


def restore_code(bind) -> None:
    """Downgrade: put each templated query's origin agent back into its code."""
    rows = bind.execute(sa.text(
        "SELECT id, organization_id, code, origin_data_source_id FROM entities "
        "WHERE code_mode = 'templated' AND origin_data_source_id IS NOT NULL"
    )).mappings().all()
    agents_by_org: Dict[str, Dict[str, AgentInfo]] = {}
    for e in rows:
        org = str(e["organization_id"])
        if org not in agents_by_org:
            agents_by_org[org] = _agents(bind, org)
        agent = agents_by_org[org].get(str(e["origin_data_source_id"]))
        if agent is None:
            continue
        try:
            code = render(e["code"] or "", agent)
        except Exception:
            # The only connection of the type is inactive: render (active
            # connections only) refuses it, but the pre-migration code named
            # exactly that connection — put it back.
            try:
                code = render(e["code"] or "", AgentInfo(
                    id=agent.id, name=agent.name,
                    connections=tuple(replace(c, is_active=True) for c in agent.connections),
                ))
            except Exception:
                continue  # still ambiguous: leave it templated rather than guess
        bind.execute(sa.text("UPDATE entities SET code = :code WHERE id = :id"), {"code": code, "id": e["id"]})
