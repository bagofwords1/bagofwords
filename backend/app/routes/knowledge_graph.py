"""Knowledge graph: agents, instructions, labels and tables as one graph.

Two scopes:
- org  (no data_source_id): agents + instructions (+ labels) the caller can see.
- agent (data_source_id):   tables of that agent (FK edges) + the instructions
                            that reference or are attached to it (+ labels).

Read-only. Access is scoped exactly like the agents tree: the caller sees the
agents `/data_sources/active` would return, the instructions attached to those
(or attached to none = global), and nothing else.
"""
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, lazyload

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.instruction_reference import InstructionReference
from app.models.datasource_table import DataSourceTable
from app.services.data_source_service import DataSourceService
from app.models.build_content import BuildContent
from app.core.main_build import resolve_main_build_id

router = APIRouter(tags=["knowledge-graph"])
data_source_service = DataSourceService()


def _node(id_, kind, label, **extra):
    d = {"id": id_, "kind": kind, "label": label}
    d.update(extra)
    return d


@router.get("/knowledge_graph")
async def get_knowledge_graph(
    data_source_id: Optional[str] = Query(None, description="Scope the graph to one agent (tables + instructions). Omit for the org-level agents/instructions graph."),
    show_all: bool = Query(False),
    max_tables: int = Query(60, ge=1, le=500, description="Agent scope: cap on tables, top by centrality then by instruction references."),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    agents = await data_source_service.get_active_data_sources(
        db, organization, current_user, include_unconnected=True, show_all=show_all
    )
    agent_by_id = {str(a.id): a for a in agents}
    if data_source_id and data_source_id not in agent_by_id:
        return {"scope": "agent", "nodes": [], "edges": [], "stats": {}}

    # Instructions: attached to a visible agent, or attached to none (global).
    stmt = (
        select(Instruction)
        .options(
            lazyload("*"),
            selectinload(Instruction.data_sources).options(lazyload("*")),
            selectinload(Instruction.labels).options(lazyload("*")),
            selectinload(Instruction.references).options(lazyload("*")),
        )
        .where(
            Instruction.organization_id == organization.id,
            Instruction.deleted_at.is_(None),
            Instruction.status != "archived",
        )
    )
    # Same "live" rule as the instructions tree: only what the main build carries.
    main_build_id = await resolve_main_build_id(db, str(organization.id))
    if main_build_id:
        stmt = stmt.where(Instruction.id.in_(
            select(BuildContent.instruction_id).where(BuildContent.build_id == main_build_id)
        ))
    if data_source_id:
        stmt = stmt.where(
            Instruction.id.in_(
                select(instruction_data_source_association.c.instruction_id).where(
                    instruction_data_source_association.c.data_source_id == data_source_id
                )
            )
        )
    instructions = (await db.execute(stmt)).scalars().all()

    nodes, edges = [], []
    label_nodes = {}

    def add_label_edges(ins):
        for lb in (ins.labels or []):
            lid = f"label:{lb.id}"
            if lid not in label_nodes:
                label_nodes[lid] = _node(lid, "label", lb.name, color=lb.color)
            edges.append({"source": f"instruction:{ins.id}", "target": lid, "kind": "labeled"})

    if not data_source_id:
        # ---- org scope: agents + instructions --------------------------------
        for a in agents:
            nodes.append(_node(
                f"agent:{a.id}", "agent", a.name,
                type=getattr(a, "type", None), icon=getattr(a, "icon", None),
                connector_key=getattr(a, "connector_key", None),
                is_public=getattr(a, "is_public", None),
                publish_status=getattr(a, "publish_status", None),
            ))
        for ins in instructions:
            ds_ids = [str(d.id) for d in (ins.data_sources or []) if str(d.id) in agent_by_id]
            if ins.data_sources and not ds_ids:
                continue  # attached only to agents the caller can't see
            nodes.append(_node(
                f"instruction:{ins.id}", "instruction", ins.title or (ins.text or "")[:60],
                is_global=not ins.data_sources, load_mode=ins.load_mode, status=ins.status,
                source_type=ins.source_type, agent_count=len(ds_ids),
                table_count=len([r for r in (ins.references or []) if r.object_type == "datasource_table"]),
            ))
            for dsid in ds_ids:
                edges.append({"source": f"agent:{dsid}", "target": f"instruction:{ins.id}", "kind": "attached"})
            add_label_edges(ins)
        nodes.extend(label_nodes.values())
        return {
            "scope": "org", "nodes": nodes, "edges": edges,
            "stats": {"agents": len(agents), "instructions": len([n for n in nodes if n["kind"] == "instruction"]),
                      "shared": len([n for n in nodes if n["kind"] == "instruction" and n.get("agent_count", 0) > 1]),
                      "global": len([n for n in nodes if n["kind"] == "instruction" and n.get("is_global")]),
                      "labels": len(label_nodes)},
        }

    # ---- agent scope: tables + instructions --------------------------------
    trows = (await db.execute(
        select(DataSourceTable).where(
            DataSourceTable.datasource_id == data_source_id,
            DataSourceTable.is_active == True,  # noqa: E712
        )
    )).scalars().all()
    ref_count = defaultdict(int)
    for ins in instructions:
        for r in (ins.references or []):
            if r.object_type == "datasource_table":
                ref_count[str(r.object_id)] += 1
    trows = sorted(trows, key=lambda t: (ref_count[str(t.id)], t.centrality_score or 0, t.name), reverse=True)[:max_tables]
    table_by_name = {t.name: t for t in trows}
    table_ids = {str(t.id) for t in trows}
    for t in trows:
        nodes.append(_node(
            f"table:{t.id}", "table", t.name,
            columns=len(t.columns or []), rows=t.no_rows, centrality=t.centrality_score,
            ref_count=ref_count[str(t.id)],
        ))
        for fk in (t.fks or []):
            ref = fk.get("references_name") if isinstance(fk, dict) else None
            if ref in table_by_name:
                edges.append({"source": f"table:{t.id}", "target": f"table:{table_by_name[ref].id}", "kind": "fk",
                              "label": (fk.get("column") or {}).get("name")})
    for ins in instructions:
        nodes.append(_node(
            f"instruction:{ins.id}", "instruction", ins.title or (ins.text or "")[:60],
            is_global=not ins.data_sources, load_mode=ins.load_mode, status=ins.status, source_type=ins.source_type,
            agent_count=len(ins.data_sources or []),
            shared=len(ins.data_sources or []) > 1,
        ))
        linked = False
        for r in (ins.references or []):
            if r.object_type == "datasource_table" and str(r.object_id) in table_ids:
                edges.append({"source": f"instruction:{ins.id}", "target": f"table:{r.object_id}", "kind": "references",
                              "column": r.column_name})
                linked = True
        if not linked:
            edges.append({"source": f"instruction:{ins.id}", "target": f"agent:{data_source_id}", "kind": "attached"})
        add_label_edges(ins)
    a = agent_by_id[data_source_id]
    nodes.append(_node(f"agent:{a.id}", "agent", a.name, type=getattr(a, "type", None), icon=getattr(a, "icon", None), connector_key=getattr(a, "connector_key", None)))
    nodes.extend(label_nodes.values())
    return {
        "scope": "agent", "nodes": nodes, "edges": edges,
        "stats": {"tables": len(trows), "tables_total": len(table_by_name) if len(trows) < max_tables else None,
                  "instructions": len(instructions), "fks": len([e for e in edges if e["kind"] == "fk"]), "labels": len(label_nodes)},
    }
