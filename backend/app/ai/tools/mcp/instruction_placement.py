"""Where an instruction applies — its folders and agents — as the MCP tools show it.

An instruction is either ``global`` (no folder, no agent: every report) or
``targeted`` (one or more folders and/or agents). The caller sees the targets
they may see by name; the rest are only counted, so sharing a rule with a
private folder or agent never reveals that folder's or agent's name.
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.folder import folder_instructions
from app.models.instruction import Instruction, instruction_data_source_association


@dataclass
class Placement:
    scope_mode: str = "global"
    owner_folder_id: Optional[str] = None
    folder_ids: List[str] = field(default_factory=list)
    agent_ids: List[str] = field(default_factory=list)

    @property
    def scope(self) -> str:
        """'global' only when the row is global AND has no targets; a targeted
        row that lost every target stays 'targeted' (it applies nowhere)."""
        if self.scope_mode == "global" and not self.folder_ids and not self.agent_ids:
            return "global"
        return "targeted"


async def load_placements(db: AsyncSession, instruction_ids: Iterable[str]) -> Dict[str, Placement]:
    ids = list(dict.fromkeys(str(i) for i in instruction_ids if i))
    if not ids:
        return {}
    out = {
        str(iid): Placement(scope_mode=mode or "global", owner_folder_id=owner)
        for iid, mode, owner in (await db.execute(
            select(Instruction.id, Instruction.scope_mode, Instruction.owner_folder_id).where(Instruction.id.in_(ids))
        )).all()
    }
    for iid, fid in (await db.execute(
        select(folder_instructions.c.instruction_id, folder_instructions.c.folder_id)
        .where(folder_instructions.c.instruction_id.in_(ids))
        .order_by(folder_instructions.c.folder_id)
    )).all():
        if str(iid) in out:
            out[str(iid)].folder_ids.append(str(fid))
    assoc = instruction_data_source_association
    for iid, dsid in (await db.execute(
        select(assoc.c.instruction_id, assoc.c.data_source_id)
        .where(assoc.c.instruction_id.in_(ids))
        .order_by(assoc.c.data_source_id)
    )).all():
        if str(iid) in out:
            out[str(iid)].agent_ids.append(str(dsid))
    return out


class PlacementRenderer:
    """Renders placements for one caller; loads what it needs once."""

    def __init__(self, db: AsyncSession, organization, user):
        self.db, self.organization, self.user = db, organization, user
        self._tree = None
        self._visible_agents = None
        self._agent_names: Dict[str, str] = {}

    async def _load(self, agent_ids: Iterable[str]):
        from app.services.folder_service import browsable_folder_tree
        if self._tree is None:
            self._tree, _ = await browsable_folder_tree(self.db, self.organization, self.user)
        if self._visible_agents is None:
            from app.core.derived_access import visible_data_source_ids
            self._visible_agents = {str(i) for i in await visible_data_source_ids(self.db, self.user, self.organization)}
        missing = [a for a in agent_ids if a in self._visible_agents and a not in self._agent_names]
        if missing:
            from app.models.data_source import DataSource
            for dsid, name in (await self.db.execute(
                select(DataSource.id, DataSource.name).where(DataSource.id.in_(missing))
            )).all():
                self._agent_names[str(dsid)] = name

    async def render(self, p: Placement) -> dict:
        from app.services.folder_service import folder_path
        await self._load(p.agent_ids)
        folders = [
            {"id": fid, "name": self._tree[fid][1], "path": folder_path(self._tree, fid)}
            for fid in p.folder_ids if fid in self._tree
        ]
        agents = [
            {"id": aid, "name": self._agent_names.get(aid)}
            for aid in p.agent_ids if aid in self._visible_agents
        ]
        out = {
            "scope": p.scope,
            "folders": folders,
            "agents": agents,
            "hidden_target_count": (len(p.folder_ids) - len(folders)) + (len(p.agent_ids) - len(agents)),
        }
        if p.scope == "targeted" and not p.folder_ids and not p.agent_ids:
            out["applies_nowhere"] = True
        return out
