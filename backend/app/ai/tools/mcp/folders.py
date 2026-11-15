"""MCP Tool: browse_agents — the folders and agents a client may scope a report to.

A client picks a report's scope before create_report: folders (every agent in
the folder's branch, resolved at run time) and/or individual agents. This tool
is how it finds their ids. It shows only what the caller may see:

- folders the caller can query (``view``) or curate (``manage_instructions``);
  a hidden parent is clipped, so a visible folder inside a private branch
  appears as a top-level folder and the branch's names never leak;
- agents the caller can use over MCP; an agent reached by a direct grant whose
  home folder is hidden comes back with no folder, so the grant reveals
  neither its parent nor its siblings.
"""

import difflib
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.models.organization import Organization
from app.models.user import User


class BrowseAgentsInput(BaseModel):
    search: Optional[str] = Field(
        default=None,
        description=(
            "Words to look for in folder paths and agent names, e.g. 'disputes' or "
            "'finance auth'. Every word must appear; close misspellings are matched too."
        ),
    )
    folder_id: Optional[str] = Field(
        default=None,
        description="Open this folder: its subfolders and the agents directly in it.",
    )
    limit: int = Field(default=100, ge=1, le=500, description="Maximum folders and agents (each) to return")


class FolderEntry(BaseModel):
    id: str
    name: str
    path: str = Field(description="Folder path as you can see it, e.g. 'Payments / Disputes'")
    parent_id: Optional[str] = Field(default=None, description="Visible parent folder, if any")
    can_query: bool = Field(description="True when you can use this folder as a report scope")
    can_manage_instructions: bool = Field(description="True when you can attach instructions to this folder")
    agent_count: int = Field(description="Agents directly in this folder that you can use")
    subfolder_count: int = 0


class AgentEntry(BaseModel):
    id: str
    name: str
    type: Optional[str] = None
    folder_id: Optional[str] = Field(default=None, description="The agent's folder, when you can see it")
    folder_path: Optional[str] = None


class BrowseAgentsOutput(BaseModel):
    folders: List[FolderEntry]
    agents: List[AgentEntry]
    folders_total: int
    agents_total: int
    truncated: bool = False
    hint: str


HINT = (
    "Pass folder ids as agent_folder_ids and/or agent ids as data_source_ids to create_report "
    "to scope a report (a folder covers every agent in its branch). Use the same ids in "
    "create_instruction / edit_instruction (folder_ids, data_source_ids)."
)

_WORD = re.compile(r"[\w'-]+", re.UNICODE)


def _words(text: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def _matches(query_words: List[str], haystack: str) -> bool:
    hay = (haystack or "").lower()
    hay_words = _words(hay)
    for q in query_words:
        if q in hay:
            continue
        # Tolerate typos ("disputs", "authorisation") the way a person types.
        if not difflib.get_close_matches(q, hay_words, n=1, cutoff=0.75):
            return False
    return True


async def folder_catalog(db: AsyncSession, organization: Organization, user: User):
    """(folder tree, rights, usable agents, agent → visible folder path)."""
    from app.services.data_source_service import DataSourceService
    from app.services.folder_service import browsable_folder_tree, folder_path

    tree, rights = await browsable_folder_tree(db, organization, user)
    agents = await DataSourceService().get_active_data_sources(
        db, organization, current_user=user, channel="mcp"
    )
    return tree, rights, agents, {fid: folder_path(tree, fid) for fid in tree}


class BrowseAgentsTool(MCPTool):
    name = "browse_agents"
    read_only_hint = True
    description = (
        "Browse or search the folders and agents you can access, with their ids and paths. "
        "Call it before create_report when the user names a team, folder or agent "
        "('use the disputes stuff'), then pass the ids to create_report. Without arguments it "
        "lists everything you can see; folder_id opens one folder; search finds folders and "
        "agents by name."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return BrowseAgentsInput.model_json_schema()

    async def execute(self, args: Dict[str, Any], db: AsyncSession, user: User, organization: Organization):
        data = BrowseAgentsInput(**args)
        tree, rights, agents, paths = await folder_catalog(db, organization, user)

        agents_in: Dict[Optional[str], List[Any]] = {}
        for a in agents:
            home = a.folder_id if getattr(a, "folder_id", None) in tree else None
            agents_in.setdefault(home, []).append(a)
        children: Dict[Optional[str], List[str]] = {}
        for fid, (parent, _name) in tree.items():
            children.setdefault(parent, []).append(fid)

        def folder_entry(fid: str) -> FolderEntry:
            parent, name = tree[fid]
            return FolderEntry(
                id=fid, name=name, path=paths[fid], parent_id=parent,
                can_query=rights.has_resource_permission("folder", fid, "view"),
                can_manage_instructions=rights.has_resource_permission("folder", fid, "manage_instructions"),
                agent_count=len(agents_in.get(fid, [])),
                subfolder_count=len(children.get(fid, [])),
            )

        def agent_entry(a) -> AgentEntry:
            home = a.folder_id if getattr(a, "folder_id", None) in tree else None
            return AgentEntry(
                id=str(a.id), name=a.name, type=getattr(a, "type", None),
                folder_id=home, folder_path=paths.get(home) if home else None,
            )

        if data.folder_id:
            if data.folder_id not in tree:
                raise ValueError(
                    "Folder not found, or you don't have access to it. Call browse_agents without "
                    "folder_id to see the folders you can use."
                )
            folder_ids = [data.folder_id] + sorted(children.get(data.folder_id, []), key=lambda f: paths[f].lower())
            agent_rows = agents_in.get(data.folder_id, [])
        else:
            folder_ids = sorted(tree, key=lambda f: paths[f].lower())
            agent_rows = list(agents)

        partial = False
        if data.search and _words(data.search):
            q = _words(data.search)

            def agent_text(a) -> str:
                # An agent matches on its own name or its folder path: "disputes"
                # finds the agents filed under a Disputes folder too.
                return f"{paths.get(a.folder_id, '') if a.folder_id in tree else ''} {a.name}"

            def pick(words):
                return ([f for f in folder_ids if _matches(words, paths[f])],
                        [a for a in agent_rows if _matches(words, agent_text(a))])

            hit_folders, hit_agents = pick(q)
            if not hit_folders and not hit_agents and len(q) > 1:
                # "disputes auth" usually names two things: match any word.
                matched = [pick([w]) for w in q]
                hit_folders = [f for f in folder_ids if any(f in fs for fs, _ in matched)]
                hit_agents = [a for a in agent_rows if any(a in ags for _, ags in matched)]
                partial = True
            folder_ids, agent_rows = hit_folders, hit_agents

        agent_rows = sorted(agent_rows, key=lambda a: ((paths.get(a.folder_id) or "") if a.folder_id in tree else "", a.name.lower()))
        out = BrowseAgentsOutput(
            folders=[folder_entry(f) for f in folder_ids[: data.limit]],
            agents=[agent_entry(a) for a in agent_rows[: data.limit]],
            folders_total=len(folder_ids),
            agents_total=len(agent_rows),
            truncated=len(folder_ids) > data.limit or len(agent_rows) > data.limit,
            hint=(("No entry matched every word, so these match any of them. " if partial else "") + HINT)
            if (folder_ids or agent_rows) else (
                "Nothing matched. Call browse_agents without search to see everything you can access."
            ),
        )
        return out.model_dump()
